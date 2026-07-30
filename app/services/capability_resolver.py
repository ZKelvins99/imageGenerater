from __future__ import annotations

from typing import Any

from app.schemas.generation import (
    FlexibleSizeConstraints,
    GenerationRequest,
    ModelCapabilities,
    SizeSpec,
)
from app.schemas.provider import ProviderProfile

# Official-ish defaults for gpt-image-2 (Phase 3 baseline table)
GPT_IMAGE_2_CAPABILITIES = ModelCapabilities(
    text_to_image=True,
    image_edit=True,
    multi_image_reference=True,
    mask_edit=True,
    max_input_images=16,
    max_input_bytes_each=25 * 1024 * 1024,
    max_input_bytes_total=50 * 1024 * 1024,
    supports_n=True,
    max_n=4,
    qualities=["low", "medium", "high", "auto"],
    sizes=[],  # flexible only
    flexible_size_constraints=FlexibleSizeConstraints(),
    output_formats=["png", "jpeg", "webp"],
    supports_output_compression=True,
    background_modes=["auto", "opaque"],  # no transparent
    moderation_modes=["auto", "low"],
    supports_partial_images=True,
    max_partial_images=3,
    supports_responses_conversation=True,
    input_fidelity_mode="unsupported",
)

CONSERVATIVE_CAPABILITIES = ModelCapabilities(
    text_to_image=True,
    image_edit=True,
    multi_image_reference=False,
    mask_edit=False,
    max_input_images=1,
    supports_n=True,
    max_n=4,
    qualities=["low", "medium", "high", "auto"],
    sizes=["1024x1024", "1536x1024", "1024x1536", "auto"],
    flexible_size_constraints=None,
    output_formats=["png"],
    supports_output_compression=False,
    background_modes=["auto", "opaque"],
    moderation_modes=["auto"],
    supports_partial_images=False,
    max_partial_images=0,
    supports_responses_conversation=False,
    input_fidelity_mode="unsupported",
)

BUILTIN: dict[str, ModelCapabilities] = {
    "gpt-image-2": GPT_IMAGE_2_CAPABILITIES,
}


class CapabilityError(Exception):
    def __init__(self, message: str, code: str = "CAPABILITY_UNSUPPORTED"):
        super().__init__(message)
        self.message = message
        self.code = code


def resolve_capabilities(
    model: str,
    profile: ProviderProfile | None = None,
) -> ModelCapabilities:
    mid = (model or "").strip().lower()
    caps = BUILTIN.get(mid, CONSERVATIVE_CAPABILITIES).model_copy(deep=True)
    if profile and profile.capability_overrides:
        # Shallow override of known fields only
        data = caps.model_dump()
        for k, v in profile.capability_overrides.items():
            if k in data and v is not None:
                data[k] = v
        caps = ModelCapabilities.model_validate(data)
    return caps


def validate_flexible_size(
    spec: SizeSpec, constraints: FlexibleSizeConstraints
) -> None:
    if spec.auto:
        return
    if spec.width is None or spec.height is None:
        raise CapabilityError("自定义尺寸需要 width 和 height", code="INPUT_INVALID")
    w, h = spec.width, spec.height
    if w <= 0 or h <= 0:
        raise CapabilityError("尺寸必须为正整数", code="INPUT_INVALID")
    if max(w, h) > constraints.max_edge:
        raise CapabilityError(
            f"最长边不得超过 {constraints.max_edge}px",
            code="INPUT_INVALID",
        )
    if w % constraints.multiple_of or h % constraints.multiple_of:
        raise CapabilityError(
            f"宽高必须是 {constraints.multiple_of} 的倍数",
            code="INPUT_INVALID",
        )
    ratio = max(w, h) / min(w, h)
    if ratio > constraints.max_aspect_ratio + 1e-9:
        raise CapabilityError(
            f"长短边比例不得超过 {constraints.max_aspect_ratio}:1",
            code="INPUT_INVALID",
        )
    pixels = w * h
    if pixels < constraints.min_pixels or pixels > constraints.max_pixels:
        raise CapabilityError(
            f"总像素须在 {constraints.min_pixels}–{constraints.max_pixels} 之间",
            code="INPUT_INVALID",
        )


