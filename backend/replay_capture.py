"""Production replay: the fixture format, its sanitizer, and the opt-in capture.

Why this exists
---------------

Every Swarm V2 failure so far was a SHAPE a real model produced that the
offline suite had never seen: an output schema the runtime could not enforce
(280fc9e5), a completion criterion naming an output the schema never declared
(6825eb96), a replan decision the contract refused (c4b8bb54). The replay
harness (`tests/replay_harness.py`) runs the whole engine offline against
those recorded outputs, so a shape a model already produced can never fail a
run again unnoticed.

This module holds the parts that are NOT test-only:

* the fixture format (`REPLAY_FORMAT`) and the one sanitizer every fixture and
  every export passes (`sanitization_findings`);
* the opt-in CAPTURE. Behind `MILO_CAPTURE_REPLAY` (default OFF, pinned off by
  every deploy script, enforced by `scripts/check_unsafe_defaults.py`) the
  worker records the INERT provider outputs of one Swarm V2 run -- the answer
  `content` and `finish_reason` of each completion, never `reasoning_content`
  -- and the Registry-validated Government tool results, into a bounded
  per-run artifact carried on the run's own checkpoints (`run_checkpoints`,
  service-only under RLS: no browser can read it). An operator exports it with
  `scripts/export_replay_capture.py`.

Off means OFF: `capture_enabled()` is read once at engine construction and,
when false, no recorder exists, nothing is wrapped and nothing is written.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Callable, Iterable, Mapping

#: The flag. Default off, and off for every value that is not an explicit
#: truthy spelling (the repository's shared convention).
CAPTURE_FLAG = "MILO_CAPTURE_REPLAY"
_TRUE = frozenset({"1", "true", "yes", "on"})

#: The committed fixture format (tests/replay/<run_id>/manifest.json).
REPLAY_FORMAT = "replay/1"
#: The capture artifact's own format, inside a checkpoint's `artifacts`.
CAPTURE_FORMAT = "replay-capture/1"
#: Where the capture rides inside a checkpoint's `artifacts`.
CAPTURE_ARTIFACT_KEY = "replay_capture"
#: The checkpoint phase of the ONE capture checkpoint written when a run
#: fails before the engine wrote any checkpoint of its own (a plan the
#: firewall refused twice). The run is terminal right after, so it is never
#: handed to an engine as resume state.
CAPTURE_PHASE = "replay_capture"

#: Bounds. One completion is kept whole up to MAX_CAPTURED_CONTENT_CHARS; the
#: whole artifact stops growing at MAX_CAPTURE_BYTES. Past either bound the
#: artifact says so (`truncated`, `dropped`) instead of silently losing data.
MAX_CAPTURED_COMPLETIONS = 200
MAX_CAPTURED_TOOL_CALLS = 100
MAX_CAPTURED_CONTENT_CHARS = 96_000
MAX_CAPTURE_BYTES = 1_500_000

#: Provenance of one fixture artifact.
CAPTURED, RECONSTRUCTED = "captured", "reconstructed"
PROVENANCE_KINDS = (CAPTURED, RECONSTRUCTED)

#: The roles a completion may be recorded under, and their phases.
ROLE_PHASES: Mapping[str, frozenset[str]] = {
    "commander": frozenset({"planning", "replanning"}),
    "worker": frozenset({"execute"}),
    "verifier": frozenset({"verification"}),
}


def capture_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether this process captures replay material. Default OFF."""
    value = (env if env is not None else os.environ).get(CAPTURE_FLAG, "")
    return str(value).strip().lower() in _TRUE


# =============================================================================
# The sanitizer
# =============================================================================

