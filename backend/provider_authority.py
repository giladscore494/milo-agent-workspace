"""THE provider authority: ONE contract for every paid provider request.

Why this module exists
----------------------

Before it, "the provider" was four different opinions held by four different
places:

* ``vehicle_catalog_v1.core`` had its own rate-limit retry loop with its own
  fixed delay and its own bound, on top of the shared scheduler's;
* ``swarm_v2.model_gateway`` computed its own admission tokens and held its
  own ``ProviderScheduler`` instance, built separately from V1's;
* ``backend.budget``'s guarded client decided, with a DIFFERENT classifier
  from the scheduler's, whether a failure was backpressure or a semantic
  failure -- so a 503 was backpressure to the scheduler and a semantic retry
  to the ledger, and one provider event was charged twice;
* ``backend.provider_scheduler`` decided retries and settlement with two more
  classifiers (``classify_provider_error`` and
  ``request_completion_is_proven``).

Four opinions about one request is not an architecture, it is a race between
accounting surfaces. This module is the single authority:

1. :class:`ProviderOutcome` / :func:`classify_outcome` -- the ONE taxonomy.
   Every other surface asks it instead of re-reading the exception.
2. :func:`admission_demand` -- the ONE token-admission rule, which is
   CONSERVATIVE and fails closed rather than under-counting a hard ceiling.
3. :class:`ProviderAdapter` -- the ONE call path. V1 and V2 both hand it a
   request; it owns admission, retries, deadline, token accounting, search
   accounting and settlement. Neither engine implements any of those.

What this module deliberately does NOT change is the #102 ownership
invariant: an outcome that cannot be PROVEN finished never returns an
organization concurrency permit. :attr:`ProviderVerdict.completion_proven`
is the same structural judgement ``request_completion_is_proven`` made, moved
here so one classification answers both questions instead of two classifiers
answering them differently.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.standalone_search import SearchOutcome

from backend.runtime import CancellationRequested

# =============================================================================
# 1. THE ONE ERROR TAXONOMY
# =============================================================================

#: The distinct Kimi 429/5xx classes, which must NOT be treated alike.
RATE_LIMIT_REACHED = "rate_limit_reached_error"
ENGINE_OVERLOADED = "engine_overloaded_error"
EXCEEDED_CURRENT_QUOTA = "exceeded_current_quota_error"
SEARCH_RATE_LIMITED = "rate_limited"
SEARCH_RATE_LIMIT_UNAVAILABLE = "rate_limit_unavailable"


class ProviderOutcome(StrEnum):
    """What ONE provider attempt turned out to be. Exactly seven answers.

    The set is closed on purpose. Anything a classifier cannot place is
    :attr:`UNKNOWN`, which is the conservative answer in every direction:
    it holds the organization permit, it is not retried as backpressure, and
    it is not silently charged as a success.
    """

    SUCCESS = "success"
    #: The provider asked MILO to slow down (429, 503/overloaded, search QPS).
    #: A SCHEDULING outcome, never a semantic model failure.
    RATE_LIMIT = "rate_limit"
    #: The provider failed in a way a later attempt could plausibly survive
    #: (5xx that is not overload, a connection that never left the process).
    RETRYABLE_FAILURE = "retryable_provider_failure"
    #: The provider refused in a way no retry can fix (4xx, quota exhausted).
    NON_RETRYABLE_FAILURE = "non_retryable_provider_failure"
    #: MILO stopped waiting. Says nothing about whether the provider stopped.
    TIMEOUT = "timeout"
    #: The run was cancelled cooperatively.
    CANCELLATION = "cancellation"
    #: MILO cannot say what happened to the request. Fails closed everywhere.
    UNKNOWN = "unknown_request_outcome"


#: Outcomes the scheduler paces and retries WITHOUT spending a semantic retry.
BACKPRESSURE_OUTCOMES = frozenset({ProviderOutcome.RATE_LIMIT})

#: Outcomes that leave the engine with a bounded REPAIR to perform -- a
#: fallback prompt, a second structured attempt -- and therefore legitimately
#: consume the run's semantic retry allowance (``MILO_MAX_RETRIES``). That
#: allowance is what bounds how many times a run redoes work, so an outcome
#: the engine will repair belongs in it even when the fault was the
#: provider's.
#:
#: What is deliberately ABSENT is the whole point of this set:
#:
#: * :attr:`ProviderOutcome.RATE_LIMIT` -- 429 AND 503/`engine_overloaded`.
#:   Provider capacity is a SCHEDULING outcome; the scheduler paces and
#:   retries it, no work is redone, and nothing semantic happened. Charging
#:   it here is how Attempt 6 could die at RETRY_LIMIT_REACHED with no model
#:   having misbehaved -- and, before this module, the scheduler and the
#:   ledger disagreed about exactly this for 503.
#: * :attr:`ProviderOutcome.CANCELLATION` -- a run being torn down is not
#:   repairing anything.
#: * ``exceeded_current_quota_error`` -- terminal for the call; no repair
#:   follows, and a counter that fills up because the account is out of money
#:   tells nobody anything true. Excluded by
#:   :attr:`ProviderVerdict.consumes_semantic_retry` rather than by outcome,
#:   because the provider spells it as a 429-family refusal.
SEMANTIC_RETRY_OUTCOMES = frozenset({
    ProviderOutcome.RETRYABLE_FAILURE,
    ProviderOutcome.NON_RETRYABLE_FAILURE,
    ProviderOutcome.TIMEOUT,
    ProviderOutcome.UNKNOWN,
})


@dataclass(frozen=True)
class ProviderVerdict:
    """One classification of one attempt, answering every question at once."""

    outcome: ProviderOutcome
    #: The provider's own failure-class marker when one was recognisable.
    provider_code: str | None = None
    #: Can MILO PROVE the request that held a permit is no longer running?
    #: This is the #102 invariant and its default is NO.
    completion_proven: bool = False
    #: A static reason code when completion is not proven. Never free text.
    unproven_reason: str = ""
    #: A valid ``Retry-After`` value, in seconds, when the provider sent one.
    retry_after: float | None = None
    #: The numeric ``X-RateLimit-*`` values the response published, if any.
    headers: Mapping[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.headers is None:
            object.__setattr__(self, "headers", {})

    @property
    def is_backpressure(self) -> bool:
        """Should the scheduler pace and retry this without charging a retry?"""
        return self.outcome in BACKPRESSURE_OUTCOMES

    @property
    def consumes_semantic_retry(self) -> bool:
        """Does this spend the run's ``max_retries`` allowance?

        A quota exhaustion does not: it is terminal for the call, and a
        counter that fills up while the account is simply out of money tells
        nobody anything true.
        """
        if self.provider_code == EXCEEDED_CURRENT_QUOTA:
            return False
        return self.outcome in SEMANTIC_RETRY_OUTCOMES

    @property
    def is_quota_exhaustion(self) -> bool:
        return self.provider_code == EXCEEDED_CURRENT_QUOTA

    @property
    def succeeded(self) -> bool:
        return self.outcome is ProviderOutcome.SUCCESS


SUCCESS_VERDICT = ProviderVerdict(outcome=ProviderOutcome.SUCCESS,
                                  completion_proven=True)

#: httpx failures that happen BEFORE a request is on the wire. For these,
#: and only these, "it failed" really does prove "it is not running".
_NEVER_SENT = ("ConnectError", "ConnectTimeout", "PoolTimeout",
               "UnsupportedProtocol", "InvalidURL", "ProxyError")


def _cause_chain(exc: BaseException) -> Iterable[BaseException]:
    """Walk ``__cause__``/``__context__`` once each: the SDK wraps transport errors."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def failed_before_the_request_was_sent(exc: BaseException) -> bool:
    return any(type(link).__name__ in _NEVER_SENT for link in _cause_chain(exc))


