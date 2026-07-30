from __future__ import annotations

import asyncio
import base64
import io
import random
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.repositories import assets as asset_repo
from app.schemas.generation import (
    GeneratedImage,
    GenerationRequest,
    GenerationResult,
)
from app.schemas.provider import ProviderProfile
from app.services import capability_resolver
from app.services.capability_resolver import CapabilityError
from app.services.config_service import resolve_data_path
from app.services.errors import (
    AppError,
    classify_http_error,
    extract_upstream_request_id,
)
from app.services.provider_client import request_with_401_retry
from app.services.provider_service import get_active_provider, get_provider

FORMAT_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
FORMAT_EXT = {
    "png": ".png",
    "jpeg": ".jpg",
    "webp": ".webp",
}

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0


async def generate(
    req: GenerationRequest,
    *,
    profile: ProviderProfile | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> GenerationResult:
    """Execute a validated Images API generation/edit with controlled retries."""
    if profile is None:
        if req.provider_id:
            profile = get_provider(req.provider_id)
        else:
            profile = get_active_provider()

    caps = capability_resolver.resolve_capabilities(req.model, profile)
    try:
        plan = capability_resolver.validate_request(req, caps)
    except CapabilityError as e:
        raise AppError(e.message, code=e.code, status_code=400) from e

    attempt = 0
    last_error: BaseException | None = None
    while attempt < max_attempts:
        attempt += 1
        try:
            return await _dispatch_once(
                req, profile, plan, transport=transport, attempt_count=attempt
            )
        except AppError as e:
            last_error = e
            if not e.retryable or attempt >= max_attempts:
                raise
            delay = e.retry_after
            if delay is None:
                delay = min(
                    DEFAULT_MAX_DELAY,
                    DEFAULT_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25),
                )
            await asyncio.sleep(delay)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            if attempt >= max_attempts:
                raise AppError(
                    f"上游网络错误: {e}",
                    code="UPSTREAM_TIMEOUT",
                    retryable=False,
                ) from e
            delay = min(
                DEFAULT_MAX_DELAY,
                DEFAULT_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.25),
            )
            await asyncio.sleep(delay)

    assert last_error is not None
    if isinstance(last_error, AppError):
        raise last_error
    raise AppError(str(last_error), code="UPSTREAM_UNAVAILABLE") from last_error


