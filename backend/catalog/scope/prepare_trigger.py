"""E': executing the EXISTING capture job for ONE plan revision, from the API.

What this is, and what it is not
--------------------------------

The API's Prepare route (`web_preparation.py`) must start exactly the
execution the operator's `government-production-capture.sh
--prepare-work-scope` starts: the capture Cloud Run job, with the scoped
entrypoint arguments and the ONE per-execution override that turns the
scoped-preparation switch on. Both build that execution from
`backend/capture_invocation.py`; this module only sends it.

It is the Cloud Run Admin API (v2) equivalent of `gcloud run jobs execute
--args=... --update-env-vars ...`: `jobs.run` with
`overrides.containerOverrides[0].args` (which REPLACE the job's args, exactly
like gcloud's `--args`, so the module is restated first) and `.env`.

Identity: Application Default Credentials on the API's own service account --
no key file is read, stored or accepted. That account holds
`roles/run.jobsExecutorWithOverrides` on THIS one job only (granted by
`government-production-capture.sh --ensure-job`), plus the same role it
already holds on the product worker job, and `roles/run.viewer` on those two
jobs only (`website-execution-activate.sh --apply-web-preparation`): the
executor role does not carry `run.jobs.get`, and `release_refusal` GETs both
jobs. Without the read, every Prepare is refused `JOB_UNREADABLE`.

Fail closed on the image
------------------------

Before anything runs, the capture job must run the DEPLOYED RELEASE image:
the product worker job's image, tagged with this API's own
`MILO_RELEASE_SHA`, and the capture job must state that same release. A
capture job left on an earlier release -- whose operator entrypoint and
queue build may differ -- is refused (`WORK_SCOPE_PREPARATION_JOB_NOT_RELEASE`),
and so is a job this API cannot read (`WORK_SCOPE_PREPARATION_JOB_UNREADABLE`).

Nothing here reads an execution's outcome: the website's status is derived
from durable database state only (`web_preparation.derive_status`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from backend.capture_invocation import Invocation

#: What a trigger answers. `triggered`: Cloud Run accepted the execution.
#: `trigger_failed`: it DEFINITELY did not (an error response). `trigger_unknown`:
#: the request may have reached Cloud Run (a timeout, a reset) -- an execution
#: may exist, so the attempt is treated as in flight until the grace says it
#: never started.
TRIGGERED = "triggered"
TRIGGER_FAILED = "trigger_failed"
TRIGGER_UNKNOWN = "trigger_unknown"

#: Why a trigger refuses before anything is executed.
JOB_NOT_RELEASE = "WORK_SCOPE_PREPARATION_JOB_NOT_RELEASE"
JOB_UNREADABLE = "WORK_SCOPE_PREPARATION_JOB_UNREADABLE"

_EXECUTION = re.compile(r"^[a-z]([-a-z0-9]{0,126}[a-z0-9])?$")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]{0,299}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class TriggerOutcome:
    state: str
    execution_name: str | None = None


class CaptureJobTrigger(Protocol):
    def release_refusal(self) -> str | None: ...
    def run(self, invocation: Invocation) -> TriggerOutcome: ...


def _container(job: Any) -> Mapping[str, Any]:
    """`template.template.containers[0]` of a v2 Job resource, or {}."""
    template = (job or {}).get("template") if isinstance(job, Mapping) else None
    inner = template.get("template") if isinstance(template, Mapping) else None
    containers = inner.get("containers") if isinstance(inner, Mapping) else None
    if isinstance(containers, list) and containers and isinstance(containers[0], Mapping):
        return containers[0]
    return {}


def release_refusal_for(capture_job: Any, worker_job: Any, release_sha: str) -> str | None:
    """Pure: whether the capture job runs exactly the deployed release image."""
    if not _SHA.fullmatch(release_sha or ""):
        return JOB_NOT_RELEASE
    capture, worker = _container(capture_job), _container(worker_job)
    image, released = capture.get("image"), worker.get("image")
    if not isinstance(image, str) or not isinstance(released, str) or not image:
        return JOB_UNREADABLE
    env = {entry.get("name"): entry.get("value") for entry in capture.get("env") or []
           if isinstance(entry, Mapping)}
    if image != released or not released.endswith(f"/worker:{release_sha}") \
            or env.get("MILO_RELEASE_SHA") != release_sha:
        return JOB_NOT_RELEASE
    return None


def run_request_body(invocation: Invocation) -> dict[str, Any]:
    """The `jobs.run` body for one invocation: args REPLACE the job's own."""
    return {"overrides": {"containerOverrides": [{
        "args": list(invocation.container_args),
        "env": [{"name": name, "value": value} for name, value in invocation.env_overrides],
    }]}}


