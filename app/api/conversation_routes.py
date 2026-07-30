from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import Response

from app.schemas.conversation import (
    ConversationCreateBody,
    ConversationTurnCreateBody,
)
from app.services import conversation_service
from app.services.errors import AppError

router = APIRouter(prefix="/api/v1", tags=["response-conversations"])


def _error_response(error: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code or 400,
        content=error.to_public_dict(),
    )


@router.get("/conversations")
async def list_conversations(limit: int = 100) -> dict:
    items = await conversation_service.list_conversations(limit=min(limit, 200))
    return {"items": [item.model_dump() for item in items]}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict:
    item = await conversation_service.get_conversation(conversation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return item.model_dump()


@router.post("/conversations", response_model=None)
async def create_conversation(body: ConversationCreateBody) -> Response | dict:
    try:
        item = await conversation_service.create_turn(
            prompt=body.prompt,
            provider_id=body.provider_id,
            responses_model=body.responses_model,
            source_job_id=body.source_job_id,
            action=body.action,
        )
    except AppError as error:
        return _error_response(error)
    return item.model_dump()


@router.post("/conversations/{conversation_id}/turns", response_model=None)
async def create_turn(
    conversation_id: str,
    body: ConversationTurnCreateBody,
) -> Response | dict:
    try:
        item = await conversation_service.create_turn(
            prompt=body.prompt,
            conversation_id=conversation_id,
            action=body.action,
        )
    except AppError as error:
        return _error_response(error)
    return item.model_dump()
