from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.services import history_service
from app.services.history_service import output_url
from app.services.task_service import task_manager

router = APIRouter(prefix="/api", tags=["generate"])


@router.get("/history")
async def get_history(limit: int = 200) -> dict:
    items = history_service.list_history(limit=limit)
    return {
        "items": [
            {
                **item.model_dump(),
                "output_urls": [output_url(p) for p in item.output_paths],
                "reference_url": output_url(item.reference_path)
                if item.reference_path
                else None,
            }
            for item in items
        ]
    }


@router.get("/history/{item_id}")
async def get_history_item(item_id: str) -> dict:
    item = history_service.get_history(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        **item.model_dump(),
        "output_urls": [output_url(p) for p in item.output_paths],
        "reference_url": output_url(item.reference_path)
        if item.reference_path
        else None,
    }


@router.delete("/history/{item_id}")
async def delete_history_item(item_id: str) -> dict:
    ok = history_service.delete_history(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.post("/generate")
async def generate(
    mode: str = Form("text"),
    prompt: str = Form(...),
    model: str | None = Form(None),
    size: str | None = Form(None),
    quality: str | None = Form(None),
    n: int | None = Form(None),
    reference_path: str | None = Form(None),
    image: UploadFile | None = File(None),
) -> dict:
    if mode not in ("text", "image"):
        raise HTTPException(status_code=400, detail="mode must be text or image")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt required")

    upload_bytes = None
    upload_filename = None
    if image is not None and image.filename:
        upload_bytes = await image.read()
        upload_filename = image.filename

    try:
        status = await task_manager.start_generate(
            mode=mode,
            prompt=prompt.strip(),
            model=model or None,
            size=size or None,
            quality=quality or None,  # type: ignore[arg-type]
            n=n,
            reference_path=reference_path or None,
            upload_bytes=upload_bytes,
            upload_filename=upload_filename,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return status.model_dump()


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    status = task_manager.get(task_id)
    if not status:
        raise HTTPException(status_code=404, detail="Task not found")
    return status.model_dump()
