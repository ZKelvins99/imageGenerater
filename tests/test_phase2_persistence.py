from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.db import connection as db_conn
from app.db import migrate as db_migrate
from app.repositories import jobs as job_repo
from app.repositories.jobs import JobRecord
from app.services import asset_service, jsonl_migration
from app.services.asset_service import AssetError
from httpx import AsyncClient
from PIL import Image
from tests.helpers import PNG_1X1


async def _init_db() -> None:
    await db_conn.close()
    db_conn.reset_connection_for_tests()
    await db_migrate.migrate()


@pytest.mark.asyncio
async def test_migrate_runs_and_is_versioned(isolated_env: Path) -> None:
    await _init_db()
    v1 = await db_migrate.migrate()
    v2 = await db_migrate.migrate()
    assert v1 == v2 == 1


@pytest.mark.asyncio
async def test_jsonl_migration_idempotent(isolated_env: Path) -> None:
    await _init_db()
    from app.services import config_service

    # Write a done history line pointing at a real output file
    day = "2026-07-30"
    out_dir = config_service.OUTPUTS_DIR / day
    out_dir.mkdir(parents=True)
    out_file = out_dir / "abc123.png"
    out_file.write_bytes(PNG_1X1)
    line = (
        '{"id":"abc123","created_at":"2026-07-30T00:00:00+08:00","mode":"text",'
        '"model":"gpt-image-2","prompt":"cat","size":"1024x1024","quality":"medium",'
        '"n":1,"reference_path":null,"output_paths":["outputs/2026-07-30/abc123.png"],'
        '"status":"done","error":null,"extra":{}}\n'
    )
    config_service.HISTORY_PATH.write_text(line, encoding="utf-8")

    r1 = await jsonl_migration.migrate_history_jsonl()
    r2 = await jsonl_migration.migrate_history_jsonl()
    assert r1.success == 1
    assert r2.skipped == 1
    assert r2.success == 0
    job = await job_repo.get_job_by_history_id("abc123")
    assert job is not None
    assert job.status == "succeeded"


@pytest.mark.asyncio
async def test_asset_rejects_fake_mime_and_svg(isolated_env: Path) -> None:
    await _init_db()
    with pytest.raises(AssetError):
        await asset_service.save_bytes_as_asset(
            b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            category="input",
            original_filename="x.svg",
            claimed_mime="image/png",
        )


@pytest.mark.asyncio
async def test_asset_rejects_pixel_bomb(isolated_env: Path) -> None:
    await _init_db()
    # Tiny compressed file that claims huge dimensions — Pillow will allocate.
    # Use a moderately large image over MAX_PIXELS by monkeypatching limit.
    import app.services.asset_service as asvc

    original = asvc.MAX_PIXELS
    asvc.MAX_PIXELS = 100  # 1x1 would be ok; make 20x20 fail
    try:
        img = Image.new("RGB", (20, 20), color=(1, 2, 3))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        with pytest.raises(AssetError) as ei:
            await asset_service.save_bytes_as_asset(
                buf.getvalue(), category="input", original_filename="big.png"
            )
        assert ei.value.code == "ASSET_TOO_LARGE"
    finally:
        asvc.MAX_PIXELS = original


@pytest.mark.asyncio
async def test_asset_upload_api(client: AsyncClient) -> None:
    res = await client.post(
        "/api/v1/assets",
        files={"files": ("a.png", PNG_1X1, "image/png")},
        data={"category": "input"},
    )
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["mime"] == "image/png"
    assert items[0]["width"] == 1
    assert items[0]["content_url"].startswith("/media/")
    assert items[0]["thumbnail_url"]
    assert items[0]["thumbnail_url"].startswith("/media/")


@pytest.mark.asyncio
async def test_job_status_conditional_update(isolated_env: Path) -> None:
    await _init_db()
    job = JobRecord(
        id="job1",
        history_id="h1",
        status="succeeded",
        progress_kind="stage",
        progress=1.0,
        request_snapshot={"mode": "text", "prompt": "x", "output_paths": []},
        provider_id="default",
        upstream_request_id=None,
        attempt_count=1,
        error_code=None,
        error_message_public=None,
        error_detail_internal=None,
        message="done",
        created_at="2026-07-30T00:00:00+08:00",
        started_at="2026-07-30T00:00:00+08:00",
        finished_at="2026-07-30T00:00:01+08:00",
        parent_job_id=None,
    )
    await job_repo.insert_job(job)
    # Attempt to clobber succeeded -> running must fail
    ok = await job_repo.update_job_status(
        "job1",
        new_status="running",
        expected_statuses={"succeeded"},
        progress=0.5,
    )
    assert ok is False
    # Also default transition check
    ok2 = await job_repo.update_job_status(
        "job1",
        new_status="running",
        progress=0.5,
    )
    assert ok2 is False
    refreshed = await job_repo.get_job("job1")
    assert refreshed is not None
    assert refreshed.status == "succeeded"


