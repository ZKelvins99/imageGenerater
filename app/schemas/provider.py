from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ProviderType = Literal["openai_images", "openai_compatible_images"]
AuthType = Literal["static_bearer", "token_distributor"]
DistributorAuthMode = Literal["none", "bearer", "basic", "header"]


class TokenDistributorConfig(BaseModel):
    """Configurable company token distributor contract (no hardcoded company URLs)."""

    base_url: str = ""
    path: str = "/token"
    method: Literal["GET", "POST"] = "POST"
    auth_mode: DistributorAuthMode = "bearer"
    # When auth_mode == "header": name of the header that carries the secret
    auth_header_name: str = "X-Client-Secret"
    # Non-sensitive request body fields (scope, audience, model_group, ...)
    request_body: dict[str, Any] = Field(default_factory=dict)
    # Dot-separated JSON paths into the response payload
    token_path: str = "access_token"
    expires_in_path: str | None = "expires_in"
    expires_at_path: str | None = None
    token_type_path: str | None = "token_type"
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)


class ProviderProfile(BaseModel):
    id: str
    name: str = ""
    provider_type: ProviderType = "openai_compatible_images"
    base_url: str = ""
    auth_type: AuthType = "static_bearer"
    default_model: str = ""
    enabled: bool = True
    verify_tls: bool = True
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    capability_overrides: dict[str, Any] = Field(default_factory=dict)
    responses_enabled: bool = False
    responses_model: str = ""
    token_distributor: TokenDistributorConfig | None = None
    # Soft-delete flag; soft-deleted providers are hidden from selection
    deleted: bool = False


class ProviderSecret(BaseModel):
    api_key: str = ""
    distributor_client_id: str = ""
    distributor_client_secret: str = ""


class ProviderSecretUpdate(BaseModel):
    api_key: str | None = None
    distributor_client_id: str | None = None
    distributor_client_secret: str | None = None


class ProviderCreate(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1)
    provider_type: ProviderType = "openai_compatible_images"
    base_url: str = ""
    auth_type: AuthType = "static_bearer"
    default_model: str = ""
    enabled: bool = True
    verify_tls: bool = True
    timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    capability_overrides: dict[str, Any] = Field(default_factory=dict)
    responses_enabled: bool = False
    responses_model: str = ""
    token_distributor: TokenDistributorConfig | None = None
    # Secrets accepted on create; never echoed back
    api_key: str | None = None
    distributor_client_id: str | None = None
    distributor_client_secret: str | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    provider_type: ProviderType | None = None
    base_url: str | None = None
    auth_type: AuthType | None = None
    default_model: str | None = None
    enabled: bool | None = None
    verify_tls: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    extra_headers: dict[str, str] | None = None
    capability_overrides: dict[str, Any] | None = None
    responses_enabled: bool | None = None
    responses_model: str | None = None
    token_distributor: TokenDistributorConfig | None = None
    api_key: str | None = None
    distributor_client_id: str | None = None
    distributor_client_secret: str | None = None


class ProviderPublic(BaseModel):
    """Provider returned to the UI (secrets masked / flags only)."""

    id: str
    name: str
    provider_type: ProviderType
    base_url: str
    auth_type: AuthType
    default_model: str
    enabled: bool
    verify_tls: bool
    timeout_seconds: float
    extra_headers: dict[str, str]
    capability_overrides: dict[str, Any]
    responses_enabled: bool
    responses_model: str
    token_distributor: TokenDistributorConfig | None
    deleted: bool
    api_key_set: bool
    api_key_masked: str
    distributor_client_id_set: bool
    distributor_client_secret_set: bool
    is_active: bool = False


class ConnectionTestStage(BaseModel):
    name: str
    ok: bool
    message: str = ""


class ConnectionTestResult(BaseModel):
    ok: bool
    stages: list[ConnectionTestStage]
    token_expires_at: str | None = None
    token_masked: str | None = None
    model_count: int | None = None
