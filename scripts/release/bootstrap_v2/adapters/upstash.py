"""Upstash adapter: REST API client with in-memory authorization.

Reads classify their outcome as a :class:`ProbeOutcome`; only a documented
not-found is absence. The single allowed mutation is database creation,
authorized through the mutation gate. Delete, reset, rename, token reset
and arbitrary selection do not exist in this adapter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from ..model import (
    OperationType,
    ProbeOutcome,
    ResourceIdentity,
    UpstashDatabaseState,
)
from ..subprocess_runner import MutationGate, SecretRegistry

API_BASE = "https://api.upstash.com"
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
class ProbeResult:
    outcome: ProbeOutcome
    detail: str = ""
    databases: tuple[UpstashDatabaseState, ...] = ()


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
    if status != 200:
        return ProbeOutcome.UNKNOWN_ERROR
    return None


def _parse_database(entry: object) -> UpstashDatabaseState | None:
    if not isinstance(entry, dict):
        return None
    database_id = entry.get("database_id", "")
    name = entry.get("database_name", "")
    if not isinstance(database_id, str) or not isinstance(name, str):
        return None
    if not database_id or not name:
        return None
    endpoint = entry.get("endpoint", "")
    tls = entry.get("tls", False)
    return UpstashDatabaseState(
        database_id=database_id,
        name=name,
        state=str(entry.get("state", "")),
        tls=bool(tls) if isinstance(tls, bool) else False,
        region=str(entry.get("region", "") or entry.get("primary_region", "")),
        endpoint=str(endpoint),
        rest_url=f"https://{endpoint}" if endpoint else "",
    )


class UpstashAdapter:
    def __init__(
        self,
        email: str,
        api_key: str,
        transport: Transport | None = None,
        secrets: SecretRegistry | None = None,
    ) -> None:
        self._transport = transport or default_transport
        token = base64.b64encode(f"{email}:{api_key}".encode("utf-8")).decode("ascii")
        self._auth_header = f"Basic {token}"
        self._secrets = secrets
        if secrets is not None:
            secrets.register(api_key)
            secrets.register(token)

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[ProbeOutcome | None, object, str]:
        headers = {
            "Authorization": self._auth_header,
            "Accept": "application/json",
        }
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        try:
            response = self._transport(method, f"{API_BASE}{path}", headers, body)
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

    def list_databases(self) -> ProbeResult:
        failure, parsed, detail = self._request("GET", "/v2/redis/databases")
        if failure is not None:
            return ProbeResult(outcome=failure, detail=detail)
        if not isinstance(parsed, list):
            return ProbeResult(
                outcome=ProbeOutcome.MALFORMED_OUTPUT,
                detail="list response is not a documented JSON array",
            )
        databases: list[UpstashDatabaseState] = []
        for entry in parsed:
            state = _parse_database(entry)
            if state is None:
                return ProbeResult(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="list entry missing required identity fields",
                )
            databases.append(state)
        return ProbeResult(outcome=ProbeOutcome.PRESENT, databases=tuple(databases))

    def get_database(self, database_id: str) -> tuple[ProbeResult, str]:
        """Detail read. Returns (probe, rest_token).

        The REST token stays in memory only: it is registered with the
        secret registry, its SHA-256 fingerprint is recorded on the state,
        and the raw value is returned separately so it never enters frozen
        evidence, reports, or logs.
        """

        failure, parsed, detail = self._request(
            "GET", f"/v2/redis/database/{database_id}"
        )
        if failure is not None:
            return ProbeResult(outcome=failure, detail=detail), ""
        state = _parse_database(parsed)
        if state is None:
            return (
                ProbeResult(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="detail response missing required identity fields",
                ),
                "",
            )
        rest_token = ""
        if isinstance(parsed, dict):
            raw_token = parsed.get("rest_token", "")
            if isinstance(raw_token, str) and raw_token:
                rest_token = raw_token
                if self._secrets is not None:
                    self._secrets.register(rest_token)
                state = UpstashDatabaseState(
                    database_id=state.database_id,
                    name=state.name,
                    state=state.state,
                    tls=state.tls,
                    region=state.region,
                    endpoint=state.endpoint,
                    rest_url=state.rest_url,
                    token_fingerprint_sha256=hashlib.sha256(
                        rest_token.encode("utf-8")
                    ).hexdigest(),
                )
        return ProbeResult(outcome=ProbeOutcome.PRESENT, databases=(state,)), rest_token

    # ---- the single allowed mutation ----------------------------------------

    def create_database(
        self,
        gate: MutationGate,
        idempotency_key: str,
        resource: ResourceIdentity,
        name: str,
        region: str,
    ) -> tuple[ProbeResult, str]:
        """Create exactly one production database. Returns (probe, created_id)."""

        operation = gate.authorize(
            OperationType.UPSTASH_CREATE_DATABASE, resource, idempotency_key
        )
        failure, parsed, detail = self._request(
            "POST",
            "/v2/redis/database",
            {"name": name, "region": region, "tls": True},
        )
        if failure is not None:
            gate.record_execution(operation, succeeded=False, error_class=failure.value)
            return ProbeResult(outcome=failure, detail=detail), ""
        state = _parse_database(parsed)
        if state is None:
            gate.record_execution(
                operation, succeeded=False, error_class="malformed_create_response"
            )
            return (
                ProbeResult(
                    outcome=ProbeOutcome.MALFORMED_OUTPUT,
                    detail="create response missing required identity fields",
                ),
                "",
            )
        gate.record_execution(operation, succeeded=True)
        return ProbeResult(outcome=ProbeOutcome.PRESENT, databases=(state,)), state.database_id