def fired_a_total_deadline(exc: BaseException) -> bool:
    """The transport's deadline, whether raised bare or wrapped by the SDK."""
    return any(type(link).__name__ == "ProviderRequestDeadlineExceeded"
               for link in _cause_chain(exc))


def carries_a_complete_provider_response(exc: BaseException) -> bool:
    """STRUCTURAL evidence that the provider answered.

    True only for an exception that carries a response OBJECT with an integer
    HTTP status code. That is the shape of the OpenAI SDK's ``APIStatusError``
    family, which the SDK raises only after ``response.read()`` -- so the
    exchange is over whatever the status says.

    A bare ``status_code`` attribute with no response object is deliberately
    NOT enough: MILO's own ``AppError`` carries one (an HTTP status for MILO's
    API, not the provider's), and any wrapper can set one. An attribute is a
    claim; a response is evidence.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return False
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return False
    return 100 <= status <= 599


def _claimed_status(exc: Any) -> int | None:
    """The status code an exception ADVERTISES, from either shape."""
    for candidate in (getattr(exc, "status_code", None),
                      getattr(getattr(exc, "response", None), "status_code", None)):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            continue
        return candidate
    return None


def provider_failure_code(exc: Any) -> str | None:
    """Name WHICH provider failure class an exception is, or None.

    Text-permissive BY DESIGN, and only for questions where a permissive
    reading is the safe direction (pace and retry). It is never what decides
    whether an organization permit goes back: see
    :attr:`ProviderVerdict.completion_proven`, which is structural.
    """
    text = str(exc or "").lower()
    status = _claimed_status(exc)
    for marker in (EXCEEDED_CURRENT_QUOTA, ENGINE_OVERLOADED, RATE_LIMIT_REACHED,
                   SEARCH_RATE_LIMIT_UNAVAILABLE):
        if marker in text:
            return marker
    if "project qps limit exceeded" in text:
        return SEARCH_RATE_LIMITED
    if status == 429 or "http 429" in text or "error code: 429" in text:
        return RATE_LIMIT_REACHED
    if status == 503 or "overloaded" in text:
        return ENGINE_OVERLOADED
    if "max organization concurrency" in text or "organization max rpm" in text:
        return RATE_LIMIT_REACHED
    return None


def rate_limit_headers(exc: Any) -> dict[str, int]:
    """Read the numeric X-RateLimit-* values a 429 may publish.

    These are used to slow MILO down and to explain a refusal. They are NEVER
    used to widen a ceiling: a header advertising more capacity than the
    reviewed 80% configuration is ignored, because a capacity increase needs
    authoritative verification and a reviewed change, not a response header.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return {}
    out: dict[str, int] = {}
    for name, key in (("X-RateLimit-Limit", "limit"),
                      ("X-RateLimit-Remaining", "remaining"),
                      ("X-RateLimit-Reset", "reset")):
        try:
            raw = headers.get(name)
            if raw is None:
                raw = headers.get(name.lower())
            if raw is None:
                continue
            out[key] = int(float(str(raw).strip()))
        except Exception:  # noqa: BLE001 - unknown header containers fail closed
            continue
    return out


