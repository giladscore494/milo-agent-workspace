"""R5 proof-only server code: pinned fixtures, read-only tools, trusted mappers.

Shipped, reviewable server code that is NEVER wired into a production path --
the same posture as backend/testing/memory_repository.py, e2e_app.py and
evidence_mappers.py. The production ToolRegistry stays empty and
PRODUCTION_EVIDENCE_MAPPERS stays empty; nothing in this package is registered
in either, and nothing here can be reached from a production run.
"""
