from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.schemas.models import AppSettings, SettingsPublic, SettingsUpdate

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SECRETS_PATH = CONFIG_DIR / "secrets.json"
EXAMPLE_PATH = CONFIG_DIR / "settings.example.json"
SECRETS_EXAMPLE_PATH = CONFIG_DIR / "secrets.example.json"
DATA_DIR = ROOT / "data"
OUTPUTS_DIR = DATA_DIR / "outputs"
UPLOADS_DIR = DATA_DIR / "uploads"
HISTORY_PATH = DATA_DIR / "history.jsonl"

# Keys that must never be written into settings.json
SECRET_FIELDS = ("api_key",)


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        if EXAMPLE_PATH.exists():
            shutil.copy(EXAMPLE_PATH, SETTINGS_PATH)
        else:
            _write_json(SETTINGS_PATH, _public_dict(AppSettings()))
    if not SECRETS_PATH.exists():
        if SECRETS_EXAMPLE_PATH.exists():
            shutil.copy(SECRETS_EXAMPLE_PATH, SECRETS_PATH)
        else:
            _write_json(SECRETS_PATH, {"api_key": ""})
    _migrate_api_key_out_of_settings()


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _public_dict(settings: AppSettings) -> dict:
    data = settings.model_dump()
    for field in SECRET_FIELDS:
        data.pop(field, None)
    return data


def _load_secrets() -> dict:
    raw = _read_json(SECRETS_PATH)
    return {"api_key": str(raw.get("api_key") or "")}


def _save_secrets(api_key: str) -> None:
    ensure_dirs()
    _write_json(SECRETS_PATH, {"api_key": api_key})


def _migrate_api_key_out_of_settings() -> None:
    """One-time: move legacy api_key from settings.json into secrets.json."""
    if not SETTINGS_PATH.exists():
        return
    raw = _read_json(SETTINGS_PATH)
    legacy = raw.pop("api_key", None)
    if legacy is None:
        return
    secrets = _load_secrets()
    if legacy and not secrets.get("api_key"):
        _save_secrets(str(legacy))
    # Always strip key from settings.json so it is never redistributed
    _write_json(SETTINGS_PATH, raw)


def load_settings() -> AppSettings:
    ensure_dirs()
    raw = _read_json(SETTINGS_PATH)
    raw.pop("api_key", None)
    secrets = _load_secrets()
    raw["api_key"] = secrets.get("api_key") or ""
    return AppSettings.model_validate(raw)


def save_settings(settings: AppSettings) -> AppSettings:
    ensure_dirs()
    _write_json(SETTINGS_PATH, _public_dict(settings))
    _save_secrets(settings.api_key or "")
    return settings


def update_settings(patch: SettingsUpdate) -> AppSettings:
    current = load_settings()
    data = current.model_dump()
    for key, value in patch.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        # Empty api_key in patch means "do not change" (UI leaves blank to keep)
        if key == "api_key" and value == "":
            continue
        data[key] = value
    return save_settings(AppSettings.model_validate(data))


def mask_api_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def to_public(settings: AppSettings) -> SettingsPublic:
    return SettingsPublic(
        base_url=settings.base_url,
        api_key_set=bool(settings.api_key),
        api_key_masked=mask_api_key(settings.api_key),
        default_model=settings.default_model,
        default_size=settings.default_size,
        default_quality=settings.default_quality,
        default_n=settings.default_n,
        model_filter_keywords=settings.model_filter_keywords,
        host=settings.host,
        port=settings.port,
    )


def resolve_data_path(relative: str) -> Path:
    """Resolve a path under data/, rejecting traversal."""
    ensure_dirs()
    rel = relative.replace("\\", "/").lstrip("/")
    if rel.startswith("data/"):
        rel = rel[5:]
    target = (DATA_DIR / rel).resolve()
    if not str(target).startswith(str(DATA_DIR.resolve())):
        raise ValueError("Invalid path")
    return target
