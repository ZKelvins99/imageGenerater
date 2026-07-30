from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.repositories import assets as asset_repo
from app.services import asset_service
from app.services.asset_service import AssetError
from app.services.config_service import resolve_data_path

router = APIRouter(prefix="/api/v1", tags=["assets"])


@router.post("/assets")
async def upload_assets(
    files: list[UploadFile] = File(...),
    category: str = "input",
) -> dict:
    if category not in ("input", "mask", "output", "partial"):
        raise HTTPException(status_code=400, detail="invalid category")
    items = []
    for f in files:
        data = await f.read()
        try:
            asset = await asset_service.save_bytes_as_asset(
                data,
                category=category,
                original_filename=f.filename,
                claimed_mime=f.content_type,
            )
        except AssetError as e:
            raise HTTPException(status_code=400, detail=e.message) from e
        items.append((await asset_service.to_public_async(asset)).model_dump())
    return {"items": items}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: str) -> dict:
    asset = await asset_repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return (await asset_service.to_public_async(asset)).model_dump()


@router.get("/assets/{asset_id}/content")
async def get_asset_content(asset_id: str) -> FileResponse:
    asset = await asset_repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        path = resolve_data_path(asset.storage_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid path") from e
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path,
        media_type=asset.mime or "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: str) -> dict:
    ok = await asset_repo.delete_asset(asset_id)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Asset not found or still referenced by a job",
        )
    return {"ok": True}


@router.post("/assets/validate-mask")
async def validate_mask(
    file: UploadFile = File(...),
    width: int | None = None,
    height: int | None = None,
) -> dict:
    data = await file.read()
    expected = (width, height) if width and height else None
    try:
        result = await asset_service.validate_mask_bytes(data, expected_size=expected)
    except AssetError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    return result