def retry_after_seconds(exc: Any) -> float | None:
    """Extract a valid Retry-After value (seconds) from a provider error."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
        if raw is None:
            raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - unknown header container shapes fail closed
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def completion_is_proven(exc: BaseException | None) -> tuple[bool, str]:
    """Can MILO PROVE the request that held a permit is no longer running?

    Unchanged in substance from the function this replaced, because it is the
    #102 invariant and the invariant is not what this refactor is changing.
    The default is NO: an outcome this function does not recognise holds the
    slot rather than freeing it.

    Every YES is STRUCTURAL -- an object the transport or the SDK built, or a
    statement from code that ran on one side of the request. None of them is
    the TEXT of a message:

    * the call RETURNED. The response was read to completion.
    * the exception carries a provider response OBJECT with a status code.
    * the failure happened before anything was sent.
    * the exception carries ``provider_request_completed``, set by code that
      knows which side of the request it ran on.

    Everything else is NO -- most importantly a fired total deadline and a
    read timeout: those mean MILO stopped waiting, and nothing in the httpx
    or OpenAI contract turns that into the provider stopping work.

    WHY THE TEXT CLASSIFIER IS NOT CONSULTED HERE: for RETRY decisions a
    permissive reading is the safe direction, because treating something as
    backpressure only costs a wait. For CONCURRENCY it is the opposite -- any
    exception whose message happened to contain "429" could free an
    organization slot. The two questions want opposite defaults, so the
    classifier stays permissive and settlement requires structure.
    """
    if exc is None:
        return True, ""
    declared = getattr(exc, "provider_request_completed", None)
    if declared is not None:
        return bool(declared), "" if declared else "PROVIDER_REQUEST_OUTCOME_DECLARED_UNKNOWN"
    if fired_a_total_deadline(exc):
        return False, "PROVIDER_REQUEST_DEADLINE_EXCEEDED"
    if carries_a_complete_provider_response(exc):
        return True, ""
    if failed_before_the_request_was_sent(exc):
        return True, ""
    return False, "PROVIDER_REQUEST_OUTCOME_UNKNOWN"


def classify_outcome(exc: BaseException | None) -> ProviderVerdict:
    """THE classifier. Every surface that reasons about a provider attempt
    calls this one function, so two surfaces cannot disagree about one event.

    Order matters and is deliberate:

    1. no exception -> SUCCESS.
    2. cancellation -> CANCELLATION, before anything reads the message: a
       cancelled run is not a provider verdict at all.
    3. a fired TOTAL DEADLINE -> TIMEOUT, before the status tests, so a
       deadline can never be talked into looking like an answer.
    4. the provider's own failure classes -> RATE_LIMIT / NON_RETRYABLE.
       ``engine_overloaded_error`` (503) lands in RATE_LIMIT with 429: both
       are the provider saying "not now", and neither is a model failure.
    5. an HTTP status the provider really produced -> by status family.
    6. a failure that never left the process -> RETRYABLE_FAILURE.
    7. anything else -> UNKNOWN, which holds the permit.
    """
    if exc is None:
        return SUCCESS_VERDICT

    proven, unproven_reason = completion_is_proven(exc)

    def verdict(outcome: ProviderOutcome, code: str | None = None) -> ProviderVerdict:
        return ProviderVerdict(
            outcome=outcome, provider_code=code, completion_proven=proven,
            unproven_reason="" if proven else unproven_reason,
            retry_after=retry_after_seconds(exc), headers=rate_limit_headers(exc))

    if isinstance(exc, CancellationRequested):
        return verdict(ProviderOutcome.CANCELLATION)
    if fired_a_total_deadline(exc):
        return verdict(ProviderOutcome.TIMEOUT)

    code = provider_failure_code(exc)
    if code == EXCEEDED_CURRENT_QUOTA:
        # Not transient. Retrying cannot create quota, so this is terminal for
        # the call rather than something to pace.
        return verdict(ProviderOutcome.NON_RETRYABLE_FAILURE, code)
    if code is not None:
        return verdict(ProviderOutcome.RATE_LIMIT, code)

    status = _claimed_status(exc) if carries_a_complete_provider_response(exc) else None
    if status is not None:
        if 500 <= status <= 599:
            return verdict(ProviderOutcome.RETRYABLE_FAILURE)
        if 400 <= status <= 499:
            return verdict(ProviderOutcome.NON_RETRYABLE_FAILURE)
        # A 1xx/2xx/3xx carried on an exception is not something this taxonomy
        # can name; it stays UNKNOWN rather than being guessed at.
        return verdict(ProviderOutcome.UNKNOWN)
    if failed_before_the_request_was_sent(exc):
        # Nothing was sent, so a later attempt is a genuinely fresh request.
        return verdict(ProviderOutcome.RETRYABLE_FAILURE)
    return verdict(ProviderOutcome.UNKNOWN)


# =============================================================================
# 2. THE ONE TOKEN-ADMISSION RULE
# =============================================================================

class UnknownTokenDemand(ValueError):
    """A request's token demand could not be bounded, so it is NOT admitted.

    Fail-closed on purpose. An admission value that is a guess cannot prove a
    hard TPM ceiling, and "we could not measure it" must never resolve to
    "assume it is small".
    """


class MissingOutputCap(ValueError):
    """A provider request reached admission without an explicit output cap."""


class TokenCeilingExceeded(ValueError):
    """One request's bounded demand alone exceeds a configured TPM ceiling."""


