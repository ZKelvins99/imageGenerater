from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.schemas.generation import GenerationCreateBody, GenerationRequest
from app.services import capability_resolver, task_service
from app.services.capability_resolver import CapabilityError
from app.services.config_service import load_settings
from app.services.errors import AppError
from app.services.provider_service import get_active_provider, get_provider

router = APIRouter(prefix="/api/v1", tags=["generations"])


@router.post("/generations", response_model=None)
async def create_generation(body: GenerationCreateBody) -> Response | dict:
    settings = load_settings()
    try:
        if body.provider_id:
            profile = get_provider(body.provider_id)
        else:
            profile = get_active_provider()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    model = (
        body.model or profile.default_model or settings.default_model or ""
    ).strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型未选择")

    quality = body.quality or settings.default_quality
    req = GenerationRequest(
        provider_id=profile.id,
        mode=body.mode,
        prompt=body.prompt,
        model=model,
        input_asset_ids=body.input_asset_ids,
        primary_asset_id=body.primary_asset_id,
        mask_asset_id=body.mask_asset_id,
        size=body.size if body.size is not None else settings.default_size,
        quality=quality,
        n=body.n or settings.default_n,
        output_format=body.output_format or "png",
        output_compression=body.output_compression,
        background=body.background,
        moderation=body.moderation or "auto",
        partial_images=body.partial_images,
        seed=body.seed,
        metadata=body.metadata,
        input_fidelity=body.input_fidelity,
    )

    caps = capability_resolver.resolve_capabilities(model, profile)
    try:
        capability_resolver.validate_request(req, caps)
    except CapabilityError as e:
        return JSONResponse(
            status_code=400,
            content=AppError(e.message, code=e.code, status_code=400).to_public_dict(),
        )

    try:
        status = await task_service.task_manager.start_generation(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AppError as e:
        return JSONResponse(
            status_code=e.status_code or 400, content=e.to_public_dict()
        )

    return status.model_dump()


@router.get("/models/{model}/capabilities")
async def get_model_capabilities(model: str, provider_id: str | None = None) -> dict:
    profile = None
    if provider_id:
        try:
            profile = get_provider(provider_id)
        except Exception as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    else:
        try:
            profile = get_active_provider()
        except Exception:
            profile = None
    caps = capability_resolver.resolve_capabilities(model, profile)
    return caps.model_dump()
