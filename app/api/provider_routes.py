from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.provider import ProviderCreate, ProviderUpdate
from app.services import config_service, provider_client, provider_service
from app.services.provider_service import ProviderError

router = APIRouter(prefix="/api/v1", tags=["providers"])


@router.get("/providers")
async def list_providers() -> dict:
    return {"items": [p.model_dump() for p in provider_service.list_providers()]}


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderCreate) -> dict:
    try:
        return provider_service.create_provider(body).model_dump()
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=e.message) from e


@router.get("/providers/{provider_id}")
async def get_provider(provider_id: str) -> dict:
    try:
        profile = provider_service.get_provider(provider_id)
    except ProviderError as e:
        raise HTTPException(status_code=404, detail=e.message) from e
    settings = config_service.load_settings()
    return provider_service.to_public(
        profile, is_active=(settings.active_provider_id == provider_id)
    ).model_dump()


@router.patch("/providers/{provider_id}")
async def patch_provider(provider_id: str, body: ProviderUpdate) -> dict:
    try:
        return provider_service.update_provider(provider_id, body).model_dump()
    except ProviderError as e:
        code = 404 if e.code == "MODEL_NOT_FOUND" else 400
        raise HTTPException(status_code=code, detail=e.message) from e


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict:
    try:
        provider_service.delete_provider(provider_id)
    except ProviderError as e:
        code = 404 if e.code == "MODEL_NOT_FOUND" else 400
        raise HTTPException(status_code=code, detail=e.message) from e
    return {"ok": True}


@router.post("/providers/{provider_id}/activate")
async def activate_provider(provider_id: str) -> dict:
    try:
        return provider_service.set_active_provider(provider_id).model_dump()
    except ProviderError as e:
        code = 404 if e.code == "MODEL_NOT_FOUND" else 400
        raise HTTPException(status_code=code, detail=e.message) from e


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> dict:
    result = await provider_service.test_connection(provider_id)
    return result.model_dump()


@router.post("/providers/{provider_id}/refresh-models")
async def refresh_models(provider_id: str) -> dict:
    try:
        profile = provider_service.get_provider(provider_id)
        models = await provider_client.list_models_for_provider(profile, show_all=True)
    except ProviderError as e:
        raise HTTPException(status_code=404, detail=e.message) from e
    except provider_client.ProviderClientError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=e.message) from e
    return {
        "data": [{"id": m.get("id"), "owned_by": m.get("owned_by")} for m in models]
    }


@router.get("/providers/{provider_id}/models")
async def get_provider_models(provider_id: str, show_all: bool = False) -> dict:
    try:
        profile = provider_service.get_provider(provider_id)
        models = await provider_client.list_models_for_provider(
            profile, show_all=show_all
        )
    except ProviderError as e:
        raise HTTPException(status_code=404, detail=e.message) from e
    except provider_client.ProviderClientError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=e.message) from e
    return {
        "data": [{"id": m.get("id"), "owned_by": m.get("owned_by")} for m in models],
        "show_all": show_all,
    }
