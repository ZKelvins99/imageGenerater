"""Filesystem layout for dev vs. packaged (PyInstaller) runs.

Two distinct concerns live here:

* ``bundle_dir`` — read-only assets (``static/``, ``templates/``) that ship
  *inside* the executable.  Under PyInstaller these are unpacked into
  ``sys._MEIPASS``; in development they live in the project root.

* ``base_dir`` — writable state (``config/``, ``data/`` with the SQLite DB and
  generated images).  In development this is the project root so behaviour is
  unchanged; when frozen it moves to the per-user data directory so the app
  never writes next to the executable.

Both may be overridden via env vars for portable/testing installs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ImageGenerater"

# Env overrides (used by tests and portable installs).
ENV_BASE_DIR = "IMAGEGENERATER_DATA_DIR"


def is_frozen() -> bool:
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Read-only directory containing packaged ``static/`` and ``templates/``."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def base_dir() -> Path:
    """Writable directory holding ``config/`` and ``data/``."""
    override = os.environ.get(ENV_BASE_DIR)
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        import platformdirs

        return Path(
            platformdirs.user_data_dir(APP_NAME, appauthor=APP_NAME, roaming=False)
        )
    return Path(__file__).resolve().parents[1]


CONFIG_DIR = base_dir() / "config"
DATA_DIR = base_dir() / "data"
