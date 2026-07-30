"""Idempotent JSONL -> SQLite migration. Never deletes the original JSONL."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.repositories import jobs as job_repo
from app.repositories.jobs import JobRecord
from app.schemas.asset import MigrationReport
from app.services import asset_service, config_service


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _map_legacy_status(status: str) -> str:
    return {
        "pending": "queued",
        "running": "running",
        "done": "succeeded",
        "error": "failed",
    }.get(status, "succeeded")


async def migrate_history_jsonl(
    path: Path | None = None,
) -> MigrationReport:
    report = MigrationReport()
    jsonl = path or config_service.HISTORY_PATH
    if not jsonl.exists():
        return report

    for line_no, raw in enumerate(
        jsonl.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            history_id = str(data.get("id") or "")
            if not history_id:
                report.failed += 1
                report.errors.append(f"line {line_no}: missing id")
                continue
            existing = await job_repo.get_job_by_history_id(history_id)
            if existing is not None:
                report.skipped += 1
                continue

            job_id = uuid.uuid4().hex[:12]
            legacy_status = str(data.get("status") or "done")
            job_status = _map_legacy_status(legacy_status)
            snapshot = {
                "mode": data.get("mode") or "text",
                "prompt": data.get("prompt") or "",
                "model": data.get("model") or "",
                "size": data.get("size") or "",
                "quality": data.get("quality") or "",
                "n": data.get("n") or 1,
                "reference_path": data.get("reference_path"),
                "output_paths": data.get("output_paths") or [],
                "migrated_from": "history.jsonl",
            }
            created_at = str(data.get("created_at") or _now())
            job = JobRecord(
                id=job_id,
                history_id=history_id,
                status=job_status,
                progress_kind="stage",
                progress=1.0
                if job_status in ("succeeded", "failed", "cancelled")
                else 0.0,
                request_snapshot=snapshot,
                provider_id=None,
                upstream_request_id=None,
                attempt_count=1,
                error_code="LEGACY_ERROR" if job_status == "failed" else None,
                error_message_public=data.get("error"),
                error_detail_internal=None,
                message="migrated from history.jsonl",
                created_at=created_at,
                started_at=created_at,
                finished_at=created_at
                if job_status in ("succeeded", "failed")
                else None,
                parent_job_id=None,
            )
            await job_repo.insert_job(job)

            ref = data.get("reference_path")
            if ref:
                try:
                    asset = await asset_service.register_existing_path(
                        str(ref),
                        category="input",
                        parent_job_id=job_id,
                    )
                    await job_repo.link_job_asset(
                        job_id, asset.id, role="reference", position=0
                    )
                except Exception as e:
                    report.errors.append(f"line {line_no}: reference asset {ref}: {e}")

            for i, out in enumerate(data.get("output_paths") or []):
                try:
                    asset = await asset_service.register_existing_path(
                        str(out),
                        category="output",
                        parent_job_id=job_id,
                    )
                    await job_repo.link_job_asset(
                        job_id, asset.id, role="output", position=i
                    )
                except Exception as e:
                    report.errors.append(f"line {line_no}: output asset {out}: {e}")

            report.success += 1
        except Exception as e:
            report.failed += 1
            report.errors.append(f"line {line_no}: {e}")
    return report
