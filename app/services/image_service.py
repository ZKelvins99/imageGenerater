from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from app.services import config_service, provider_client
from app.services.provider_client import ProviderClientError, request_with_401_retry
from app.services.provider_service import get_active_provider


class ImageAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _client() -> httpx.AsyncClient:
    """Deprecated sync-style factory — prefer provider_client.create_client.

    Kept for any residual callers; raises if used without active provider.
    New code should use async create_client / request_with_401_retry.
    """
    settings = config_service.load_settings()
    base = (settings.base_url or "").rstrip("/")
    if not base:
        raise ImageAPIError("Base URL 未配置，请先在设置页填写")
    if not settings.api_key:
        raise ImageAPIError("API Key 未配置，请先在设置页填写")
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
    }
    return httpx.AsyncClient(
        base_url=base,
        headers=headers,
        timeout=httpx.Timeout(300.0, connect=30.0),
        trust_env=False,
    )


async def list_models(show_all: bool = False) -> list[dict[str, Any]]:
    try:
        profile = get_active_provider()
    except Exception as e:
        raise ImageAPIError(str(e)) from e
    try:
        return await provider_client.list_models_for_provider(
            profile, show_all=show_all
        )
    except ProviderClientError as e:
        raise ImageAPIError(e.message, status_code=e.status_code) from e


async def generate_text_to_image(
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    n: int,
) -> list[bytes]:
    body = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": n,
    }
    try:
        profile = get_active_provider()
        resp = await request_with_401_retry(
            profile, "POST", "/v1/images/generations", json=body
        )
    except ProviderClientError as e:
        raise ImageAPIError(e.message, status_code=e.status_code) from e
    if resp.status_code >= 400:
        raise ImageAPIError(_error_message(resp), status_code=resp.status_code)
    return _extract_images(resp.json())


async def generate_image_to_image(
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    n: int,
    image_path: Path,
) -> list[bytes]:
    """Call /v1/images/edits with one reference image (multipart)."""
    mime = _guess_mime(image_path)
    data = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": str(n),
    }
    file_bytes = image_path.read_bytes()
    files = {"image": (image_path.name, file_bytes, mime)}
    try:
        profile = get_active_provider()
        resp = await request_with_401_retry(
            profile, "POST", "/v1/images/edits", data=data, files=files
        )
    except ProviderClientError as e:
        raise ImageAPIError(e.message, status_code=e.status_code) from e
    if resp.status_code >= 400:
        raise ImageAPIError(_error_message(resp), status_code=resp.status_code)
    return _extract_images(resp.json())


def _extract_images(payload: dict[str, Any]) -> list[bytes]:
    items = payload.get("data") or []
    if not items:
        raise ImageAPIError("API returned no image data")
    out: list[bytes] = []
    for item in items:
        b64 = item.get("b64_json")
        if b64:
            out.append(base64.b64decode(b64))
            continue
        url = item.get("url")
        if url:
            # Rare for this gateway; fetch if present
            with httpx.Client(timeout=120.0, trust_env=False) as sync:
                r = sync.get(url)
                r.raise_for_status()
                out.append(r.content)
            continue
        raise ImageAPIError("Image item missing b64_json and url")
    return out


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if isinstance(err, str):
            return err
        return resp.text[:500] or f"HTTP {resp.status_code}"
    except Exception:
        return resp.text[:500] or f"HTTP {resp.status_code}"


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
