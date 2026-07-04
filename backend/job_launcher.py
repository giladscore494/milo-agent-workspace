from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.config import Settings
from backend.errors import AppError


class JobLauncher(Protocol):
    def launch(self, run_id: UUID) -> dict[str, str]: ...


@dataclass
class DisabledJobLauncher:
    reason: str = "Cloud Run Job launch disabled for local/offline environment"

    def launch(self, run_id: UUID) -> dict[str, str]:
        return {"mode": "disabled", "run_id": str(run_id), "reason": self.reason}


class CloudRunJobLauncher:
    """Invoke a Cloud Run Job execution using Application Default Credentials.

    This uses metadata/ADC at runtime and never requires or reads a service-account JSON key.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def launch(self, run_id: UUID) -> dict[str, str]:
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise AppError("JOB_LAUNCHER_UNAVAILABLE", "google-auth is required to launch Cloud Run Jobs", 500) from exc

        project = self.settings.gcp_project_id
        region = self.settings.gcp_region
        job = self.settings.cloud_run_worker_job
        url = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run"
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials)
        body = {"overrides": {"containerOverrides": [{"env": [{"name": "RUN_ID", "value": str(run_id)}]}]}}
        response = session.post(url, data=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=15)
        if response.status_code >= 400:
            raise AppError("JOB_LAUNCH_FAILED", f"Cloud Run Job launch failed with HTTP {response.status_code}", 502)
        data = response.json() if response.content else {}
        return {"mode": "cloud_run_job", "run_id": str(run_id), "execution": data.get("name", "")}


def build_job_launcher(settings: Settings) -> JobLauncher:
    if settings.job_launcher == "cloud_run":
        return CloudRunJobLauncher(settings)
    return DisabledJobLauncher()
