from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from app.services import image_service, images_adapter, provider_client
from app.services.provider_service import get_active_provider
from tests.helpers import PNG_1X1


def _b64_png() -> str:
    return base64.b64encode(PNG_1X1).decode("ascii")


@pytest.mark.asyncio
async def test_list_models_filters_by_keyword(isolated_env: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/models")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-image-2", "owned_by": "openai"},
                    {"id": "gpt-4o", "owned_by": "openai"},
                ]
            },
        )

    profile = get_active_provider()
    models = await provider_client.list_models_for_provider(
        profile, show_all=False, transport=httpx.MockTransport(handler)
    )
    assert [m["id"] for m in models] == ["gpt-image-2"]


@pytest.mark.asyncio
async def test_generate_text_to_image_mock(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.method == "POST"
        assert request.url.path.endswith("/v1/images/generations")
        body = json.loads(request.content.decode("utf-8"))
        assert body["prompt"] == "a red cube"
        assert body["model"] == "gpt-image-2"
        assert body["n"] == 1
        return httpx.Response(200, json={"data": [{"b64_json": _b64_png()}]})

    transport = httpx.MockTransport(handler)
    original = provider_client.request_with_401_retry

    async def fake_request(profile, method, url, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("token_transport", None)
        return await original(profile, method, url, transport=transport, **kwargs)

    monkeypatch.setattr(provider_client, "request_with_401_retry", fake_request)
    monkeypatch.setattr(images_adapter, "request_with_401_retry", fake_request)

    images = await image_service.generate_text_to_image(
        prompt="a red cube",
        model="gpt-image-2",
        size="1024x1024",
        quality="medium",
        n=1,
    )
    assert len(images) == 1
    assert images[0][:8] == b"\x89PNG\r\n\x1a\n"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_generate_image_to_image_mock(
    isolated_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()
    ref = tmp_path / "ref.png"
    ref.write_bytes(PNG_1X1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/v1/images/edits")
        content_type = request.headers.get("content-type", "")
        assert "multipart/form-data" in content_type
        return httpx.Response(200, json={"data": [{"b64_json": _b64_png()}]})

    transport = httpx.MockTransport(handler)
    original = provider_client.request_with_401_retry

    async def fake_request(profile, method, url, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("token_transport", None)
        return await original(profile, method, url, transport=transport, **kwargs)

    monkeypatch.setattr(provider_client, "request_with_401_retry", fake_request)
    monkeypatch.setattr(images_adapter, "request_with_401_retry", fake_request)

    images = await image_service.generate_image_to_image(
        prompt="make it blue",
        model="gpt-image-2",
        size="1024x1024",
        quality="medium",
        n=1,
        image_path=ref,
    )
    assert len(images) == 1


@pytest.mark.asyncio
async def test_image_api_error_message(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {"message": "moderation blocked", "type": "invalid_request"}
            },
        )

    transport = httpx.MockTransport(handler)
    original = provider_client.request_with_401_retry

    async def fake_request(profile, method, url, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("token_transport", None)
        return await original(profile, method, url, transport=transport, **kwargs)

    monkeypatch.setattr(provider_client, "request_with_401_retry", fake_request)
    monkeypatch.setattr(images_adapter, "request_with_401_retry", fake_request)

    with pytest.raises(image_service.ImageAPIError) as ei:
        await image_service.generate_text_to_image(
            prompt="x",
            model="gpt-image-2",
            size="1024x1024",
            quality="medium",
            n=1,
        )
    assert "moderation blocked" in ei.value.message
    assert ei.value.status_code == 400
    assert ei.value.code == "MODERATION_BLOCKED"


@pytest.mark.asyncio
async def test_401_refreshes_token_once(isolated_env: Path) -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        auth = request.headers.get("authorization", "")
        if state["calls"] == 1:
            assert "sk-test-key-12345678" in auth
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"data": [{"b64_json": _b64_png()}]})

    transport = httpx.MockTransport(handler)
    profile = get_active_provider()
    resp = await provider_client.request_with_401_retry(
        profile, "POST", "/v1/images/generations", transport=transport, json={}
    )
    assert resp.status_code == 200
    assert state["calls"] == 2
