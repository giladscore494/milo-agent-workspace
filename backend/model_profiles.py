"""The fail-closed Model Profile Registry (PR-R).

ONE server-owned description of every model MILO may send a paid request to:
its prices, its context window, how it reasons, how its reasoning is
controlled, which field carries its output cap, which request parameters it
refuses and which structured-output formats it supports.

It replaced ``backend.model_pricing``'s bare price table (that compatibility
module was removed in cleanup PR-3), which failed OPEN:
an unrecognised model was priced at 0.0, so a deployment that switched the
Commander to a model the table did not name would have run with every dollar
ceiling (per run, per user per day, per project per day) silently disarmed.
Here an unrecognised model is refused with a static reason code, at worker
boot in the paid posture and again on every call, BEFORE any provider request
exists. There is no zero price and no default profile.

Prices are USD per 1,000,000 tokens and are held as ``Decimal`` so that a
reservation and a settlement computed from the same numbers can never drift
apart through float rounding.

Provenance of the numbers (MILO_V2_REASONING_BUDGET_PR_SPEC.md, PR-R):

* ``kimi-k3`` -- operator-verified pricing page
  (platform.kimi.ai/docs/pricing/chat, 2026-09-25): input cache miss 3.00,
  input cache hit 0.30, cache write 3.00 for the 5-minute TTL tier and 6.00
  for the 1-hour tier, output 15.00. Output INCLUDES reasoning tokens: the
  provider documents that ``reasoning_content`` and ``content`` together must
  fit inside ``max_completion_tokens`` and both are billed as output.
* ``kimi-k2.6`` -- same page: 0.95 miss / 0.16 hit / 4.00 output, with no
  separate cache-write charge. The previous table said 0.60 / 2.50, which
  under-recorded every V1 and V2 call by 37-60%; this is the one V1-visible
  change PR-R makes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal, Mapping

#: Static reason codes. Every refusal in this module carries exactly one, and
#: never the value that was refused.
MODEL_PROFILE_UNKNOWN = "MODEL_PROFILE_UNKNOWN"
MODEL_NOT_ALLOWLISTED = "MODEL_NOT_ALLOWLISTED"
SWARM_MODEL_CONFIG_INVALID = "SWARM_MODEL_CONFIG_INVALID"

ReasoningMode = Literal["none", "optional", "always"]
ReasoningControl = Literal["none", "thinking_toggle", "reasoning_effort"]
OutputCapField = Literal["max_tokens", "max_completion_tokens"]

#: The only two cache lifetimes the provider bills separately. V2 never asks
#: for the 1-hour tier; it can only appear if the provider reports it.
CACHE_TTL_5M = "5m"
CACHE_TTL_1H = "1h"

_PER_MILLION = Decimal(1_000_000)
#: Money is carried to eight decimal places, the precision the ledger stores.
_MONEY_QUANTUM = Decimal("0.00000001")


class UnknownModelProfile(ValueError):
    """A model with no registered profile. Refused before any provider call."""

    code = MODEL_PROFILE_UNKNOWN

    def __init__(self) -> None:
        # Deliberately value-free: the refused model name comes from
        # configuration and is not echoed into logs or durable state.
        super().__init__("the model has no registered profile; paid calls to it are refused")


class ModelConfigError(ValueError):
    """A Swarm V2 model configuration that cannot be honoured. Static code only."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.safe_message = message
        super().__init__(message)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class ModelProfile:
    model: str
    provider: str
    #: USD per 1M input tokens that were NOT served from the context cache.
    price_input_miss: Decimal
    #: USD per 1M input tokens served from the context cache.
    price_input_hit: Decimal
    #: USD per 1M input tokens WRITTEN to the cache, per TTL tier. ``None``
    #: means the provider does not bill cache writes separately for this
    #: model, and written tokens are ordinary (miss) input.
    price_cache_write_5m: Decimal | None
    price_cache_write_1h: Decimal | None
    #: USD per 1M output tokens. Includes reasoning tokens.
    price_output: Decimal
    context_window: int
    reasoning: ReasoningMode
    reasoning_control: ReasoningControl
    #: Values the provider accepts for ``reasoning_effort`` (reasoning_effort
    #: control only), weakest first.
    reasoning_effort_values: tuple[str, ...]
    output_cap_field: OutputCapField
    #: Request parameters the provider fixes or rejects for this model. A
    #: caller that tries to send one is refused; they are never forwarded.
    forbidden_params: frozenset[str]
    response_formats: frozenset[str]
    #: Whether strict ``json_schema`` output has been PROVEN against the live
    #: provider for this model. Off by default: even with ``json_schema`` in
    #: ``response_formats``, the builder uses it only when this is set AND the
    #: role's schema passes ``is_strict_compatible``; otherwise json_object.
    strict_json_schema_verified: bool = False

    def cache_write_price(self, ttl: str) -> Decimal:
        """The per-1M price of a cache-written input token at ``ttl``."""
        if ttl == CACHE_TTL_5M:
            price = self.price_cache_write_5m
        else:
            # Anything that is not the 5-minute tier is priced at the 1-hour
            # tier, the dearer one: an unrecognised TTL is never under-charged.
            price = self.price_cache_write_1h
        return self.price_input_miss if price is None else price

    @property
    def reserve_input_price(self) -> Decimal:
        """The per-1M input price a WORST-CASE reservation uses.

        ``max(miss, 5-minute write)``: under default behaviour (V2 never asks
        for explicit or 1-hour caching) an input token is billed at most at
        one of those two rates. For kimi-k3 that is 3.00.
        """
        write = self.price_cache_write_5m
        return self.price_input_miss if write is None else max(self.price_input_miss, write)

    def usage_cost(self, *, input_tokens: int, output_tokens: int,
                   cached_input_tokens: int | None = None,
                   cache_write_tokens: int | None = None,
                   cache_write_ttl: str = CACHE_TTL_5M) -> Decimal:
        """Price ONE settled call from its usage breakdown.

        Cached input at the hit price, cache-written input at the applicable
        write tier, every other input token at the miss price, and ALL output
        -- reasoning included -- at the output price. An absent breakdown
        field prices those tokens as ordinary miss input, which is never
        cheaper than the hit price. A breakdown that claims more cached or
        written tokens than the prompt had is clamped to the prompt, so a
        malformed usage block can never produce a NEGATIVE miss count.
        """
        prompt = max(0, int(input_tokens or 0))
        output = max(0, int(output_tokens or 0))
        cached = min(prompt, max(0, int(cached_input_tokens or 0)))
        written = min(prompt - cached, max(0, int(cache_write_tokens or 0)))
        missed = prompt - cached - written
        total = (Decimal(missed) * self.price_input_miss
                 + Decimal(cached) * self.price_input_hit
                 + Decimal(written) * self.cache_write_price(cache_write_ttl)
                 + Decimal(output) * self.price_output) / _PER_MILLION
        return _money(total)

    def worst_case_cost(self, input_upper_bound: int, max_output: int) -> Decimal:
        """The most ONE call can cost: every input token at the reserve rate
        and every allowed output token (reasoning included) billed."""
        total = (Decimal(max(0, int(input_upper_bound))) * self.reserve_input_price
                 + Decimal(max(0, int(max_output))) * self.price_output) / _PER_MILLION
        return _money(total)