#: Structural framing the provider's chat template adds around each message
#: (role markers, separators). Small, fixed, and added on TOP of the measured
#: content so the bound stays an upper bound rather than a hope.
STRUCTURAL_TOKENS_PER_MESSAGE = 8
#: The same, for each tool definition the request advertises.
STRUCTURAL_TOKENS_PER_TOOL = 32

#: WHY BYTES, AND NOT ``chars / 4``.
#:
#: ``chars // 4`` is an AVERAGE for English prose. It is not a bound, and the
#: two places MILO uses it are both places where the average is the wrong
#: instrument:
#:
#: * Hebrew, which this product's market is written in, is 2 UTF-8 bytes per
#:   character, and a byte-level BPE tokenizer can emit up to one token per
#:   byte. ``chars // 4`` can therefore under-count real Hebrew input by up
#:   to 8x.
#: * JSON payloads, schemas and URLs tokenize far worse than prose.
#:
#: A byte-level BPE tokenizer merges bytes into tokens; it never splits one
#: byte into two. So for ANY input, ``tokens <= utf8_bytes``. That is a real
#: upper bound rather than an estimate, it needs no tokenizer, and it cannot
#: be wrong in the direction that breaches a shared ceiling.
CONSERVATIVE_BASIS = "conservative_byte_bound"
AUTHORITATIVE_BASIS = "authoritative_count"


@dataclass(frozen=True)
class TokenDemand:
    """What ONE request is admitted against, and how that number was got."""

    tokens: int
    basis: str
    input_tokens: int
    output_cap: int

    @property
    def is_authoritative(self) -> bool:
        return self.basis == AUTHORITATIVE_BASIS


#: An authoritative counter, when a deployment has one (a real tokenizer, or
#: the provider's own token-count endpoint). It receives ``(messages, tools)``
#: and returns an integer count of INPUT tokens. Anything else -- a raise, a
#: non-integer, a non-positive number -- falls back to the conservative bound,
#: never to an optimistic one.
TokenCounter = Callable[[Any, Any], int]
_token_counter: TokenCounter | None = None
_counter_lock = threading.Lock()


def register_token_counter(counter: TokenCounter | None) -> None:
    """Install (or clear) the authoritative input-token counter."""
    global _token_counter
    with _counter_lock:
        _token_counter = counter


def _refuse_unmeasurable(value: Any) -> Any:
    raise UnknownTokenDemand(
        "a provider request carries content whose token demand cannot be bounded")


def _measured_bytes(payload: Any) -> int:
    """UTF-8 byte length of the canonical serialization, or fail closed."""
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                          default=_refuse_unmeasurable)
    except UnknownTokenDemand:
        raise
    except (TypeError, ValueError) as exc:
        raise UnknownTokenDemand(
            "a provider request could not be serialized for token admission") from exc
    return len(body.encode("utf-8"))


def conservative_input_tokens(messages: Any, tools: Any = None) -> int:
    """An UPPER BOUND on the input tokens one request can cost.

    Bounded, never estimated: the serialized request's UTF-8 byte count plus
    fixed structural framing. Unmeasurable content raises rather than
    resolving to a small number.
    """
    if messages is None:
        raise UnknownTokenDemand("a provider request carried no messages to measure")
    try:
        message_count = len(messages)
    except TypeError:
        raise UnknownTokenDemand(
            "a provider request's messages are not a countable sequence") from None
    total = _measured_bytes(messages) + STRUCTURAL_TOKENS_PER_MESSAGE * message_count
    if tools:
        try:
            tool_count = len(tools)
        except TypeError:
            raise UnknownTokenDemand(
                "a provider request's tools are not a countable sequence") from None
        total += _measured_bytes(tools) + STRUCTURAL_TOKENS_PER_TOOL * tool_count
    return max(1, total)


