# -*- coding: utf-8 -*-
"""MILO-mediated standalone web search: ONE admitted invocation per call.

Why this module exists
----------------------
V1 used to reach the internet through Moonshot's BUILTIN ``$web_search``
tool. That tool is executed by the provider, inside a Chat Completions
request, and the provider alone decides how many times it runs. MILO could
reserve a worst case before dispatch and reconcile afterwards, but it could
never *admit* an individual search: if four were reserved and the provider
performed five, the fifth had already happened by the time MILO learned of
it. Post-facto detection is not a ceiling.

The mediated path inverts that ownership. The model is offered an ORDINARY
function tool, which the provider cannot execute. It can only *ask*. Every
ask comes back to MILO, and MILO decides -- against the run's own search
allowance and the endpoint's QPS bucket -- whether that one search happens,
performs exactly that one search itself, and hands the results back.

    the model asks  ->  MILO admits  ->  ONE standalone search
                    ->  durable accounting  ->  results returned to the model

Internet access is not reduced by this: the same research happens, over the
same provider's search endpoints. What changes is that the number of
searches a run can perform became a quantity MILO can refuse, rather than a
number it can only read afterwards.

What is server-owned here
-------------------------
Everything. The tool's name and schema, the query bounds, the result bounds,
the endpoint, and the shape of the material that reaches a model prompt are
all constants in this module. Nothing a model emits can widen them: a query
is truncated, a result set is truncated, and a payload that would exceed the
budget is cut. Model-authored text is DATA on its way back into a prompt, and
is never interpreted as an instruction by anything here.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

# =============================================================================
# 1. THE TOOL MILO OFFERS
# =============================================================================

#: MILO's own function tool. Deliberately NOT ``$web_search``: the ``$`` names
#: belong to the provider's builtin namespace, and a ``builtin_function`` tool
#: is exactly the provider-executed capability this path removes. This one is
#: a plain ``function`` tool, so the provider can only request it.
MEDIATED_SEARCH_TOOL_NAME = "web_search"

#: The one tool type a mediated request may carry. A request that declares
#: anything else is not mediated, whatever the tool is called.
MEDIATED_TOOL_TYPE = "function"

#: The provider-executed spelling, kept here only so callers can ASSERT its
#: absence. Nothing in this module ever emits it.
PROVIDER_BUILTIN_SEARCH_NAME = "$web_search"
PROVIDER_BUILTIN_TOOL_TYPE = "builtin_function"

#: Server-owned material bounds. A model can ask for anything; these decide
#: what a search may actually cost and what may re-enter a prompt.
MAX_QUERY_CHARS = 400
MAX_RESULTS_PER_SEARCH = 8
MAX_RESULT_TITLE_CHARS = 200
MAX_RESULT_URL_CHARS = 500
MAX_RESULT_SNIPPET_CHARS = 1_000
MAX_TOOL_CONTENT_CHARS = 12_000

_SEARCH_TOOL_DESCRIPTION = (
    "Search the public internet for current information and return ranked "
    "results with source URLs. One call performs exactly one search. Ask a "
    "single, specific question per call and cite the returned source_url for "
    "any fact you take from a result."
)

_SEARCH_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "The search query. One focused query per call; "
                f"at most {MAX_QUERY_CHARS} characters."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def mediated_search_tool() -> list[dict[str, Any]]:
    """The tools list a search-enabled MILO chat request carries.

    A fresh structure every time, because a shared mutable default that a
    request payload can be edited through is a way for one call to change
    the next one's tools.
    """
    return [{
        "type": MEDIATED_TOOL_TYPE,
        "function": {
            "name": MEDIATED_SEARCH_TOOL_NAME,
            "description": _SEARCH_TOOL_DESCRIPTION,
            "parameters": json.loads(json.dumps(_SEARCH_TOOL_PARAMETERS)),
        },
    }]


def request_offers_provider_executed_search(request: Mapping[str, Any]) -> bool:
    """Does this chat request hand the PROVIDER a search capability?

    The structural question the V1 production path must always answer ``no``
    to. It reads the tool TYPE as well as the name, because the type is what
    decides who executes: a ``builtin_function`` entry is run and billed by
    the provider however many times it likes, and that is the multiplicity
    MILO cannot admit one invocation at a time.
    """
    for tool in (request.get("tools") or ()):
        if not isinstance(tool, Mapping):
            continue
        if str(tool.get("type") or "") == PROVIDER_BUILTIN_TOOL_TYPE:
            return True
        function = tool.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str) and name.startswith("$"):
            return True
    return False


# =============================================================================
# 2. FAILURES
# =============================================================================

class SearchQueryInvalid(ValueError):
    """The model asked for a search without a usable query.

    Raised BEFORE admission, and that placement is the point: nothing was
    admitted, nothing was performed, and nothing is charged. An empty query
    is a malformed request from the model, not a search the run spent.
    """


class SearchUnavailable(RuntimeError):
    """No standalone search transport is configured.

    Also raised before admission. A run that cannot search must not have its
    allowance debited for searches it could never perform -- and it must not
    silently fall back to a provider-executed capability either, which is why
    this is an error rather than a degraded mode.
    """


class SearchTransportError(RuntimeError):
    """One admitted search failed at the transport.

    This one is raised AFTER admission, and the charge stands. Whether the
    provider ran and billed the search before failing is not knowable from
    here, and an unknown amount of provider spend is not an absence of spend:
    refunding it would make the ceiling a number the transport could reset.
    """


# =============================================================================
# 3. RESULTS
# =============================================================================

def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", " ").strip()
    return text[:limit]


@dataclass(frozen=True)
class SearchResult:
    """One internet-derived result, bounded and JSON-safe."""

    title: str = ""
    url: str = ""
    snippet: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "source_url": self.url, "snippet": self.snippet}


@dataclass(frozen=True)
class SearchOutcome:
    """What ONE mediated search call produced, for the model and the record.

    ``admitted`` says whether a search invocation was really taken from the
    run's allowance. It is the field that separates "MILO refused before
    anything happened" from "MILO spent a search and it went wrong", and the
    two must never be reported as the same thing.
    """

    query: str
    endpoint: str
    results: tuple[SearchResult, ...] = ()
    error: str | None = None
    admitted: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_tool_content(self) -> str:
        """The bounded JSON the model reads back as the tool result.

        Truncation is reported rather than silent: a model that is told it
        received part of a result set can ask a narrower question, whereas one
        that is quietly handed a trimmed answer cannot tell the difference
        between "nothing more exists" and "MILO cut it".
        """
        payload: dict[str, Any] = {
            "tool": MEDIATED_SEARCH_TOOL_NAME,
            "query": self.query,
            "status": "ok" if self.ok else "error",
        }
        if self.error is not None:
            payload["error"] = self.error
            payload["instruction"] = (
                "This search returned no results. Do NOT invent sources or "
                "facts. Continue with what is already established, or ask a "
                "different, narrower query."
            )
        else:
            payload["results"] = [result.as_dict() for result in self.results]
            payload["result_count"] = len(self.results)
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) <= MAX_TOOL_CONTENT_CHARS:
            return encoded
        # Shed results, newest-ranked last, until it fits. The envelope is
        # rebuilt each time rather than string-sliced, because half a JSON
        # document is not something a model can read.
        results = list(payload.get("results") or ())
        while results:
            results.pop()
            payload["results"] = results
            payload["result_count"] = len(results)
            payload["truncated"] = True
            encoded = json.dumps(payload, ensure_ascii=False)
            if len(encoded) <= MAX_TOOL_CONTENT_CHARS:
                return encoded
        return json.dumps({
            "tool": MEDIATED_SEARCH_TOOL_NAME,
            "query": self.query[:MAX_QUERY_CHARS],
            "status": "error",
            "error": "result payload exceeded the server-owned size budget",
        }, ensure_ascii=False)


def normalize_query(raw: Any) -> str:
    """The one place a model-supplied query becomes a server-owned string."""
    if isinstance(raw, Mapping):
        raw = raw.get("query")
    if not isinstance(raw, str):
        raise SearchQueryInvalid("a search requires a text query")
    query = " ".join(raw.split())[:MAX_QUERY_CHARS].strip()
    if not query:
        raise SearchQueryInvalid("a search requires a non-empty query")
    return query


def _result_rows(payload: Any) -> Sequence[Any]:
    """Find the result rows in a response whose exact shape is not pinned.

    The standalone search wire format is not something this repository has
    verified against the live provider, so the reader accepts the shapes such
    an endpoint plausibly returns and treats anything else as EMPTY rather
    than guessing. An empty result set is honest; an invented one is not.
    """
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return payload
    if not isinstance(payload, Mapping):
        return ()
    for key in ("results", "data", "items", "documents", "search_results", "web_results"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return value
        if isinstance(value, Mapping):
            nested = _result_rows(value)
            if nested:
                return nested
    return ()


def normalize_results(payload: Any) -> tuple[SearchResult, ...]:
    """Bound and flatten a transport payload into result material."""
    rows = _result_rows(payload)
    results: list[SearchResult] = []
    for row in rows:
        if len(results) >= MAX_RESULTS_PER_SEARCH:
            break
        if isinstance(row, str):
            results.append(SearchResult(snippet=_clip(row, MAX_RESULT_SNIPPET_CHARS)))
            continue
        if not isinstance(row, Mapping):
            continue
        title = row.get("title") or row.get("name") or row.get("heading")
        url = (row.get("url") or row.get("link") or row.get("source_url")
               or row.get("href"))
        snippet = (row.get("snippet") or row.get("content") or row.get("text")
                   or row.get("summary") or row.get("description"))
        result = SearchResult(title=_clip(title, MAX_RESULT_TITLE_CHARS),
                              url=_clip(url, MAX_RESULT_URL_CHARS),
                              snippet=_clip(snippet, MAX_RESULT_SNIPPET_CHARS))
        if result.title or result.url or result.snippet:
            results.append(result)
    return tuple(results)


# =============================================================================
# 4. THE TRANSPORT
# =============================================================================

#: Moonshot's standalone Web Search endpoints, each with its own QPS bucket.
#: These are the endpoints `backend.provider_scheduler.admit_search` has always
#: paced; until now nothing in production called them.
SEARCH_ENDPOINT_PATHS = {"search": "/tools/search", "search_pro": "/tools/search_pro"}

DEFAULT_SEARCH_BASE_URL = "https://api.moonshot.ai/v1"


def search_base_url() -> str:
    """The provider base a standalone search is sent to.

    Deliberately the SAME variable the worker resolves the chat base from,
    rather than a search-specific one: a second knob is a second thing a
    deployment can get wrong, and there is no reviewed reason for search and
    chat to address different hosts. A deployment that ever needs them split
    adds that as a reviewed change, with an inventory entry.
    """
    return (os.getenv("MILO_MODEL_BASE_URL")
            or DEFAULT_SEARCH_BASE_URL).strip().rstrip("/")


def search_api_key() -> str:
    """Credentials come ONLY from worker-scoped environment, as everywhere."""
    return (os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or "").strip()


class MoonshotStandaloneSearch:
    """ONE HTTP request to ONE standalone search endpoint. Nothing else.

    It runs no retry loop, keeps no allowance of its own and makes no second
    request: pacing, volume and price are all decided before it is called, and
    a transport that quietly retried would perform searches nobody admitted.

    The request deadline is the same total-deadline client every MILO provider
    request uses, so a hung search cannot outlive a run.
    """

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 deadline_seconds: float | None = None,
                 http_client: Any = None) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._deadline_seconds = deadline_seconds
        self._http_client = http_client
        self._built_client: Any = None
        self._client_lock = threading.Lock()

    def available(self) -> bool:
        """Can this transport actually perform a search RIGHT NOW?

        Asked before admission, so a deployment with no provider credential
        refuses the search instead of debiting the run's allowance for one it
        was always going to fail. Configuration is knowable for free; a
        provider outcome is not, which is why only this half is checked early.
        """
        if self._api_key is not None:
            return bool(str(self._api_key).strip())
        return bool(search_api_key())

    def _client(self) -> Any:
        """ONE http client for this transport, built once.

        Cached rather than rebuilt per search: a fresh client per invocation
        means a fresh connection pool per invocation, and the sockets of a
        pool nobody closes are exactly the leak the deadline transport exists
        to avoid on the chat path.
        """
        if self._http_client is not None:
            return self._http_client
        with self._client_lock:
            if self._built_client is None:
                from backend.budget import build_provider_http_client

                self._built_client = build_provider_http_client(
                    self._deadline_seconds)
            return self._built_client

    def __call__(self, query: str, *, endpoint: str) -> tuple[SearchResult, ...]:
        path = SEARCH_ENDPOINT_PATHS.get(endpoint)
        if path is None:
            raise SearchTransportError(f"unknown search endpoint: {endpoint!r}")
        api_key = self._api_key if self._api_key is not None else search_api_key()
        if not api_key:
            # Configuration, not a provider outcome -- and it is raised as a
            # transport failure rather than a silent empty result so a
            # misconfigured deployment is visible instead of producing
            # ungrounded answers that look like searched ones.
            raise SearchTransportError("no provider credential for standalone search")
        base = (self._base_url if self._base_url is not None else search_base_url())
        client = self._client()
        try:
            response = client.post(
                f"{base.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={"query": query},
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                raise SearchTransportError(f"standalone search returned HTTP {status}")
            payload = response.json()
        except SearchTransportError:
            raise
        except BaseException as exc:  # noqa: BLE001 - one closed transport failure
            raise SearchTransportError(f"standalone search failed: {type(exc).__name__}") from exc
        return normalize_results(payload)


#: Process-wide override, for a deployment or a test that installs its own
#: transport. ``None`` means "use the credential-resolved default", which is
#: itself absent when no credential exists -- and a run asking to search then
#: fails closed rather than reaching for a provider-executed capability.
DEFAULT_SEARCH_EXECUTOR: Callable[..., Any] | None = None


def build_default_search_executor(*, api_key: str | None = None,
                                  base_url: str | None = None,
                                  deadline_seconds: float | None = None,
                                  ) -> MoonshotStandaloneSearch:
    return MoonshotStandaloneSearch(api_key=api_key, base_url=base_url,
                                    deadline_seconds=deadline_seconds)


def default_search_executor() -> Callable[..., Any] | None:
    """The transport a process falls back to when none was injected.

    Returns ``None`` -- not a transport that will fail -- when there is no
    credential, so the refusal lands before admission and the run is not
    charged for a search that could never have happened.
    """
    if DEFAULT_SEARCH_EXECUTOR is not None:
        return DEFAULT_SEARCH_EXECUTOR
    transport = MoonshotStandaloneSearch()
    return transport if transport.available() else None


__all__ = [
    "DEFAULT_SEARCH_EXECUTOR", "MAX_QUERY_CHARS", "MAX_RESULTS_PER_SEARCH",
    "MAX_TOOL_CONTENT_CHARS", "MEDIATED_SEARCH_TOOL_NAME", "MEDIATED_TOOL_TYPE",
    "MoonshotStandaloneSearch", "PROVIDER_BUILTIN_SEARCH_NAME",
    "PROVIDER_BUILTIN_TOOL_TYPE", "SEARCH_ENDPOINT_PATHS", "SearchOutcome",
    "SearchQueryInvalid", "SearchResult", "SearchTransportError",
    "SearchUnavailable", "build_default_search_executor",
    "default_search_executor",
    "mediated_search_tool", "normalize_query", "normalize_results",
    "request_offers_provider_executed_search", "search_api_key",
    "search_base_url",
]
