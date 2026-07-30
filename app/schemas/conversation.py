from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationCreateBody(BaseModel):
    provider_id: str | None = None
    responses_model: str | None = None
    prompt: str = Field(min_length=1)
    source_job_id: str | None = None
    action: Literal["auto", "generate", "edit"] = "edit"


class ConversationTurnCreateBody(BaseModel):
    prompt: str = Field(min_length=1)
    action: Literal["auto", "generate", "edit"] = "edit"


class ConversationTurnPublic(BaseModel):
    id: str
    conversation_id: str
    job_id: str
    parent_turn_id: str | None = None
    response_id: str
    previous_response_id: str | None = None
    prompt: str
    revised_prompt: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    image_call_id: str | None = None
    output_urls: list[str] = Field(default_factory=list)
    created_at: str


class ConversationPublic(BaseModel):
    id: str
    provider_id: str
    responses_model: str
    title: str
    root_job_id: str | None = None
    latest_response_id: str | None = None
    latest_turn_id: str | None = None
    created_at: str
    updated_at: str
    turns: list[ConversationTurnPublic] = Field(default_factory=list)
