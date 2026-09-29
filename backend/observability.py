"""Error reporting to Sentry -- OFF unless a DSN is configured.

Default everywhere (tests, CI, local, and production until an operator stores
a DSN): ``SENTRY_DSN`` is empty, :func:`init_sentry` returns ``False`` and
``sentry_sdk`` is never even imported. Production reads the DSN from the
Secret Manager secret ``SENTRY_DSN``, bound only when that secret has an
enabled version (``MILO_OPTIONAL_RUNTIME_SECRETS`` in
scripts/deploy/deployment-contract.sh).

What an event may carry is decided HERE, not by the SDK's defaults:

* ``send_default_pii=False``, no request bodies (``max_request_body_size``
  ``never``), no local variables, no breadcrumbs, no log capture, and NO
  auto-enabled integrations -- the SDK's OpenAI / httpx integrations would
  otherwise record model prompts, model outputs and outbound URLs;
* :func:`scrub_event` runs as ``before_send`` AND ``before_send_transaction``
  and removes, whatever an integration attached: the request's body, headers,
  cookies, query string and environment; ``user``; ``extra``; breadcrumbs;
  every stack frame's local variables; exception MESSAGES (an exception text
  can quote a prompt, a model output, a tool result or a register payload --
  the exception TYPE and the stack stay); span data; and any context other
  than the runtime/OS/trace ones;
* every event is tagged with ``release`` (``MILO_RELEASE_SHA``, the deployed
  commit), ``service`` and, when present, ``run_id``;
* traces are off (``MILO_SENTRY_TRACES_SAMPLE_RATE`` defaults to 0 and is
  clamped to at most 0.05).

Reporting never changes an outcome: every public function swallows its own
failures.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DSN_ENV = "SENTRY_DSN"
TRACES_RATE_ENV = "MILO_SENTRY_TRACES_SAMPLE_RATE"
RELEASE_ENV = "MILO_RELEASE_SHA"
#: The highest trace sample rate the configuration can ask for.
MAX_TRACES_SAMPLE_RATE = 0.05
#: DSN values that mean "disabled" explicitly (a Secret Manager version cannot
#: be empty, so an operator who wants the secret present but reporting off
#: stores one of these).
DISABLED_DSN_VALUES = frozenset({"", "disabled", "off", "none", "false", "0"})

REDACTED = "[redacted]"
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_RUN_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
#: A run named in a request path (API routes /runs/{run_id}/...).
_PATH_RUN_ID_RE = re.compile(
    r"/runs/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:/|$)")
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: Contexts the SDK fills from the host, not from request or model material.
_SAFE_CONTEXTS = frozenset({"runtime", "os", "trace"})
#: Tags this module sets. Anything else an integration adds is dropped.
_SAFE_TAGS = frozenset({"service", "run_id", "error_code", "workflow_key"})

_enabled = False


def configured_dsn(env: dict[str, str] | None = None) -> str:
    """The DSN to use, or "" when reporting is off."""
    source = os.environ if env is None else env
    value = (source.get(DSN_ENV) or "").strip()
    if value.lower() in DISABLED_DSN_VALUES:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or not parts.username:
        # A malformed DSN disables reporting; it never stops the service.
        # The value itself is never printed.
        print("sentry: SENTRY_DSN is not a valid DSN; error reporting stays disabled", flush=True)
        return ""
    return value


def traces_sample_rate(env: dict[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    raw = (source.get(TRACES_RATE_ENV) or "").strip()
    try:
        rate = float(raw) if raw else 0.0
    except ValueError:
        return 0.0
    if rate != rate or rate <= 0:  # NaN or non-positive
        return 0.0
    return min(rate, MAX_TRACES_SAMPLE_RATE)


def release(env: dict[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    value = (source.get(RELEASE_ENV) or "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def service_name(default: str, env: dict[str, str] | None = None) -> str:
    """Cloud Run's own name for this surface, else ``default``."""
    source = os.environ if env is None else env
    for key in ("K_SERVICE", "CLOUD_RUN_JOB"):
        value = (source.get(key) or "").strip()
        if _SERVICE_RE.fullmatch(value):
            return value
    return default


def is_enabled() -> bool:
    return _enabled


# -- scrubbing ---------------------------------------------------------------

def _strip_query(url: Any) -> Any:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))


