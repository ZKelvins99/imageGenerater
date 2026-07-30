from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from app.schemas.models import HistoryItem, TaskStatus
from app.services import history_service, image_service
from app.services.config_service import (
    OUTPUTS_DIR,
    UPLOADS_DIR,
    ensure_dirs,
    load_settings,
    resolve_data_path,
)


BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskStatus] = {}
        self._broadcast: BroadcastFn | None = None
        self._lock = asyncio.Lock()

    def set_broadcast(self, fn: BroadcastFn) -> None:
        self._broadcast = fn

    def get(self, task_id: str) -> TaskStatus | None:
        return self._tasks.get(task_id)

    async def _emit(self, status: TaskStatus) -> None:
        self._tasks[status.task_id] = status
        if self._broadcast:
            await self._broadcast(
                {"type": "task", "payload": status.model_dump()}
            )

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
    ) -> TaskStatus:
        settings = load_settings()
        if not settings.base_url.strip():
            raise ValueError("Base URL 未配置，请先在设置页填写")
        if not settings.api_key:
            raise ValueError("API Key 未配置，请先在设置页填写")

        task_id = uuid.uuid4().hex[:12]
        history_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

        model_name = (model or settings.default_model or "").strip()
        if not model_name:
            raise ValueError("模型未选择，请先在设置页填写默认模型或在页面选择模型")
        size_v = size or settings.default_size or "1024x1024"
        quality_v = quality or settings.default_quality or "medium"
        n_v = n or settings.default_n or 1

        ref_rel: str | None = None
        ensure_dirs()
        if mode == "image":
            if upload_bytes is not None:
                day = datetime.now().strftime("%Y-%m-%d")
                dest_dir = UPLOADS_DIR / day
                dest_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(upload_filename or "ref.png").suffix or ".png"
                fname = f"{history_id}{ext}"
                dest = dest_dir / fname
                dest.write_bytes(upload_bytes)
                ref_rel = f"uploads/{day}/{fname}"
            elif reference_path:
                # Validate exists
                resolve_data_path(reference_path)
                ref_rel = reference_path.replace("\\", "/")
                if ref_rel.startswith("data/"):
                    ref_rel = ref_rel[5:]
            else:
                raise ValueError("图生图需要上传参考图或指定 reference_path")

        item = HistoryItem(
            id=history_id,
            created_at=now,
            mode=mode,  # type: ignore[arg-type]
            model=model_name,
            prompt=prompt,
            size=size_v,
            quality=quality_v,
            n=n_v,
            reference_path=ref_rel,
            output_paths=[],
            status="pending",
        )
        history_service.append_history(item)

        status = TaskStatus(
            task_id=task_id,
            status="pending",
            message="任务已创建",
            progress=0.05,
            history_id=history_id,
        )
        await self._emit(status)

        asyncio.create_task(
            self._run_job(
                task_id=task_id,
                history_id=history_id,
                mode=mode,
                prompt=prompt,
                model=model_name,
                size=size_v,
                quality=quality_v,
                n=n_v,
                reference_path=ref_rel,
            )
        )
        return status

    async def _run_job(
        self,
        *,
        task_id: str,
        history_id: str,
        mode: str,
        prompt: str,
        model: str,
        size: str,
        quality: str,
        n: int,
        reference_path: str | None,
    ) -> None:
        item = history_service.get_history(history_id)
        if item is None:
            return

        try:
            await self._emit(
                TaskStatus(
                    task_id=task_id,
                    status="running",
                    message="正在调用生图接口…",
                    progress=0.2,
                    history_id=history_id,
                )
            )
            item.status = "running"
            history_service.upsert_history(item)

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
                path = resolve_data_path(reference_path)
                images = await image_service.generate_image_to_image(
                    prompt=prompt,
                    model=model,
                    size=size,
                    quality=quality,
                    n=n,
                    image_path=path,
                )

            await self._emit(
                TaskStatus(
                    task_id=task_id,
                    status="running",
                    message="正在保存图片…",
                    progress=0.85,
                    history_id=history_id,
                )
            )

            day = datetime.now().strftime("%Y-%m-%d")
            out_dir = OUTPUTS_DIR / day
            out_dir.mkdir(parents=True, exist_ok=True)
            output_paths: list[str] = []
            for i, blob in enumerate(images):
                suffix = f"_{i}" if len(images) > 1 else ""
                fname = f"{history_id}{suffix}.png"
                (out_dir / fname).write_bytes(blob)
                output_paths.append(f"outputs/{day}/{fname}")

            item.output_paths = output_paths
            item.status = "done"
            item.error = None
            history_service.upsert_history(item)

            urls = [history_service.output_url(p) for p in output_paths]
            await self._emit(
                TaskStatus(
                    task_id=task_id,
                    status="done",
                    message="生成完成",
                    progress=1.0,
                    history_id=history_id,
                    output_urls=urls,
                )
            )
        except image_service.ImageAPIError as e:
            item.status = "error"
            item.error = e.message
            history_service.upsert_history(item)
            await self._emit(
                TaskStatus(
                    task_id=task_id,
                    status="error",
                    message="生成失败",
                    progress=1.0,
                    history_id=history_id,
                    error=e.message,
                )
            )
        except Exception as e:
            item.status = "error"
            item.error = str(e)
            history_service.upsert_history(item)
            await self._emit(
                TaskStatus(
                    task_id=task_id,
                    status="error",
                    message="生成失败",
                    progress=1.0,
                    history_id=history_id,
                    error=str(e),
                )
            )


task_manager = TaskManager()
