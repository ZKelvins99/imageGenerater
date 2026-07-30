from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from app.schemas.generation import GenerationRequest
from app.services import asset_service, capability_resolver, images_adapter
from app.services.capability_resolver import CapabilityError
from app.services.errors import AppError, classify_http_error
from httpx import AsyncClient
from tests.helpers import PNG_1X1


def _b64(data: bytes = PNG_1X1) -> str:
    return base64.b64encode(data).decode("ascii")


def _jpeg_1x1() -> bytes:
    # Minimal valid JPEG
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def _webp_1x1() -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), (0, 255, 0)).save(buf, format="WEBP")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_flexible_size_boundaries() -> None:
    caps = capability_resolver.resolve_capabilities("gpt-image-2")
    assert caps.flexible_size_constraints is not None

    # Valid: 1024x1024
    req = GenerationRequest(
        mode="generate", prompt="x", model="gpt-image-2", size="1024x1024"
    )
    capability_resolver.validate_request(req, caps)

    # Invalid: not multiple of 16
    with pytest.raises(CapabilityError):
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate", prompt="x", model="gpt-image-2", size="1000x1000"
            ),
            caps,
        )

    # Invalid: aspect > 3:1
    with pytest.raises(CapabilityError):
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate", prompt="x", model="gpt-image-2", size="3840x1024"
            ),
            caps,
        )

    # Invalid: too few pixels (16x16)
    with pytest.raises(CapabilityError):
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate", prompt="x", model="gpt-image-2", size="16x16"
            ),
            caps,
        )

    # Invalid: edge > 3840
    with pytest.raises(CapabilityError):
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate",
                prompt="x",
                model="gpt-image-2",
                size="3856x3856",
            ),
            caps,
        )

    # Invalid: total pixels above max (3840x2176 = 8,355,840 > 8,294,400)
    with pytest.raises(CapabilityError):
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate",
                prompt="x",
                model="gpt-image-2",
                size="3840x2176",
            ),
            caps,
        )


@pytest.mark.asyncio
async def test_gpt_image_2_rejects_transparent_and_fidelity() -> None:
    caps = capability_resolver.resolve_capabilities("gpt-image-2")
    with pytest.raises(CapabilityError) as ei:
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate",
                prompt="x",
                model="gpt-image-2",
                background="transparent",
            ),
            caps,
        )
    assert ei.value.code == "CAPABILITY_UNSUPPORTED"

    with pytest.raises(CapabilityError):
        capability_resolver.validate_request(
            GenerationRequest(
                mode="generate",
                prompt="x",
                model="gpt-image-2",
                input_fidelity="high",
            ),
            caps,
        )


@pytest.mark.asyncio
async def test_generations_contract_mock(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/v1/images/generations")
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "gpt-image-2"
        assert body["prompt"] == "a cube"
        assert body["size"] == "1024x1024"
        assert body["quality"] == "medium"
        assert body["n"] == 2
        assert body["output_format"] == "jpeg"
        assert body["moderation"] == "auto"
        assert body["background"] == "opaque"
        assert body["output_compression"] == 50
        assert "input_fidelity" not in body
        return httpx.Response(
            200,
            headers={"x-request-id": "req_gen_1"},
            json={"data": [{"b64_json": base64.b64encode(_jpeg_1x1()).decode()}]},
        )

    transport = httpx.MockTransport(handler)
    req = GenerationRequest(
        mode="generate",
        prompt="a cube",
        model="gpt-image-2",
        size="1024x1024",
        quality="medium",
        n=2,
        output_format="jpeg",
        background="opaque",
        output_compression=50,
    )
    result = await images_adapter.generate(req, transport=transport)
    assert len(result.images) == 1
    assert result.upstream_request_id == "req_gen_1"
    assert result.images[0].extension == ".jpg"
    assert result.sent_params["output_format"] == "jpeg"
    assert result.sent_params["n"] == 2
    assert result.sent_params["background"] == "opaque"
    assert result.sent_params["output_compression"] == 50


@pytest.mark.asyncio
async def test_edits_multi_image_and_mask_multipart(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()
    assets = []
    for i in range(4):
        a = await asset_service.save_bytes_as_asset(
            PNG_1X1, category="input", original_filename=f"r{i}.png"
        )
        assets.append(a)
    mask = await asset_service.save_bytes_as_asset(
        # 1x1 RGBA PNG
        __import__("base64").b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        ),
        category="mask",
        original_filename="mask.png",
    )
    # Force has_alpha true for mask validation path — PNG_1X1 may be palette
    # Re-save a true RGBA
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(buf, format="PNG")
    mask = await asset_service.save_bytes_as_asset(
        buf.getvalue(), category="mask", original_filename="mask2.png"
    )
    assert mask.has_alpha

    seen = {"images": 0, "mask": False}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/images/edits")
        body = request.content
        # multipart should contain multiple image parts + mask
        text = body.decode("latin-1", errors="ignore")
        seen["images"] = text.count('name="image"')
        seen["mask"] = 'name="mask"' in text
        assert "output_format" in text or b"output_format" in body
        return httpx.Response(
            200,
            headers={"x-request-id": "req_edit_1"},
            json={"data": [{"b64_json": _b64()}]},
        )

    transport = httpx.MockTransport(handler)
    req = GenerationRequest(
        mode="edit_mask",
        prompt="fix region",
        model="gpt-image-2",
        size="1024x1024",
        quality="high",
        input_asset_ids=[a.id for a in assets],
        primary_asset_id=assets[0].id,
        mask_asset_id=mask.id,
        output_format="png",
    )
    result = await images_adapter.generate(req, transport=transport)
    assert result.upstream_request_id == "req_edit_1"
    assert seen["images"] == 4
    assert seen["mask"] is True


@pytest.mark.asyncio
async def test_output_formats_jpeg_webp(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()

    jpeg = _jpeg_1x1()
    webp = _webp_1x1()

    def handler_jpeg(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(jpeg).decode()}]}
        )

    def handler_webp(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(webp).decode()}]}
        )

    for fmt, handler, ext in (
        ("jpeg", handler_jpeg, ".jpg"),
        ("webp", handler_webp, ".webp"),
    ):
        req = GenerationRequest(
            mode="generate",
            prompt="x",
            model="gpt-image-2",
            size="1024x1024",
            output_format=fmt,  # type: ignore[arg-type]
            output_compression=80,
        )
        result = await images_adapter.generate(
            req, transport=httpx.MockTransport(handler)
        )
        assert result.images[0].extension == ext