def admission_demand(messages: Any, output_cap: Any, *, tools: Any = None,
                     counter: TokenCounter | None = None) -> TokenDemand:
    """The EXACT value the organization admits one request against.

    Kimi's rate limiter admits on request tokens PLUS ``max_completion_tokens``
    -- it does not wait to see how much output is actually generated. So the
    admission value is the input demand plus the *requested cap*, and a caller
    that omitted a cap cannot be admitted at all: charging such a request as
    "input only" would systematically under-count the organization TPM window
    and is exactly how a shared ceiling gets breached.

    The input side prefers an authoritative count and otherwise uses the
    conservative byte bound. It NEVER uses an average.
    """
    if output_cap is None:
        raise MissingOutputCap(
            "provider admission requires an explicit max_completion_tokens")
    try:
        cap = int(output_cap)
    except (TypeError, ValueError):
        raise MissingOutputCap("max_completion_tokens must be an integer") from None
    if cap <= 0:
        raise MissingOutputCap("max_completion_tokens must be positive")

    resolved = counter if counter is not None else _token_counter
    basis = CONSERVATIVE_BASIS
    inputs: int | None = None
    if resolved is not None:
        try:
            candidate = resolved(messages, tools)
        except Exception:  # noqa: BLE001 - a broken counter is not an admission
            candidate = None
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            # Authoritative for the CONTENT; the chat template's own framing
            # is still added, because a counter of the messages does not see
            # what the server wraps around them.
            try:
                message_count = len(messages or ())
            except TypeError:
                message_count = 1
            inputs = candidate + STRUCTURAL_TOKENS_PER_MESSAGE * message_count
            basis = AUTHORITATIVE_BASIS
    if inputs is None:
        inputs = conservative_input_tokens(messages, tools)
    return TokenDemand(tokens=inputs + cap, basis=basis, input_tokens=inputs,
                       output_cap=cap)


def assert_within_token_ceiling(demand: int, ceiling: int | None, *,
                                dimension: str = "tpm") -> None:
    """One request whose bounded demand alone exceeds a ceiling is refused.

    Sending it anyway would either be rejected by the provider or, worse,
    admitted while MILO's own window believed something smaller.
    """
    if ceiling is None:
        return
    if demand > ceiling:
        raise TokenCeilingExceeded(
            f"a single request's bounded token demand exceeds the configured "
            f"{dimension} ceiling")


# =============================================================================
# 3. SEARCH ACCOUNTING
# =============================================================================

#: Moonshot's SERVER-SIDE search, invoked as a builtin tool inside a Chat
#: Completions request. The provider runs and bills it inside the chat call,
#: so it is paced by the chat concurrency/RPM/TPM gate -- but it is still a
#: search the run performed, and it is accounted as one.
BUILTIN_WEB_SEARCH = "$web_search"
#: The standalone endpoints, which have their own QPS buckets.
SEARCH_BASIC = "search"
SEARCH_PRO = "search_pro"


def request_offers_builtin_search(request: Mapping[str, Any]) -> bool:
    """Does this chat request advertise the provider's builtin web search?"""
    for tool in (request.get("tools") or ()):
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if name == BUILTIN_WEB_SEARCH:
            return True
    return False


def builtin_searches_in_response(response: Any) -> int | None:
    """How many builtin searches the provider actually ran, or None.

    Counted from the tool calls the provider returned, which is what it
    charges for -- not from the fact that the tool was offered. A request that
    advertised search and got a plain answer performed no search and is
    counted as none.

    ``None`` means the count is UNKNOWN: the response is not a shape this can
    walk, so MILO cannot say how much the provider did. That is deliberately
    NOT the same answer as zero, and the difference is the whole point --
    settlement charges the full reservation for an unknown count, because an
    unmeasurable amount of provider spend is not an absence of spend.
    """
    choices = getattr(response, "choices", None)
    if choices is None:
        return None
    try:
        iter(choices)
    except TypeError:
        return None
    total = 0
    for choice in choices:
        message = getattr(choice, "message", None)
        if message is None:
            # A choice with no message at all is not something this can read.
            return None
        calls = getattr(message, "tool_calls", None)
        if calls is None:
            continue
        try:
            iter(calls)
        except TypeError:
            return None
        for call in calls:
            function = getattr(call, "function", None)
            if getattr(function, "name", None) == BUILTIN_WEB_SEARCH:
                total += 1
    return total


# =============================================================================
# 4. THE ONE ADAPTER
# =============================================================================

