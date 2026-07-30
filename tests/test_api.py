from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.services import config_service, image_service, task_service
from httpx import AsyncClient
from tests.helpers import PNG_1X1

BASELINES = Path(__file__).parent / "baselines"


@pytest.mark.asyncio
async def test_settings_get_baseline(client: AsyncClient) -> None:
    res = await client.get("/api/settings")
    assert res.status_code == 200
    data = res.json()
    expected = json.loads(
        (BASELINES / "settings_public.json").read_text(encoding="utf-8")
    )
    # Shape + stable fields; masked key may vary by fixture key length rules
    for key in expected:
        assert key in data
    assert data["api_key_set"] is True
    assert "api_key" not in data
    assert data["default_model"] == expected["default_model"]
    assert data["default_size"] == expected["default_size"]
    assert data["base_url"] == expected["base_url"]


@pytest.mark.asyncio
async def test_settings_put_does_not_echo_secret(client: AsyncClient) -> None:
    res = await client.put(
        "/api/settings",
        json={"default_quality": "high", "api_key": "sk-new-secret-ABCDEFGH"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "api_key" not in data
    assert data["api_key_set"] is True
    assert data["default_quality"] == "high"
    assert "ABCDEFGH" not in json.dumps(data)


@pytest.mark.asyncio
async def test_history_empty_baseline(client: AsyncClient) -> None:
    res = await client.get("/api/history")
    assert res.status_code == 200
    assert res.json() == {"items": []}


@pytest.mark.asyncio
async def test_generate_requires_prompt(client: AsyncClient) -> None:
    res = await client.post(
        "/api/generate",
        data={"mode": "text", "prompt": "   ", "model": "gpt-image-2"},
    )
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_generate_text_flow_mocked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_text(**kwargs: object) -> list[bytes]:
        return [PNG_1X1]

    monkeypatch.setattr(image_service, "generate_text_to_image", fake_text)

    res = await client.post(
        "/api/generate",
        data={
            "mode": "text",
            "prompt": "a blue square",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "medium",
            "n": "1",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "task_id" in body
    assert body["status"] == "pending"
    task_id = body["task_id"]

    final = None
    for _ in range(100):
        status = await client.get(f"/api/tasks/{task_id}")
        assert status.status_code == 200
        final = status.json()
        if final["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)

    assert final is not None
    assert final["status"] == "done"
    assert final["output_urls"]
    assert final["output_urls"][0].startswith("/media/")

    hist = await client.get("/api/history")
    items = hist.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "done"
    assert items[0]["prompt"] == "a blue square"


@pytest.mark.asyncio
async def test_generate_image_flow_mocked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_edit(**kwargs: object) -> list[bytes]:
        return [PNG_1X1]

    monkeypatch.setattr(image_service, "generate_image_to_image", fake_edit)

    res = await client.post(
        "/api/generate",
        data={
            "mode": "image",
            "prompt": "make warmer",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "medium",
            "n": "1",
        },
        files={"image": ("ref.png", PNG_1X1, "image/png")},
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
    hist = await client.get("/api/history")
    item = hist.json()["items"][0]
    assert item["mode"] == "image"
    assert item["reference_path"]
    assert item["reference_path"].startswith(("uploads/", "assets/"))


@pytest.mark.asyncio
async def test_models_endpoint_mocked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(show_all: bool = False) -> list[dict]:
        return [{"id": "gpt-image-2", "owned_by": "openai"}]

    monkeypatch.setattr(image_service, "list_models", fake_list)
    res = await client.get("/api/models")
    assert res.status_code == 200
    assert res.json() == {
        "data": [{"id": "gpt-image-2", "owned_by": "openai"}],
        "show_all": False,
    }


def test_ws_hello_and_pong(isolated_env: Path) -> None:
    from app.api import ws_routes
    from app.main import create_app
    from starlette.testclient import TestClient

    config_service.ensure_dirs()
    app = create_app()
    task_service.task_manager.set_broadcast(ws_routes.ws_manager.broadcast)

    with TestClient(app) as tc:
        with tc.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["payload"]["ok"] is True
            ws.send_text("ping")
            pong = ws.receive_json()
            assert pong["type"] == "pong"
