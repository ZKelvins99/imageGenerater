from __future__ import annotations

import json
from pathlib import Path

from app.repositories import jobs as job_repo
from app.repositories.jobs import JobRecord
from app.schemas.models import HistoryItem
from app.services.config_service import DATA_DIR, HISTORY_PATH, ensure_dirs

LEGACY_FROM_JOB = {
    "queued": "pending",
    "preparing": "pending",
    "running": "running",
    "streaming": "running",
    "saving": "running",
    "succeeded": "done",
    "failed": "error",
    "cancel_requested": "error",
    "cancelled": "error",
}


def output_url(relative: str) -> str:
    rel = relative.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    return f"/media/{rel}"


def _job_to_history(job: JobRecord) -> HistoryItem:
    snap = job.request_snapshot or {}
    status = LEGACY_FROM_JOB.get(job.status, "error")
    raw_mode = str(snap.get("legacy_mode") or snap.get("mode") or "text")
    if raw_mode in ("generate",):
        mode = "text"
    elif raw_mode in ("reference", "edit_mask", "image"):
        mode = "image"
    else:
        mode = "text"
    return HistoryItem(
        id=job.history_id or job.id,
        created_at=job.created_at,
        mode=mode,  # type: ignore[arg-type]
        model=str(snap.get("model") or ""),
        prompt=str(snap.get("prompt") or ""),
        size=str(snap.get("size") or ""),
        quality=str(snap.get("quality") or ""),
        n=int(snap.get("n") or 1),
        reference_path=snap.get("reference_path"),
        output_paths=list(snap.get("output_paths") or []),
        status=status,  # type: ignore[arg-type]
        error=job.error_message_public,
        extra={"job_id": job.id, "job_status": job.status},
    )


async def list_history_async(limit: int = 200) -> list[HistoryItem]:
    jobs = await job_repo.list_jobs(limit=limit)
    return [_job_to_history(j) for j in jobs]


async def get_history_async(item_id: str) -> HistoryItem | None:
    job = await job_repo.get_job_by_history_id(item_id)
    if job is None:
        # Also allow lookup by job id
        job = await job_repo.get_job(item_id)
    if job is None:
        return None
    return _job_to_history(job)


async def delete_history_async(item_id: str, delete_files: bool = True) -> bool:
    job = await job_repo.get_job_by_history_id(item_id)
    if job is None:
        job = await job_repo.get_job(item_id)
    if job is None:
        return False
    # Soft-delete via failed+cancelled marker is not ideal; hard-delete row for now
    from app.db import connection as db_conn

    conn = await db_conn.connect()
    if delete_files:
        assets = await job_repo.list_job_assets(job.id)
        for a in assets:
            path = _safe_unlink(a["storage_path"])
            if path:
                path.unlink(missing_ok=True)
        for rel in job.request_snapshot.get("output_paths") or []:
            path = _safe_unlink(str(rel))
            if path:
                path.unlink(missing_ok=True)
    await conn.execute("DELETE FROM job_assets WHERE job_id = ?", (job.id,))
    await conn.execute("DELETE FROM job_tags WHERE job_id = ?", (job.id,))
    await conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
    await conn.commit()
    return True


def _safe_unlink(relative: str) -> Path | None:
    rel = relative.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    target = (DATA_DIR / rel).resolve()
    if not str(target).startswith(str(DATA_DIR.resolve())):
        return None
    return target


# ---- Sync wrappers used by older unit tests (JSONL fallback if DB empty path) ----


def list_history(limit: int = 200) -> list[HistoryItem]:
    """Sync helper: prefer reading JSONL for unit tests that don't init DB.

    Production routes use list_history_async.
    """
    ensure_dirs()
    items: list[HistoryItem] = []
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(HistoryItem.model_validate(json.loads(line)))
                except Exception:
                    continue
    items.reverse()
    return items[:limit]


def get_history(item_id: str) -> HistoryItem | None:
    for item in list_history(limit=10_000):
        if item.id == item_id:
            return item
    return None


def append_history(item: HistoryItem) -> HistoryItem:
    ensure_dirs()
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(item.model_dump_json() + "\n")
    return item


def upsert_history(item: HistoryItem) -> HistoryItem:
    ensure_dirs()
    items = list_history(limit=10_000)
    items.reverse()  # chronological
    found = False
    for i, existing in enumerate(items):
        if existing.id == item.id:
            items[i] = item
            found = True
            break
    if not found:
        items.append(item)
    with HISTORY_PATH.open("w", encoding="utf-8") as f:
        for row in items:
            f.write(row.model_dump_json() + "\n")
    return item


def delete_history(item_id: str, delete_files: bool = True) -> bool:
    ensure_dirs()
    items = list_history(limit=10_000)
    items.reverse()
    kept: list[HistoryItem] = []
    removed: HistoryItem | None = None
    for item in items:
        if item.id == item_id:
            removed = item
        else:
            kept.append(item)
    if removed is None:
        return False
    with HISTORY_PATH.open("w", encoding="utf-8") as f:
        for row in kept:
            f.write(row.model_dump_json() + "\n")
    if delete_files:
        for rel in removed.output_paths:
            path = _safe_unlink(rel)
            if path:
                path.unlink(missing_ok=True)
        if removed.reference_path and removed.reference_path.startswith("uploads/"):
            path = _safe_unlink(removed.reference_path)
            if path:
                path.unlink(missing_ok=True)
    return True