class ProviderAdapter:
    """The single provider call path for BOTH engines.

    V1 and V2 hand it a request; it owns everything between that and the
    response:

    * the explicit output cap contract (no cap, no call);
    * the conservative token-admission rule;
    * admission through the process-local AND organization-wide gates, on
      EVERY attempt including retries;
    * bounded backpressure retries, which are its business alone -- no engine
      runs a second retry loop;
    * the total request deadline, through the client it builds;
    * search invocation/cost accounting;
    * settlement of the organization permit on PROVEN completion only.

    It holds no engine state, so one instance serves a whole worker process
    and both engines draw on ONE organization allowance rather than two that
    each believe they own the account.
    """

    def __init__(self, scheduler: Any, *, tracker: Any = None,
                 client_factory: Callable[[str, str], Any] | None = None,
                 request_deadline_seconds: float | None = None,
                 token_counter: TokenCounter | None = None,
                 search_executor: Callable[..., Any] | None = None) -> None:
        self._scheduler = scheduler
        self._tracker = tracker
        self._client_factory = client_factory
        self._request_deadline_seconds = request_deadline_seconds
        self._token_counter = token_counter
        self._search_executor = search_executor
        self._clients: dict[tuple[str, str], Any] = {}
        self._client_lock = threading.Lock()

    # -- what the authority IS -------------------------------------------
    @property
    def scheduler(self) -> Any:
        return self._scheduler

    @property
    def config(self) -> Any:
        return getattr(self._scheduler, "config", None)

    @property
    def coordinator(self) -> Any:
        return getattr(self._scheduler, "_coordinator", None)

    @property
    def tracker(self) -> Any:
        return self._tracker

    @property
    def search_executor(self) -> Callable[..., Any] | None:
        """The transport ONE admitted search is performed through, if any.

        ``None`` means this process has no standalone search configured, and
        `run_search` then refuses BEFORE admission rather than debiting the
        run for a search it could not perform -- and, far more importantly,
        rather than falling back to a provider-executed capability whose
        multiplicity MILO cannot admit.
        """
        return self._search_executor

    # -- the client, built in ONE place ----------------------------------
    def default_client_factory(self, api_key: str, base_url: str) -> Any:
        """The provider client every unguarded/local path gets.

        ``max_retries=0`` is load-bearing, not tidiness: the SDK retries
        retryable failures TWICE by default, turning one logical request into
        up to three provider attempts that consume organization RPM and
        concurrency while MILO's scheduler, budget, attempt accounting and
        distributed limiter never see them. Retries are MILO-owned, bounded,
        and re-enter the shared admission gate.

        The http client is the total-deadline one: an inactivity timeout does
        not bound a request at all, so the deadline transport is what makes a
        request's lifetime finite.
        """
        from openai import OpenAI

        from backend.budget import (build_provider_http_client,
                                    provider_request_timeout,
                                    resolved_request_deadline)

        deadline = resolved_request_deadline(self._request_deadline_seconds)
        return OpenAI(api_key=api_key, base_url=base_url, max_retries=0,
                      http_client=build_provider_http_client(deadline),
                      timeout=provider_request_timeout(deadline))

    def client(self, api_key: str, base_url: str, *,
               factory: Callable[[str, str], Any] | None = None) -> Any:
        """One cached client per (key, base url), built by ONE factory."""
        build = factory or self._client_factory or self.default_client_factory
        cache_key = (str(api_key), str(base_url))
        with self._client_lock:
            existing = self._clients.get(cache_key)
            if existing is not None and factory is None:
                return existing
            client = build(api_key, base_url)
            if factory is None:
                self._clients[cache_key] = client
            return client

    # -- token admission --------------------------------------------------
    def token_demand(self, messages: Any, output_cap: Any,
                     tools: Any = None) -> TokenDemand:
        demand = admission_demand(messages, output_cap, tools=tools,
                                  counter=self._token_counter)
        config = self.config
        assert_within_token_ceiling(demand.tokens, getattr(config, "tpm_limit", None))
        coordinator = self.coordinator
        organization = getattr(getattr(coordinator, "config", None), "max_tpm", None)
        assert_within_token_ceiling(demand.tokens, organization,
                                    dimension="organization tpm")
        return demand

    # -- search accounting -------------------------------------------------
    @property
    def max_builtin_searches_per_request(self) -> int:
        """The MOST searches one request may perform, reserved before dispatch.

        Read from the run's own budget configuration, which derives it from
        the canonical runtime policy, so the number admission reserves and the
        number the policy publishes cannot drift.
        """
        config = getattr(self._tracker, "config", None)
        bound = getattr(config, "max_builtin_searches_per_request", None)
        try:
            return max(1, int(bound))
        except (TypeError, ValueError):
            return 1

    def _reserve_searches(self, count: int) -> int | None:
        """Hold worst-case search capacity before a request is dispatched."""
        tracker = self._tracker
        if tracker is None or count <= 0:
            return None
        reserve = getattr(tracker, "reserve_search", None)
        if not callable(reserve):
            return None
        try:
            return reserve(count)
        except BaseException as exc:
            # A refusal here happens BEFORE anything is sent, exactly like the
            # guarded client's `open_call`. Saying so is load-bearing: the
            # scheduler settles the organization permit on whether the request
            # can be proven finished, and an unmarked refusal would quarantine
            # a shared slot the request never used.
            exc.provider_request_completed = True
            raise

    def _settle_searches(self, reservation: int | None, actual: int | None,
                         *, after: BaseException | None = None) -> None:
        """Release a reservation, without changing what is KNOWN about the request.

        Settlement can itself refuse -- a tripped search or cost ceiling -- and
        that refusal escapes the callable the scheduler settles the
        organization permit on. It must not be allowed to change the verdict:

        * after a response was read to completion, the exchange is over
          whatever the accounting then says, so the refusal is marked proven;
        * after a provider failure, the ORIGINAL exception's verdict is
          carried across, or an ordinary 429 would start holding a shared slot
          until a human reclaimed it.

        This is the same rule `backend.budget`'s guarded client applies to its
        own settlement, for the same reason.
        """
        tracker = self._tracker
        if tracker is None or reservation is None:
            return
        settle = getattr(tracker, "settle_search", None)
        if not callable(settle):
            return
        try:
            settle(reservation, actual=actual)
        except BaseException as refusal:
            refusal.provider_request_completed = (
                True if after is None else classify_outcome(after).completion_proven)
            raise

    def _searches_performed(self, response: Any) -> int | None:
        return builtin_searches_in_response(response)

    def _searches_after_failure(self, exc: BaseException) -> int | None:
        """What an attempt that RAISED spent on search. Unknown by default.

        The one case that is genuinely zero is structural, and it is the same
        structure the concurrency invariant uses: a failure that happened
        before the request was ever sent. Nothing ran, so charging for it
        would over-report spend, and a ledger that over-reports is as wrong as
        one that under-reports.

        Everything else -- a provider error, a fired deadline, an outcome
        nobody named -- is UNKNOWN, and unknown fails closed onto the whole
        reservation.
        """
        if failed_before_the_request_was_sent(exc):
            return 0
        return None

    # -- the guarded call path ---------------------------------------------
    def chat(self, request: Mapping[str, Any], *, client: Any = None,
             api_key: str = "", base_url: str = "", agent: str = "",
             phase: str = "", client_factory: Callable[[str, str], Any] | None = None,
             ) -> Any:
        """Run ONE logical provider chat request under the whole authority."""
        payload = dict(request)
        target = client if client is not None else self.client(
            api_key, base_url, factory=client_factory)

        from backend.budget import read_output_cap

        cap = read_output_cap(payload)
        if cap is None:
            raise MissingOutputCap(
                "every provider request must carry an explicit output cap")
        demand = self.token_demand(payload.get("messages"), cap,
                                   payload.get("tools"))
        searching = request_offers_builtin_search(payload)

        def attempt() -> Any:
            """ONE provider attempt, with its own search reservation.

            PER ATTEMPT, not per logical call, because a retry is a real
            provider request that can perform its own searches. Reserving once
            outside the retry loop would bound the first attempt and nothing
            after it.
            """
            # The WORST CASE this request could spend, held before it is
            # dispatched. A run that cannot pay for the maximum does not get
            # to send the request and find out afterwards. A request that
            # offers no search holds nothing, and settling nothing is a no-op,
            # so there stays exactly ONE provider call site.
            reservation = (self._reserve_searches(self.max_builtin_searches_per_request)
                           if searching else None)
            try:
                response = target.chat.completions.create(**payload)
            except BaseException as exc:
                self._settle_searches(reservation, self._searches_after_failure(exc),
                                      after=exc)
                raise
            self._settle_searches(reservation, self._searches_performed(response))
            return response

        return self._scheduler.execute(
            attempt,
            estimated_tokens=demand.tokens,
            reserved_tokens=demand.tokens,
            agent=agent,
            phase=phase,
        )

    def search(self, endpoint: str, *, agent: str = "", phase: str = "",
               max_wait_seconds: float | None = None) -> float:
        """Admit and account ONE standalone Web Search request.

        The QPS bucket is the provider's; the invocation and cost counters are
        the run's. Both are consulted, in that order, so a search is never
        performed by a run that has no search allowance left.

        This is the ADMISSION half, and it completes BEFORE the search runs
        (see `run_search`). The charge is committed here on purpose: a
        settlement that waited for the search to come back could be lost to a
        crash between the request and the reply, and a performed search that
        no longer appears in the ledger is a refund by another name. Charging
        first can only ever over-report -- the fail-closed direction, and the
        same one an unknown builtin count takes.
        """
        tracker = self._tracker
        reserve = getattr(tracker, "reserve_search", None) if tracker else None
        reservation = reserve(1) if callable(reserve) else None
        try:
            waited = self._scheduler.admit_search(endpoint, agent=agent, phase=phase,
                                                  max_wait_seconds=max_wait_seconds)
        except BaseException:
            # The QPS bucket refused, so no search happened: release the hold
            # rather than charging for one MILO never got to perform.
            self._settle_searches(reservation, 0)
            raise
        self._settle_searches(reservation, 1)
        return waited

    def run_search(self, query: Any, *, endpoint: str = SEARCH_BASIC,
                   agent: str = "", phase: str = "",
                   executor: Callable[..., Any] | None = None,
                   max_wait_seconds: float | None = None) -> "SearchOutcome":
        """Admit, perform and account ONE mediated standalone search.

        This is the whole of MILO's internet capability for an engine that
        does not hand the provider a builtin search tool, and the ORDER is
        the reason it is a ceiling rather than a report:

        1. the query and the transport are resolved -- both are decidable
           without spending anything, so a malformed ask or an unconfigured
           deployment costs the run nothing;
        2. the run's own `max_search_invocations_per_run` allowance is taken;
        3. the endpoint's QPS bucket is taken;
        4. the invocation and its cost are charged, durably;
        5. and only then is exactly ONE search performed.

        Steps 2-4 are `search` above. Nothing between here and the provider
        can multiply step 5: one call to this method is one search, and the
        next one re-enters at step 1. A model that wants to research more asks
        again and is admitted again -- or refused, with nothing performed.

        A transport failure after step 4 is NOT refunded. MILO cannot know
        whether the provider ran the search before the failure, and an unknown
        amount of provider spend is not an absence of spend; the outcome
        reports the failure to the caller with `admitted` set, so the model is
        told the search produced nothing rather than being handed invented
        results.
        """
        from backend.standalone_search import (SearchOutcome, SearchUnavailable,
                                               default_search_executor,
                                               normalize_query, normalize_results)

        # -- decided before anything is spent -------------------------------
        text = normalize_query(query)
        execute = executor or self._search_executor or default_search_executor()
        if not callable(execute):
            raise SearchUnavailable(
                "no standalone search transport is configured for this process")
        ready = getattr(execute, "available", None)
        if callable(ready) and not ready():
            raise SearchUnavailable(
                "the configured standalone search transport is not usable")

        # -- admitted, paced and charged, in that order ---------------------
        self.search(endpoint, agent=agent, phase=phase,
                    max_wait_seconds=max_wait_seconds)

        # -- exactly one search ---------------------------------------------
        try:
            results = execute(text, endpoint=endpoint)
        except BaseException as exc:
            # A run-level stop or a cancellation is never a search result: it
            # is the run ending, and it must not be reported to a model as an
            # empty search.
            from backend.budget import BudgetExceeded
            from backend.runtime import CancellationRequested

            if isinstance(exc, (BudgetExceeded, CancellationRequested)):
                raise
            if not isinstance(exc, Exception):
                raise
            return SearchOutcome(query=text, endpoint=endpoint, admitted=True,
                                 error=f"search failed: {type(exc).__name__}")
        return SearchOutcome(query=text, endpoint=endpoint, admitted=True,
                             results=normalize_results(results))


