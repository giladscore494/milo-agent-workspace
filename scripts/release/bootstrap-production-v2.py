#!/usr/bin/env python3
"""Thin module launcher for MILO bootstrap v2.

All orchestration, planning, parsing, result handling and provider access
live in scripts/release/bootstrap_v2/. This file only resolves the import
path and delegates to the package CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from bootstrap_v2.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
