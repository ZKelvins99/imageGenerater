from __future__ import annotations

import asyncio
import base64
import io
import random
from typing import Any, Literal

import httpx
from PIL import Image
from pydantic import BaseModel, Field

from app.schemas.generation import GeneratedImage
from app.schemas.provider import ProviderProfile
from app.services.errors import (
    AppError,
    classify_http_error,
    extract_upstream_request_id,
)
from app.services.provider_client import request_with_401_retry


class ResponsesImageResult(BaseModel):
    response_id: str
    image: GeneratedImage
    revised_prompt: str | None = None
    image_call_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    upstream_request_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}


async def create_image_turn(
    *,
    profile: ProviderProfile,
    responses_model: str,
    prompt: str,
    previous_response_id: str | None = None,
    source_image: tuple[bytes, str] | None = None,
    action: Literal["auto", "generate", "edit"] = "edit",
    transport: httpx.AsyncBaseTransport | None = None,
    max_attempts: int = 3,
) -> ResponsesImageResult:
    """Call the Responses image tool without coupling it to the Images adapter."""
    tool: dict[str, Any] = {"type": "image_generation", "action": action}
    body: dict[str, Any] = {
        "model": responses_model,
        "tools": [tool],
        "store": True,
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
        body["input"] = prompt
    elif source_image:
        raw, mime = source_image
        encoded = base64.b64encode(raw).decode("ascii")
        body["input"] = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime};base64,{encoded}",
                        "detail": "auto",
                    },
                ],
            }
        ]
    else:
        if action == "edit":
            raise AppError(
                "首次对话编辑需要一张来源图片",
                code="INPUT_INVALID",
                status_code=400,
            )
        body["input"] = prompt

    last_error: AppError | None = None
    for attempt in range(1, max_attempts + 1):
        resp = await request_with_401_retry(
            profile,
            "POST",
            "/v1/responses",
            transport=transport,
            json=body,
        )
        if resp.status_code < 400:
            return _parse_response(resp)
        error = classify_http_error(resp)
        if previous_response_id and resp.status_code in (400, 404):
            raise AppError(
                "上游多轮会话已失效，无法继续；请从最后一张图片新建会话",
                code="RESPONSES_CONTEXT_UNAVAILABLE",
                status_code=409,
                request_id=error.request_id,
            ) from error
        last_error = error
        if not error.retryable or attempt >= max_attempts:
            raise error
        delay = error.retry_after
        if delay is None:
            delay = min(8.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.25))
        await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def _parse_response(resp: httpx.Response) -> ResponsesImageResult:
    request_id = extract_upstream_request_id(resp)
    try:
        payload = resp.json()
    except Exception as e:
        raise AppError(
            "Responses API 返回了无效 JSON",
            code="UPSTREAM_PROTOCOL_ERROR",
            request_id=request_id,
        ) from e

    response_id = str(payload.get("id") or "")
    if not response_id:
        raise AppError(
            "Responses API 响应缺少 id",
            code="UPSTREAM_PROTOCOL_ERROR",
            request_id=request_id,
        )
    image_call = next(
        (
            item
            for item in (payload.get("output") or [])
            if isinstance(item, dict) and item.get("type") == "image_generation_call"
        ),
        None,
    )
    if not image_call or not image_call.get("result"):
        raise AppError(
            "Responses API 未返回图像工具结果",
            code="UPSTREAM_PROTOCOL_ERROR",
            request_id=request_id,
        )
    try:
        blob = base64.b64decode(image_call["result"], validate=True)
    except (TypeError, ValueError) as e:
        raise AppError(
            "Responses API 图像结果 Base64 无效",
            code="UPSTREAM_PROTOCOL_ERROR",
            request_id=request_id,
        ) from e

    mime, extension = _detect_image_format(blob)
    width = height = None
    try:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
    except Exception:
        pass
    return ResponsesImageResult(
        response_id=response_id,
        image=GeneratedImage(
            data=blob,
            mime=mime,
            extension=extension,
            width=width,
            height=height,
            byte_size=len(blob),
        ),
        revised_prompt=image_call.get("revised_prompt"),
        image_call_id=image_call.get("id"),
        usage=payload.get("usage") or {},
        upstream_request_id=request_id,
    )


def _detect_image_format(data: bytes) -> tuple[str, str]:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "image/png", ".png"