@pytest.mark.asyncio
async def test_recover_interrupted_jobs(isolated_env: Path) -> None:
    await _init_db()
    job = JobRecord(
        id="jobrun",
        history_id="hrun",
        status="running",
        progress_kind="stage",
        progress=0.4,
        request_snapshot={"mode": "text", "prompt": "x", "output_paths": []},
        provider_id="default",
        upstream_request_id=None,
        attempt_count=1,
        error_code=None,
        error_message_public=None,
        error_detail_internal=None,
        message="running",
        created_at="2026-07-30T00:00:00+08:00",
        started_at="2026-07-30T00:00:00+08:00",
        finished_at=None,
        parent_job_id=None,
    )
    await job_repo.insert_job(job)
    from app.services.task_service import TaskManager

    mgr = TaskManager()
    stats = await mgr.recover_on_startup()
    assert stats["interrupted"] == 1
    refreshed = await job_repo.get_job("jobrun")
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error_code == "INTERRUPTED"


@pytest.mark.asyncio
async def test_generate_persists_job_and_history(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import image_service

    async def fake_text(**kwargs: object) -> list[bytes]:
        return [PNG_1X1]

    monkeypatch.setattr(image_service, "generate_text_to_image", fake_text)
    res = await client.post(
        "/api/generate",
        data={"mode": "text", "prompt": "persist me", "model": "gpt-image-2"},
    )
    assert res.status_code == 200
    task_id = res.json()["task_id"]

    import asyncio

    final = None
    for _ in range(100):
        status = await client.get(f"/api/tasks/{task_id}")
        final = status.json()
        if final["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert final is not None
    assert final["status"] == "done"

    hist = await client.get("/api/history")
    items = hist.json()["items"]
    assert any(i["prompt"] == "persist me" for i in items)

    jobs = await client.get("/api/v1/jobs")
    assert any(j["id"] == task_id for j in jobs.json()["items"])


@pytest.mark.asyncio
async def test_path_traversal_on_media(client: AsyncClient) -> None:
    res = await client.get("/media/../config/secrets.json")
    assert res.status_code in (400, 404)


@pytest.mark.asyncio
async def test_recover_requeues_queued_jobs(isolated_env: Path) -> None:
    await _init_db()
    job = JobRecord(
        id="jobq",
        history_id="hq",
        status="queued",
        progress_kind="stage",
        progress=0.05,
        request_snapshot={"mode": "text", "prompt": "q", "output_paths": []},
        provider_id="default",
        upstream_request_id=None,
        attempt_count=0,
        error_code=None,
        error_message_public=None,
        error_detail_internal=None,
        message="queued",
        created_at="2026-07-30T00:00:00+08:00",
        started_at=None,
        finished_at=None,
        parent_job_id=None,
    )
    await job_repo.insert_job(job)
    from app.services.task_service import TaskManager

    mgr = TaskManager()
    stats = await mgr.recover_on_startup()
    assert stats["requeued"] == 1
    assert mgr._queue.qsize() == 1  # noqa: SLF001
    refreshed = await job_repo.get_job("jobq")
    assert refreshed is not None
    assert refreshed.status == "queued"


@pytest.mark.asyncio
async def test_cancel_queued_job(client: AsyncClient) -> None:
    # Insert queued job directly so workers don't race it away
    from app.db import migrate as db_migrate
    from app.services.task_service import _now

    await db_migrate.migrate()
    job = JobRecord(
        id="cancelme",
        history_id="hcancel",
        status="queued",
        progress_kind="stage",
        progress=0.05,
        request_snapshot={"mode": "text", "prompt": "x", "output_paths": []},
        provider_id="default",
        upstream_request_id=None,
        attempt_count=0,
        error_code=None,
        error_message_public=None,
        error_detail_internal=None,
        message="queued",
        created_at=_now(),
        started_at=None,
        finished_at=None,
        parent_job_id=None,
    )
    await job_repo.insert_job(job)
    res = await client.post("/api/v1/jobs/cancelme/cancel")
    assert res.status_code == 200
    got = await client.get("/api/v1/jobs/cancelme")
    assert got.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_failed_job(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import image_service
    from app.services.task_service import _now

    job = JobRecord(
        id="fail1",
        history_id="hfill",
        status="failed",
        progress_kind="stage",
        progress=1.0,
        request_snapshot={
            "mode": "text",
            "prompt": "retry please",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "medium",
            "n": 1,
            "output_paths": [],
        },
        provider_id="default",
        upstream_request_id=None,
        attempt_count=1,
        error_code="UPSTREAM_ERROR",
        error_message_public="boom",
        error_detail_internal="boom",
        message="failed",
        created_at=_now(),
        started_at=_now(),
        finished_at=_now(),
        parent_job_id=None,
    )
    await job_repo.insert_job(job)

    async def fake_text(**kwargs: object) -> list[bytes]:
        return [PNG_1X1]

    monkeypatch.setattr(image_service, "generate_text_to_image", fake_text)
    res = await client.post("/api/v1/jobs/fail1/retry")
    assert res.status_code == 200
    new_id = res.json()["task_id"]
    assert new_id != "fail1"

    import asyncio

    final = None
    for _ in range(100):
        status = await client.get(f"/api/tasks/{new_id}")
        final = status.json()
        if final["status"] in ("done", "error"):
            break
        await asyncio.sleep(0.05)
    assert final is not None
    assert final["status"] == "done"
    new_job = await job_repo.get_job(new_id)
    assert new_job is not None
    assert new_job.parent_job_id == "fail1"
