from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.repositories import jobs as job_repo
from app.schemas.asset import JobPublic
from app.services import history_service, task_service
from app.services.history_service import output_url
from app.services.task_service import LEGACY_STATUS, job_to_task_status

router = APIRouter(prefix="/api/v1", tags=["jobs"])


async def _to_public(job) -> JobPublic:
    assets = await job_repo.list_job_assets(job.id, role="output")
    urls = [f"/media/{a['storage_path']}" for a in assets]
    partial_assets = await job_repo.list_job_assets(job.id, role="partial")
    partial_urls = [f"/media/{a['storage_path']}" for a in partial_assets]
    if not urls:
        urls = [output_url(p) for p in (job.request_snapshot.get("output_paths") or [])]
    return JobPublic(
        id=job.id,
        history_id=job.history_id,
        status=job.status,
        legacy_status=LEGACY_STATUS.get(job.status, "error"),
        progress_kind=job.progress_kind,
        progress=job.progress,
        message=job.message,
        provider_id=job.provider_id,
        request_snapshot=job.request_snapshot,
        output_urls=urls,
        partial_urls=partial_urls,
        error_code=job.error_code,
        error=job.error_message_public,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.get("/jobs")
async def list_jobs(limit: int = 200) -> dict:
    jobs = await job_repo.list_jobs(limit=limit)
    items = [await _to_public(j) for j in jobs]
    return {"items": [i.model_dump() for i in items]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = await job_repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return (await _to_public(job)).model_dump()


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = await job_repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == "queued":
        ok = await job_repo.update_job_status(
            job_id,
            new_status="cancelled",
            expected_statuses={"queued"},
            progress=1.0,
            message="已取消",
            error_code="CANCELLED",
            error_message_public="用户取消",
        )
        if not ok:
            raise HTTPException(status_code=409, detail="无法取消")
    elif job.status in ("preparing", "running", "streaming", "saving"):
        ok = await job_repo.update_job_status(
            job_id,
            new_status="cancel_requested",
            expected_statuses={job.status},
            message="取消请求已发送",
            error_code="CANCELLED",
            error_message_public="取消请求已发送（上游可能仍在计费）",
        )
        if not ok:
            raise HTTPException(status_code=409, detail="无法取消")
    else:
        raise HTTPException(status_code=400, detail="任务已结束，无法取消")
    job = await job_repo.get_job(job_id)
    assert job is not None
    status = job_to_task_status(job)
    task_service.task_manager.cache_status(status)
    return status.model_dump()


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    try:
        status = await task_service.task_manager.retry_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return status.model_dump()


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    job = await job_repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ("queued", "preparing", "running", "streaming", "saving"):
        raise HTTPException(status_code=400, detail="运行中的任务请先取消再删除")
    ok = await history_service.delete_history_async(
        job.history_id or job_id, delete_files=True
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")
    task_service.task_manager.drop_cached(job_id)
    return {"ok": True}
