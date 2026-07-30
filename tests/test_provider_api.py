from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app.schemas.provider import ProviderCreate, TokenDistributorConfig
from app.services import provider_client, provider_service
from app.services.token_provider import get_access_token
from httpx import AsyncClient
from tests.helpers import PNG_1X1


@pytest.mark.asyncio
async def test_list_providers_after_migration(client: AsyncClient) -> None:
    res = await client.get("/api/v1/providers")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "default"
    assert items[0]["is_active"] is True
    assert items[0]["api_key_set"] is True
    assert "api_key" not in items[0]
    dumped = json.dumps(items)
    assert "sk-test-key-12345678" not in dumped


@pytest.mark.asyncio
async def test_create_provider_secret_not_echoed(client: AsyncClient) -> None:
    res = await client.post(
        "/api/v1/providers",
        json={
            "id": "openai-direct",
            "name": "OpenAI Direct",
            "base_url": "https://api.example.test/v1",
            "auth_type": "static_bearer",
            "default_model": "gpt-image-2",
            "api_key": "sk-secret-SHOULD-NOT-APPEAR",
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "openai-direct"
    assert body["api_key_set"] is True
    assert "api_key" not in body
    assert "SHOULD-NOT-APPEAR" not in json.dumps(body)


@pytest.mark.asyncio
async def test_activate_and_legacy_settings_sync(client: AsyncClient) -> None:
    await client.post(
        "/api/v1/providers",
        json={
            "id": "alt",
            "name": "Alt",
            "base_url": "https://alt.example.test/v1",
            "default_model": "alt-model",
            "api_key": "sk-alt-key-12345678",
        },
    )
    act = await client.post("/api/v1/providers/alt/activate")
    assert act.status_code == 200
    assert act.json()["is_active"] is True

    settings = await client.get("/api/settings")
    data = settings.json()
    assert data["base_url"] == "https://alt.example.test/v1"
    assert data["default_model"] == "alt-model"
    assert data["active_provider_id"] == "alt"
    assert "api_key" not in data
    assert data["api_key_set"] is True


@pytest.mark.asyncio
async def test_cannot_delete_active_provider(client: AsyncClient) -> None:
    res = await client.delete("/api/v1/providers/default")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_connection_test_static_mocked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-image-2", "owned_by": "openai"}]},
        )

    transport = httpx.MockTransport(handler)
    original_list = provider_client.list_models_for_provider

    async def fake_list(profile, *, show_all: bool = False, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("token_transport", None)
        return await original_list(profile, show_all=show_all, transport=transport)

    monkeypatch.setattr(provider_client, "list_models_for_provider", fake_list)

    res = await client.post("/api/v1/providers/default/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert [s["name"] for s in body["stages"]] == ["config", "token", "models"]
    assert all(s["ok"] for s in body["stages"])
    assert body["token_masked"]
    assert "sk-test-key-12345678" not in json.dumps(body)
    assert body["model_count"] == 1


@pytest.mark.asyncio
async def test_connection_test_distributor_mocked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await client.post(
        "/api/v1/providers",
        json={
            "id": "corp",
            "name": "Corp",
            "base_url": "https://images.example.test/v1",
            "auth_type": "token_distributor",
            "default_model": "gpt-image-2",
            "distributor_client_secret": "dist-secret",
            "token_distributor": TokenDistributorConfig(
                base_url="https://distributor.example.test",
                path="/token",
                auth_mode="bearer",
                token_path="access_token",
                expires_in_path="expires_in",
            ).model_dump(),
        },
    )
    assert created.status_code == 201

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "distributor.example.test" in url:
            return httpx.Response(
                200,
                json={
                    "access_token": "ephemeral-TOKEN-value",
                    "expires_in": 600,
                    "token_type": "Bearer",
                },
            )
        if request.url.path.endswith("/v1/models"):
            auth = request.headers.get("authorization", "")
            assert "ephemeral-TOKEN-value" in auth
            return httpx.Response(
                200, json={"data": [{"id": "gpt-image-2", "owned_by": "x"}]}
            )
        return httpx.Response(404, json={"error": {"message": f"not found: {url}"}})

    transport = httpx.MockTransport(handler)

    async def fake_get_token(profile, *, force_refresh: bool = False, **kwargs):
        return await get_access_token(
            profile, force_refresh=force_refresh, transport=transport
        )

    original_list = provider_client.list_models_for_provider

    async def fake_list(profile, *, show_all: bool = False, **kwargs):
        return await original_list(
            profile,
            show_all=show_all,
            transport=transport,
            token_transport=transport,
        )

    monkeypatch.setattr(
        "app.services.provider_service.get_access_token", fake_get_token
    )
    monkeypatch.setattr(provider_client, "list_models_for_provider", fake_list)

    res = await client.post("/api/v1/providers/corp/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True, body
    assert body["token_masked"]
    assert "ephemeral-TOKEN-value" not in json.dumps(body)


@pytest.mark.asyncio
async def test_legacy_generate_still_works_after_migration(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import image_service

    async def fake_text(**kwargs: object) -> list[bytes]:
        return [PNG_1X1]

    monkeypatch.setattr(image_service, "generate_text_to_image", fake_text)
    res = await client.post(
        "/api/generate",
        data={
            "mode": "text",
            "prompt": "hello after migrate",
            "model": "gpt-image-2",
        },
    )
    assert res.status_code == 200
    assert "task_id" in res.json()


@pytest.mark.asyncio
async def test_provider_models_endpoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-image-2", "owned_by": "openai"}]},
        )

    transport = httpx.MockTransport(handler)
    original_list = provider_client.list_models_for_provider

    async def fake_list(profile, *, show_all: bool = False, **kwargs):
        return await original_list(profile, show_all=show_all, transport=transport)

    monkeypatch.setattr(provider_client, "list_models_for_provider", fake_list)

    res = await client.get("/api/v1/providers/default/models")
    assert res.status_code == 200
    assert res.json()["data"][0]["id"] == "gpt-image-2"


def test_soft_delete_hides_provider(isolated_env: Path) -> None:
    provider_service.create_provider(
        ProviderCreate(
            id="tmp",
            name="Tmp",
            base_url="https://tmp.example.test",
            api_key="sk-tmp-key-12345678",
        )
    )
    provider_service.delete_provider("tmp")
    ids = {p.id for p in provider_service.list_providers()}
    assert "tmp" not in ids
    ids_all = {p.id for p in provider_service.list_providers(include_deleted=True)}
    assert "tmp" in ids_all
