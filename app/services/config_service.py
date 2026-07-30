from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.schemas.models import AppSettings, SettingsPublic, SettingsUpdate
from app.schemas.provider import ProviderProfile, ProviderSecret

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SECRETS_PATH = CONFIG_DIR / "secrets.json"
EXAMPLE_PATH = CONFIG_DIR / "settings.example.json"
SECRETS_EXAMPLE_PATH = CONFIG_DIR / "secrets.example.json"
DATA_DIR = ROOT / "data"
OUTPUTS_DIR = DATA_DIR / "outputs"
UPLOADS_DIR = DATA_DIR / "uploads"
ASSETS_DIR = DATA_DIR / "assets"
ASSETS_INPUT_DIR = ASSETS_DIR / "input"
ASSETS_MASK_DIR = ASSETS_DIR / "mask"
ASSETS_OUTPUT_DIR = ASSETS_DIR / "output"
ASSETS_PARTIAL_DIR = ASSETS_DIR / "partial"
HISTORY_PATH = DATA_DIR / "history.jsonl"

# Keys that must never be written into settings.json
SECRET_FIELDS = ("api_key",)
DEFAULT_PROVIDER_ID = "default"


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for d in (
        ASSETS_DIR,
        ASSETS_INPUT_DIR,
        ASSETS_MASK_DIR,
        ASSETS_OUTPUT_DIR,
        ASSETS_PARTIAL_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_PATH.exists():
        if EXAMPLE_PATH.exists():
            shutil.copy(EXAMPLE_PATH, SETTINGS_PATH)
        else:
            _write_json(SETTINGS_PATH, _public_dict(AppSettings()))
    if not SECRETS_PATH.exists():
        if SECRETS_EXAMPLE_PATH.exists():
            shutil.copy(SECRETS_EXAMPLE_PATH, SECRETS_PATH)
        else:
            _write_json(SECRETS_PATH, {"api_key": "", "providers": {}})
    _migrate_api_key_out_of_settings()
    _migrate_legacy_to_providers()


def _write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _public_dict(settings: AppSettings) -> dict:
    data = settings.model_dump()
    for field in SECRET_FIELDS:
        data.pop(field, None)
    return data


def _load_secrets_raw() -> dict[str, Any]:
    raw = _read_json(SECRETS_PATH)
    if "providers" not in raw or not isinstance(raw.get("providers"), dict):
        raw["providers"] = {}
    return raw


def load_provider_secret(provider_id: str) -> ProviderSecret:
    raw = _load_secrets_raw()
    entry = raw.get("providers", {}).get(provider_id) or {}
    # Fall back to legacy top-level api_key for the default provider
    api_key = str(entry.get("api_key") or "")
    if not api_key and provider_id == DEFAULT_PROVIDER_ID:
        api_key = str(raw.get("api_key") or "")
    return ProviderSecret(
        api_key=api_key,
        distributor_client_id=str(entry.get("distributor_client_id") or ""),
        distributor_client_secret=str(entry.get("distributor_client_secret") or ""),
    )


def save_provider_secret(provider_id: str, secret: ProviderSecret) -> None:
    ensure_dirs()
    raw = _load_secrets_raw()
    providers = dict(raw.get("providers") or {})
    providers[provider_id] = {
        "api_key": secret.api_key or "",
        "distributor_client_id": secret.distributor_client_id or "",
        "distributor_client_secret": secret.distributor_client_secret or "",
    }
    raw["providers"] = providers
    # Keep legacy top-level api_key in sync with active/default for older tools
    if provider_id == DEFAULT_PROVIDER_ID:
        raw["api_key"] = secret.api_key or ""
    _write_json(SECRETS_PATH, raw)


def delete_provider_secret(provider_id: str) -> None:
    ensure_dirs()
    raw = _load_secrets_raw()
    providers = dict(raw.get("providers") or {})
    providers.pop(provider_id, None)
    raw["providers"] = providers
    if provider_id == DEFAULT_PROVIDER_ID:
        raw["api_key"] = ""
    _write_json(SECRETS_PATH, raw)


def _load_secrets() -> dict:
    """Legacy helper: active/default provider api_key."""
    settings_raw = _read_json(SETTINGS_PATH)
    active_id = settings_raw.get("active_provider_id") or DEFAULT_PROVIDER_ID
    secret = load_provider_secret(str(active_id))
    if not secret.api_key:
        # Fallback to legacy top-level
        raw = _load_secrets_raw()
        return {"api_key": str(raw.get("api_key") or "")}
    return {"api_key": secret.api_key}


def _save_secrets(api_key: str) -> None:
    ensure_dirs()
    settings_raw = _read_json(SETTINGS_PATH)
    active_id = str(settings_raw.get("active_provider_id") or DEFAULT_PROVIDER_ID)
    current = load_provider_secret(active_id)
    current.api_key = api_key
    save_provider_secret(active_id, current)
    # Also keep legacy top-level for tools that still read it
    raw = _load_secrets_raw()
    raw["api_key"] = api_key
    _write_json(SECRETS_PATH, raw)


def _migrate_api_key_out_of_settings() -> None:
    """One-time: move legacy api_key from settings.json into secrets.json."""
    if not SETTINGS_PATH.exists():
        return
    raw = _read_json(SETTINGS_PATH)
    legacy = raw.pop("api_key", None)
    if legacy is None:
        return
    secrets = _load_secrets_raw()
    if legacy and not secrets.get("api_key"):
        secrets["api_key"] = str(legacy)
        _write_json(SECRETS_PATH, secrets)
    # Always strip key from settings.json so it is never redistributed
    _write_json(SETTINGS_PATH, raw)


def _migrate_legacy_to_providers() -> None:
    """Ensure providers[] exists; seed from flat base_url / default_model / api_key."""
    if not SETTINGS_PATH.exists():
        return
    raw = _read_json(SETTINGS_PATH)
    providers_raw = raw.get("providers")
    if isinstance(providers_raw, list) and len(providers_raw) > 0:
        if not raw.get("active_provider_id"):
            raw["active_provider_id"] = (
                providers_raw[0].get("id") or DEFAULT_PROVIDER_ID
            )
            _write_json(SETTINGS_PATH, raw)
        # Ensure default provider secret exists if only legacy top-level key
        active = str(raw.get("active_provider_id") or DEFAULT_PROVIDER_ID)
        secret = load_provider_secret(active)
        legacy_key = str(_load_secrets_raw().get("api_key") or "")
        if legacy_key and not secret.api_key:
            save_provider_secret(
                active,
                ProviderSecret(api_key=legacy_key),
            )
        return

    base_url = str(raw.get("base_url") or "")
    default_model = str(raw.get("default_model") or "")
    profile = ProviderProfile(
        id=DEFAULT_PROVIDER_ID,
        name="Default",
        provider_type="openai_compatible_images",
        base_url=base_url,
        auth_type="static_bearer",
        default_model=default_model,
        enabled=True,
    )
    raw["providers"] = [profile.model_dump()]
    raw["active_provider_id"] = DEFAULT_PROVIDER_ID
    # Strip flat connection fields from persisted public settings once migrated;
    # they remain available via load_settings() compatibility view.
    _write_json(SETTINGS_PATH, raw)

    legacy_key = str(_load_secrets_raw().get("api_key") or "")
    if legacy_key:
        save_provider_secret(
            DEFAULT_PROVIDER_ID,
            ProviderSecret(api_key=legacy_key),
        )


def _active_provider(settings: AppSettings) -> ProviderProfile | None:
    if not settings.providers:
        return None
    pid = settings.active_provider_id
    for p in settings.providers:
        if p.id == pid and not p.deleted:
            return p
    for p in settings.providers:
        if not p.deleted and p.enabled:
            return p
    for p in settings.providers:
        if not p.deleted:
            return p
    return None


def _sync_flat_from_active(settings: AppSettings) -> AppSettings:
    """Fill legacy flat fields from the active provider for old callers."""
    active = _active_provider(settings)
    if active is None:
        return settings
    secret = load_provider_secret(active.id)
    settings.base_url = active.base_url
    settings.default_model = active.default_model or settings.default_model
    settings.api_key = secret.api_key
    settings.active_provider_id = active.id
    return settings


def load_settings() -> AppSettings:
    ensure_dirs()
    raw = _read_json(SETTINGS_PATH)
    raw.pop("api_key", None)
    settings = AppSettings.model_validate(raw)
    return _sync_flat_from_active(settings)


def save_settings(settings: AppSettings) -> AppSettings:
    """Persist public settings + sync active provider secret / flat view."""
    ensure_dirs()
    # Keep active provider profile fields in sync when flat fields were edited
    active = _active_provider(settings)
    if active is not None:
        updated: list[ProviderProfile] = []
        for p in settings.providers:
            if p.id == active.id:
                updated.append(
                    p.model_copy(
                        update={
                            "base_url": settings.base_url,
                            "default_model": settings.default_model or p.default_model,
                        }
                    )
                )
            else:
                updated.append(p)
        settings = settings.model_copy(update={"providers": updated})
        existing_secret = load_provider_secret(active.id)
        save_provider_secret(
            active.id,
            ProviderSecret(
                api_key=settings.api_key or "",
                distributor_client_id=existing_secret.distributor_client_id,
                distributor_client_secret=existing_secret.distributor_client_secret,
            ),
        )
    else:
        _save_secrets(settings.api_key or "")

    _write_json(SETTINGS_PATH, _public_dict(settings))
    return _sync_flat_from_active(settings)


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
        active_provider_id=settings.active_provider_id,
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
