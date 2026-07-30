"""Start the local ImageGenerater server.

Usage (with uv):
  uv venv
  uv pip install -r requirements.txt
  .venv\\Scripts\\python run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import main

if __name__ == "__main__":
    main()
