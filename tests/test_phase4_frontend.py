from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


def test_workspace_exposes_all_generation_modes() -> None:
    assert 'x-data="imageStudio()"' in TEMPLATE
    assert "文生图" in TEMPLATE
    assert "参考合成" in TEMPLATE
    assert "局部编辑" in TEMPLATE
    assert "inputAssets" in TEMPLATE
    assert "maskAsset" in TEMPLATE


def test_workspace_uses_stable_v1_apis() -> None:
    required_paths = (
        "/api/v1/providers",
        "/api/v1/assets",
        "/api/v1/generations",
        "/api/v1/jobs",
        "/capabilities",
    )
    for path in required_paths:
        assert path in SCRIPT


def test_capability_driven_controls_and_task_recovery_exist() -> None:
    assert "loadCapabilities" in SCRIPT
    assert "normalizeOptionsToCapabilities" in SCRIPT
    assert "maxInputImages" in SCRIPT
    assert "restoreActiveJob" in SCRIPT
    assert "ig:activeJob" in SCRIPT
    assert "cancelActiveJob" in SCRIPT


def test_provider_secrets_are_not_loaded_into_visible_state() -> None:
    assert 'api_key: ""' in SCRIPT
    assert 'distributor_client_secret: ""' in SCRIPT
    assert "已设置，留空不修改" in TEMPLATE


def test_responsive_and_accessibility_baseline() -> None:
    assert "@media (max-width: 980px)" in STYLES
    assert "@media (max-width: 560px)" in STYLES
    assert "@media (prefers-reduced-motion: reduce)" in STYLES
    assert 'aria-label="创作模式"' in TEMPLATE
    assert 'aria-live="polite"' in TEMPLATE
