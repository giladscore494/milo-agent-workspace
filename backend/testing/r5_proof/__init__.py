"""R5 proof-only server code: pinned fixtures, read-only tools, trusted mappers.

Shipped, reviewable server code that is NEVER wired into a production path --
the same posture as backend/testing/memory_repository.py, e2e_app.py and
evidence_mappers.py. The production ToolRegistry stays empty and
The production evidence-mapper allowlist names one Government operation and
nothing from this package; nothing here is registered
in either, and nothing here can be reached from a production run.
"""
