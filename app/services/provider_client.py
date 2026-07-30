from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.schemas.provider import ProviderProfile
from app.services import config_service
from app.services.token_provider import (
    AccessToken,
    TokenError,
    get_access_token,
    get_token_cache,
)


class ProviderClientError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _safe_extra_headers(extra: dict[str, str]) -> dict[str, str]:
    blocked = {
        "authorization",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
    }
    out: dict[str, str] = {}
    for k, v in extra.items():
        if k.lower() in blocked:
            continue
        out[k] = v
    return out


@asynccontextmanager
async def create_client(
    profile: ProviderProfile | None = None,
    *,
    force_refresh: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    token_transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Build an authenticated httpx client for the given (or active) provider."""
    if profile is None:
        from app.services.provider_service import get_active_provider

        profile = get_active_provider()

    if not profile.base_url.strip():
        raise ProviderClientError("Base URL 未配置，请先在设置页填写")

    token = await get_access_token(
        profile,
        force_refresh=force_refresh,
        transport=token_transport,
    )
    headers = {
        "Authorization": f"{token.token_type} {token.token}".strip(),
        **_safe_extra_headers(profile.extra_headers),
    }
    timeout = httpx.Timeout(profile.timeout_seconds, connect=30.0)
    client = httpx.AsyncClient(
        base_url=profile.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        trust_env=False,
        verify=profile.verify_tls,
        transport=transport,
    )
    try:
        yield client
    finally:
        await client.aclose()


async def request_with_401_retry(
    profile: ProviderProfile,
    method: str,
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    token_transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue one request; on 401 invalidate token, refresh once, replay once."""
    async with create_client(
        profile,
        transport=transport,
        token_transport=token_transport,
    ) as client:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code != 401:
            return resp

    get_token_cache().invalidate(profile.id)
    async with create_client(
        profile,
        force_refresh=True,
        transport=transport,
        token_transport=token_transport,
    ) as client:
        return await client.request(method, url, **kwargs)


async def list_models_for_provider(
    profile: ProviderProfile,
    *,
    show_all: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    token_transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    settings = config_service.load_settings()
    resp = await request_with_401_retry(
        profile,
        "GET",
        "/v1/models",
        transport=transport,
        token_transport=token_transport,
    )
    if resp.status_code >= 400:
        raise ProviderClientError(_error_message(resp), status_code=resp.status_code)
    payload = resp.json()
    data = payload.get("data") or []
    if show_all:
        return data
    keywords = [k.lower() for k in settings.model_filter_keywords]
    if not keywords:
        return data
    filtered = []
    for m in data:
        mid = str(m.get("id", "")).lower()
        if any(k in mid for k in keywords):
            filtered.append(m)
    return filtered or data


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


# Keep AccessToken / TokenError re-exports handy for callers
__all__ = [
    "ProviderClientError",
    "create_client",
    "request_with_401_retry",
    "list_models_for_provider",
    "AccessToken",
    "TokenError",
]
