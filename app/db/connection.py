"""SQLite connection helpers (WAL)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.services import config_service

_DB: aiosqlite.Connection | None = None


def db_path() -> Path:
    return config_service.DATA_DIR / "app.db"


async def connect() -> aiosqlite.Connection:
    global _DB
    if _DB is not None:
        return _DB
    config_service.ensure_dirs()
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    _DB = conn
    return conn


async def close() -> None:
    global _DB
    if _DB is not None:
        await _DB.close()
        _DB = None


def reset_connection_for_tests() -> None:
    """Drop cached connection handle (tests must close first)."""
    global _DB
    _DB = None


@asynccontextmanager
async def transaction() -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect()
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
