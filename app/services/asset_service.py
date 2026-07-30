from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.repositories import assets as asset_repo
from app.repositories.assets import AssetRecord
from app.schemas.asset import AssetPublic
from app.services import config_service
from app.services.config_service import ensure_dirs, resolve_data_path

# Safety limits (Phase 2)
MAX_ASSET_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 50_000_000  # decompress-bomb guard
ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
THUMB_MAX = 256


class AssetError(Exception):
    def __init__(self, message: str, code: str = "INPUT_INVALID"):
        super().__init__(message)
        self.message = message
        self.code = code


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def detect_mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # Reject SVG / HTML disguised as images
    head = data[:256].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml") or b"<html" in head:
        return None
    return None


def _inspect_image(data: bytes) -> tuple[int, int, str, bool]:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = im.size
            if width * height > MAX_PIXELS:
                raise AssetError(
                    f"图片像素过多（{width}x{height}），可能是解压炸弹",
                    code="ASSET_TOO_LARGE",
                )
            mode = im.mode
            has_alpha = mode in ("RGBA", "LA") or (
                mode == "P" and "transparency" in im.info
            )
            return width, height, mode, has_alpha
    except AssetError:
        raise
    except UnidentifiedImageError as e:
        raise AssetError("无法识别的图片内容", code="ASSET_FORMAT_UNSUPPORTED") from e
    except Exception as e:
        raise AssetError(f"图片解析失败: {e}", code="INPUT_INVALID") from e


def _category_dir(category: str) -> Path:
    mapping = {
        "input": config_service.ASSETS_INPUT_DIR,
        "mask": config_service.ASSETS_MASK_DIR,
        "output": config_service.ASSETS_OUTPUT_DIR,
        "partial": config_service.ASSETS_PARTIAL_DIR,
        "thumbnail": config_service.ASSETS_PARTIAL_DIR,
    }
    return mapping.get(category, config_service.ASSETS_INPUT_DIR)


def _safe_display_name(name: str | None) -> str:
    raw = (name or "image").replace("\\", "/").split("/")[-1]
    return raw[:180] or "image"


async def save_bytes_as_asset(
    data: bytes,
    *,
    category: str,
    original_filename: str | None = None,
    parent_job_id: str | None = None,
    claimed_mime: str | None = None,
) -> AssetRecord:
    ensure_dirs()
    if len(data) > MAX_ASSET_BYTES:
        raise AssetError(
            f"文件过大（>{MAX_ASSET_BYTES} bytes）", code="ASSET_TOO_LARGE"
        )
    mime = detect_mime(data)
    if mime is None or mime not in ALLOWED_MIME:
        raise AssetError(
            "不支持的图片格式（仅 PNG/JPEG/WebP）",
            code="ASSET_FORMAT_UNSUPPORTED",
        )
    if claimed_mime and claimed_mime.split(";")[0].strip().lower() not in (
        mime,
        "application/octet-stream",
        "binary/octet-stream",
        "",
    ):
        # Browser Content-Type may lie; we trust content detection, but reject
        # obvious mismatches like image/svg+xml
        claimed = claimed_mime.split(";")[0].strip().lower()
        if claimed.startswith("image/") and claimed not in ALLOWED_MIME:
            raise AssetError(
                f"声明的 MIME 不被支持: {claimed}",
                code="ASSET_FORMAT_UNSUPPORTED",
            )

    width, height, color_mode, has_alpha = _inspect_image(data)
    sha = hashlib.sha256(data).hexdigest()
    existing = await asset_repo.find_by_sha256(sha, category=category)
    if existing is not None:
        return existing

    asset_id = uuid.uuid4().hex[:16]
    ext = ALLOWED_MIME[mime]
    day = _day()
    dest_dir = _category_dir(category) / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{asset_id}{ext}"
    dest = dest_dir / fname
    dest.write_bytes(data)
    rel = f"assets/{category}/{day}/{fname}"

    record = AssetRecord(
        id=asset_id,
        category=category,
        storage_path=rel,
        original_filename=original_filename,
        display_name=_safe_display_name(original_filename),
        mime=mime,
        extension=ext,
        byte_size=len(data),
        width=width,
        height=height,
        color_mode=color_mode,
        has_alpha=has_alpha,
        sha256=sha,
        parent_job_id=parent_job_id,
        created_at=_now(),
    )
    await asset_repo.insert_asset(record)

    # Best-effort thumbnail for inputs
    if category in ("input", "mask", "output"):
        try:
            await _write_thumbnail(record, data)
        except Exception:
            pass
    return record


