from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AssetCategory = Literal["input", "mask", "output", "partial", "thumbnail"]


class AssetPublic(BaseModel):
    id: str
    category: AssetCategory
    storage_path: str
    original_filename: str | None = None
    display_name: str | None = None
    mime: str | None = None
    extension: str | None = None
    byte_size: int = 0
    width: int | None = None
    height: int | None = None
    color_mode: str | None = None
    has_alpha: bool = False
    sha256: str | None = None
    parent_job_id: str | None = None
    created_at: str
    content_url: str | None = None
    thumbnail_url: str | None = None


class AssetValidateMaskResult(BaseModel):
    ok: bool
    message: str = ""
    width: int | None = None
    height: int | None = None
    has_alpha: bool | None = None


class JobPublic(BaseModel):
    id: str
    history_id: str | None = None
    status: str
    legacy_status: str
    progress_kind: str = "stage"
    progress: float = 0.0
    message: str = ""
    provider_id: str | None = None
    request_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_urls: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class MigrationReport(BaseModel):
    success: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)
