from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.schemas.provider import ProviderProfile, TokenDistributorConfig
from app.services import config_service


class TokenError(Exception):
    def __init__(self, message: str, code: str = "TOKEN_FETCH_FAILED"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class AccessToken:
    token: str
    token_type: str = "Bearer"
    expires_at: float | None = None  # unix epoch seconds
    source: str = "unknown"

    def is_expired(self, *, skew_seconds: float = 0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - skew_seconds)


class TokenProvider(Protocol):
    async def get_token(
        self, provider_id: str, *, force_refresh: bool = False
    ) -> AccessToken: ...

    def invalidate(self, provider_id: str) -> None: ...


def _json_path(data: Any, path: str | None) -> Any:
    if not path:
        return None
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class TokenCache:
    """In-memory token cache with single-flight refresh per provider."""

    def __init__(
        self,
        *,
        refresh_skew_seconds: float = 60.0,
        jitter_seconds: float = 15.0,
        static_ttl_seconds: float = 3600.0,
    ) -> None:
        self._tokens: dict[str, AccessToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()
        self.refresh_skew_seconds = refresh_skew_seconds
        self.jitter_seconds = jitter_seconds
        self.static_ttl_seconds = static_ttl_seconds

    async def _lock_for(self, provider_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(provider_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[provider_id] = lock
            return lock

    def invalidate(self, provider_id: str) -> None:
        self._tokens.pop(provider_id, None)

    def get_cached(self, provider_id: str) -> AccessToken | None:
        return self._tokens.get(provider_id)

    def _needs_refresh(self, token: AccessToken | None) -> bool:
        if token is None:
            return True
        skew = self.refresh_skew_seconds + random.uniform(0, self.jitter_seconds)
        if token.expires_at is None:
            return False
        return token.is_expired(skew_seconds=skew)

    async def get_or_fetch(
        self,
        provider_id: str,
        fetcher,
        *,
        force_refresh: bool = False,
    ) -> AccessToken:
        lock = await self._lock_for(provider_id)
        async with lock:
            cached = self._tokens.get(provider_id)
            if (
                not force_refresh
                and cached is not None
                and not self._needs_refresh(cached)
            ):
                return cached
            token = await fetcher()
            self._tokens[provider_id] = token
            return token


_CACHE = TokenCache()


def get_token_cache() -> TokenCache:
    return _CACHE


def reset_token_cache() -> None:
    global _CACHE
    _CACHE = TokenCache()


class StaticBearerTokenProvider:
    def __init__(self, cache: TokenCache | None = None) -> None:
        self._cache = cache or _CACHE

    def invalidate(self, provider_id: str) -> None:
        self._cache.invalidate(provider_id)

    async def get_token(
        self, provider_id: str, *, force_refresh: bool = False
    ) -> AccessToken:
        async def fetch() -> AccessToken:
            secret = config_service.load_provider_secret(provider_id)
            if not secret.api_key:
                raise TokenError("API Key 未配置", code="CONFIG_INVALID")
            ttl = self._cache.static_ttl_seconds
            return AccessToken(
                token=secret.api_key,
                token_type="Bearer",
                expires_at=time.time() + ttl,
                source="static_bearer",
            )

        return await self._cache.get_or_fetch(
            provider_id, fetch, force_refresh=force_refresh
        )


class CompanyTokenDistributorProvider:
    def __init__(
        self,
        profile: ProviderProfile,
        cache: TokenCache | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._profile = profile
        self._cache = cache or _CACHE
        self._transport = transport

    def invalidate(self, provider_id: str) -> None:
        self._cache.invalidate(provider_id)

    async def get_token(
        self, provider_id: str, *, force_refresh: bool = False
    ) -> AccessToken:
        async def fetch() -> AccessToken:
            return await self._fetch_from_distributor(provider_id)

        return await self._cache.get_or_fetch(
            provider_id, fetch, force_refresh=force_refresh
        )

    async def _fetch_from_distributor(self, provider_id: str) -> AccessToken:
        cfg = self._profile.token_distributor
        if cfg is None:
            raise TokenError("token_distributor 配置缺失", code="CONFIG_INVALID")
        if not cfg.base_url.strip():
            raise TokenError("Distributor base_url 未配置", code="CONFIG_INVALID")

        secret = config_service.load_provider_secret(provider_id)
        url = cfg.base_url.rstrip("/") + (
            cfg.path if cfg.path.startswith("/") else f"/{cfg.path}"
        )
        headers = self._auth_headers(cfg, secret.distributor_client_secret)
        body = dict(cfg.request_body)
        if secret.distributor_client_id and "client_id" not in body:
            body["client_id"] = secret.distributor_client_id

        timeout = httpx.Timeout(
            cfg.timeout_seconds, connect=min(10.0, cfg.timeout_seconds)
        )
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=timeout,
            trust_env=False,
            verify=self._profile.verify_tls,
        ) as client:
            try:
                if cfg.method == "GET":
                    resp = await client.get(url, headers=headers, params=body or None)
                else:
                    resp = await client.post(url, headers=headers, json=body or None)
            except httpx.HTTPError as e:
                raise TokenError(
                    f"Distributor 请求失败: {e}", code="TOKEN_FETCH_FAILED"
                ) from e

        if resp.status_code >= 400:
            raise TokenError(
                f"Distributor 返回 HTTP {resp.status_code}",
                code="TOKEN_FETCH_FAILED",
            )

        try:
            payload = resp.json()
        except Exception as e:
            raise TokenError(
                "Distributor 响应不是合法 JSON", code="TOKEN_FETCH_FAILED"
            ) from e

        token_val = _json_path(payload, cfg.token_path)
        if not token_val:
            raise TokenError(
                f"Distributor 响应缺少 token 字段 ({cfg.token_path})",
                code="TOKEN_FETCH_FAILED",
            )

        token_type = _json_path(payload, cfg.token_type_path) or "Bearer"
        expires_at = self._parse_expiry(payload, cfg)

        return AccessToken(
            token=str(token_val),
            token_type=str(token_type),
            expires_at=expires_at,
            source="token_distributor",
        )

    @staticmethod
    def _auth_headers(
        cfg: TokenDistributorConfig, client_secret: str
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if cfg.auth_mode == "none":
            return headers
        if not client_secret:
            raise TokenError("Distributor client secret 未配置", code="CONFIG_INVALID")
        if cfg.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {client_secret}"
        elif cfg.auth_mode == "basic":
            # client_secret treated as raw Basic credential (already encoded or user:pass)
            headers["Authorization"] = f"Basic {client_secret}"
        elif cfg.auth_mode == "header":
            headers[cfg.auth_header_name] = client_secret
        return headers

    @staticmethod
    def _parse_expiry(payload: Any, cfg: TokenDistributorConfig) -> float | None:
        if cfg.expires_at_path:
            raw = _json_path(payload, cfg.expires_at_path)
            if raw is None:
                return None
            if isinstance(raw, (int, float)):
                # Heuristic: ms vs s
                val = float(raw)
                return val / 1000.0 if val > 1e12 else val
            if isinstance(raw, str):
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    return dt.timestamp()
                except ValueError:
                    return None
        if cfg.expires_in_path:
            raw = _json_path(payload, cfg.expires_in_path)
            if isinstance(raw, (int, float)):
                return time.time() + float(raw)
        return time.time() + 3600.0


def build_token_provider(
    profile: ProviderProfile,
    *,
    cache: TokenCache | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> StaticBearerTokenProvider | CompanyTokenDistributorProvider:
    if profile.auth_type == "token_distributor":
        return CompanyTokenDistributorProvider(
            profile, cache=cache, transport=transport
        )
    return StaticBearerTokenProvider(cache=cache)


async def get_access_token(
    profile: ProviderProfile,
    *,
    force_refresh: bool = False,
    cache: TokenCache | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AccessToken:
    provider = build_token_provider(profile, cache=cache, transport=transport)
    return await provider.get_token(profile.id, force_refresh=force_refresh)