def _scrub_frames(container: Any) -> None:
    if not isinstance(container, dict):
        return
    stacktrace = container.get("stacktrace")
    if isinstance(stacktrace, dict):
        for frame in stacktrace.get("frames") or []:
            if isinstance(frame, dict):
                frame.pop("vars", None)


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """Remove every field class that can carry request, user or model material.

    Mutates and returns ``event`` (a non-dict is returned unchanged).
    """
    if not isinstance(event, dict):
        return event
    request = event.get("request")
    path_run_id = None
    if isinstance(request, dict) and isinstance(request.get("url"), str):
        match = _PATH_RUN_ID_RE.search(urlsplit(request["url"]).path)
        path_run_id = match.group(1).lower() if match else None
    if isinstance(request, dict):
        # Method and path only: no body, headers, cookies, query or env.
        event["request"] = {key: value for key, value in (
            ("method", request.get("method") if isinstance(request.get("method"), str) else None),
            ("url", _strip_query(request.get("url"))),
        ) if value}
    else:
        event.pop("request", None)
    for key in ("user", "extra", "breadcrumbs", "modules", "server_name"):
        event.pop(key, None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        event["contexts"] = {key: value for key, value in contexts.items() if key in _SAFE_CONTEXTS}
        trace = event["contexts"].get("trace")
        if isinstance(trace, dict):
            trace.pop("data", None)
    tags = event.get("tags")
    if isinstance(tags, dict):
        event["tags"] = {key: value for key, value in tags.items() if key in _SAFE_TAGS}
    elif tags is not None:
        event.pop("tags", None)
    if path_run_id:
        # An API event about one run carries THAT run's id (the path is kept
        # anyway); it wins over any scope-level tag.
        event.setdefault("tags", {})["run_id"] = path_run_id
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    for value in values or []:
        if isinstance(value, dict):
            if value.get("value"):
                value["value"] = REDACTED
            _scrub_frames(value)
    threads = event.get("threads")
    for thread in (threads.get("values") if isinstance(threads, dict) else None) or []:
        _scrub_frames(thread)
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        # Only this module's own fixed messages reach Sentry (log capture is
        # off); their parameters are dropped anyway.
        logentry.pop("params", None)
        logentry.pop("formatted", None)
    for span in event.get("spans") or []:
        if isinstance(span, dict):
            span.pop("data", None)
            span.pop("tags", None)
            if isinstance(span.get("description"), str):
                span["description"] = _strip_query(span["description"]) or REDACTED
    return event


def _scrub_transaction(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    return scrub_event(event, hint)


# -- lifecycle -----------------------------------------------------------------

def init_sentry(service: str, *, env: dict[str, str] | None = None, web: bool = False,
                transport: Any = None) -> bool:
    """Start reporting for ``service`` if a DSN is configured; else do nothing.

    ``web`` adds the Starlette/FastAPI integrations (API service only), so an
    unhandled 5xx is reported; its request data is still scrubbed.
    ``transport`` exists for tests only (an in-memory envelope sink).
    """
    global _enabled
    dsn = configured_dsn(env)
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        integrations: list[Any] = [LoggingIntegration(level=None, event_level=None)]
        if web:
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.starlette import StarletteIntegration
            integrations += [StarletteIntegration(), FastApiIntegration()]
        source = os.environ if env is None else env
        sentry_sdk.init(
            dsn=dsn,
            release=release(env),
            environment=(source.get("ENVIRONMENT") or "production").strip() or "production",
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            max_breadcrumbs=0,
            auto_enabling_integrations=False,
            integrations=integrations,
            traces_sample_rate=traces_sample_rate(env),
            enable_logs=False,
            before_send=scrub_event,
            before_send_transaction=_scrub_transaction,
            **({"transport": transport} if transport is not None else {}),
        )
        sentry_sdk.set_tag("service", service_name(service, env))
    except Exception:
        # Observability must never stop the service.
        print("sentry: initialization failed; error reporting stays disabled", flush=True)
        _enabled = False
        return False
    _enabled = True
    return True


def set_run_id(run_id: Any) -> None:
    if not _enabled:
        return
    value = str(run_id or "")
    if not _RUN_ID_RE.fullmatch(value):
        return
    try:
        import sentry_sdk
        sentry_sdk.set_tag("run_id", value)
    except Exception:
        pass


def safe_code(code: Any) -> str:
    value = str(code or "")
    return value if _CODE_RE.fullmatch(value) else "UNCLASSIFIED"


def report_run_failed(run_id: Any, code: Any, workflow_key: Any = None) -> bool:
    """One event for a run the worker terminalized as ``failed``.

    Carries the static error code and the workflow key -- never the failure
    message, which can quote engine or model material.
    """
    if not _enabled:
        return False
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            if _RUN_ID_RE.fullmatch(str(run_id or "")):
                scope.set_tag("run_id", str(run_id))
            scope.set_tag("error_code", safe_code(code))
            if isinstance(workflow_key, str) and re.fullmatch(r"[a-z0-9_]{1,40}", workflow_key):
                scope.set_tag("workflow_key", workflow_key)
            sentry_sdk.capture_message("run failed", level="error")
        return True
    except Exception:
        return False


def report_exception(exc: BaseException) -> bool:
    """Report an exception that is about to end the process (type and stack only)."""
    if not _enabled:
        return False
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
        return True
    except Exception:
        return False


def flush(timeout: float = 2.0) -> None:
    if not _enabled:
        return
    try:
        import sentry_sdk
        sentry_sdk.flush(timeout=timeout)
    except Exception:
        pass
