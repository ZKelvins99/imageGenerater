from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from app.repositories import assets as asset_repo
from app.repositories import conversations as conversation_repo
from app.repositories import jobs as job_repo
from app.repositories.conversations import (
    ConversationRecord,
    ConversationTurnRecord,
)
from app.repositories.jobs import JobRecord
from app.schemas.conversation import ConversationPublic, ConversationTurnPublic
from app.services import asset_service, responses_adapter
from app.services.config_service import resolve_data_path
from app.services.errors import AppError
from app.services.provider_service import (
    ProviderError,
    get_active_provider,
    get_provider,
)


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


async def create_turn(
    *,
    prompt: str,
    conversation_id: str | None = None,
    provider_id: str | None = None,
    responses_model: str | None = None,
    source_job_id: str | None = None,
    action: Literal["auto", "generate", "edit"] = "edit",
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConversationPublic:
    prompt = prompt.strip()
    if not prompt:
        raise AppError("请输入本轮修改要求", code="INPUT_INVALID", status_code=400)

    existing = None
    parent_turn_id = None
    previous_response_id = None
    if conversation_id:
        existing = await conversation_repo.get_conversation(conversation_id)
        if existing is None:
            raise AppError("多轮会话不存在", code="MODEL_NOT_FOUND", status_code=404)
        provider_id = existing.provider_id
        responses_model = existing.responses_model
        source_job_id = existing.root_job_id
        parent_turn_id = existing.latest_turn_id
        previous_response_id = existing.latest_response_id

    try:
        profile = get_provider(provider_id) if provider_id else get_active_provider()
    except ProviderError as exc:
        raise AppError(
            exc.message,
            code=exc.code,
            status_code=404 if exc.code == "MODEL_NOT_FOUND" else 400,
        ) from exc
    if not profile.responses_enabled:
        raise AppError(
            "当前 Provider 未启用 Responses API",
            code="CAPABILITY_UNSUPPORTED",
            status_code=400,
        )
    model = (responses_model or profile.responses_model).strip()
    if not model:
        raise AppError(
            "请先为 Provider 配置 Responses 主模型",
            code="CONFIG_INVALID",
            status_code=400,
        )

    source_image: tuple[bytes, str] | None = None
    if not previous_response_id and source_job_id:
        source_assets = await job_repo.list_job_assets(source_job_id, role="output")
        if not source_assets:
            raise AppError(
                "来源任务没有可用输出图片",
                code="INPUT_INVALID",
                status_code=400,
            )
        source = await asset_repo.get_asset(str(source_assets[0]["id"]))
        if source is None:
            raise AppError("来源图片不存在", code="INPUT_INVALID", status_code=400)
        source_image = (
            resolve_data_path(source.storage_path).read_bytes(),
            source.mime or "image/png",
        )

    result = await responses_adapter.create_image_turn(
        profile=profile,
        responses_model=model,
        prompt=prompt,
        previous_response_id=previous_response_id,
        source_image=source_image,
        action=action,
        transport=transport,
    )

    now = _now()
    conv_id = conversation_id or uuid.uuid4().hex[:16]
    turn_id = uuid.uuid4().hex[:16]
    job_id = uuid.uuid4().hex[:12]
    history_id = uuid.uuid4().hex[:12]
    snapshot: dict[str, Any] = {
        "generation_api": "responses",
        "mode": "conversation",
        "conversation_id": conv_id,
        "prompt": prompt,
        "model": model,
        "provider_id": profile.id,
        "source_job_id": source_job_id,
        "previous_response_id": previous_response_id,
        "response_id": result.response_id,
        "revised_prompt": result.revised_prompt,
        "usage": result.usage,
        "image_call_id": result.image_call_id,
        "output_paths": [],
    }
    job = JobRecord(
        id=job_id,
        history_id=history_id,
        status="saving",
        progress_kind="stage",
        progress=0.85,
        request_snapshot=snapshot,
        provider_id=profile.id,
        upstream_request_id=result.upstream_request_id,
        attempt_count=1,
        error_code=None,
        error_message_public=None,
        error_detail_internal=None,
        message="正在保存多轮编辑结果…",
        created_at=now,
        started_at=now,
        finished_at=None,
        parent_job_id=(
            (await _turn_job_id(parent_turn_id))
            if parent_turn_id
            else source_job_id
        ),
    )
    await job_repo.insert_job(job)
    asset = await asset_service.save_bytes_as_asset(
        result.image.data,
        category="output",
        original_filename=f"{history_id}{result.image.extension}",
        parent_job_id=job_id,
        claimed_mime=result.image.mime,
    )
    await job_repo.link_job_asset(job_id, asset.id, role="output", position=0)
    await job_repo.update_job_status(
        job_id,
        new_status="succeeded",
        expected_statuses={"saving"},
        progress=1.0,
        message="多轮编辑完成",
        finished_at=now,
        request_snapshot=snapshot,
        upstream_request_id=result.upstream_request_id,
        attempt_count=1,
        error_message_public="",
    )

    conversation = ConversationRecord(
        id=conv_id,
        provider_id=profile.id,
        responses_model=model,
        title=(existing.title if existing else prompt[:80]),
        root_job_id=(existing.root_job_id if existing else source_job_id),
        latest_response_id=result.response_id,
        latest_turn_id=turn_id,
        created_at=(existing.created_at if existing else now),
        updated_at=now,
    )
    turn = ConversationTurnRecord(
        id=turn_id,
        conversation_id=conv_id,
        job_id=job_id,
        parent_turn_id=parent_turn_id,
        response_id=result.response_id,
        previous_response_id=previous_response_id,
        prompt=prompt,
        revised_prompt=result.revised_prompt,
        usage=result.usage,
        image_call_id=result.image_call_id,
        created_at=now,
    )
    await conversation_repo.save_conversation_turn(conversation, turn)
    public = await get_conversation(conv_id)
    assert public is not None
    return public


async def _turn_job_id(turn_id: str) -> str | None:
    if not turn_id:
        return None
    turn = await conversation_repo.get_turn(turn_id)
    return turn.job_id if turn else None


async def get_conversation(conversation_id: str) -> ConversationPublic | None:
    record = await conversation_repo.get_conversation(conversation_id)
    if record is None:
        return None
    turns = await conversation_repo.list_turns(conversation_id)
    public_turns: list[ConversationTurnPublic] = []
    for turn in turns:
        assets = await job_repo.list_job_assets(turn.job_id, role="output")
        public_turns.append(
            ConversationTurnPublic(
                id=turn.id,
                conversation_id=turn.conversation_id,
                job_id=turn.job_id,
                parent_turn_id=turn.parent_turn_id,
                response_id=turn.response_id,
                previous_response_id=turn.previous_response_id,
                prompt=turn.prompt,
                revised_prompt=turn.revised_prompt,
                usage=turn.usage,
                image_call_id=turn.image_call_id,
                output_urls=[f"/media/{asset['storage_path']}" for asset in assets],
                created_at=turn.created_at,
            )
        )
    return ConversationPublic(
        id=record.id,
        provider_id=record.provider_id,
        responses_model=record.responses_model,
        title=record.title,
        root_job_id=record.root_job_id,
        latest_response_id=record.latest_response_id,
        latest_turn_id=record.latest_turn_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        turns=public_turns,
    )


async def list_conversations(limit: int = 100) -> list[ConversationPublic]:
    records = await conversation_repo.list_conversations(limit=limit)
    items: list[ConversationPublic] = []
    for record in records:
        item = await get_conversation(record.id)
        if item:
            items.append(item)
    return items
