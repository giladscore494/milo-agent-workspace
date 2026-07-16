"""Vercel adapter: REST API client with in-memory authorization.

Reads classify outcomes; the only mutation is setting one environment
variable, gated through the frozen plan. There is no deploy, promote,
redeploy, link, unlink, environment-remove, or ``--prod`` capability in
this adapter, by construction.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ..validators.redis import fingerprint_sha256

from ..model import (
    OperationType,
    ProbeOutcome,
    ResourceIdentity,
    VercelEnvVarState,
    VercelProjectState,
)
from ..subprocess_runner import MutationGate, SecretRegistry

API_BASE = "https://api.vercel.com"
MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def default_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return HttpResponse(
                status=response.status, body=response.read(MAX_RESPONSE_BYTES + 1)
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read(MAX_RESPONSE_BYTES + 1))


@dataclass(frozen=True, slots=True)
class ProjectProbe:
    outcome: ProbeOutcome
    detail: str = ""
    project: VercelProjectState | None = None


@dataclass(frozen=True, slots=True)
class EnvProbe:
    outcome: ProbeOutcome
    detail: str = ""
    env_vars: tuple[VercelEnvVarState, ...] = ()


_fingerprint = fingerprint_sha256


def _classify_status(status: int, allow_not_found: bool) -> ProbeOutcome | None:
    if status == 401:
        return ProbeOutcome.AUTH_FAILURE
    if status == 403:
        return ProbeOutcome.PERMISSION_DENIED
    if status == 404:
        return ProbeOutcome.CLEANLY_ABSENT if allow_not_found else ProbeOutcome.UNKNOWN_ERROR
    if status == 429:
        return ProbeOutcome.RATE_LIMITED
    if 500 <= status < 600:
        return ProbeOutcome.UNKNOWN_ERROR
    if status not in (200, 201):
        return ProbeOutcome.UNKNOWN_ERROR
    return None


class VercelAdapter:
    def __init__(
        self,
        token: str,
        team_id: str,
        transport: Transport | None = None,
        secrets: SecretRegistry | None = None,
    ) -> None:
        self._transport = transport or default_transport
        self._auth_header = f"Bearer {token}"
        self._team_id = team_id
        if secrets is not None:
            secrets.register(token)

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[ProbeOutcome | None, object, str]:
        separator = "&" if "?" in path else "?"
        url = f"{API_BASE}{path}{separator}teamId={self._team_id}"
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            response = self._transport(method, url, headers, body)
        except TimeoutError:
            return ProbeOutcome.TIMEOUT, None, "request timed out"
        except OSError as exc:
            return ProbeOutcome.NETWORK_FAILURE, None, f"network failure: {exc.__class__.__name__}"
        if len(response.body) > MAX_RESPONSE_BYTES:
            return ProbeOutcome.MALFORMED_OUTPUT, None, "response exceeds size cap"
        failure = _classify_status(response.status, allow_not_found=method == "GET")
        if failure is not None:
            return failure, None, f"http status {response.status}"
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return ProbeOutcome.MALFORMED_OUTPUT, None, "response is not valid JSON"
        return None, parsed, ""

    # ---- reads -------------------------------------------------------------

    def get_project(self, project_id: str) -> ProjectProbe:
        failure, parsed, detail = self._request("GET", f"/v9/projects/{project_id}")
        if failure is not None:
            return ProjectProbe(outcome=failure, detail=detail)
        if not isinstance(parsed, dict):
            return ProjectProbe(
                outcome=ProbeOutcome.MALFORMED_OUTPUT, detail="project response not an object"
            )
        pid = parsed.get("id", "")
        name = parsed.get("name", "")
        org = parsed.get("accountId", "")
        if not (isinstance(pid, str) and isinstance(name, str) and isinstance(org, str)) or not pid:
            return ProjectProbe(
                outcome=ProbeOutcome.MALFORMED_OUTPUT,
                detail="project response missing identity fields",
            )
        return ProjectProbe(
            outcome=ProbeOutcome.PRESENT,
            project=VercelProjectState(project_id=pid, org_id=org, name=name),
        )

    def list_env(self, project_id: str) -> EnvProbe:
        failure, parsed, detail = self._request(
            "GET", f"/v10/projects/{project_id}/env?decrypt=true"
        )
        if failure is not None:
            return EnvProbe(outcome=failure, detail=detail)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("envs"), list):
            return EnvProbe(
                outcome=ProbeOutcome.MALFORMED_OUTPUT,
                detail="env list response missing documented 'envs' array",
            )
        env_vars: list[VercelEnvVarState] = []
        for entry in parsed["envs"]:
            if not isinstance(entry, dict):
                return EnvProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT, detail="env entry not an object"
                )
            key = entry.get("key", "")
            if not isinstance(key, str) or not key:
                return EnvProbe(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT, detail="env entry missing key"
                )
            target = entry.get("target", [])
            if isinstance(target, str):
                # Vercel returns a bare string for single-target variables.
                targets: tuple[str, ...] = (target,)
            elif isinstance(target, list):
                targets = tuple(str(t) for t in target)
            else:
                targets = ()
            value = entry.get("value", "")
            fingerprint = _fingerprint(value) if isinstance(value, str) and value else ""
            env_vars.append(
                VercelEnvVarState(
                    key=key,
                    target=targets,
                    value_fingerprint_sha256=fingerprint,
                    env_var_id=str(entry.get("id", "")),
                )
            )
        return EnvProbe(outcome=ProbeOutcome.PRESENT, env_vars=tuple(env_vars))

    # ---- the single allowed mutation ----------------------------------------

    def set_env_var(
        self,
        gate: MutationGate,
        idempotency_key: str,
        resource: ResourceIdentity,
        project_id: str,
        key: str,
        value: str,
        secret: bool,
    ) -> tuple[ProbeOutcome, str]:
        """Upsert one production env var. Value stays in memory only."""

        operation = gate.authorize(
            OperationType.VERCEL_SET_ENV_VAR, resource, idempotency_key
        )
        payload: dict[str, object] = {
            "key": key,
            "value": value,
            "type": "encrypted" if secret else "plain",
            "target": ["production"],
        }
        failure, _, detail = self._request(
            "POST", f"/v10/projects/{project_id}/env?upsert=true", payload
        )
        if failure is not None:
            gate.record_execution(operation, succeeded=False, error_class=failure.value)
            return failure, detail
        gate.record_execution(operation, succeeded=True)
        return ProbeOutcome.PRESENT, ""
