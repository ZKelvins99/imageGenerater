from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import connection as db_conn


@dataclass
class AssetRecord:
    id: str
    category: str
    storage_path: str
    original_filename: str | None
    display_name: str | None
    mime: str | None
    extension: str | None
    byte_size: int
    width: int | None
    height: int | None
    color_mode: str | None
    has_alpha: bool
    sha256: str | None
    parent_job_id: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> AssetRecord:
        return cls(
            id=row["id"],
            category=row["category"],
            storage_path=row["storage_path"],
            original_filename=row["original_filename"],
            display_name=row["display_name"],
            mime=row["mime"],
            extension=row["extension"],
            byte_size=int(row["byte_size"] or 0),
            width=row["width"],
            height=row["height"],
            color_mode=row["color_mode"],
            has_alpha=bool(row["has_alpha"]),
            sha256=row["sha256"],
            parent_job_id=row["parent_job_id"],
            created_at=row["created_at"],
        )


async def insert_asset(asset: AssetRecord) -> AssetRecord:
    conn = await db_conn.connect()
    await conn.execute(
        """
        INSERT INTO assets (
          id, category, storage_path, original_filename, display_name,
          mime, extension, byte_size, width, height, color_mode, has_alpha,
          sha256, parent_job_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset.id,
            asset.category,
            asset.storage_path,
            asset.original_filename,
            asset.display_name,
            asset.mime,
            asset.extension,
            asset.byte_size,
            asset.width,
            asset.height,
            asset.color_mode,
            1 if asset.has_alpha else 0,
            asset.sha256,
            asset.parent_job_id,
            asset.created_at,
        ),
    )
    await conn.commit()
    return asset


async def get_asset(asset_id: str) -> AssetRecord | None:
    conn = await db_conn.connect()
    cur = await conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,))
    row = await cur.fetchone()
    return AssetRecord.from_row(row) if row else None


async def find_by_sha256(
    sha256: str, category: str | None = None
) -> AssetRecord | None:
    conn = await db_conn.connect()
    if category:
        cur = await conn.execute(
            "SELECT * FROM assets WHERE sha256 = ? AND category = ? LIMIT 1",
            (sha256, category),
        )
    else:
        cur = await conn.execute(
            "SELECT * FROM assets WHERE sha256 = ? LIMIT 1",
            (sha256,),
        )
    row = await cur.fetchone()
    return AssetRecord.from_row(row) if row else None


async def find_thumbnail_for(asset_id: str) -> AssetRecord | None:
    """Thumbnails are stored with original_filename = '{asset_id}_thumb.png'."""
    conn = await db_conn.connect()
    cur = await conn.execute(
        """
        SELECT * FROM assets
        WHERE category = 'thumbnail' AND original_filename = ?
        LIMIT 1
        """,
        (f"{asset_id}_thumb.png",),
    )
    row = await cur.fetchone()
    return AssetRecord.from_row(row) if row else None


async def delete_asset(asset_id: str) -> bool:
    """Delete asset row only if not referenced by job_assets."""
    conn = await db_conn.connect()
    cur = await conn.execute(
        "SELECT 1 FROM job_assets WHERE asset_id = ? LIMIT 1", (asset_id,)
    )
    if await cur.fetchone():
        return False
    cur = await conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    await conn.commit()
    return cur.rowcount > 0
