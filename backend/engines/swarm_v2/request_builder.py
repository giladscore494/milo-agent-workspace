"""Build ONE Swarm V2 provider request from a model profile (PR-R).

Every Swarm V2 request is assembled here, from the model's registered
:class:`~backend.model_profiles.ModelProfile` and the role's
:class:`RolePolicy`, and from nothing else:

* the output cap is written under the profile's OWN field --
  ``max_completion_tokens`` for kimi-k3, ``max_tokens`` for kimi-k2.6 -- and
  exactly once;
* reasoning is ALWAYS stated explicitly. kimi-k3 receives ``reasoning_effort``;
  kimi-k2.6 receives ``extra_body={"thinking": {"type": "enabled"|"disabled"}}``.
  Relying on a provider default is how run 4761a8ce thought through its whole
  4,000-token cap without MILO ever having asked it to think. The two controls
  never appear in one request;
* parameters the profile forbids (K3 fixes temperature, top_p, n,
  presence_penalty and frequency_penalty) are never sent, and a caller that
  tries to send one -- or any parameter this builder does not own -- is
  refused with a static code before anything reaches the provider;
* structured output is ``json_schema`` with ``strict: true`` when the model
  supports it and the caller has a schema, otherwise ``json_object``.

The builder never adds ``reasoning_content`` to a message and never requests
explicit or 1-hour context caching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from backend.model_profiles import ModelProfile

MODEL_PARAM_FORBIDDEN = "MODEL_PARAM_FORBIDDEN"
MODEL_EFFORT_UNSUPPORTED = "MODEL_EFFORT_UNSUPPORTED"
MODEL_RESPONSE_FORMAT_UNSUPPORTED = "MODEL_RESPONSE_FORMAT_UNSUPPORTED"
MODEL_OUTPUT_CAP_INVALID = "MODEL_OUTPUT_CAP_INVALID"
MODEL_MESSAGE_INVALID = "MODEL_MESSAGE_INVALID"
#: Same code the registry and the guarded client use (backend.model_profiles).
MODEL_PROFILE_UNKNOWN = "MODEL_PROFILE_UNKNOWN"

REQUEST_REFUSAL_CODES = frozenset({
    MODEL_PARAM_FORBIDDEN, MODEL_EFFORT_UNSUPPORTED, MODEL_RESPONSE_FORMAT_UNSUPPORTED,
    MODEL_OUTPUT_CAP_INVALID, MODEL_MESSAGE_INVALID, MODEL_PROFILE_UNKNOWN,
})

#: The server-owned reasoning vocabulary, weakest first. ``none`` means "do
#: not think" and is only honoured by a model whose thinking can be turned off.
EFFORT_LADDER: tuple[str, ...] = ("none", "low", "high", "max")

#: Message keys a Swarm V2 prompt may carry. `reasoning_content` is absent on
#: purpose: V2 is single-turn and never re-prompts a model's reasoning.
_MESSAGE_KEYS = frozenset({"role", "content"})


class ModelRequestRefused(RuntimeError):
    """A provider request that must not be built. Static code only."""

    MESSAGES = {
        MODEL_PARAM_FORBIDDEN: "the request carries a parameter the model contract does not allow",
        MODEL_EFFORT_UNSUPPORTED: "the role's reasoning effort is not supported by the model",
        MODEL_RESPONSE_FORMAT_UNSUPPORTED: "the model supports no structured-output format the role can use",
        MODEL_OUTPUT_CAP_INVALID: "the request's output cap is not a positive integer",
        MODEL_MESSAGE_INVALID: "a request message carries a field the model contract does not allow",
        MODEL_PROFILE_UNKNOWN: "the model has no registered profile; paid calls to it are refused",
    }

    def __init__(self, code: str) -> None:
        if code not in REQUEST_REFUSAL_CODES:
            raise ValueError("request refusal code must come from the static allowlist")
        self.code = code
        self.safe_message = self.MESSAGES[code]
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class RolePolicy:
    """What ONE Swarm V2 role may spend on ONE call.

    ``max_output`` bounds reasoning AND answer together (the provider counts
    both against the cap). ``min_answer_reserve`` is the smallest cap the call
    is still worth making with: a budget that cannot grant at least that much
    refuses the call (``BUDGET_INSUFFICIENT_FOR_ROLE``) instead of silently
    shrinking it into a guaranteed truncation.
    """

    effort: str
    max_output: int
    min_answer_reserve: int
    #: Whether the role's output has a JSON Schema the provider may enforce.
    structured: bool = True

    def __post_init__(self) -> None:
        if self.effort not in EFFORT_LADDER:
            raise ValueError("role effort must come from the effort ladder")
        if not (0 < self.min_answer_reserve <= self.max_output):
            raise ValueError("a role's answer reserve must be positive and within its cap")


def resolve_effort(profile: ModelProfile, effort: str) -> str:
    """The effort ``profile`` will actually be asked for, or a refusal."""
    if effort not in EFFORT_LADDER:
        raise ModelRequestRefused(MODEL_EFFORT_UNSUPPORTED)
    control = profile.reasoning_control
    if control == "reasoning_effort":
        if effort not in profile.reasoning_effort_values:
            # `none` on an always-reasoning model is a contradiction, not a
            # hint to ignore.
            raise ModelRequestRefused(MODEL_EFFORT_UNSUPPORTED)
        return effort
    if control == "thinking_toggle":
        return effort
    if effort != "none":
        raise ModelRequestRefused(MODEL_EFFORT_UNSUPPORTED)
    return effort


def lower_effort(profile: ModelProfile, effort: str) -> str | None:
    """One notch less reasoning than ``effort``, or None when there is none.

    kimi-k3: max -> high -> low -> (none; K3 cannot stop reasoning).
    kimi-k2.6: any thinking effort -> none (thinking disabled).
    """
    control = profile.reasoning_control
    if control == "reasoning_effort":
        values = profile.reasoning_effort_values
        if effort not in values:
            return None
        index = values.index(effort)
        return values[index - 1] if index > 0 else None
    if control == "thinking_toggle":
        return "none" if effort != "none" else None
    return None


def _reasoning_fields(profile: ModelProfile, effort: str) -> dict[str, Any]:
    control = profile.reasoning_control
    if control == "reasoning_effort":
        return {"reasoning_effort": effort}
    if control == "thinking_toggle":
        return {"extra_body": {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}}
    return {}


def _response_format(profile: ModelProfile, schema: Mapping[str, Any] | None,
                     schema_name: str) -> dict[str, Any]:
    if schema is not None and "json_schema" in profile.response_formats:
        return {"type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": dict(schema)}}
    if "json_object" in profile.response_formats:
        return {"type": "json_object"}
    raise ModelRequestRefused(MODEL_RESPONSE_FORMAT_UNSUPPORTED)


def _checked_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    checked = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) - _MESSAGE_KEYS:
            raise ModelRequestRefused(MODEL_MESSAGE_INVALID)
        checked.append(dict(message))
    return checked


def build_provider_request(profile: ModelProfile, policy: RolePolicy,
                           messages: Sequence[Mapping[str, Any]],
                           schema: Mapping[str, Any] | None = None, *,
                           output_cap: int | None = None,
                           effort: str | None = None,
                           schema_name: str = "milo_output",
                           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The complete provider request for ONE Swarm V2 call.

    ``output_cap`` defaults to the role's ``max_output`` and ``effort`` to the
    role's effort; a repair passes an escalated pair. ``extra`` exists only so
    that a caller trying to smuggle a parameter is refused rather than
    silently dropped: every key in it is refused, forbidden ones included.
    """
    if extra:
        # A parameter the profile forbids, or any parameter the builder does
        # not own, is refused. Nothing a caller adds reaches the provider.
        raise ModelRequestRefused(MODEL_PARAM_FORBIDDEN)
    cap = policy.max_output if output_cap is None else output_cap
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ModelRequestRefused(MODEL_OUTPUT_CAP_INVALID)
    resolved = resolve_effort(profile, policy.effort if effort is None else effort)
    request: dict[str, Any] = {
        "model": profile.model,
        "messages": _checked_messages(messages),
        profile.output_cap_field: int(cap),
        "response_format": _response_format(
            profile, schema if policy.structured else None, schema_name),
        **_reasoning_fields(profile, resolved),
    }
    leaked = set(request) & profile.forbidden_params
    if "extra_body" in request:
        leaked |= set(request["extra_body"]) & profile.forbidden_params
    if leaked:
        # Unreachable by construction; asserted so a future edit that adds a
        # field cannot quietly send one the model contract forbids.
        raise ModelRequestRefused(MODEL_PARAM_FORBIDDEN)
    return request


__all__ = ["EFFORT_LADDER", "MODEL_EFFORT_UNSUPPORTED", "MODEL_MESSAGE_INVALID",
           "MODEL_OUTPUT_CAP_INVALID", "MODEL_PROFILE_UNKNOWN", "MODEL_PARAM_FORBIDDEN",
           "MODEL_RESPONSE_FORMAT_UNSUPPORTED", "ModelRequestRefused", "REQUEST_REFUSAL_CODES",
           "RolePolicy", "build_provider_request", "lower_effort", "resolve_effort"]