# The K3 request parameters the provider fixes (temperature=1.0, top_p=0.95,
# n=1, presence_penalty=0, frequency_penalty=0 per the Kimi K3 quickstart).
# `thinking` is the kimi-k2.6 control and K3 cannot turn thinking off, so it is
# refused on K3 too: the two reasoning controls never share one request.
_K3_FORBIDDEN = frozenset({"temperature", "top_p", "n", "presence_penalty",
                           "frequency_penalty", "thinking"})
# kimi-k2.6 is controlled by the `thinking` toggle; `reasoning_effort` is the K3
# control and is refused here so the two never appear together.
_K26_FORBIDDEN = frozenset({"reasoning_effort"})

PROFILES: Mapping[str, ModelProfile] = {
    "kimi-k3": ModelProfile(
        model="kimi-k3", provider="moonshot",
        price_input_miss=Decimal("3.00"), price_input_hit=Decimal("0.30"),
        price_cache_write_5m=Decimal("3.00"), price_cache_write_1h=Decimal("6.00"),
        price_output=Decimal("15.00"),
        context_window=1_048_576,
        reasoning="always", reasoning_control="reasoning_effort",
        reasoning_effort_values=("low", "high", "max"),
        output_cap_field="max_completion_tokens",
        forbidden_params=_K3_FORBIDDEN,
        # json_object ONLY until strict json_schema is proven against the
        # live provider (PR #124 review): Kimi strict mode requires every
        # property in `required` and additionalProperties:false everywhere,
        # and the CommanderPlan / verifier-batch schemas satisfy neither. The
        # json_schema path stays in the builder behind
        # `strict_json_schema_verified` and `is_strict_compatible`.
        response_formats=frozenset({"json_object"}),
    ),
    "kimi-k2.6": ModelProfile(
        model="kimi-k2.6", provider="moonshot",
        price_input_miss=Decimal("0.95"), price_input_hit=Decimal("0.16"),
        price_cache_write_5m=None, price_cache_write_1h=None,
        price_output=Decimal("4.00"),
        context_window=262_144,
        reasoning="optional", reasoning_control="thinking_toggle",
        reasoning_effort_values=(),
        # The field V1 has always sent to kimi-k2.6; unchanged.
        output_cap_field="max_tokens",
        forbidden_params=_K26_FORBIDDEN,
        response_formats=frozenset({"json_object"}),
    ),
}


