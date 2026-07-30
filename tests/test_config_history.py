from __future__ import annotations

import json
from pathlib import Path

from app.schemas.models import HistoryItem, SettingsUpdate
from app.services import config_service, history_service


def test_mask_api_key() -> None:
    assert config_service.mask_api_key("") == ""
    assert config_service.mask_api_key("short") == "*****"
    assert config_service.mask_api_key("sk-test-key-12345678").startswith("sk-t")
    assert "..." in config_service.mask_api_key("sk-test-key-12345678")


def test_to_public_never_exposes_raw_key(isolated_env: Path) -> None:
    settings = config_service.load_settings()
    public = config_service.to_public(settings)
    dumped = public.model_dump()
    assert "api_key" not in dumped
    assert dumped["api_key_set"] is True
    assert dumped["api_key_masked"] != settings.api_key
    assert settings.api_key not in json.dumps(dumped)


def test_update_settings_keeps_key_when_blank(isolated_env: Path) -> None:
    before = config_service.load_settings().api_key
    config_service.update_settings(SettingsUpdate(api_key="", default_n=2))
    after = config_service.load_settings()
    assert after.api_key == before
    assert after.default_n == 2
    # secrets stay out of settings.json
    raw = json.loads(config_service.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "api_key" not in raw


def test_resolve_data_path_rejects_traversal(isolated_env: Path) -> None:
    try:
        config_service.resolve_data_path("../secrets.json")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_history_append_list_delete(isolated_env: Path) -> None:
    item = HistoryItem(
        id="abc123",
        created_at="2026-07-30T00:00:00+08:00",
        mode="text",
        model="gpt-image-2",
        prompt="a cat",
        size="1024x1024",
        quality="medium",
        n=1,
        status="done",
    )
    history_service.append_history(item)
    listed = history_service.list_history()
    assert len(listed) == 1
    assert listed[0].id == "abc123"

    item.status = "error"
    item.error = "boom"
    history_service.upsert_history(item)
    got = history_service.get_history("abc123")
    assert got is not None
    assert got.status == "error"
    assert got.error == "boom"

    assert history_service.delete_history("abc123") is True
    assert history_service.list_history() == []