def build_provider_adapter(limits: Any = None, *, tracker: Any = None,
                           coordinator: Any = None,
                           client_factory: Callable[[str, str], Any] | None = None,
                           request_deadline_seconds: float | None = None,
                           sleep_fn: Callable[[float], None] | None = None,
                           clock: Callable[[], float] | None = None,
                           cancellation_checker: Callable[[], bool] | None = None,
                           backpressure_callback: Any = None,
                           token_counter: TokenCounter | None = None,
                           search_executor: Callable[..., Any] | None = None,
                           ) -> ProviderAdapter:
    """Build THE adapter for a worker process. One per process, not per engine."""
    from backend.provider_scheduler import ProviderLimitsConfig, ProviderScheduler

    scheduler = ProviderScheduler(
        limits if limits is not None else ProviderLimitsConfig.from_env(),
        sleep_fn=sleep_fn, clock=clock,
        cancellation_checker=cancellation_checker,
        backpressure_callback=backpressure_callback,
        coordinator=coordinator)
    return ProviderAdapter(scheduler, tracker=tracker, client_factory=client_factory,
                           request_deadline_seconds=request_deadline_seconds,
                           token_counter=token_counter,
                           search_executor=search_executor)


__all__ = [
    "AUTHORITATIVE_BASIS", "BUILTIN_WEB_SEARCH", "CONSERVATIVE_BASIS",
    "ENGINE_OVERLOADED", "EXCEEDED_CURRENT_QUOTA", "MissingOutputCap",
    "ProviderAdapter", "ProviderOutcome", "ProviderVerdict",
    "RATE_LIMIT_REACHED", "SEARCH_BASIC", "SEARCH_PRO", "SEARCH_RATE_LIMITED",
    "SEARCH_RATE_LIMIT_UNAVAILABLE", "TokenCeilingExceeded", "TokenDemand",
    "UnknownTokenDemand", "admission_demand", "assert_within_token_ceiling",
    "build_provider_adapter", "builtin_searches_in_response",
    "classify_outcome", "completion_is_proven", "conservative_input_tokens",
    "provider_failure_code", "rate_limit_headers", "register_token_counter",
    "request_offers_builtin_search", "retry_after_seconds",
]
