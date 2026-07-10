"""Compatibility API entrypoint.

This module intentionally re-exports the canonical FastAPI app from
``backend.main`` so there is no alternate app with weaker authentication or
authorization behavior.
"""

from backend.main import app

__all__ = ["app"]