async def _write_thumbnail(asset: AssetRecord, data: bytes) -> AssetRecord | None:
    with Image.open(io.BytesIO(data)) as im:
        converted = (
            im.convert("RGBA") if im.mode in ("P", "RGBA", "LA") else im.convert("RGB")
        )
        converted.thumbnail((THUMB_MAX, THUMB_MAX))
        buf = io.BytesIO()
        converted.save(buf, format="PNG")
        thumb_bytes = buf.getvalue()
        thumb_w, thumb_h = converted.size
    thumb_id = uuid.uuid4().hex[:16]
    day = _day()
    dest_dir = config_service.ASSETS_PARTIAL_DIR / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{thumb_id}_thumb.png"
    (dest_dir / fname).write_bytes(thumb_bytes)
    rel = f"assets/partial/{day}/{fname}"
    thumb = AssetRecord(
        id=thumb_id,
        category="thumbnail",
        storage_path=rel,
        original_filename=f"{asset.id}_thumb.png",
        display_name=f"{asset.display_name} thumbnail",
        mime="image/png",
        extension=".png",
        byte_size=len(thumb_bytes),
        width=None,
        height=None,
        color_mode="RGB",
        has_alpha=False,
        sha256=hashlib.sha256(thumb_bytes).hexdigest(),
        parent_job_id=asset.parent_job_id,
        created_at=_now(),
    )
    thumb.width, thumb.height = thumb_w, thumb_h
    await asset_repo.insert_asset(thumb)
    return thumb


async def register_existing_path(
    relative_path: str,
    *,
    category: str,
    parent_job_id: str | None = None,
    original_filename: str | None = None,
) -> AssetRecord:
    """Register a legacy file under data/ as an asset (no re-encode)."""
    path = resolve_data_path(relative_path)
    if not path.is_file():
        raise AssetError(f"文件不存在: {relative_path}", code="INPUT_INVALID")
    data = path.read_bytes()
    mime = detect_mime(data) or "application/octet-stream"
    width = height = None
    color_mode = None
    has_alpha = False
    if mime in ALLOWED_MIME:
        try:
            width, height, color_mode, has_alpha = _inspect_image(data)
        except AssetError:
            pass
    sha = hashlib.sha256(data).hexdigest()
    existing = await asset_repo.find_by_sha256(sha, category=category)
    if existing is not None:
        return existing
    rel = relative_path.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    ext = path.suffix.lower() or ALLOWED_MIME.get(mime, "")
    record = AssetRecord(
        id=uuid.uuid4().hex[:16],
        category=category,
        storage_path=rel,
        original_filename=original_filename or path.name,
        display_name=_safe_display_name(original_filename or path.name),
        mime=mime if mime in ALLOWED_MIME else None,
        extension=ext,
        byte_size=len(data),
        width=width,
        height=height,
        color_mode=color_mode,
        has_alpha=has_alpha,
        sha256=sha,
        parent_job_id=parent_job_id,
        created_at=_now(),
    )
    await asset_repo.insert_asset(record)
    return record


def to_public(asset: AssetRecord, *, thumbnail_url: str | None = None) -> AssetPublic:
    return AssetPublic(
        id=asset.id,
        category=asset.category,  # type: ignore[arg-type]
        storage_path=asset.storage_path,
        original_filename=asset.original_filename,
        display_name=asset.display_name,
        mime=asset.mime,
        extension=asset.extension,
        byte_size=asset.byte_size,
        width=asset.width,
        height=asset.height,
        color_mode=asset.color_mode,
        has_alpha=asset.has_alpha,
        sha256=asset.sha256,
        parent_job_id=asset.parent_job_id,
        created_at=asset.created_at,
        content_url=f"/media/{asset.storage_path}",
        thumbnail_url=thumbnail_url,
    )


async def to_public_async(asset: AssetRecord) -> AssetPublic:
    thumb = await asset_repo.find_thumbnail_for(asset.id)
    thumb_url = f"/media/{thumb.storage_path}" if thumb else None
    return to_public(asset, thumbnail_url=thumb_url)


async def validate_mask_bytes(
    data: bytes, *, expected_size: tuple[int, int] | None = None
) -> dict:
    mime = detect_mime(data)
    if mime != "image/png":
        return {
            "ok": False,
            "message": "蒙版必须是含 alpha 的 PNG",
            "width": None,
            "height": None,
            "has_alpha": None,
        }
    width, height, _mode, has_alpha = _inspect_image(data)
    if not has_alpha:
        return {
            "ok": False,
            "message": "蒙版必须包含 alpha 通道",
            "width": width,
            "height": height,
            "has_alpha": False,
        }
    if expected_size and (width, height) != expected_size:
        return {
            "ok": False,
            "message": f"蒙版尺寸 {width}x{height} 与主图 {expected_size[0]}x{expected_size[1]} 不一致",
            "width": width,
            "height": height,
            "has_alpha": True,
        }
    return {
        "ok": True,
        "message": "蒙版校验通过",
        "width": width,
        "height": height,
        "has_alpha": True,
    }
