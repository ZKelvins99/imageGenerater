from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AppSettings(BaseModel):
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    default_size: str = "1024x1024"
    default_quality: Literal["low", "medium", "high", "auto"] = "medium"
    default_n: int = Field(default=1, ge=1, le=4)
    model_filter_keywords: list[str] = Field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 27183


class SettingsUpdate(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    default_size: str | None = None
    default_quality: Literal["low", "medium", "high", "auto"] | None = None
    default_n: int | None = Field(default=None, ge=1, le=4)
    model_filter_keywords: list[str] | None = None
    host: str | None = None
    port: int | None = None


class SettingsPublic(BaseModel):
    """Settings returned to the UI (api_key masked)."""

    base_url: str
    api_key_set: bool
    api_key_masked: str
    default_model: str
    default_size: str
    default_quality: str
    default_n: int
    model_filter_keywords: list[str]
    host: str
    port: int


class GenerateRequest(BaseModel):
    mode: Literal["text", "image"] = "text"
    prompt: str = Field(min_length=1)
    model: str | None = None
    size: str | None = None
    quality: Literal["low", "medium", "high", "auto"] | None = None
    n: int | None = Field(default=None, ge=1, le=4)
    # For image mode: path relative to data/, or history image id reuse
    reference_path: str | None = None


class HistoryItem(BaseModel):
    id: str
    created_at: str
    mode: Literal["text", "image"]
    model: str
    prompt: str
    size: str
    quality: str
    n: int
    reference_path: str | None = None
    output_paths: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "done", "error"] = "done"
    error: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TaskStatus(BaseModel):
    task_id: str
    status: Literal["pending", "running", "done", "error"]
    message: str = ""
    progress: float = 0.0
    history_id: str | None = None
    output_urls: list[str] = Field(default_factory=list)
    error: str | None = None
