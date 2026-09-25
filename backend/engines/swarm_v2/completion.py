"""Classify ONE provider completion before any role parses it (PR-R 4.6).

A reasoning model that runs out of output cap stops with
``finish_reason="length"``. Before this module every Swarm V2 role read
``message.content`` straight away, so run 4761a8ce's Commander -- which spent
its entire 4,000-token cap thinking and returned an empty answer -- was
reported as ``COMMANDER_COMPLETION_SHAPE_INVALID``, a code that is not
repairable, and the run died without a second chance.

:func:`classify_completion` names what really happened, with a static code:

====================================  =================================  =========
situation                             code                               repairable
====================================  =================================  =========
``length`` and an empty answer        ``MODEL_REASONING_EXHAUSTED_OUTPUT``  once
``length`` and a partial answer       ``MODEL_OUTPUT_TRUNCATED``            once
``stop`` (or other) and empty answer  ``MODEL_EMPTY_COMPLETION``            no
broken completion structure           the role's own shape code             no
====================================  =================================  =========

The ONE repair of a truncation escalates the call rather than repeating it
(:func:`escalate`): the output cap doubles, up to the role's ``max_output``,
when the failed call ran below it; otherwise the reasoning effort drops one
notch. When neither is possible the failure is not repaired. The repair is an
ordinary guarded call, so it only goes out if its own worst-case reservation
passes every dollar ceiling.

Only ``message.content`` and ``finish_reason`` are read; ``reasoning_content``
is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.model_profiles import UnknownModelProfile, get_profile

from .request_builder import lower_effort

MODEL_REASONING_EXHAUSTED_OUTPUT = "MODEL_REASONING_EXHAUSTED_OUTPUT"
MODEL_OUTPUT_TRUNCATED = "MODEL_OUTPUT_TRUNCATED"
MODEL_EMPTY_COMPLETION = "MODEL_EMPTY_COMPLETION"

#: Repairable exactly once, by escalation.
TRUNCATION_CODES = frozenset({MODEL_REASONING_EXHAUSTED_OUTPUT, MODEL_OUTPUT_TRUNCATED})
COMPLETION_CODES = frozenset({*TRUNCATION_CODES, MODEL_EMPTY_COMPLETION})

COMPLETION_MESSAGES = {
    MODEL_REASONING_EXHAUSTED_OUTPUT: "the model spent its whole output cap reasoning and returned no answer",
    MODEL_OUTPUT_TRUNCATED: "the model's answer was cut off at the output cap",
    MODEL_EMPTY_COMPLETION: "the model finished without an answer",
}


@dataclass(frozen=True)
class CallShape:
    """The two knobs a truncation repair may turn: the cap and the effort."""

    output_cap: int
    effort: str


class ModelCompletionError(RuntimeError):
    """A delivered completion with no usable answer. Static code only."""

    def __init__(self, code: str) -> None:
        if code not in COMPLETION_CODES:
            raise ValueError("completion code must come from the static allowlist")
        self.code = code
        self.safe_message = COMPLETION_MESSAGES[code]
        super().__init__(self.safe_message)


class CompletionShapeInvalid(ValueError):
    """The completion has no readable choice/message. Each role maps this to
    its own historical shape code."""


def _get(container: Any, name: str) -> Any:
    if isinstance(container, dict):
        return container.get(name)
    return getattr(container, name, None)


def classify_completion(response: Any) -> Any:
    """Return the answer ``content`` of ONE completion, or raise.

    A bare ``str``/``bytes``/``dict`` is already content (offline adapters and
    test gateways hand those back) and is returned unchanged.
    """
    if isinstance(response, (str, bytes, bytearray, dict)):
        return response
    try:
        choice = _get(response, "choices")[0]
        message = _get(choice, "message")
    except (AttributeError, IndexError, KeyError, TypeError):
        raise CompletionShapeInvalid() from None
    if message is None:
        raise CompletionShapeInvalid()
    content = _get(message, "content")
    finish_reason = _get(choice, "finish_reason")
    empty = content is None or (isinstance(content, (str, bytes, bytearray)) and not str(
        content if isinstance(content, str) else bytes(content).decode("utf-8", "replace")).strip())
    if finish_reason == "length":
        raise ModelCompletionError(MODEL_REASONING_EXHAUSTED_OUTPUT if empty else MODEL_OUTPUT_TRUNCATED)
    if empty:
        raise ModelCompletionError(MODEL_EMPTY_COMPLETION)
    return content


def initial_shape(agent: str, phase: str) -> CallShape:
    """The cap and effort a role's FIRST call is made with."""
    from .model_gateway import role_policy

    policy = role_policy(agent, phase)
    return CallShape(output_cap=policy.max_output, effort=policy.effort)


def escalate(model: str, agent: str, phase: str, shape: CallShape) -> CallShape | None:
    """The ONE repair of a truncation, or None when nothing can change.

    Double the cap (up to the role's ``max_output``) when the failed call ran
    below it; otherwise lower the reasoning effort one notch.
    """
    from .model_gateway import role_policy

    policy = role_policy(agent, phase)
    if shape.output_cap < policy.max_output:
        return CallShape(output_cap=min(2 * shape.output_cap, policy.max_output),
                         effort=shape.effort)
    try:
        profile = get_profile(model)
    except UnknownModelProfile:
        return None
    lowered = lower_effort(profile, shape.effort)
    if lowered is None:
        return None
    return CallShape(output_cap=shape.output_cap, effort=lowered)


__all__ = ["COMPLETION_CODES", "COMPLETION_MESSAGES", "CallShape", "CompletionShapeInvalid",
           "MODEL_EMPTY_COMPLETION", "MODEL_OUTPUT_TRUNCATED", "MODEL_REASONING_EXHAUSTED_OUTPUT",
           "ModelCompletionError", "TRUNCATION_CODES", "classify_completion", "escalate",
           "initial_shape"]
