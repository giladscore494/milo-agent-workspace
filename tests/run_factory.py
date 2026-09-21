"""Create a run in tests the way Console 6 creates one in production.

Before Console 6 a test could insert a run row and claim it. A run now has an
IMMUTABLE IDENTITY that is established in the SAME transaction as the message
and the run, and a run without one is readable history that is never executed
or resumed -- so a helper that still creates the bare row is producing a run
the product itself would refuse to launch.

This mirrors `backend/main.py::_create_and_launch_run` exactly: the workflow
comes from the TRUSTED project relation (never from request metadata), the run
id is chosen before the insert so the identity can name the run it belongs to,
and every other dimension is a property of the running image.
"""

from typing import Any
from uuid import UUID, uuid4

from backend.run_identity import RunIdentity


def identity_kwargs(repo: Any, conversation_id: Any, *, run_id: UUID | None = None,
                    workflow_key: str | None = None) -> dict[str, Any]:
    """The `run_id` + `run_identity` pair the atomic creator requires.

    `workflow_key` is an override for the control-plane identities that do not
    come from a project workflow at all (`operator_capture`); leaving it unset
    reads the project relation, which is what an ordinary product run does.
    """
    run_id = run_id or uuid4()
    if workflow_key is None:
        project_id = repo.get_conversation(conversation_id)["project_id"]
        workflow_key = (repo.projects.get(str(project_id)) or {}).get("workflow_key")
    identity = RunIdentity.bind(run_id, str(workflow_key or ""))
    return {"run_id": run_id, "run_identity": identity.as_record()}
