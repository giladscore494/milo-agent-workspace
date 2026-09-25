"""The SETOF wrapper RPCs depend on service_role EXECUTE on their base RPCs.

Moved unchanged from tests/test_stage_c_toolkit.py when the consumed Stage C
toolkit was deleted (cleanup item 6): it pins two applied migrations, not the
toolkit.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_setof_wrapper_base_rpc_acl_dependencies_are_pinned():
    """SECURITY INVOKER wrappers require service_role EXECUTE on their base RPCs."""
    migrations = REPO / "supabase" / "migrations"
    setof = (migrations / "20260810000400_setof_rpc_returns.sql").read_text()
    acl = (migrations / "20260818000100_stage_c_service_role_rpc_acl.sql").read_text()

    dependencies = {
        "create_message_and_run_v2": (
            "public.create_message_and_run(",
            "public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer)",
        ),
        "create_project_from_proposal_with_owner_v2": (
            "public.create_project_from_proposal_with_owner(",
            "public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid)",
        ),
    }

    assert "SECURITY DEFINER" not in setof.upper()

    for wrapper, (base_call, signature) in dependencies.items():
        assert f"function public.{wrapper}(" in setof
        assert f"return next {base_call}" in setof
        assert signature in acl
        assert "grant execute on function %s to service_role" in acl
