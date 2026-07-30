from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEXT_GLOBS = (
    "*.py",
    "*.js",
    "*.css",
    "*.html",
    "*.md",
    "*.json",
    "*.txt",
    "*.toml",
    "*.yml",
    "*.yaml",
)

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".codegraph",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "data",
}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {
            ".py",
            ".js",
            ".css",
            ".html",
            ".md",
            ".json",
            ".txt",
            ".toml",
            ".yml",
            ".yaml",
        } or path.name in {".gitignore"}:
            files.append(path)
    return files


def test_source_files_are_utf8() -> None:
    bad: list[str] = []
    for path in _iter_text_files():
        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(str(path.relative_to(ROOT)))
    assert bad == [], f"non-UTF-8 files: {bad}"
