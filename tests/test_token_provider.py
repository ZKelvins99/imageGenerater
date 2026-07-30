from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from app.schemas.provider import ProviderProfile, ProviderSecret, TokenDistributorConfig
from app.services import config_service
from app.services.config_service import save_provider_secret
from app.services.token_provider import (
    AccessToken,
    CompanyTokenDistributorProvider,
    StaticBearerTokenProvider,
    TokenCache,
    TokenError,
    get_access_token,
    reset_token_cache,
)


@pytest.mark.asyncio
async def test_static_bearer_cache_and_invalidate(isolated_env: Path) -> None:
    reset_token_cache()
    cache = TokenCache(static_ttl_seconds=3600)
    provider = StaticBearerTokenProvider(cache=cache)
    t1 = await provider.get_token("default")
    t2 = await provider.get_token("default")
    assert t1.token == t2.token == "sk-test-key-12345678"
    assert t1.source == "static_bearer"

    save_provider_secret("default", ProviderSecret(api_key="sk-rotated-ABCDEFGH"))
    # Without invalidate / force, still cached
    t3 = await provider.get_token("default")
    assert t3.token == "sk-test-key-12345678"

    provider.invalidate("default")
    t4 = await provider.get_token("default", force_refresh=True)
    assert t4.token == "sk-rotated-ABCDEFGH"


@pytest.mark.asyncio
async def test_static_single_flight(isolated_env: Path) -> None:
    reset_token_cache()
    cache = TokenCache(static_ttl_seconds=3600)
    fetches = {"n": 0}
    original = config_service.load_provider_secret

    async def fetch():
        async def _fetch():
            fetches["n"] += 1
            await asyncio.sleep(0.05)
            secret = original("default")
            return AccessToken(
                token=secret.api_key,
                token_type="Bearer",
                expires_at=time.time() + 3600,
                source="static_bearer",
            )

        return await cache.get_or_fetch("default", _fetch)

    results = await asyncio.gather(*[fetch() for _ in range(8)])
    assert all(r.token == "sk-test-key-12345678" for r in results)
    assert fetches["n"] == 1


@pytest.mark.asyncio
async def test_distributor_parses_expires_in(isolated_env: Path) -> None:
    reset_token_cache()
    save_provider_secret(
        "corp",
        ProviderSecret(
            distributor_client_id="cid",
            distributor_client_secret="csecret",
        ),
    )
    profile = ProviderProfile(
        id="corp",
        name="Corp",
        base_url="https://images.example.test/v1",
        auth_type="token_distributor",
        token_distributor=TokenDistributorConfig(
            base_url="https://distributor.example.test",
            path="/oauth/token",
            method="POST",
            auth_mode="bearer",
            token_path="access_token",
            expires_in_path="expires_in",
            token_type_path="token_type",
            request_body={"scope": "images"},
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url).endswith("/oauth/token")
        assert request.headers.get("authorization") == "Bearer csecret"
        return httpx.Response(
            200,
            json={
                "access_token": "dist-token-XYZ",
                "token_type": "Bearer",
                "expires_in": 120,
            },
        )

    transport = httpx.MockTransport(handler)
    provider = CompanyTokenDistributorProvider(
        profile, cache=TokenCache(), transport=transport
    )
    before = time.time()
    token = await provider.get_token("corp", force_refresh=True)
    assert token.token == "dist-token-XYZ"
    assert token.source == "token_distributor"
    assert token.expires_at is not None
    assert before + 100 <= token.expires_at <= before + 140


@pytest.mark.asyncio
async def test_distributor_missing_config(isolated_env: Path) -> None:
    profile = ProviderProfile(
        id="bad",
        name="Bad",
        auth_type="token_distributor",
        token_distributor=None,
    )
    with pytest.raises(TokenError) as ei:
        await get_access_token(profile, force_refresh=True)
    assert ei.value.code == "CONFIG_INVALID"


@pytest.mark.asyncio
async def test_early_refresh_skew(isolated_env: Path) -> None:
    cache = TokenCache(refresh_skew_seconds=30, jitter_seconds=0)
    # Token that expires in 10s should be treated as needing refresh (skew=30)
    cache._tokens["default"] = AccessToken(
        token="old",
        expires_at=time.time() + 10,
        source="static_bearer",
    )
    assert cache._needs_refresh(cache.get_cached("default")) is True