def execution_name_from(response: Any) -> str | None:
    """The execution (short name) a `jobs.run` operation names, else the
    operation's own name, else None. Never free text."""
    if not isinstance(response, Mapping):
        return None
    metadata = response.get("metadata")
    name = metadata.get("name") if isinstance(metadata, Mapping) else None
    if isinstance(name, str) and "/executions/" in name:
        short = name.rsplit("/", 1)[-1]
        if _EXECUTION.fullmatch(short):
            return short
    operation = response.get("name")
    return operation if isinstance(operation, str) and _OPERATION.fullmatch(operation) else None


class CloudRunCaptureJobTrigger:
    """The real trigger: Cloud Run Admin API v2 over ADC. No key file."""

    TIMEOUT_SECONDS = 15

    def __init__(self, *, project: str, region: str, capture_job: str, worker_job: str,
                 release_sha: str, session_factory: Callable[[], Any] | None = None) -> None:
        self.project, self.region = project, region
        self.capture_job, self.worker_job = capture_job, worker_job
        self.release_sha = release_sha
        self._session_factory = session_factory

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(credentials)

    def _job_url(self, job: str) -> str:
        return (f"https://run.googleapis.com/v2/projects/{self.project}/locations/"
                f"{self.region}/jobs/{job}")

    def release_refusal(self) -> str | None:
        try:
            session = self._session()
            documents = []
            for job in (self.capture_job, self.worker_job):
                response = session.get(self._job_url(job), timeout=self.TIMEOUT_SECONDS)
                if response.status_code >= 400:
                    return JOB_UNREADABLE
                documents.append(response.json())
        except Exception:
            return JOB_UNREADABLE
        return release_refusal_for(documents[0], documents[1], self.release_sha)

    def run(self, invocation: Invocation) -> TriggerOutcome:
        try:
            session = self._session()
        except Exception:
            return TriggerOutcome(TRIGGER_FAILED)
        try:
            response = session.post(self._job_url(self.capture_job) + ":run",
                                    data=json.dumps(run_request_body(invocation)),
                                    headers={"Content-Type": "application/json"},
                                    timeout=self.TIMEOUT_SECONDS)
        except Exception:
            # The request may have reached Cloud Run: never retried here.
            return TriggerOutcome(TRIGGER_UNKNOWN)
        if response.status_code >= 400:
            return TriggerOutcome(TRIGGER_FAILED)
        try:
            body = response.json() if response.content else {}
        except Exception:
            body = {}
        return TriggerOutcome(TRIGGERED, execution_name_from(body))


def build_capture_trigger(settings: Any, env: Mapping[str, str], *,
                          job_setting: str = "cloud_run_capture_job") -> CaptureJobTrigger | None:
    """The trigger this API is configured for, or None (it can prepare nothing).
    PR-D3: job_setting="cloud_run_normalisation_job" is the normalisation job's."""
    job = str(getattr(settings, job_setting, "") or "").strip()
    if not job:
        return None
    return CloudRunCaptureJobTrigger(
        project=settings.gcp_project_id, region=settings.gcp_region, capture_job=job,
        worker_job=settings.cloud_run_worker_job,
        release_sha=str(env.get("MILO_RELEASE_SHA") or "").strip())


__all__ = ["CaptureJobTrigger", "CloudRunCaptureJobTrigger", "JOB_NOT_RELEASE", "JOB_UNREADABLE",
           "TRIGGERED", "TRIGGER_FAILED", "TRIGGER_UNKNOWN", "TriggerOutcome",
           "build_capture_trigger", "execution_name_from", "release_refusal_for",
           "run_request_body"]
