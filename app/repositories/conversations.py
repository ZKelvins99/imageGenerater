from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.db import connection as db_conn


@dataclass
class ConversationRecord:
    id: str
    provider_id: str
    responses_model: str
    title: str
    root_job_id: str | None
    latest_response_id: str | None
    latest_turn_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> ConversationRecord:
        return cls(**dict(row))


@dataclass
class ConversationTurnRecord:
    id: str
    conversation_id: str
    job_id: str
    parent_turn_id: str | None
    response_id: str
    previous_response_id: str | None
    prompt: str
    revised_prompt: str | None
    usage: dict[str, Any]
    image_call_id: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> ConversationTurnRecord:
        data = dict(row)
        data["usage"] = json.loads(data.pop("usage_json") or "{}")
        return cls(**data)


async def get_conversation(conversation_id: str) -> ConversationRecord | None:
    conn = await db_conn.connect()
    cur = await conn.execute(
        "SELECT * FROM response_conversations WHERE id = ?",
        (conversation_id,),
    )
    row = await cur.fetchone()
    return ConversationRecord.from_row(row) if row else None


async def list_conversations(limit: int = 100) -> list[ConversationRecord]:
    conn = await db_conn.connect()
    cur = await conn.execute(
        """
        SELECT * FROM response_conversations
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [ConversationRecord.from_row(row) for row in await cur.fetchall()]


async def list_turns(conversation_id: str) -> list[ConversationTurnRecord]:
    conn = await db_conn.connect()
    cur = await conn.execute(
        """
        SELECT * FROM response_turns
        WHERE conversation_id = ?
        ORDER BY created_at ASC
        """,
        (conversation_id,),
    )
    return [ConversationTurnRecord.from_row(row) for row in await cur.fetchall()]


async def get_turn(turn_id: str) -> ConversationTurnRecord | None:
    conn = await db_conn.connect()
    cur = await conn.execute(
        "SELECT * FROM response_turns WHERE id = ?",
        (turn_id,),
    )
    row = await cur.fetchone()
    return ConversationTurnRecord.from_row(row) if row else None


async def save_conversation_turn(
    conversation: ConversationRecord,
    turn: ConversationTurnRecord,
) -> None:
    conn = await db_conn.connect()
    async with db_conn.transaction():
        await conn.execute(
            """
            INSERT INTO response_conversations(
              id, provider_id, responses_model, title, root_job_id,
              latest_response_id, latest_turn_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              latest_response_id = excluded.latest_response_id,
              latest_turn_id = excluded.latest_turn_id,
              updated_at = excluded.updated_at,
              title = excluded.title
            """,
            (
                conversation.id,
                conversation.provider_id,
                conversation.responses_model,
                conversation.title,
                conversation.root_job_id,
                conversation.latest_response_id,
                conversation.latest_turn_id,
                conversation.created_at,
                conversation.updated_at,
            ),
        )
        await conn.execute(
            """
            INSERT INTO response_turns(
              id, conversation_id, job_id, parent_turn_id, response_id,
              previous_response_id, prompt, revised_prompt, usage_json,
              image_call_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                turn.id,
                turn.conversation_id,
                turn.job_id,
                turn.parent_turn_id,
                turn.response_id,
                turn.previous_response_id,
                turn.prompt,
                turn.revised_prompt,
                json.dumps(turn.usage, ensure_ascii=False),
                turn.image_call_id,
                turn.created_at,
            ),
        )