async def _dispatch_once(
    req: GenerationRequest,
    profile: ProviderProfile,
    plan: dict[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None,
    attempt_count: int,
) -> GenerationResult:
    if req.mode == "generate":
        resp = await _post_generations(profile, plan, req, transport=transport)
    else:
        resp = await _post_edits(profile, plan, req, transport=transport)

    if resp.status_code >= 400:
        raise classify_http_error(resp)

    request_id = extract_upstream_request_id(resp)
    try:
        payload = resp.json()
    except Exception as e:
        raise AppError(
            "上游响应不是合法 JSON",
            code="UPSTREAM_PROTOCOL_ERROR",
            request_id=request_id,
        ) from e

    images = _extract_images(payload, preferred_format=req.output_format)
    revised = None
    data_items = payload.get("data") or []
    if data_items and isinstance(data_items[0], dict):
        revised = data_items[0].get("revised_prompt")

    return GenerationResult(
        images=images,
        upstream_request_id=request_id,
        revised_prompt=revised,
        attempt_count=attempt_count,
        sent_params=plan,
    )


async def _post_generations(
    profile: ProviderProfile,
    plan: dict[str, Any],
    req: GenerationRequest,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "model": plan["model"],
        "prompt": req.prompt,
        "size": plan["size"],
        "quality": plan["quality"],
        "n": plan["n"],
        "output_format": plan["output_format"],
        "moderation": plan["moderation"],
    }
    if "output_compression" in plan:
        body["output_compression"] = plan["output_compression"]
    if "background" in plan:
        body["background"] = plan["background"]
    if "partial_images" in plan:
        body["partial_images"] = plan["partial_images"]
    # Never send input_fidelity for gpt-image-2
    return await request_with_401_retry(
        profile,
        "POST",
        "/v1/images/generations",
        transport=transport,
        json=body,
    )


async def _post_edits(
    profile: ProviderProfile,
    plan: dict[str, Any],
    req: GenerationRequest,
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.Response:
    asset_ids = list(req.input_asset_ids)
    if req.primary_asset_id and req.primary_asset_id not in asset_ids:
        asset_ids.insert(0, req.primary_asset_id)
    if not asset_ids:
        raise AppError("缺少输入图", code="INPUT_INVALID", status_code=400)

    # Ensure primary is first (mask applies to first image)
    if req.primary_asset_id and asset_ids[0] != req.primary_asset_id:
        asset_ids = [req.primary_asset_id] + [
            a for a in asset_ids if a != req.primary_asset_id
        ]

    files: list[tuple[str, tuple[str, bytes, str]]] = []
    total_bytes = 0
    primary_wh: tuple[int, int] | None = None
    caps = capability_resolver.resolve_capabilities(req.model, profile)

    for i, aid in enumerate(asset_ids):
        asset = await asset_repo.get_asset(aid)
        if asset is None:
            raise AppError(f"资产不存在: {aid}", code="INPUT_INVALID", status_code=400)
        path = resolve_data_path(asset.storage_path)
        raw = path.read_bytes()
        if len(raw) > caps.max_input_bytes_each:
            raise AppError(
                f"单张输入图不得超过 {caps.max_input_bytes_each} bytes",
                code="ASSET_TOO_LARGE",
                status_code=400,
            )
        total_bytes += len(raw)
        if total_bytes > caps.max_input_bytes_total:
            raise AppError(
                f"输入图总大小不得超过 {caps.max_input_bytes_total} bytes",
                code="ASSET_TOO_LARGE",
                status_code=400,
            )
        mime = asset.mime or "application/octet-stream"
        name = asset.display_name or f"{aid}{asset.extension or '.png'}"
        # OpenAI multipart: repeated "image" fields for multi-image
        files.append(("image", (name, raw, mime)))
        if i == 0:
            if asset.width and asset.height:
                primary_wh = (asset.width, asset.height)
            else:
                try:
                    with Image.open(io.BytesIO(raw)) as im:
                        primary_wh = im.size
                except Exception:
                    primary_wh = None

    if req.mask_asset_id:
        mask = await asset_repo.get_asset(req.mask_asset_id)
        if mask is None:
            raise AppError("蒙版资产不存在", code="MASK_INVALID", status_code=400)
        if not mask.has_alpha:
            raise AppError(
                "蒙版必须含 alpha 通道", code="MASK_INVALID", status_code=400
            )
        mpath = resolve_data_path(mask.storage_path)
        mask_bytes = mpath.read_bytes()
        mask_wh: tuple[int, int] | None = None
        if mask.width and mask.height:
            mask_wh = (mask.width, mask.height)
        else:
            try:
                with Image.open(io.BytesIO(mask_bytes)) as im:
                    mask_wh = im.size
            except Exception:
                mask_wh = None
        if primary_wh and mask_wh and primary_wh != mask_wh:
            raise AppError(
                f"蒙版尺寸 {mask_wh[0]}x{mask_wh[1]} 必须与主图 "
                f"{primary_wh[0]}x{primary_wh[1]} 一致",
                code="MASK_INVALID",
                status_code=400,
            )
        files.append(
            (
                "mask",
                (
                    mask.display_name or f"{mask.id}.png",
                    mask_bytes,
                    mask.mime or "image/png",
                ),
            )
        )

    data: dict[str, str] = {
        "model": str(plan["model"]),
        "prompt": req.prompt,
        "size": str(plan["size"]),
        "quality": str(plan["quality"]),
        "n": str(plan["n"]),
        "output_format": str(plan["output_format"]),
        "moderation": str(plan["moderation"]),
    }
    if "output_compression" in plan:
        data["output_compression"] = str(plan["output_compression"])
    if "background" in plan:
        data["background"] = str(plan["background"])
    if "partial_images" in plan:
        data["partial_images"] = str(plan["partial_images"])

    return await request_with_401_retry(
        profile,
        "POST",
        "/v1/images/edits",
        transport=transport,
        data=data,
        files=files,
    )


def _extract_images(
    payload: dict[str, Any], *, preferred_format: str
) -> list[GeneratedImage]:
    items = payload.get("data") or []
    if not items:
        raise AppError("上游未返回图片数据", code="UPSTREAM_PROTOCOL_ERROR")
    out: list[GeneratedImage] = []
    for item in items:
        blob: bytes | None = None
        if item.get("b64_json"):
            blob = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            with httpx.Client(timeout=120.0, trust_env=False) as sync:
                r = sync.get(item["url"])
                r.raise_for_status()
                blob = r.content
        if blob is None:
            raise AppError("图片项缺少 b64_json 和 url", code="UPSTREAM_PROTOCOL_ERROR")
        mime, ext = _detect_format(blob, preferred_format)
        width = height = None
        try:
            with Image.open(io.BytesIO(blob)) as im:
                width, height = im.size
        except Exception:
            pass
        out.append(
            GeneratedImage(
                data=blob,
                mime=mime,
                extension=ext,
                width=width,
                height=height,
                byte_size=len(blob),
            )
        )
    return out


def _detect_format(data: bytes, preferred: str) -> tuple[str, str]:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    # Fall back to requested format
    return FORMAT_MIME.get(preferred, "image/png"), FORMAT_EXT.get(preferred, ".png")


# ---- Legacy helpers used by old task_service paths ----


async def generate_text_to_image_legacy(
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    n: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[bytes]:
    req = GenerationRequest(
        mode="generate",
        prompt=prompt,
        model=model,
        size=size,
        quality=quality,  # type: ignore[arg-type]
        n=n,
    )
    result = await generate(req, transport=transport)
    return [img.data for img in result.images]


async def generate_image_to_image_legacy(
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    n: int,
    image_path: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[bytes]:
    """Legacy single-file edit: register path as temp asset id via direct multipart."""
    # Build a one-off edit without asset table by using temporary GenerationRequest
    # after registering the file.
    from app.services import asset_service

    data = image_path.read_bytes()
    asset = await asset_service.save_bytes_as_asset(
        data, category="input", original_filename=image_path.name
    )
    req = GenerationRequest(
        mode="reference",
        prompt=prompt,
        model=model,
        size=size,
        quality=quality,  # type: ignore[arg-type]
        n=n,
        input_asset_ids=[asset.id],
        primary_asset_id=asset.id,
    )
    result = await generate(req, transport=transport)
    return [img.data for img in result.images]
