from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.services import config_service, images_adapter, provider_client
from app.services.errors import AppError
from app.services.provider_client import ProviderClientError
from app.services.provider_service import get_active_provider


class ImageAPIError(Exception):
    """Legacy error type kept for Phase 0–2 callers."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        code: str | None = None,
        retryable: bool = False,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code or "UPSTREAM_ERROR"
        self.retryable = retryable
        self.request_id = request_id

    @classmethod
    def from_app_error(cls, err: AppError) -> ImageAPIError:
        return cls(
            err.message,
            err.status_code,
            code=err.code,
            retryable=err.retryable,
            request_id=err.request_id,
        )


def _client() -> httpx.AsyncClient:
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
    try:
        return await images_adapter.generate_text_to_image_legacy(
            prompt=prompt,
            model=model,
            size=size,
            quality=quality,
            n=n,
        )
    except AppError as e:
        raise ImageAPIError.from_app_error(e) from e


async def generate_image_to_image(
    *,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    n: int,
    image_path: Path,
) -> list[bytes]:
    try:
        return await images_adapter.generate_image_to_image_legacy(
            prompt=prompt,
            model=model,
            size=size,
            quality=quality,
            n=n,
            image_path=image_path,
        )
    except AppError as e:
        raise ImageAPIError.from_app_error(e) from e


# Re-export request helper for tests that monkeypatch it
request_with_401_retry = provider_client.request_with_401_retry
