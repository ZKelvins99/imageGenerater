from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from app.services import images_adapter, provider_client
from httpx import AsyncClient
from tests.helpers import PNG_1X1


@pytest.mark.asyncio
async def test_v1_generations_endpoint(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["prompt"] == "hello v1"
        assert body["output_format"] == "png"
        return httpx.Response(
            200,
            headers={"x-request-id": "abc"},
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(PNG_1X1).decode(),
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    original = provider_client.request_with_401_retry

    async def fake_request(profile, method, url, **kwargs):
        kwargs.pop("transport", None)
        return await original(profile, method, url, transport=transport, **kwargs)

    monkeypatch.setattr(provider_client, "request_with_401_retry", fake_request)
    monkeypatch.setattr(images_adapter, "request_with_401_retry", fake_request)

    res = await client.post(
        "/api/v1/generations",
        json={
            "mode": "generate",
            "prompt": "hello v1",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "medium",
            "output_format": "png",
        },
    )
    assert res.status_code == 200
    task_id = res.json()["task_id"]
    final = None
    for _ in range(100):
        status = await client.get(f"/api/tasks/{task_id}")
        final = status.json()
        if final["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert final is not None
    assert final["status"] == "done"

    job = await client.get(f"/api/v1/jobs/{task_id}")
    assert job.json()["request_snapshot"].get("upstream_request_id") == "abc"


@pytest.mark.asyncio
async def test_capabilities_endpoint(client: AsyncClient) -> None:
    res = await client.get("/api/v1/models/gpt-image-2/capabilities")
    assert res.status_code == 200
    data = res.json()
    assert data["multi_image_reference"] is True
    assert data["mask_edit"] is True
    assert "transparent" not in data["background_modes"]
    assert data["input_fidelity_mode"] == "unsupported"


@pytest.mark.asyncio
async def test_reject_illegal_size_before_upstream(client: AsyncClient) -> None:
    res = await client.post(
        "/api/v1/generations",
        json={
            "mode": "generate",
            "prompt": "x",
            "model": "gpt-image-2",
            "size": "16x16",
        },
    )
    assert res.status_code == 400
    body = res.json()
    assert body["error"]["code"] == "INPUT_INVALID"


@pytest.mark.asyncio
async def test_reject_input_fidelity(client: AsyncClient) -> None:
    res = await client.post(
        "/api/v1/generations",
        json={
            "mode": "generate",
            "prompt": "x",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "input_fidelity": "high",
        },
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "CAPABILITY_UNSUPPORTED"


@pytest.mark.asyncio
async def test_reference_four_images(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = []
    for i in range(4):
        up = await client.post(
            "/api/v1/assets",
            files={"files": (f"r{i}.png", PNG_1X1, "image/png")},
            data={"category": "input"},
        )
        ids.append(up.json()["items"][0]["id"])

    def handler(request: httpx.Request) -> httpx.Response:
        text = request.content.decode("latin-1", errors="ignore")
        assert text.count('name="image"') == 4
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG_1X1).decode()}]},
        )

    transport = httpx.MockTransport(handler)
    original = provider_client.request_with_401_retry

    async def fake_request(profile, method, url, **kwargs):
        kwargs.pop("transport", None)
        return await original(profile, method, url, transport=transport, **kwargs)

    monkeypatch.setattr(provider_client, "request_with_401_retry", fake_request)
    monkeypatch.setattr(images_adapter, "request_with_401_retry", fake_request)

    res = await client.post(
        "/api/v1/generations",
        json={
            "mode": "reference",
            "prompt": "combine",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "input_asset_ids": ids,
            "primary_asset_id": ids[0],
        },
    )
    assert res.status_code == 200
    task_id = res.json()["task_id"]
    final = None
    for _ in range(100):
        status = await client.get(f"/api/tasks/{task_id}")
        final = status.json()
        if final["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert final is not None
    assert final["status"] == "done"
