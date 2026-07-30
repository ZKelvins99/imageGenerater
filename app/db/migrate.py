"""Schema migrations for local SQLite."""

from __future__ import annotations

from datetime import UTC, datetime

from app.db import connection as db_conn

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS assets (
          id TEXT PRIMARY KEY,
          category TEXT NOT NULL,
          storage_path TEXT NOT NULL,
          original_filename TEXT,
          display_name TEXT,
          mime TEXT,
          extension TEXT,
          byte_size INTEGER NOT NULL DEFAULT 0,
          width INTEGER,
          height INTEGER,
          color_mode TEXT,
          has_alpha INTEGER NOT NULL DEFAULT 0,
          sha256 TEXT,
          parent_job_id TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_assets_sha256 ON assets(sha256);
        CREATE INDEX IF NOT EXISTS idx_assets_parent_job ON assets(parent_job_id);

        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          history_id TEXT UNIQUE,
          status TEXT NOT NULL,
          progress_kind TEXT NOT NULL DEFAULT 'stage',
          progress REAL NOT NULL DEFAULT 0,
          request_snapshot TEXT NOT NULL,
          provider_id TEXT,
          upstream_request_id TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          error_code TEXT,
          error_message_public TEXT,
          error_detail_internal TEXT,
          message TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          parent_job_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

        CREATE TABLE IF NOT EXISTS job_assets (
          job_id TEXT NOT NULL,
          asset_id TEXT NOT NULL,
          role TEXT NOT NULL,
          position INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (job_id, asset_id, role),
          FOREIGN KEY (job_id) REFERENCES jobs(id),
          FOREIGN KEY (asset_id) REFERENCES assets(id)
        );

        CREATE TABLE IF NOT EXISTS collections (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tags (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS job_tags (
          job_id TEXT NOT NULL,
          tag_id TEXT NOT NULL,
          PRIMARY KEY (job_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS provider_capabilities (
          provider_id TEXT NOT NULL,
          model TEXT NOT NULL,
          capabilities_json TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (provider_id, model)
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS response_conversations (
          id TEXT PRIMARY KEY,
          provider_id TEXT NOT NULL,
          responses_model TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '',
          root_job_id TEXT,
          latest_response_id TEXT,
          latest_turn_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_response_conversations_updated
          ON response_conversations(updated_at);

        CREATE TABLE IF NOT EXISTS response_turns (
          id TEXT PRIMARY KEY,
          conversation_id TEXT NOT NULL,
          job_id TEXT NOT NULL,
          parent_turn_id TEXT,
          response_id TEXT NOT NULL,
          previous_response_id TEXT,
          prompt TEXT NOT NULL,
          revised_prompt TEXT,
          usage_json TEXT NOT NULL DEFAULT '{}',
          image_call_id TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY (conversation_id) REFERENCES response_conversations(id)
        );

        CREATE INDEX IF NOT EXISTS idx_response_turns_conversation
          ON response_turns(conversation_id, created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_response_turns_job
          ON response_turns(job_id);
        """,
    ),
]


async def current_version() -> int:
    conn = await db_conn.connect()
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    await conn.commit()
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
    )
    row = await cursor.fetchone()
    return int(row["v"] if row else 0)


async def migrate() -> int:
    """Apply pending migrations. Returns latest version."""
    conn = await db_conn.connect()
    version = await current_version()
    for ver, sql in MIGRATIONS:
        if ver <= version:
            continue
        await conn.executescript(sql)
        now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        await conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (ver, now),
        )
        await conn.commit()
        version = ver
    return version
