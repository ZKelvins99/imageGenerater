from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from app.db import connection as db_conn

JobStatus = Literal[
    "queued",
    "preparing",
    "running",
    "streaming",
    "saving",
    "succeeded",
    "failed",
    "cancel_requested",
    "cancelled",
]

# Allowed transitions for conditional updates (prevents clobbering terminal states)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"preparing", "running", "cancelled", "failed"},
    "preparing": {"running", "failed", "cancelled", "cancel_requested"},
    "running": {
        "streaming",
        "saving",
        "succeeded",
        "failed",
        "cancel_requested",
        "cancelled",
    },
    "streaming": {"saving", "succeeded", "failed", "cancel_requested", "cancelled"},
    "saving": {"succeeded", "failed", "cancelled", "cancel_requested"},
    "cancel_requested": {"cancelled", "failed", "succeeded"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}


@dataclass
class JobRecord:
    id: str
    history_id: str | None
    status: str
    progress_kind: str
    progress: float
    request_snapshot: dict[str, Any]
    provider_id: str | None
    upstream_request_id: str | None
    attempt_count: int
    error_code: str | None
    error_message_public: str | None
    error_detail_internal: str | None
    message: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    parent_job_id: str | None

    @classmethod
    def from_row(cls, row: Any) -> JobRecord:
        snap = row["request_snapshot"]
        if isinstance(snap, str):
            snap = json.loads(snap)
        return cls(
            id=row["id"],
            history_id=row["history_id"],
            status=row["status"],
            progress_kind=row["progress_kind"],
            progress=float(row["progress"] or 0),
            request_snapshot=snap or {},
            provider_id=row["provider_id"],
            upstream_request_id=row["upstream_request_id"],
            attempt_count=int(row["attempt_count"] or 0),
            error_code=row["error_code"],
            error_message_public=row["error_message_public"],
            error_detail_internal=row["error_detail_internal"],
            message=row["message"] or "",
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            parent_job_id=row["parent_job_id"],
        )


async def insert_job(job: JobRecord) -> JobRecord:
    conn = await db_conn.connect()
    await conn.execute(
        """
        INSERT INTO jobs (
          id, history_id, status, progress_kind, progress, request_snapshot,
          provider_id, upstream_request_id, attempt_count, error_code,
          error_message_public, error_detail_internal, message,
          created_at, started_at, finished_at, parent_job_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.id,
            job.history_id,
            job.status,
            job.progress_kind,
            job.progress,
            json.dumps(job.request_snapshot, ensure_ascii=False),
            job.provider_id,
            job.upstream_request_id,
            job.attempt_count,
            job.error_code,
            job.error_message_public,
            job.error_detail_internal,
            job.message,
            job.created_at,
            job.started_at,
            job.finished_at,
            job.parent_job_id,
        ),
    )
    await conn.commit()
    return job


async def get_job(job_id: str) -> JobRecord | None:
    conn = await db_conn.connect()
    cur = await conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = await cur.fetchone()
    return JobRecord.from_row(row) if row else None


async def get_job_by_history_id(history_id: str) -> JobRecord | None:
    conn = await db_conn.connect()
    cur = await conn.execute("SELECT * FROM jobs WHERE history_id = ?", (history_id,))
    row = await cur.fetchone()
    return JobRecord.from_row(row) if row else None


async def list_jobs(
    *,
    limit: int = 200,
    statuses: list[str] | None = None,
) -> list[JobRecord]:
    conn = await db_conn.connect()
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        cur = await conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*statuses, limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT * FROM jobs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = await cur.fetchall()
    return [JobRecord.from_row(r) for r in rows]


async def update_job_status(
    job_id: str,
    *,
    new_status: str,
    expected_statuses: set[str] | None = None,
    progress: float | None = None,
    progress_kind: str | None = None,
    message: str | None = None,
    error_code: str | None = None,
    error_message_public: str | None = None,
    error_detail_internal: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    upstream_request_id: str | None = None,
    attempt_count: int | None = None,
    request_snapshot: dict[str, Any] | None = None,
) -> bool:
    """Conditional status update. Returns False if no row matched (stale write)."""
    conn = await db_conn.connect()
    current = await get_job(job_id)
    if current is None:
        return False

    # Always enforce the state machine — never allow e.g. succeeded -> running
    if new_status != current.status and new_status not in ALLOWED_TRANSITIONS.get(
        current.status, set()
    ):
        return False

    allowed_from = (
        expected_statuses if expected_statuses is not None else {current.status}
    )

    sets = ["status = ?"]
    params: list[Any] = [new_status]
    if progress is not None:
        sets.append("progress = ?")
        params.append(progress)
    if progress_kind is not None:
        sets.append("progress_kind = ?")
        params.append(progress_kind)
    if message is not None:
        sets.append("message = ?")
        params.append(message)
    if error_code is not None:
        sets.append("error_code = ?")
        params.append(error_code)
    if error_message_public is not None:
        sets.append("error_message_public = ?")
        params.append(error_message_public)
    if error_detail_internal is not None:
        sets.append("error_detail_internal = ?")
        params.append(error_detail_internal)
    if started_at is not None:
        sets.append("started_at = ?")
        params.append(started_at)
    if finished_at is not None:
        sets.append("finished_at = ?")
        params.append(finished_at)
    if upstream_request_id is not None:
        sets.append("upstream_request_id = ?")
        params.append(upstream_request_id)
    if attempt_count is not None:
        sets.append("attempt_count = ?")
        params.append(attempt_count)
    if request_snapshot is not None:
        sets.append("request_snapshot = ?")
        params.append(json.dumps(request_snapshot, ensure_ascii=False))

    placeholders = ",".join("?" for _ in allowed_from)
    params.extend([job_id, *allowed_from])
    cur = await conn.execute(
        f"""
        UPDATE jobs
        SET {", ".join(sets)}
        WHERE id = ? AND status IN ({placeholders})
        """,
        params,
    )
    await conn.commit()
    return cur.rowcount > 0


async def link_job_asset(
    job_id: str, asset_id: str, role: str, position: int = 0
) -> None:
    conn = await db_conn.connect()
    await conn.execute(
        """
        INSERT OR REPLACE INTO job_assets(job_id, asset_id, role, position)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, asset_id, role, position),
    )
    await conn.commit()


async def list_job_assets(job_id: str, role: str | None = None) -> list[dict[str, Any]]:
    conn = await db_conn.connect()
    if role:
        cur = await conn.execute(
            """
            SELECT a.*, ja.role, ja.position
            FROM job_assets ja
            JOIN assets a ON a.id = ja.asset_id
            WHERE ja.job_id = ? AND ja.role = ?
            ORDER BY ja.position ASC
            """,
            (job_id, role),
        )
    else:
        cur = await conn.execute(
            """
            SELECT a.*, ja.role, ja.position
            FROM job_assets ja
            JOIN assets a ON a.id = ja.asset_id
            WHERE ja.job_id = ?
            ORDER BY ja.role, ja.position ASC
            """,
            (job_id,),
        )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]