#: Credential-shaped text. The repository secret scan's patterns plus the
#: token shapes a provider or a database could echo.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SECRET_PROVIDER_KEY", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("SECRET_JWT", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("SECRET_PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("SECRET_SUPABASE_KEY", re.compile(r"(service_role|sb_secret_|sb_publishable_)", re.I)),
    ("SECRET_CLOUD_KEY", re.compile(r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}")),
    ("SECRET_BEARER", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)),
    ("SECRET_ASSIGNMENT", re.compile(
        r"(api[_-]?key|secret|password|passwd|lease[_-]?token|access[_-]?token)"
        r"\s*[\"']?\s*[:=]", re.I)),
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_URL = re.compile(r"(?:https?|wss?|ftp)://([^/\s\"'<>?#]+)", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
#: The only host a fixture may name: the Israeli open-data portal.
_ALLOWED_URL_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*data\.gov\.il(?::443)?$", re.I)
#: Keys that name a person, a session or a credential. A fixture never
#: carries one, whatever its value.
_FORBIDDEN_KEY = re.compile(
    r"(^|_)(user|users|email|e_mail|owner|created_by|requested_by|lease|lease_token|"
    r"worker_id|api_key|apikey|secret|password|token|authorization|cookie|session|"
    r"ip|ip_address|phone)(_id|_ids)?$", re.I)
#: Keys whose UUID values are DATA identifiers of the register and the run --
#: never a person. Any other UUID anywhere in a fixture is refused, because a
#: user id is a UUID too and nothing else could tell them apart.
_DATA_ID_KEYS = frozenset({"run_id", "candidate_id", "raw_record_id", "snapshot_id",
                           "resource_id", "batch_id", "work_scope_id"})


def _walk(value: Any, path: str) -> Iterable[tuple[str, str | None, Any]]:
    """(path, key, value) for every node, decoding JSON text in place."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield f"{path}.{key}", str(key), item
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield f"{path}[{index}]", None, item
            yield from _walk(item, f"{path}[{index}]")
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                decoded = json.loads(stripped)
            except ValueError:
                return
            yield from _walk(decoded, f"{path}<json>")


def _data_ids(document: Any) -> set[str]:
    ids: set[str] = set()
    for _path, key, value in _walk(document, "$"):
        if key in _DATA_ID_KEYS and isinstance(value, str):
            ids.add(value.lower())
    return ids


def sanitization_findings(document: Any) -> list[str]:
    """Every reason `document` may NOT be committed or exported, as static codes.

    Each finding is ``"<CODE> at <json path>"``: the offending VALUE is never
    repeated, so the report itself cannot leak what it found. An empty list
    means the document carries no secret, no user id, no e-mail address and
    no URL outside data.gov.il.
    """
    findings: list[str] = []
    allowed_ids = _data_ids(document)
    for path, key, value in _walk(document, "$"):
        if key is not None and _FORBIDDEN_KEY.search(key):
            findings.append(f"FORBIDDEN_KEY at {path}")
        if not isinstance(value, str):
            continue
        for code, pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                findings.append(f"{code} at {path}")
        if _EMAIL.search(value):
            findings.append(f"EMAIL at {path}")
        for match in _URL.finditer(value):
            if not _ALLOWED_URL_HOST.match(match.group(1)):
                findings.append(f"URL_NOT_DATA_GOV_IL at {path}")
        for match in _UUID.finditer(value):
            if match.group(0).lower() not in allowed_ids:
                findings.append(f"UNEXPLAINED_UUID at {path}")
    return sorted(set(findings))


# =============================================================================
# The capture
# =============================================================================

def _content_of(response: Any) -> tuple[Any, Any]:
    """(content, finish_reason) of ONE completion -- and nothing else.

    `reasoning_content`, usage, headers and ids are never read: the capture
    holds what the role PARSES, which is exactly what a replay must return.
    A bare str/bytes/dict response (offline adapters) is its own content.
    """
    if isinstance(response, (str, bytes, bytearray, dict)):
        content = response.decode("utf-8", "replace") if isinstance(
            response, (bytes, bytearray)) else response
        return content, "stop"

    def get(container: Any, name: str) -> Any:
        return container.get(name) if isinstance(container, dict) else getattr(container, name, None)

    try:
        choice = get(response, "choices")[0]
        message = get(choice, "message")
        return get(message, "content"), get(choice, "finish_reason")
    except (AttributeError, IndexError, KeyError, TypeError):
        return None, None


class ReplayCaptureRecorder:
    """The bounded, per-run record of one run's inert provider outputs.

    Thread-safe (workers run concurrently). Everything it holds is plain JSON.
    It never raises into the run: a capture that cannot record something
    records that it dropped it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completions: list[dict[str, Any]] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._attempts: dict[str, int] = {}
        self._bytes = 0
        self._dropped = 0
        self._pending = threading.local()
        self._models: dict[str, str] = {}

    def set_models(self, *, commander: str, worker: str) -> None:
        """The role models the run was built with (server config, not provider text)."""
        self._models = {"commander": str(commander), "worker": str(worker)}

    # --- recording -----------------------------------------------------------

    def _admit(self, entry: Mapping[str, Any], bucket: list[dict[str, Any]],
               limit: int) -> None:
        size = len(json.dumps(entry, ensure_ascii=False, default=str))
        if len(bucket) >= limit or self._bytes + size > MAX_CAPTURE_BYTES:
            self._dropped += 1
            return
        self._bytes += size
        bucket.append(dict(entry))

    def record_completion(self, *, agent: str, phase: str, response: Any) -> None:
        role, _, task_id = str(agent or "").partition(":")
        try:
            content, finish_reason = _content_of(response)
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            truncated = isinstance(content, str) and len(content) > MAX_CAPTURED_CONTENT_CHARS
            entry: dict[str, Any] = {
                "role": role, "phase": str(phase or ""),
                "content": content[:MAX_CAPTURED_CONTENT_CHARS] if truncated else content,
                "finish_reason": finish_reason if isinstance(finish_reason, str) else None}
            if truncated:
                entry["content_truncated"] = True
            if task_id:
                entry["task_id"] = task_id
        except Exception:
            with self._lock:
                self._dropped += 1
            return
        with self._lock:
            key = f"{role}:{task_id}:{phase}"
            entry["attempt"] = self._attempts.get(key, 0) + 1
            self._attempts[key] = entry["attempt"]
            self._admit(entry, self._completions, MAX_CAPTURED_COMPLETIONS)

    def note_tool_execution(self, *, name: str, operation: str,
                            arguments: Mapping[str, Any]) -> None:
        """Remember the RESOLVED arguments of the call this thread just made."""
        self._pending.call = {"tool": str(name), "operation": str(operation),
                              "arguments": json.loads(json.dumps(dict(arguments), default=str))}

    def record_tool_result(self, record: Any) -> None:
        """Pair a Registry-validated result with the arguments that produced it.

        The worker executes a call and hands its record to the sink on the SAME
        thread, immediately after, so the thread-local pairing is exact.
        """
        pending = getattr(self._pending, "call", None)
        self._pending.call = None
        try:
            entry = {"task_id": str(record.task_id), "call_id": str(record.call_id),
                     "tool": str(record.tool), "operation": str(record.operation),
                     "arguments": (pending or {}).get("arguments"),
                     "result": json.loads(json.dumps(dict(record.result), default=str))}
        except Exception:
            with self._lock:
                self._dropped += 1
            return
        with self._lock:
            self._admit(entry, self._tool_calls, MAX_CAPTURED_TOOL_CALLS)

    # --- the artifact --------------------------------------------------------

    def artifact(self) -> dict[str, Any]:
        with self._lock:
            return {"format": CAPTURE_FORMAT, "models": dict(self._models),
                    "completions": [dict(item) for item in self._completions],
                    "tool_calls": [dict(item) for item in self._tool_calls],
                    "truncated": self._dropped > 0, "dropped": self._dropped}


class CapturingAuthority:
    """The provider authority, unchanged, with each answer recorded after it returns.

    It delegates EVERY call and returns exactly what the authority returned;
    the only addition is one in-memory append per completion.
    """

    def __init__(self, inner: Any, recorder: ReplayCaptureRecorder) -> None:
        self._inner, self._recorder = inner, recorder

    def chat(self, request: Mapping[str, Any], *args: Any, **kwargs: Any) -> Any:
        response = self._inner.chat(request, *args, **kwargs)
        self._recorder.record_completion(agent=str(kwargs.get("agent", "")),
                                         phase=str(kwargs.get("phase", "")),
                                         response=response)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class CapturingTools:
    """The ToolRegistry, unchanged, noting each call's resolved arguments."""

    def __init__(self, inner: Any, recorder: ReplayCaptureRecorder) -> None:
        self._inner, self._recorder = inner, recorder

    def execute(self, name: str, operation: str, context: Any,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._inner.execute(name, operation, context, payload)
        self._recorder.note_tool_execution(name=name, operation=operation, arguments=payload)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def capturing_result_sink(inner: Callable[[Any], None] | None,
                          recorder: ReplayCaptureRecorder) -> Callable[[Any], None]:
    """The trusted tool-result sink, with the validated result recorded first."""

    def sink(record: Any) -> None:
        recorder.record_tool_result(record)
        if inner is not None:
            inner(record)

    return sink


def with_capture(artifacts: Mapping[str, Any] | None,
                 recorder: ReplayCaptureRecorder | None) -> dict[str, Any]:
    """A checkpoint's `artifacts` with the capture riding on it (when capturing)."""
    merged = dict(artifacts or {})
    if recorder is not None:
        merged[CAPTURE_ARTIFACT_KEY] = recorder.artifact()
    return merged


__all__ = ["CAPTURED", "CAPTURE_ARTIFACT_KEY", "CAPTURE_FLAG", "CAPTURE_FORMAT",
           "CAPTURE_PHASE", "CapturingAuthority", "CapturingTools",
           "MAX_CAPTURE_BYTES", "MAX_CAPTURED_COMPLETIONS", "MAX_CAPTURED_CONTENT_CHARS",
           "MAX_CAPTURED_TOOL_CALLS", "PROVENANCE_KINDS", "RECONSTRUCTED", "REPLAY_FORMAT",
           "ROLE_PHASES", "ReplayCaptureRecorder", "capture_enabled", "capturing_result_sink",
           "sanitization_findings", "with_capture"]