@pytest.mark.asyncio
async def test_429_retries_then_succeeds(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(200, json={"data": [{"b64_json": _b64()}]})

    req = GenerationRequest(
        mode="generate", prompt="x", model="gpt-image-2", size="1024x1024"
    )
    result = await images_adapter.generate(
        req, transport=httpx.MockTransport(handler), max_attempts=3
    )
    assert len(result.images) == 1
    assert state["n"] == 3
    assert result.attempt_count == 3


@pytest.mark.asyncio
async def test_moderation_not_retried(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(
            400,
            json={
                "error": {"message": "moderation blocked", "type": "invalid_request"}
            },
        )

    req = GenerationRequest(
        mode="generate", prompt="bad", model="gpt-image-2", size="1024x1024"
    )
    with pytest.raises(AppError) as ei:
        await images_adapter.generate(
            req, transport=httpx.MockTransport(handler), max_attempts=3
        )
    assert ei.value.code == "MODERATION_BLOCKED"
    assert ei.value.retryable is False
    assert state["n"] == 1


def test_classify_5xx_retryable() -> None:
    resp = httpx.Response(503, json={"error": {"message": "unavailable"}})
    err = classify_http_error(resp)
    assert err.retryable is True
    assert err.code == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 2:
            return httpx.Response(
                503,
                headers={"retry-after": "0"},
                json={"error": {"message": "unavailable"}},
            )
        return httpx.Response(200, json={"data": [{"b64_json": _b64()}]})

    req = GenerationRequest(
        mode="generate", prompt="x", model="gpt-image-2", size="1024x1024"
    )
    result = await images_adapter.generate(
        req, transport=httpx.MockTransport(handler), max_attempts=3
    )
    assert len(result.images) == 1
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_mask_size_mismatch_rejected(isolated_env: Path) -> None:
    await __import__("app.db.migrate", fromlist=["migrate"]).migrate()
    from io import BytesIO

    from PIL import Image

    primary_buf = BytesIO()
    Image.new("RGB", (32, 32), (1, 2, 3)).save(primary_buf, format="PNG")
    primary = await asset_service.save_bytes_as_asset(
        primary_buf.getvalue(), category="input", original_filename="p.png"
    )
    mask_buf = BytesIO()
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(mask_buf, format="PNG")
    mask = await asset_service.save_bytes_as_asset(
        mask_buf.getvalue(), category="mask", original_filename="m.png"
    )
    assert primary.width != mask.width

    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"data": [{"b64_json": _b64()}]})

    req = GenerationRequest(
        mode="edit_mask",
        prompt="fix",
        model="gpt-image-2",
        size="1024x1024",
        input_asset_ids=[primary.id],
        primary_asset_id=primary.id,
        mask_asset_id=mask.id,
    )
    with pytest.raises(AppError) as ei:
        await images_adapter.generate(req, transport=httpx.MockTransport(handler))
    assert ei.value.code == "MASK_INVALID"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_jpeg_saved_and_downloaded(
    client: AsyncClient, isolated_env: Path
) -> None:
    jpeg = _jpeg_1x1()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(jpeg).decode()}]},
        )

    req = GenerationRequest(
        mode="generate",
        prompt="jpeg out",
        model="gpt-image-2",
        size="1024x1024",
        output_format="jpeg",
        output_compression=80,
    )
    result = await images_adapter.generate(req, transport=httpx.MockTransport(handler))
    assert result.images[0].mime == "image/jpeg"
    asset = await asset_service.save_bytes_as_asset(
        result.images[0].data,
        category="output",
        original_filename="out.jpg",
    )
    assert asset.mime == "image/jpeg"
    assert asset.extension == ".jpg"
    assert asset.byte_size == len(jpeg)
    content = await client.get(f"/api/v1/assets/{asset.id}/content")
    assert content.status_code == 200
    assert "image/jpeg" in content.headers.get("content-type", "")
    media = await client.get(f"/media/{asset.storage_path}")
    assert media.status_code == 200
    assert "image/jpeg" in media.headers.get("content-type", "")
