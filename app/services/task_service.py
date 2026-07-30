from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.repositories import jobs as job_repo
from app.repositories.jobs import JobRecord
from app.schemas.models import TaskStatus
from app.services import asset_service, history_service, image_service
from app.services.config_service import (
    OUTPUTS_DIR,
    ensure_dirs,
    load_settings,
    resolve_data_path,
)
from app.services.provider_service import get_active_provider

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
            expected_statuses={"cancel_requested", "queued", "preparing", "running"},
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
        return await self.start_generate(
            mode=str(snap.get("mode") or "text"),
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
            if mode == "text":
                images = await image_service.generate_text_to_image(
                    prompt=prompt,
                    model=model,
                    size=size,
                    quality=quality,
                    n=n,
                )
            else:
                assert reference_path
                path = resolve_data_path(str(reference_path))
                images = await image_service.generate_image_to_image(
                    prompt=prompt,
                    model=model,
                    size=size,
                    quality=quality,
                    n=n,
                    image_path=path,
                )

            if await self._is_cancel_requested(job_id):
                # Discard arrived results; do not save as success
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
            # If we somehow entered saving from cancel_requested path, still cancel
            if await self._is_cancel_requested(job_id):
                await self._finalize_cancelled(job_id)
                return

            job = await job_repo.get_job(job_id)
            assert job is not None
            await self._emit(job_to_task_status(job))

            # Also write legacy outputs/ path for /media compatibility
            day = datetime.now().strftime("%Y-%m-%d")
            out_dir = OUTPUTS_DIR / day
            out_dir.mkdir(parents=True, exist_ok=True)
            output_paths: list[str] = []
            urls: list[str] = []
            for i, blob in enumerate(images):
                asset = await asset_service.save_bytes_as_asset(
                    blob,
                    category="output",
                    original_filename=f"{job.history_id or job_id}_{i}.png",
                    parent_job_id=job_id,
                )
                await job_repo.link_job_asset(
                    job_id, asset.id, role="output", position=i
                )
                # Mirror into legacy outputs/ for older clients
                suffix = f"_{i}" if len(images) > 1 else ""
                fname = f"{job.history_id or job_id}{suffix}.png"
                legacy = out_dir / fname
                legacy.write_bytes(blob)
                rel = f"outputs/{day}/{fname}"
                output_paths.append(rel)
                urls.append(f"/media/{asset.storage_path}")

            snap = dict(job.request_snapshot)
            snap["output_paths"] = output_paths
            ok = await job_repo.update_job_status(
                job_id,
                new_status="succeeded",
                expected_statuses={"saving"},
                progress=1.0,
                message="生成完成",
                finished_at=_now(),
                request_snapshot=snap,
                error_message_public="",
            )
            if not ok:
                return
            final = await job_repo.get_job(job_id)
            assert final is not None
            status = job_to_task_status(final, urls)
            await self._emit(status)

        except image_service.ImageAPIError as e:
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
                error_code="UPSTREAM_ERROR",
                error_message_public=e.message,
                error_detail_internal=e.message,
                finished_at=_now(),
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
