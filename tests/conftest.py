from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from app.db import connection as db_conn
from app.db import migrate as db_migrate
from app.services import config_service, history_service, task_service
from app.services.task_service import TaskManager
from app.services.token_provider import reset_token_cache
from httpx import ASGITransport, AsyncClient


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point all data/config paths at a temp workspace (never touch real user files)."""
    root = tmp_path
    config_dir = root / "config"
    data_dir = root / "data"
    outputs = data_dir / "outputs"
    uploads = data_dir / "uploads"
    assets = data_dir / "assets"
    history = data_dir / "history.jsonl"
    settings = config_dir / "settings.json"
    secrets = config_dir / "secrets.json"

    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    outputs.mkdir(parents=True)
    uploads.mkdir(parents=True)
    assets.mkdir(parents=True)

    settings.write_text(
        "{\n"
        '  "base_url": "https://example.test/v1",\n'
        '  "default_model": "gpt-image-2",\n'
        '  "default_size": "1024x1024",\n'
        '  "default_quality": "medium",\n'
        '  "default_n": 1,\n'
        '  "model_filter_keywords": ["image"],\n'
        '  "host": "127.0.0.1",\n'
        '  "port": 27183\n'
        "}\n",
        encoding="utf-8",
    )
    secrets.write_text('{"api_key": "sk-test-key-12345678"}\n', encoding="utf-8")

    monkeypatch.setattr(config_service, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_service, "SETTINGS_PATH", settings)
    monkeypatch.setattr(config_service, "SECRETS_PATH", secrets)
    monkeypatch.setattr(config_service, "DATA_DIR", data_dir)
    monkeypatch.setattr(config_service, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(config_service, "UPLOADS_DIR", uploads)
    monkeypatch.setattr(config_service, "ASSETS_DIR", assets)
    monkeypatch.setattr(config_service, "ASSETS_INPUT_DIR", assets / "input")
    monkeypatch.setattr(config_service, "ASSETS_MASK_DIR", assets / "mask")
    monkeypatch.setattr(config_service, "ASSETS_OUTPUT_DIR", assets / "output")
    monkeypatch.setattr(config_service, "ASSETS_PARTIAL_DIR", assets / "partial")
    monkeypatch.setattr(config_service, "HISTORY_PATH", history)

    monkeypatch.setattr(history_service, "HISTORY_PATH", history)
    monkeypatch.setattr(history_service, "DATA_DIR", data_dir)

    monkeypatch.setattr(task_service, "OUTPUTS_DIR", outputs)

    manager = TaskManager()
    monkeypatch.setattr(task_service, "task_manager", manager)

    reset_token_cache()
    db_conn.reset_connection_for_tests()

    yield root


@pytest.fixture()
async def client(isolated_env: Path) -> AsyncIterator[AsyncClient]:
    from app.api import ws_routes
    from app.main import create_app

    await db_conn.close()
    db_conn.reset_connection_for_tests()

    config_service.ensure_dirs()
    await db_migrate.migrate()

    manager = task_service.task_manager
    manager.set_broadcast(ws_routes.ws_manager.broadcast)
    await manager.start_workers()

    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await manager.stop_workers()
    await db_conn.close()
    db_conn.reset_connection_for_tests()
