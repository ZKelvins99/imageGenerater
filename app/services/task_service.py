from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.repositories import jobs as job_repo
from app.repositories.jobs import JobRecord
from app.schemas.generation import GenerationRequest
from app.schemas.models import TaskStatus
from app.services import asset_service, history_service, image_service, images_adapter
from app.services.config_service import (
    OUTPUTS_DIR,
    ensure_dirs,
    load_settings,
    resolve_data_path,
)
from app.services.errors import AppError
from app.services.provider_service import get_active_provider, get_provider

BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]

LEGACY_STATUS = {
    "queued": "pending",
    "preparing": "pending",
    "running": "running",
    "streaming": "running",
    "saving": "running",
    "succeeded": "done",
    "failed": "error",
    "cancel_requested": "error",
    "cancelled": "error",
}


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def job_to_task_status(
    job: JobRecord, output_urls: list[str] | None = None
) -> TaskStatus:
    legacy = LEGACY_STATUS.get(job.status, "error")
    return TaskStatus(
        task_id=job.id,
        status=legacy,  # type: ignore[arg-type]
        message=job.message or "",
        progress=job.progress,
        history_id=job.history_id,
        output_urls=output_urls or [],
        error=job.error_message_public,
    )


class TaskManager:
    def __init__(self, *, max_concurrent: int = 2) -> None:
        self._broadcast: BroadcastFn | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._max_concurrent = max_concurrent
        self._started = False
        self._memory: dict[str, TaskStatus] = {}  # hot cache for GET /tasks

    def set_broadcast(self, fn: BroadcastFn) -> None:
        self._broadcast = fn

    async def start_workers(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self._max_concurrent):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))

    async def stop_workers(self) -> None:
        self._started = False
        for w in self._workers:
            w.cancel()
        self._workers.clear()

    def get(self, task_id: str) -> TaskStatus | None:
        return self._memory.get(task_id)

    def cache_status(self, status: TaskStatus) -> None:
        self._memory[status.task_id] = status

    def drop_cached(self, task_id: str) -> None:
        self._memory.pop(task_id, None)

    async def get_async(self, task_id: str) -> TaskStatus | None:
        if task_id in self._memory:
            return self._memory[task_id]
        job = await job_repo.get_job(task_id)
        if job is None:
            return None
        urls = await self._output_urls_for_job(job)
        status = job_to_task_status(job, urls)
        self._memory[task_id] = status
        return status

    async def _emit(self, status: TaskStatus) -> None:
        self._memory[status.task_id] = status
        if self._broadcast:
            await self._broadcast({"type": "task", "payload": status.model_dump()})

    async def _output_urls_for_job(self, job: JobRecord) -> list[str]:
        assets = await job_repo.list_job_assets(job.id, role="output")
        if assets:
            return [f"/media/{a['storage_path']}" for a in assets]
        # Fallback to snapshot paths (legacy / mid-flight)
        paths = job.request_snapshot.get("output_paths") or []
        return [history_service.output_url(p) for p in paths]

    async def recover_on_startup(self) -> dict[str, int]:
        """Re-queue queued jobs; mark interrupted in-flight jobs as failed."""
        stats = {"requeued": 0, "interrupted": 0}
        running = await job_repo.list_jobs(
            limit=1000,
            statuses=["preparing", "running", "streaming", "saving"],
        )
        for job in running:
            ok = await job_repo.update_job_status(
                job.id,
                new_status="failed",
                expected_statuses={job.status},
                progress=1.0,
                message="进程中断，任务未完成",
                error_code="INTERRUPTED",
                error_message_public="应用重启时任务仍在运行，已标记失败",
                finished_at=_now(),
            )
            if ok:
                stats["interrupted"] += 1
                refreshed = await job_repo.get_job(job.id)
                if refreshed:
                    await self._emit(job_to_task_status(refreshed))

        queued = await job_repo.list_jobs(limit=1000, statuses=["queued"])
        for job in queued:
            await self._queue.put(job.id)
            stats["requeued"] += 1
        return stats

    async def start_generate(
        self,
        *,
        mode: str,
        prompt: str,
        model: str | None,
        size: str | None,
        quality: str | None,
        n: int | None,
        reference_path: str | None = None,
        upload_bytes: bytes | None = None,
        upload_filename: str | None = None,
        parent_job_id: str | None = None,
    ) -> TaskStatus:
        settings = load_settings()
        if not settings.base_url.strip():
            raise ValueError("Base URL 未配置，请先在设置页填写")
        if not settings.api_key:
            raise ValueError("API Key 未配置，请先在设置页填写")

        provider_id: str | None
        try:
            active = get_active_provider()
            provider_id = active.id
        except Exception:
            provider_id = settings.active_provider_id

        task_id = uuid.uuid4().hex[:12]
        history_id = uuid.uuid4().hex[:12]
        now = _now()

        model_name = (model or settings.default_model or "").strip()
        if not model_name:
            raise ValueError("模型未选择，请先在设置页填写默认模型或在页面选择模型")
        size_v = size or settings.default_size or "1024x1024"
        quality_v = quality or settings.default_quality or "medium"
        n_v = n or settings.default_n or 1

        ensure_dirs()
        ref_rel: str | None = None
        ref_asset_id: str | None = None
        if mode == "image":
            if upload_bytes is not None:
                asset = await asset_service.save_bytes_as_asset(
                    upload_bytes,
                    category="input",
                    original_filename=upload_filename,
                    parent_job_id=task_id,
                )
                ref_rel = asset.storage_path
                ref_asset_id = asset.id
            elif reference_path:
                resolve_data_path(reference_path)
                ref_rel = reference_path.replace("\\", "/")
                if ref_rel.startswith("data/"):
                    ref_rel = ref_rel[5:]
                asset = await asset_service.register_existing_path(
                    ref_rel, category="input", parent_job_id=task_id
                )
                ref_asset_id = asset.id
                ref_rel = asset.storage_path
            else:
                raise ValueError("图生图需要上传参考图或指定 reference_path")

        snapshot = {
            "mode": mode,
            "prompt": prompt,
            "model": model_name,
            "size": size_v,
            "quality": quality_v,
            "n": n_v,
            "reference_path": ref_rel,
            "reference_asset_id": ref_asset_id,
            "output_paths": [],
        }
        job = JobRecord(
            id=task_id,
            history_id=history_id,
            status="queued",
            progress_kind="stage",
            progress=0.05,
            request_snapshot=snapshot,
            provider_id=provider_id,
            upstream_request_id=None,
            attempt_count=0,
            error_code=None,
            error_message_public=None,
            error_detail_internal=None,
            message="任务已排队",
            created_at=now,
            started_at=None,
            finished_at=None,
            parent_job_id=parent_job_id,
        )
        await job_repo.insert_job(job)
        if ref_asset_id:
            await job_repo.link_job_asset(
                task_id, ref_asset_id, role="reference", position=0
            )

        status = job_to_task_status(job)
        await self._emit(status)
        await self._queue.put(task_id)
        return status

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run_job(job_id)
            except Exception:
                # Last-resort: mark failed if still non-terminal
                try:
                    await job_repo.update_job_status(
                        job_id,
                        new_status="failed",
                        expected_statuses={
                            "queued",
                            "preparing",
                            "running",
                            "streaming",
                            "saving",
                        },
                        progress=1.0,
                        message="内部错误",
                        error_code="INTERNAL_ERROR",
                        error_message_public="任务执行异常",
                        finished_at=_now(),
                    )
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    async def _is_cancel_requested(self, job_id: str) -> bool:
        job = await job_repo.get_job(job_id)
        return job is not None and job.status == "cancel_requested"

    async def _finalize_cancelled(self, job_id: str) -> None:
        await job_repo.update_job_status(
            job_id,
            new_status="cancelled",
            expected_statuses={
                "cancel_requested",
                "queued",
                "preparing",
                "running",
                "streaming",
                "saving",
            },
            progress=1.0,
            message="已取消",
            error_code="CANCELLED",
            error_message_public="用户取消（上游可能仍在计费）",
            finished_at=_now(),
        )
        final = await job_repo.get_job(job_id)
        if final:
            await self._emit(job_to_task_status(final))

    async def retry_job(self, job_id: str) -> TaskStatus:
        job = await job_repo.get_job(job_id)
        if job is None:
            raise ValueError("任务不存在")
        if job.status not in ("failed", "cancelled"):
            raise ValueError("仅失败或已取消的任务可重试")
        snap = dict(job.request_snapshot or {})
        if snap.get("generation_api") == "v1" or snap.get("mode") in (
            "generate",
            "reference",
            "edit_mask",
        ):
            req = GenerationRequest.model_validate(
                {
                    **snap,
                    "prompt": snap.get("prompt") or "",
                    "model": snap.get("model") or "",
                }
            )
            return await self.start_generation(req, parent_job_id=job_id)
        legacy_mode = str(snap.get("mode") or "text")
        if legacy_mode in ("generate",):
            legacy_mode = "text"
        if legacy_mode in ("reference", "edit_mask"):
            legacy_mode = "image"
        return await self.start_generate(
            mode=legacy_mode,
            prompt=str(snap.get("prompt") or ""),
            model=str(snap.get("model") or "") or None,
            size=str(snap.get("size") or "") or None,
            quality=str(snap.get("quality") or "") or None,
            n=int(snap["n"]) if snap.get("n") is not None else None,
            reference_path=snap.get("reference_path"),
            upload_bytes=None,
            upload_filename=None,
            parent_job_id=job_id,
        )

    async def start_generation(
        self,
        req: GenerationRequest,
        *,
        parent_job_id: str | None = None,
    ) -> TaskStatus:
        settings = load_settings()
        profile = None
        if req.provider_id:
            profile = get_provider(req.provider_id)
        else:
            try:
                profile = get_active_provider()
            except Exception as e:
                raise ValueError("没有可用的 Provider") from e

        if not profile.base_url.strip():
            raise ValueError("Base URL 未配置，请先在设置页填写")

        model_name = (
            req.model or profile.default_model or settings.default_model or ""
        ).strip()
        if not model_name:
            raise ValueError("模型未选择")
        req = req.model_copy(update={"model": model_name, "provider_id": profile.id})

        task_id = uuid.uuid4().hex[:12]
        history_id = uuid.uuid4().hex[:12]
        now = _now()
        ensure_dirs()

        # Map to legacy history fields
        legacy_mode = "text" if req.mode == "generate" else "image"
        size_token = req.parsed_size().to_api_value()
        ref_rel = None
        if req.primary_asset_id or req.input_asset_ids:
            from app.repositories import assets as asset_repo

            aid = req.primary_asset_id or req.input_asset_ids[0]
            asset = await asset_repo.get_asset(aid)
            if asset:
                ref_rel = asset.storage_path

        snapshot = {
            "generation_api": "v1",
            **req.model_dump(),
            # Keep legacy keys for history view
            "mode": req.mode,
            "legacy_mode": legacy_mode,
            "size": size_token,
            "reference_path": ref_rel,
            "output_paths": [],
        }
        job = JobRecord(
            id=task_id,
            history_id=history_id,
            status="queued",
            progress_kind="stage",
            progress=0.05,
            request_snapshot=snapshot,
            provider_id=profile.id,
            upstream_request_id=None,
            attempt_count=0,
            error_code=None,
            error_message_public=None,
            error_detail_internal=None,
            message="任务已排队",
            created_at=now,
            started_at=None,
            finished_at=None,
            parent_job_id=parent_job_id,
        )
        await job_repo.insert_job(job)
        for i, aid in enumerate(req.input_asset_ids):
            await job_repo.link_job_asset(task_id, aid, role="reference", position=i)
        if req.mask_asset_id:
            await job_repo.link_job_asset(
                task_id, req.mask_asset_id, role="mask", position=0
            )

        status = job_to_task_status(job)
        await self._emit(status)
        await self._queue.put(task_id)
        return status

    async def _run_job(self, job_id: str) -> None:
        job = await job_repo.get_job(job_id)
        if job is None:
            return
        if job.status == "cancel_requested":
            await self._finalize_cancelled(job_id)
            return
        if job.status not in ("queued",):
            return

        ok = await job_repo.update_job_status(
            job_id,
            new_status="preparing",
            expected_statuses={"queued"},
            progress=0.1,
            message="准备中…",
            started_at=_now(),
            attempt_count=job.attempt_count + 1,
        )
        if not ok:
            return

        job = await job_repo.get_job(job_id)
        assert job is not None
        await self._emit(job_to_task_status(job))

        if await self._is_cancel_requested(job_id):
            await self._finalize_cancelled(job_id)
            return

        snap = job.request_snapshot
        mode = snap.get("mode") or "text"
        prompt = snap.get("prompt") or ""
        model = snap.get("model") or ""
        size = snap.get("size") or "1024x1024"
        quality = snap.get("quality") or "medium"
        n = int(snap.get("n") or 1)
        reference_path = snap.get("reference_path")
        use_v1 = snap.get("generation_api") == "v1" or mode in (
            "generate",
            "reference",
            "edit_mask",
        )

        ok = await job_repo.update_job_status(
            job_id,
            new_status="running",
            expected_statuses={"preparing"},
            progress=0.2,
            message="正在调用生图接口…",
        )
        if not ok:
            if await self._is_cancel_requested(job_id):
                await self._finalize_cancelled(job_id)
            return
        job = await job_repo.get_job(job_id)
        assert job is not None
        await self._emit(job_to_task_status(job))

        try:
            result_images: list[Any] = []
            upstream_request_id: str | None = None
            sent_params: dict[str, Any] = {}
            attempt_count = 1

            if use_v1:
                # Normalize legacy text/image if needed
                gen_mode = mode
                if mode == "text":
                    gen_mode = "generate"
                elif mode == "image":
                    gen_mode = "reference"
                req_data = {
                    **snap,
                    "mode": gen_mode,
                    "prompt": prompt,
                    "model": model,
                    "size": size,
                    "quality": quality,
                    "n": n,
                }
                if gen_mode == "reference" and not req_data.get("input_asset_ids"):
                    if snap.get("reference_asset_id"):
                        req_data["input_asset_ids"] = [snap["reference_asset_id"]]
                        req_data["primary_asset_id"] = snap["reference_asset_id"]
                req = GenerationRequest.model_validate(req_data)
                profile = None
                if job.provider_id:
                    try:
                        profile = get_provider(job.provider_id)
                    except Exception:
                        profile = None
                async def on_partial(index: int, blob: bytes) -> None:
                    if await self._is_cancel_requested(job_id):
                        raise AppError(
                            "用户取消",
                            code="CANCELLED",
                            status_code=499,
                        )
                    partial = await asset_service.save_bytes_as_asset(
                        blob,
                        category="partial",
                        original_filename=f"{job_id}-partial-{index}.png",
                        parent_job_id=job_id,
                    )
                    await job_repo.replace_job_asset_at_position(
                        job_id,
                        partial.id,
                        role="partial",
                        position=index,
                    )
                    partial_count = max(1, int(req.partial_images or 1))
                    progress = min(0.78, 0.3 + (index + 1) / (partial_count + 1) * 0.45)
                    await job_repo.update_job_status(
                        job_id,
                        new_status="streaming",
                        expected_statuses={"running", "streaming"},
                        progress=progress,
                        message=f"已收到第 {index + 1} 张渐进预览",
                    )
                    current = await job_repo.get_job(job_id)
                    if current:
                        await self._emit(job_to_task_status(current))
                    if self._broadcast:
                        await self._broadcast(
                            {
                                "type": "job.partial_image",
                                "payload": {
                                    "job_id": job_id,
                                    "partial_image_index": index,
                                    "asset_id": partial.id,
                                    "url": f"/media/{partial.storage_path}",
                                },
                            }
                        )

                gen_result = await images_adapter.generate(
                    req,
                    profile=profile,
                    on_partial=on_partial,
                )
                result_images = gen_result.images
                upstream_request_id = gen_result.upstream_request_id
                sent_params = gen_result.sent_params
                attempt_count = gen_result.attempt_count
            elif mode == "text":
                blobs = await image_service.generate_text_to_image(
                    prompt=prompt,
                    model=model,
                    size=size,
                    quality=quality,
                    n=n,
                )
                result_images = [
                    {"data": b, "extension": ".png", "mime": "image/png"} for b in blobs
                ]
            else:
                assert reference_path
                path = resolve_data_path(str(reference_path))
                blobs = await image_service.generate_image_to_image(
                    prompt=prompt,
                    model=model,
                    size=size,
                    quality=quality,
                    n=n,
                    image_path=path,
                )
                result_images = [
                    {"data": b, "extension": ".png", "mime": "image/png"} for b in blobs
                ]

            if await self._is_cancel_requested(job_id):
                await self._finalize_cancelled(job_id)
                return

            ok = await job_repo.update_job_status(
                job_id,
                new_status="saving",
                expected_statuses={"running", "streaming"},
                progress=0.85,
                message="正在保存图片…",
            )
            if not ok:
                if await self._is_cancel_requested(job_id):
                    await self._finalize_cancelled(job_id)
                return
            if await self._is_cancel_requested(job_id):
                await self._finalize_cancelled(job_id)
                return

            job = await job_repo.get_job(job_id)
            assert job is not None
            await self._emit(job_to_task_status(job))

            day = datetime.now().strftime("%Y-%m-%d")
            out_dir = OUTPUTS_DIR / day
            out_dir.mkdir(parents=True, exist_ok=True)
            output_paths: list[str] = []
            urls: list[str] = []
            for i, img in enumerate(result_images):
                if hasattr(img, "data"):
                    blob = img.data
                    ext = img.extension or ".png"
                    fname_hint = f"{job.history_id or job_id}_{i}{ext}"
                else:
                    blob = img["data"]
                    ext = img.get("extension") or ".png"
                    fname_hint = f"{job.history_id or job_id}_{i}{ext}"

                asset = await asset_service.save_bytes_as_asset(
                    blob,
                    category="output",
                    original_filename=fname_hint,
                    parent_job_id=job_id,
                )
                await job_repo.link_job_asset(
                    job_id, asset.id, role="output", position=i
                )
                suffix = f"_{i}" if len(result_images) > 1 else ""
                legacy_name = f"{job.history_id or job_id}{suffix}{ext}"
                legacy = out_dir / legacy_name
                legacy.write_bytes(blob)
                rel = f"outputs/{day}/{legacy_name}"
                output_paths.append(rel)
                urls.append(f"/media/{asset.storage_path}")

            snap = dict(job.request_snapshot)
            snap["output_paths"] = output_paths
            if sent_params:
                snap["sent_params"] = sent_params
            if upstream_request_id:
                snap["upstream_request_id"] = upstream_request_id
            snap["attempt_count"] = attempt_count
            ok = await job_repo.update_job_status(
                job_id,
                new_status="succeeded",
                expected_statuses={"saving"},
                progress=1.0,
                message="生成完成",
                finished_at=_now(),
                request_snapshot=snap,
                error_message_public="",
                upstream_request_id=upstream_request_id,
                attempt_count=attempt_count,
            )
            if not ok:
                return
            final = await job_repo.get_job(job_id)
            assert final is not None
            status = job_to_task_status(final, urls)
            await self._emit(status)

        except (image_service.ImageAPIError, AppError) as e:
            if await self._is_cancel_requested(job_id):
                await self._finalize_cancelled(job_id)
                return
            code = getattr(e, "code", None) or "UPSTREAM_ERROR"
            msg = getattr(e, "message", str(e))
            req_id = getattr(e, "request_id", None)
            await job_repo.update_job_status(
                job_id,
                new_status="failed",
                expected_statuses={
                    "preparing",
                    "running",
                    "streaming",
                    "saving",
                    "cancel_requested",
                },
                progress=1.0,
                message="生成失败",
                error_code=code,
                error_message_public=msg,
                error_detail_internal=msg,
                finished_at=_now(),
                upstream_request_id=req_id,
            )
            final = await job_repo.get_job(job_id)
            if final:
                await self._emit(job_to_task_status(final))
        except Exception as e:
            if await self._is_cancel_requested(job_id):
                await self._finalize_cancelled(job_id)
                return
            await job_repo.update_job_status(
                job_id,
                new_status="failed",
                expected_statuses={
                    "preparing",
                    "running",
                    "streaming",
                    "saving",
                    "cancel_requested",
                },
                progress=1.0,
                message="生成失败",
                error_code="INTERNAL_ERROR",
                error_message_public=str(e),
                error_detail_internal=str(e),
                finished_at=_now(),
            )
            final = await job_repo.get_job(job_id)
            if final:
                await self._emit(job_to_task_status(final))


task_manager = TaskManager()
