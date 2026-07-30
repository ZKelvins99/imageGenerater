from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.models import SettingsPublic, SettingsUpdate
from app.services import config_service, image_service

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/settings", response_model=SettingsPublic)
async def get_settings() -> SettingsPublic:
    return config_service.to_public(config_service.load_settings())


@router.put("/settings", response_model=SettingsPublic)
async def put_settings(body: SettingsUpdate) -> SettingsPublic:
    updated = config_service.update_settings(body)
    return config_service.to_public(updated)


@router.get("/models")
async def get_models(show_all: bool = False) -> dict:
    try:
        models = await image_service.list_models(show_all=show_all)
        return {
            "data": [
                {"id": m.get("id"), "owned_by": m.get("owned_by")} for m in models
            ],
            "show_all": show_all,
        }
    except image_service.ImageAPIError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