def get_profile(model: str) -> ModelProfile:
    """The registered profile for ``model``, or :class:`UnknownModelProfile`."""
    profile = PROFILES.get(str(model or "").strip())
    if profile is None:
        raise UnknownModelProfile()
    return profile


def has_profile(model: str) -> bool:
    return str(model or "").strip() in PROFILES


def validate_swarm_model_contract(env: Mapping[str, str], *,
                                  require_present: bool) -> tuple[str, str, tuple[str, ...]]:
    """Check the Swarm V2 model configuration against the registry.

    Returns ``(commander_model, worker_model, allowlist)``. With
    ``require_present`` (the worker, about to run a paid Swarm V2 run) every
    value must be set; without it (shared configuration validation, where an
    API deployment legitimately carries none of them) only the values that
    ARE set are checked. Either way:

    * every allowlisted model and both role models must have a profile;
    * the Commander model AND the worker model must be on the allowlist
      (the worker model used to be checked against nothing).
    """
    allowlist = tuple(filter(None, (item.strip() for item in
                                    str(env.get("MILO_COMMANDER_MODEL_ALLOWLIST") or "").split(","))))
    commander = str(env.get("MILO_COMMANDER_MODEL") or "").strip()
    worker = str(env.get("MILO_SWARM_WORKER_MODEL") or "").strip()
    if require_present and (not allowlist or not commander or not worker):
        raise ModelConfigError(SWARM_MODEL_CONFIG_INVALID,
                               "Swarm V2 model configuration is incomplete")
    for model in (*allowlist, commander, worker):
        if model and not has_profile(model):
            raise ModelConfigError(MODEL_PROFILE_UNKNOWN,
                                   "a configured Swarm V2 model has no registered profile")
    for model in (commander, worker):
        if model and allowlist and model not in allowlist:
            raise ModelConfigError(MODEL_NOT_ALLOWLISTED,
                                   "a configured Swarm V2 role model is not on the allowlist")
        if model and not allowlist and require_present:
            raise ModelConfigError(MODEL_NOT_ALLOWLISTED,
                                   "a configured Swarm V2 role model is not on the allowlist")
    return commander, worker, allowlist


__all__ = ["CACHE_TTL_1H", "CACHE_TTL_5M", "MODEL_NOT_ALLOWLISTED", "MODEL_PROFILE_UNKNOWN",
           "ModelConfigError", "ModelProfile", "PROFILES", "SWARM_MODEL_CONFIG_INVALID",
           "UnknownModelProfile", "get_profile", "has_profile",
           "validate_swarm_model_contract"]
