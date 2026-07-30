from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from app.schemas.models import HistoryItem
from app.services.config_service import HISTORY_PATH, OUTPUTS_DIR, ensure_dirs


def _iter_lines() -> Iterator[HistoryItem]:
    ensure_dirs()
    if not HISTORY_PATH.exists():
        return
    with HISTORY_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield HistoryItem.model_validate(json.loads(line))
            except Exception:
                continue


def list_history(limit: int = 200) -> list[HistoryItem]:
    items = list(_iter_lines())
    items.reverse()
    return items[:limit]


def get_history(item_id: str) -> HistoryItem | None:
    for item in _iter_lines():
        if item.id == item_id:
            return item
    return None


def append_history(item: HistoryItem) -> HistoryItem:
    ensure_dirs()
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(item.model_dump_json() + "\n")
    return item


def upsert_history(item: HistoryItem) -> HistoryItem:
    """Rewrite jsonl replacing matching id, or append if missing."""
    ensure_dirs()
    items = list(_iter_lines())
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
    items = list(_iter_lines())
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


def _safe_unlink(relative: str) -> Path | None:
    from app.services.config_service import DATA_DIR

    rel = relative.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    target = (DATA_DIR / rel).resolve()
    if not str(target).startswith(str(DATA_DIR.resolve())):
        return None
    return target


def output_url(relative: str) -> str:
    rel = relative.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    return f"/media/{rel}"
