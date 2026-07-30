from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

GenerationMode = Literal["generate", "reference", "edit_mask"]
Quality = Literal["low", "medium", "high", "auto"]
OutputFormat = Literal["png", "jpeg", "webp"]
BackgroundMode = Literal["auto", "opaque", "transparent"]
ModerationMode = Literal["auto", "low"]
InputFidelityMode = Literal["unsupported", "configurable", "always_high"]


class FlexibleSizeConstraints(BaseModel):
    max_edge: int = 3840
    multiple_of: int = 16
    max_aspect_ratio: float = 3.0
    min_pixels: int = 655_360
    max_pixels: int = 8_294_400


class ModelCapabilities(BaseModel):
    text_to_image: bool = True
    image_edit: bool = True
    multi_image_reference: bool = False
    mask_edit: bool = False
    max_input_images: int = 1
    max_input_bytes_each: int = 25 * 1024 * 1024
    max_input_bytes_total: int = 50 * 1024 * 1024
    supports_n: bool = True
    max_n: int = 4
    qualities: list[Quality] = Field(
        default_factory=lambda: list[Quality](["low", "medium", "high", "auto"])
    )
    # Fixed size presets; empty means use flexible constraints only
    sizes: list[str] = Field(default_factory=list)
    flexible_size_constraints: FlexibleSizeConstraints | None = None
    output_formats: list[OutputFormat] = Field(
        default_factory=lambda: list[OutputFormat](["png", "jpeg", "webp"])
    )
    supports_output_compression: bool = True
    background_modes: list[BackgroundMode] = Field(
        default_factory=lambda: list[BackgroundMode](["auto", "opaque"])
    )
    moderation_modes: list[ModerationMode] = Field(
        default_factory=lambda: list[ModerationMode](["auto", "low"])
    )
    supports_partial_images: bool = False
    max_partial_images: int = 0
    supports_responses_conversation: bool = False
    input_fidelity_mode: InputFidelityMode = "unsupported"


class SizeSpec(BaseModel):
    """Either auto or explicit width/height (or legacy 'WxH' string via parse)."""

    width: int | None = None
    height: int | None = None
    auto: bool = False

    @classmethod
    def parse(cls, value: str | dict[str, Any] | SizeSpec | None) -> SizeSpec:
        if value is None or value == "" or value == "auto":
            return cls(auto=True)
        if isinstance(value, SizeSpec):
            return value
        if isinstance(value, dict):
            if value.get("auto"):
                return cls(auto=True)
            return cls(width=int(value["width"]), height=int(value["height"]))
        if isinstance(value, str) and "x" in value.lower():
            w, h = value.lower().split("x", 1)
            return cls(width=int(w.strip()), height=int(h.strip()))
        raise ValueError(f"Invalid size: {value!r}")

    def to_api_value(self) -> str:
        if self.auto or self.width is None or self.height is None:
            return "auto"
        return f"{self.width}x{self.height}"


class GenerationRequest(BaseModel):
    provider_id: str | None = None
    mode: GenerationMode = "generate"
    prompt: str = Field(min_length=1)
    model: str
    input_asset_ids: list[str] = Field(default_factory=list)
    primary_asset_id: str | None = None
    mask_asset_id: str | None = None
    # Accept "auto", "1024x1024", or {"width":..,"height":..}
    size: str | dict[str, Any] | None = "auto"
    quality: Quality = "auto"
    n: int = Field(default=1, ge=1, le=10)
    output_format: OutputFormat = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    background: BackgroundMode | None = None
    moderation: ModerationMode = "auto"
    partial_images: int | None = Field(default=None, ge=0, le=3)
    seed: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Explicit unsupported params the user tried to set — used for CAPABILITY checks
    input_fidelity: str | None = None

    @model_validator(mode="after")
    def _mode_inputs(self) -> GenerationRequest:
        if self.mode == "generate" and self.input_asset_ids:
            # Allow but ignore for generate; prefer clear validation
            pass
        if self.mode == "reference" and not self.input_asset_ids:
            raise ValueError("reference 模式需要至少一张输入图")
        if self.mode == "edit_mask":
            if not self.input_asset_ids and not self.primary_asset_id:
                raise ValueError("edit_mask 模式需要主图")
            if not self.mask_asset_id:
                raise ValueError("edit_mask 模式需要 mask_asset_id")
        if self.primary_asset_id is None and self.input_asset_ids:
            self.primary_asset_id = self.input_asset_ids[0]
        return self

    def parsed_size(self) -> SizeSpec:
        return SizeSpec.parse(self.size)


class GeneratedImage(BaseModel):
    data: bytes
    mime: str
    extension: str
    width: int | None = None
    height: int | None = None
    byte_size: int = 0

    model_config = {"arbitrary_types_allowed": True}


class GenerationResult(BaseModel):
    images: list[GeneratedImage]
    upstream_request_id: str | None = None
    revised_prompt: str | None = None
    attempt_count: int = 1
    # Desensitized snapshot of params actually sent
    sent_params: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class GenerationCreateBody(BaseModel):
    """HTTP body for POST /api/v1/generations."""

    provider_id: str | None = None
    mode: GenerationMode = "generate"
    prompt: str = Field(min_length=1)
    model: str | None = None
    input_asset_ids: list[str] = Field(default_factory=list)
    primary_asset_id: str | None = None
    mask_asset_id: str | None = None
    size: str | dict[str, Any] | None = None
    quality: Quality | None = None
    n: int | None = Field(default=None, ge=1, le=10)
    output_format: OutputFormat | None = None
    output_compression: int | None = Field(default=None, ge=0, le=100)
    background: BackgroundMode | None = None
    moderation: ModerationMode | None = None
    partial_images: int | None = Field(default=None, ge=0, le=3)
    seed: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    input_fidelity: str | None = None
