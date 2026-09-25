"""Exact usage accounting for reasoning models (PR-R).

What ONE settled provider response consumed, read from its ``usage`` block
without ever touching ``reasoning_content``:

* ``input_tokens``          -- ``usage.prompt_tokens``;
* ``cached_input_tokens``   -- ``usage.prompt_tokens_details.cached_tokens``
                               (Moonshot also repeats it as ``usage.cached_tokens``);
* ``cache_write_tokens``    -- ``usage.prompt_tokens_details.cache_write_tokens``;
* ``output_tokens``         -- ``usage.completion_tokens`` (INCLUDES reasoning);
* ``reasoning_tokens``      -- ``usage.completion_tokens_details.reasoning_tokens``;
* ``answer_tokens``         -- the token count of ``message.content`` ONLY.

A field the provider did not send is ``None``, never 0: "not reported" and
"zero" are different facts, and a calibration built on an invented zero would
be wrong in exactly the direction that matters.

Moonshot's official documentation does not describe
``completion_tokens_details.reasoning_tokens`` (verified 2026-09-25; only a
third-party gateway does). When it is absent the reasoning share is ESTIMATED
as ``completion_tokens - answer_tokens`` and flagged ``reasoning_estimated``;
the estimate is recorded separately (``reasoning_tokens_estimated``) and is
never presented as a provider-reported count. The first call of a run that
lacks the field emits the static diagnostic ``USAGE_REASONING_FIELD_ABSENT``
once, so the first live run states which case applies.

``reasoning_content`` is never read, stored, tokenized or re-sent. This module
reads ``message.content`` and the ``usage`` block and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.model_profiles import CACHE_TTL_1H, CACHE_TTL_5M

USAGE_REASONING_FIELD_ABSENT = "USAGE_REASONING_FIELD_ABSENT"

#: How ``answer_tokens`` was counted. No Kimi tokenizer is vendored, so unless
#: a deployment registers an authoritative counter the count is a
#: deterministic pre-tokenization (below) and is labelled as such.
ANSWER_TOKENS_AUTHORITATIVE = "authoritative"
ANSWER_TOKENS_APPROXIMATE = "approximate"

# One token per: a run of up to 4 ASCII letters/digits, a single non-ASCII
# character (CJK, Hebrew, etc. tokenize at roughly a character each in BPE
# vocabularies), or a single punctuation/symbol character. Whitespace is
# absorbed into the following piece, as BPE vocabularies do.
_PIECES = re.compile(r"[A-Za-z0-9]{1,4}|[^\x00-\x7f]|[^\sA-Za-z0-9]")


def _get(container: Any, name: str) -> Any:
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get(name)
    return getattr(container, name, None)


def _count(value: Any) -> int | None:
    """A reported token count, or None when absent or not a count."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value == int(value):
        return int(value)
    return None


def count_content_tokens(text: Any) -> tuple[int | None, str]:
    """Tokenize the ANSWER text only. Returns ``(count, basis)``.

    Uses the deployment's authoritative token counter when one is registered
    (``backend.provider_authority.register_token_counter``); otherwise the
    deterministic approximation above. A non-text answer (a provider that
    returned a parsed object) is serialized first; an absent answer is 0.
    """
    if text is None:
        return 0, ANSWER_TOKENS_APPROXIMATE
    if not isinstance(text, str):
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", errors="replace")
        else:
            import json

            try:
                text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return None, ANSWER_TOKENS_APPROXIMATE
    if not text:
        return 0, ANSWER_TOKENS_APPROXIMATE
    from backend import provider_authority

    counter = provider_authority._token_counter
    if counter is not None:
        try:
            counted = counter([{"role": "assistant", "content": text}], None)
        except Exception:  # noqa: BLE001 - a broken counter falls back, never raises
            counted = None
        if isinstance(counted, int) and not isinstance(counted, bool) and counted >= 0:
            return counted, ANSWER_TOKENS_AUTHORITATIVE
    return len(_PIECES.findall(text)), ANSWER_TOKENS_APPROXIMATE


def response_content(response: Any) -> Any:
    """``choices[0].message.content`` -- and only that -- or None."""
    try:
        choices = _get(response, "choices")
        message = _get(choices[0], "message")
    except (IndexError, KeyError, TypeError):
        return None
    return _get(message, "content")


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    cache_write_ttl: str
    reasoning_tokens: int | None
    answer_tokens: int | None
    answer_tokens_basis: str
    reasoning_tokens_estimated: int | None
    reasoning_estimated: bool

    def ledger_fields(self) -> dict[str, Any]:
        """The per-call ledger columns. ``None`` stays ``None``."""
        return {
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "reasoning_tokens_estimated": self.reasoning_tokens_estimated,
            "reasoning_estimated": self.reasoning_estimated,
            "answer_tokens": self.answer_tokens,
        }


def _cache_write_ttl(usage: Any, details: Any) -> str:
    """The TTL tier the provider reports for cache writes.

    V2 never requests explicit or 1-hour caching, so an absent report is the
    default 5-minute tier. A report that names anything other than the
    5-minute tier is priced at the dearer 1-hour tier: an unrecognised value
    is never under-charged.
    """
    for source in (details, usage):
        for name in ("cache_write_ttl", "cache_ttl"):
            value = _get(source, name)
            if value is None:
                continue
            return CACHE_TTL_5M if str(value).strip().lower() in {"5m", "5min", "300", "300s"} \
                else CACHE_TTL_1H
    return CACHE_TTL_5M


def read_usage(response: Any) -> UsageBreakdown:
    """The usage breakdown of ONE settled response. Never reads reasoning_content."""
    usage = _get(response, "usage")
    prompt_details = _get(usage, "prompt_tokens_details")
    completion_details = _get(usage, "completion_tokens_details")
    input_tokens = _count(_get(usage, "prompt_tokens")) or 0
    output_tokens = _count(_get(usage, "completion_tokens")) or 0
    cached = _count(_get(prompt_details, "cached_tokens"))
    if cached is None:
        cached = _count(_get(usage, "cached_tokens"))
    cache_write = _count(_get(prompt_details, "cache_write_tokens"))
    reasoning = _count(_get(completion_details, "reasoning_tokens"))
    answer, basis = count_content_tokens(response_content(response))
    estimated: int | None = None
    if reasoning is None and answer is not None:
        estimated = max(0, output_tokens - answer)
    return UsageBreakdown(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached, cache_write_tokens=cache_write,
        cache_write_ttl=_cache_write_ttl(usage, prompt_details),
        reasoning_tokens=reasoning, answer_tokens=answer, answer_tokens_basis=basis,
        reasoning_tokens_estimated=estimated,
        reasoning_estimated=reasoning is None,
    )


__all__ = ["ANSWER_TOKENS_APPROXIMATE", "ANSWER_TOKENS_AUTHORITATIVE",
           "USAGE_REASONING_FIELD_ABSENT", "UsageBreakdown", "count_content_tokens",
           "read_usage", "response_content"]