def validate_request(
    req: GenerationRequest,
    caps: ModelCapabilities,
) -> dict[str, Any]:
    """Validate request against capabilities. Returns desensitized send plan.

    Raises CapabilityError for explicit unsupported params (never silently drop).
    """
    if req.mode == "generate" and not caps.text_to_image:
        raise CapabilityError("当前模型不支持文生图")
    if req.mode == "reference":
        if not caps.image_edit:
            raise CapabilityError("当前模型不支持图编辑/参考合成")
        n_in = len(req.input_asset_ids)
        if n_in > 1 and not caps.multi_image_reference:
            raise CapabilityError("当前模型不支持多图参考")
        if n_in > caps.max_input_images:
            raise CapabilityError(
                f"输入图不得超过 {caps.max_input_images} 张",
                code="INPUT_INVALID",
            )
    if req.mode == "edit_mask":
        if not caps.mask_edit:
            raise CapabilityError("当前模型不支持蒙版编辑")
        if not caps.image_edit:
            raise CapabilityError("当前模型不支持图编辑")
        n_in = len(req.input_asset_ids) or (1 if req.primary_asset_id else 0)
        if n_in > caps.max_input_images:
            raise CapabilityError(
                f"输入图不得超过 {caps.max_input_images} 张",
                code="INPUT_INVALID",
            )

    if req.input_fidelity is not None:
        if caps.input_fidelity_mode == "unsupported":
            raise CapabilityError(
                "当前模型不支持 input_fidelity（gpt-image-2 固定高保真）"
            )

    if not caps.supports_n and req.n != 1:
        raise CapabilityError("当前模型不支持 n>1")
    if req.n > caps.max_n:
        raise CapabilityError(f"n 不得超过 {caps.max_n}", code="INPUT_INVALID")

    if req.quality not in caps.qualities:
        raise CapabilityError(f"不支持的 quality: {req.quality}")

    if req.output_format not in caps.output_formats:
        raise CapabilityError(f"不支持的 output_format: {req.output_format}")

    if req.output_compression is not None:
        if not caps.supports_output_compression:
            raise CapabilityError("当前模型不支持 output_compression")
        if req.output_format == "png":
            raise CapabilityError("PNG 不支持 output_compression", code="INPUT_INVALID")

    if req.background is not None:
        if req.background not in caps.background_modes:
            raise CapabilityError(f"不支持的 background: {req.background}")

    if req.moderation not in caps.moderation_modes:
        raise CapabilityError(f"不支持的 moderation: {req.moderation}")

    if req.partial_images is not None:
        if not caps.supports_partial_images:
            raise CapabilityError("当前模型不支持 partial_images")
        if req.partial_images > caps.max_partial_images:
            raise CapabilityError(
                f"partial_images 不得超过 {caps.max_partial_images}",
                code="INPUT_INVALID",
            )

    if req.seed is not None:
        # Only when explicitly supported via override; default table has no seed
        if not (caps.model_dump().get("supports_seed")):
            raise CapabilityError("当前模型不支持 seed")

    size = req.parsed_size()
    if caps.flexible_size_constraints is not None:
        validate_flexible_size(size, caps.flexible_size_constraints)
    elif not size.auto:
        token = size.to_api_value()
        allowed = set(caps.sizes) | {"auto"}
        if token not in allowed:
            raise CapabilityError(
                f"不支持的尺寸 {token}；允许: {sorted(allowed)}",
                code="INPUT_INVALID",
            )

    plan: dict[str, Any] = {
        "mode": req.mode,
        "model": req.model,
        "size": size.to_api_value(),
        "quality": req.quality,
        "n": req.n,
        "output_format": req.output_format,
        "moderation": req.moderation,
    }
    if req.output_compression is not None:
        plan["output_compression"] = req.output_compression
    if req.background is not None:
        plan["background"] = req.background
    if req.partial_images is not None:
        plan["partial_images"] = req.partial_images
    # Never include input_fidelity for unsupported models
    return plan
