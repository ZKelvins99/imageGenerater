from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.db import connection as db_conn
from app.db import migrate as db_migrate
from app.repositories import jobs as job_repo
from app.repositories.jobs import JobRecord
from app.schemas.generation import GeneratedImage
from app.schemas.provider import ProviderUpdate
from app.services import (
    asset_service,
    conversation_service,
    provider_service,
    responses_adapter,
)
from app.services.responses_adapter import ResponsesImageResult
from tests.helpers import PNG_1X1

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_responses_adapter_initial_and_previous_response_contract(
    isolated_env: Path,
) -> None:
    encoded = base64.b64encode(PNG_1X1).decode("ascii")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        index = len(bodies)
        return httpx.Response(
            200,
            headers={"x-request-id": f"req-{index}"},
            json={
                "id": f"resp-{index}",
                "output": [
                    {
                        "id": f"ig-{index}",
                        "type": "image_generation_call",
                        "result": encoded,
                        "revised_prompt": f"revised-{index}",
                    }
                ],
                "usage": {"input_tokens": index * 10, "output_tokens": index * 3},
            },
        )

    profile = provider_service.get_active_provider().model_copy(
        update={"responses_enabled": True, "responses_model": "gpt-5.6"}
    )
    transport = httpx.MockTransport(handler)
    first = await responses_adapter.create_image_turn(
        profile=profile,
        responses_model="gpt-5.6",
        prompt="make it blue",
        source_image=(PNG_1X1, "image/png"),
        action="edit",
        transport=transport,
    )
    second = await responses_adapter.create_image_turn(
        profile=profile,
        responses_model="gpt-5.6",
        prompt="now make it realistic",
        previous_response_id=first.response_id,
        action="edit",
        transport=transport,
    )

    assert bodies[0]["store"] is True
    assert bodies[0]["tools"] == [{"type": "image_generation", "action": "edit"}]
    content = bodies[0]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "make it blue"}
    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert "previous_response_id" not in bodies[0]
    assert bodies[1]["previous_response_id"] == "resp-1"
    assert bodies[1]["input"] == "now make it realistic"
    assert second.response_id == "resp-2"
    assert second.revised_prompt == "revised-2"
    assert second.usage["input_tokens"] == 20


@pytest.mark.asyncio
async def test_conversation_persists_and_continues_after_reconnect(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await db_migrate.migrate()
    provider_service.update_provider(
        "default",
        ProviderUpdate(responses_enabled=True, responses_model="gpt-5.6"),
    )
    now = datetime.now(UTC).isoformat()
    source_job = JobRecord(
        id="source-job",
        history_id="source-history",
        status="succeeded",
        progress_kind="stage",
        progress=1.0,
        request_snapshot={"prompt": "source", "output_paths": []},
        provider_id="default",
        upstream_request_id=None,
        attempt_count=1,
        error_code=None,
        error_message_public=None,
        error_detail_internal=None,
        message="done",
        created_at=now,
        started_at=now,
        finished_at=now,
        parent_job_id=None,
    )
    await job_repo.insert_job(source_job)
    source_asset = await asset_service.save_bytes_as_asset(
        PNG_1X1,
        category="output",
        original_filename="source.png",
        parent_job_id=source_job.id,
    )
    await job_repo.link_job_asset(source_job.id, source_asset.id, "output", 0)

    calls: list[str | None] = []

    async def fake_create_image_turn(**kwargs) -> ResponsesImageResult:
        previous = kwargs.get("previous_response_id")
        calls.append(previous)
        number = len(calls)
        return ResponsesImageResult(
            response_id=f"resp-{number}",
            image=GeneratedImage(
                data=PNG_1X1,
                mime="image/png",
                extension=".png",
                width=1,
                height=1,
                byte_size=len(PNG_1X1),
            ),
            revised_prompt=f"revised-{number}",
            image_call_id=f"ig-{number}",
            usage={"input_tokens": number * 10, "output_tokens": number},
            upstream_request_id=f"request-{number}",
        )

    monkeypatch.setattr(
        responses_adapter,
        "create_image_turn",
        fake_create_image_turn,
    )
    first = await conversation_service.create_turn(
        prompt="first edit",
        provider_id="default",
        source_job_id=source_job.id,
    )
    assert len(first.turns) == 1

    await db_conn.close()
    db_conn.reset_connection_for_tests()
    await db_migrate.migrate()

    second = await conversation_service.create_turn(
        prompt="second edit",
        conversation_id=first.id,
    )
    assert calls == [None, "resp-1"]
    assert second.latest_response_id == "resp-2"
    assert [turn.prompt for turn in second.turns] == ["first edit", "second edit"]
    assert all(turn.output_urls for turn in second.turns)
    latest_job = await job_repo.get_job(second.turns[-1].job_id)
    assert latest_job is not None
    assert latest_job.request_snapshot["generation_api"] == "responses"
    assert latest_job.request_snapshot["conversation_id"] == first.id


def test_phase6_frontend_is_capability_gated() -> None:
    assert "Responses API 多轮编辑" in TEMPLATE
    assert "对话式编辑" in TEMPLATE
    assert "独立计费链路" in TEMPLATE
    assert "responses_enabled" in SCRIPT
    assert "responses_model" in SCRIPT
    assert "conversationAvailable" in SCRIPT
    assert "submitConversationTurn" in SCRIPT
    assert "/api/v1/conversations" in SCRIPT
