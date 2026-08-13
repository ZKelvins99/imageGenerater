from __future__ import annotations

import re
import uuid

from app.schemas.provider import (
    ConnectionTestResult,
    ConnectionTestStage,
    ProviderCreate,
    ProviderProfile,
    ProviderPublic,
    ProviderSecret,
    ProviderUpdate,
)
from app.services import config_service
from app.services.config_service import (
    DEFAULT_PROVIDER_ID,
    load_provider_secret,
    load_settings,
    mask_api_key,
    save_provider_secret,
    save_settings,
)
from app.services.token_provider import (
    TokenError,
    get_access_token,
    get_token_cache,
)

_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "CONFIG_INVALID"):
        super().__init__(message)
        self.message = message
        self.code = code


def _slug_id(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    base = (base or "provider")[:40]
    return f"{base}-{uuid.uuid4().hex[:6]}"


def list_providers(*, include_deleted: bool = False) -> list[ProviderPublic]:
    settings = load_settings()
    active = settings.active_provider_id
    out: list[ProviderPublic] = []
    for p in settings.providers:
        if p.deleted and not include_deleted:
            continue
        out.append(to_public(p, is_active=(p.id == active)))
    return out


def get_provider(provider_id: str) -> ProviderProfile:
    settings = load_settings()
    for p in settings.providers:
        if p.id == provider_id:
            return p
    raise ProviderError(f"Provider 不存在: {provider_id}", code="MODEL_NOT_FOUND")


def get_active_provider() -> ProviderProfile:
    settings = load_settings()
    pid = settings.active_provider_id or DEFAULT_PROVIDER_ID
    for p in settings.providers:
        if p.id == pid and not p.deleted:
            return p
    for p in settings.providers:
        if not p.deleted and p.enabled:
            return p
    raise ProviderError("没有可用的 Provider，请先在设置中配置", code="CONFIG_INVALID")


def to_public(profile: ProviderProfile, *, is_active: bool = False) -> ProviderPublic:
    secret = load_provider_secret(profile.id)
    return ProviderPublic(
        id=profile.id,
        name=profile.name,
        provider_type=profile.provider_type,
        base_url=profile.base_url,
        auth_type=profile.auth_type,
        default_model=profile.default_model,
        enabled=profile.enabled,
        verify_tls=profile.verify_tls,
        timeout_seconds=profile.timeout_seconds,
        extra_headers=profile.extra_headers,
        capability_overrides=profile.capability_overrides,
        responses_enabled=profile.responses_enabled,
        responses_model=profile.responses_model,
        token_distributor=profile.token_distributor,
        deleted=profile.deleted,
        api_key_set=bool(secret.api_key),
        api_key_masked=mask_api_key(secret.api_key),
        distributor_client_id_set=bool(secret.distributor_client_id),
        distributor_client_secret_set=bool(secret.distributor_client_secret),
        is_active=is_active,
    )


def create_provider(body: ProviderCreate) -> ProviderPublic:
    settings = load_settings()
    pid = body.id or _slug_id(body.name)
    if not _SAFE_ID.match(pid):
        raise ProviderError("Provider id 仅允许字母数字、_、-", code="CONFIG_INVALID")
    if any(p.id == pid for p in settings.providers):
        raise ProviderError(f"Provider id 已存在: {pid}", code="CONFIG_INVALID")

    profile = ProviderProfile(
        id=pid,
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url,
        auth_type=body.auth_type,
        default_model=body.default_model,
        enabled=body.enabled,
        verify_tls=body.verify_tls,
        timeout_seconds=body.timeout_seconds,
        extra_headers=body.extra_headers,
        capability_overrides=body.capability_overrides,
        responses_enabled=body.responses_enabled,
        responses_model=body.responses_model,
        token_distributor=body.token_distributor,
    )
    providers = list(settings.providers) + [profile]
    active_id = settings.active_provider_id or pid
    if not settings.providers:
        active_id = pid
    else:
        current = next(
            (p for p in settings.providers if p.id == active_id and not p.deleted),
            None,
        )
        # Auto-activate the first real provider when the active one is still the
        # unconfigured placeholder seeded on first run (empty base_url).
        if current is None or not current.base_url.strip():
            active_id = pid

    flat: dict[str, object] = {"providers": providers, "active_provider_id": active_id}
    if active_id == pid:
        # The new provider becomes active: keep the legacy flat view in sync so
        # save_settings() does not clobber its fields with stale values.
        flat["base_url"] = profile.base_url
        flat["default_model"] = profile.default_model or settings.default_model
        flat["api_key"] = body.api_key or ""
    settings = settings.model_copy(update=flat)
    save_settings(settings)

    secret = ProviderSecret(
        api_key=body.api_key or "",
        distributor_client_id=body.distributor_client_id or "",
        distributor_client_secret=body.distributor_client_secret or "",
    )
    save_provider_secret(pid, secret)
    get_token_cache().invalidate(pid)
    return to_public(profile, is_active=(active_id == pid))


def update_provider(provider_id: str, body: ProviderUpdate) -> ProviderPublic:
    settings = load_settings()
    found: ProviderProfile | None = None
    updated: list[ProviderProfile] = []
    patch = body.model_dump(exclude_unset=True)
    secret_keys = {"api_key", "distributor_client_id", "distributor_client_secret"}
    profile_patch = {k: v for k, v in patch.items() if k not in secret_keys}

    for p in settings.providers:
        if p.id != provider_id:
            updated.append(p)
            continue
        data = p.model_dump()
        data.update(profile_patch)
        found = ProviderProfile.model_validate(data)
        updated.append(found)

    if found is None:
        raise ProviderError(f"Provider 不存在: {provider_id}", code="MODEL_NOT_FOUND")

    settings = settings.model_copy(update={"providers": updated})
    if settings.active_provider_id == provider_id:
        settings = settings.model_copy(
            update={
                "base_url": found.base_url,
                "default_model": found.default_model or settings.default_model,
            }
        )
    save_settings(settings)

    secret = load_provider_secret(provider_id)
    secret_changed = False
    if "api_key" in patch and patch["api_key"] not in (None, ""):
        secret.api_key = str(patch["api_key"])
        secret_changed = True
    if "distributor_client_id" in patch and patch["distributor_client_id"] not in (
        None,
        "",
    ):
        secret.distributor_client_id = str(patch["distributor_client_id"])
        secret_changed = True
    if "distributor_client_secret" in patch and patch[
        "distributor_client_secret"
    ] not in (None, ""):
        secret.distributor_client_secret = str(patch["distributor_client_secret"])
        secret_changed = True
    if secret_changed:
        save_provider_secret(provider_id, secret)
        get_token_cache().invalidate(provider_id)

    return to_public(found, is_active=(settings.active_provider_id == provider_id))


def delete_provider(provider_id: str, *, hard: bool = False) -> None:
    settings = load_settings()
    if provider_id == settings.active_provider_id:
        raise ProviderError("不能删除当前激活的 Provider", code="CONFIG_INVALID")

    updated: list[ProviderProfile] = []
    found = False
    for p in settings.providers:
        if p.id != provider_id:
            updated.append(p)
            continue
        found = True
        if hard:
            continue
        updated.append(p.model_copy(update={"deleted": True, "enabled": False}))
    if not found:
        raise ProviderError(f"Provider 不存在: {provider_id}", code="MODEL_NOT_FOUND")

    settings = settings.model_copy(update={"providers": updated})
    save_settings(settings)
    if hard:
        config_service.delete_provider_secret(provider_id)
    get_token_cache().invalidate(provider_id)


def set_active_provider(provider_id: str) -> ProviderPublic:
    profile = get_provider(provider_id)
    if profile.deleted:
        raise ProviderError("不能激活已删除的 Provider", code="CONFIG_INVALID")
    settings = load_settings()
    settings = settings.model_copy(
        update={
            "active_provider_id": provider_id,
            "base_url": profile.base_url,
            "default_model": profile.default_model or settings.default_model,
            "api_key": load_provider_secret(provider_id).api_key,
        }
    )
    save_settings(settings)
    return to_public(profile, is_active=True)


async def test_connection(provider_id: str) -> ConnectionTestResult:
    """Phased connection test: config -> token -> models. Never runs paid generation."""
    stages: list[ConnectionTestStage] = []
    token_expires_at: str | None = None
    token_masked: str | None = None
    model_count: int | None = None

    try:
        profile = get_provider(provider_id)
    except ProviderError as e:
        stages.append(ConnectionTestStage(name="config", ok=False, message=e.message))
        return ConnectionTestResult(ok=False, stages=stages)

    # Stage 1: config
    if not profile.base_url.strip():
        stages.append(
            ConnectionTestStage(name="config", ok=False, message="base_url 未配置")
        )
        return ConnectionTestResult(ok=False, stages=stages)
    if profile.auth_type == "static_bearer":
        if not load_provider_secret(provider_id).api_key:
            stages.append(
                ConnectionTestStage(name="config", ok=False, message="API Key 未配置")
            )
            return ConnectionTestResult(ok=False, stages=stages)
    elif profile.auth_type == "token_distributor":
        if profile.token_distributor is None or not profile.token_distributor.base_url:
            stages.append(
                ConnectionTestStage(
                    name="config",
                    ok=False,
                    message="token_distributor.base_url 未配置",
                )
            )
            return ConnectionTestResult(ok=False, stages=stages)
    stages.append(ConnectionTestStage(name="config", ok=True, message="配置合法"))

    # Stage 2: token
    try:
        token = await get_access_token(profile, force_refresh=True)
        token_masked = mask_api_key(token.token)
        if token.expires_at is not None:
            from datetime import UTC, datetime

            token_expires_at = datetime.fromtimestamp(
                token.expires_at, tz=UTC
            ).isoformat()
        stages.append(
            ConnectionTestStage(name="token", ok=True, message="Token 获取成功")
        )
    except TokenError as e:
        stages.append(ConnectionTestStage(name="token", ok=False, message=e.message))
        return ConnectionTestResult(
            ok=False,
            stages=stages,
            token_masked=None,
            token_expires_at=None,
        )

    # Stage 3: models list
    try:
        from app.services import provider_client

        models = await provider_client.list_models_for_provider(profile)
        model_count = len(models)
        stages.append(
            ConnectionTestStage(
                name="models",
                ok=True,
                message=f"可访问模型列表（{model_count}）",
            )
        )
    except Exception as e:
        stages.append(
            ConnectionTestStage(name="models", ok=False, message=str(e)[:300])
        )
        return ConnectionTestResult(
            ok=False,
            stages=stages,
            token_expires_at=token_expires_at,
            token_masked=token_masked,
            model_count=None,
        )

    return ConnectionTestResult(
        ok=True,
        stages=stages,
        token_expires_at=token_expires_at,
        token_masked=token_masked,
        model_count=model_count,
    )


# Re-export for tests
__all__ = [
    "ProviderError",
    "list_providers",
    "get_provider",
    "get_active_provider",
    "to_public",
    "create_provider",
    "update_provider",
    "delete_provider",
    "set_active_provider",
    "test_connection",
]
