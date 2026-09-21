"""The worker ownership contract every run-owned write now states.

Console 6 made `run + worker + attempt + lease` part of the evidence request
schemas themselves (`backend/schemas.py::WorkerLeaseFence`), because identity
only answers "is this a worker?" and the lease answers "is this THE worker of
THIS attempt of THIS run?".

A test that builds one of those payloads by hand states the contract the same
way a real worker does. The values here are not the authority: `EvidenceBoard`
writes under the lease it is actually executing with, and strips the fence from
the payload before it is stored -- `lease_token` is a credential, and the
guarded RPCs refuse any row carrying one. So a payload built in a test only has
to state a WELL-FORMED contract, not a privileged one.
"""

from typing import Any


def worker_fence(worker_id: str = "worker-1", attempt: int = 1,
                 lease_token: str = "lease-token") -> dict[str, Any]:
    """The three fence fields, as keyword arguments for a request schema."""
    return {"worker_id": worker_id, "attempt": attempt, "lease_token": lease_token}


#: The default contract, for the many helpers that build one payload shape.
FENCE = worker_fence()
