from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from app.schemas.generation import GenerationRequest
from app.services import images_adapter
from tests.helpers import PNG_1X1

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_images_api_stream_yields_partials_and_final(
    isolated_env: Path,
) -> None:
    encoded = base64.b64encode(PNG_1X1).decode("ascii")
    events = [
        {
            "type": "image_generation.partial_image",
            "partial_image_index": 0,
            "b64_json": encoded,
        },
        {
            "type": "image_generation.partial_image",
            "partial_image_index": 1,
            "b64_json": encoded,
        },
        {
            "type": "image_generation.completed",
            "b64_json": encoded,
        },
    ]
    stream_body = "".join(
        f"data: {json.dumps(event)}\n\n" for event in events
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["stream"] is True
        assert body["partial_images"] == 2
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "req_stream_1",
            },
            content=stream_body,
        )

    received: list[tuple[int, bytes]] = []

    async def on_partial(index: int, data: bytes) -> None:
        received.append((index, data))

    req = GenerationRequest(
        mode="generate",
        prompt="stream it",
        model="gpt-image-2",
        partial_images=2,
    )
    result = await images_adapter.generate(
        req,
        transport=httpx.MockTransport(handler),
        on_partial=on_partial,
    )

    assert [index for index, _ in received] == [0, 1]
    assert all(data == PNG_1X1 for _, data in received)
    assert result.images[0].data == PNG_1X1
    assert result.upstream_request_id == "req_stream_1"


def test_frontend_has_mask_canvas_workflow() -> None:
    for marker in (
        "mask-paint-canvas",
        "画笔绘制蒙版",
        "橡皮擦",
        "撤销",
        "重做",
        "反选",
        "使用此蒙版",
    ):
        assert marker in TEMPLATE
    for method in (
        "openMaskCanvas",
        "beginMaskStroke",
        "undoMask",
        "redoMask",
        "invertMaskCanvas",
        "saveMaskCanvas",
        "uploadMaskBlob",
    ):
        assert method in SCRIPT


def test_frontend_has_partial_preview_recovery_contract() -> None:
    assert "job.partial_image" in SCRIPT
    assert "handlePartialImage" in SCRIPT
    assert "partial_urls" in SCRIPT
    assert "partial_images" in SCRIPT
    assert "partialPreviewUrls" in TEMPLATE
