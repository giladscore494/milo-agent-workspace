"""An OpenAI-compatible completions fake that THINKS inside its output cap.

MILO_V2_REASONING_BUDGET_PR_SPEC.md 4.10 item 1. It models what a reasoning
model actually does with a request, which is exactly what run 4761a8ce ran
into:

* reasoning is spent FIRST, out of the same output cap as the answer
  (Moonshot documents that ``reasoning_content`` + ``content`` must fit in
  ``max_completion_tokens`` / ``max_tokens``);
* when reasoning alone reaches the cap the answer is EMPTY and
  ``finish_reason`` is ``"length"``; when reasoning fits but the answer does
  not, the answer is cut off, again with ``"length"``;
* ``usage`` reports ``prompt_tokens``, ``completion_tokens`` (reasoning
  INCLUDED), ``prompt_tokens_details.cached_tokens`` and -- unless
  ``report_reasoning`` is False, which is the case Moonshot's official docs
  do not rule out -- ``completion_tokens_details.reasoning_tokens``;
* every message carries a ``reasoning_content`` made of a unique marker, so a
  test can prove the reasoning text never reaches anything durable.

How much it reasons is driven by the request's OWN reasoning control:
``reasoning_effort`` (kimi-k3) or ``extra_body.thinking`` (kimi-k2.6; disabled
means zero reasoning).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from backend.model_usage import count_content_tokens

REASONING_MARKER = "REASONING-TEXT-MUST-NEVER-PERSIST"


@dataclass
class ReasoningProvider:
    #: Reasoning tokens spent per effort ("none" = thinking disabled).
    reasoning_by_effort: Mapping[str, int]
    #: The complete answer, or a function of the request producing it.
    answer: str | Callable[[dict[str, Any]], str] = '{"answer": "42"}'
    prompt_tokens: int = 1200
    cached_tokens: int | None = None
    report_reasoning: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def effort_of(request: Mapping[str, Any]) -> str:
        if "reasoning_effort" in request:
            return str(request["reasoning_effort"])
        thinking = ((request.get("extra_body") or {}).get("thinking") or {}).get("type")
        if thinking == "disabled":
            return "none"
        if thinking == "enabled":
            return "enabled"
        # Neither control sent: the provider DEFAULT, which for a thinking
        # model is to think -- the 4761a8ce situation.
        return "default"

    @staticmethod
    def cap_of(request: Mapping[str, Any]) -> int:
        cap = request.get("max_completion_tokens", request.get("max_tokens"))
        assert isinstance(cap, int) and cap > 0, "every request must carry a numeric cap"
        return cap

    def create(self, **request: Any) -> Any:
        self.calls.append(request)
        cap = self.cap_of(request)
        reasoning = int(self.reasoning_by_effort.get(self.effort_of(request), 0))
        answer = self.answer(request) if callable(self.answer) else self.answer
        answer_tokens = count_content_tokens(answer)[0]
        if reasoning >= cap:
            content, reasoning_used, completion, finish = "", cap, cap, "length"
        elif reasoning + answer_tokens > cap:
            keep = max(1, len(answer) * (cap - reasoning) // max(1, answer_tokens))
            content, reasoning_used, completion, finish = answer[:keep], reasoning, cap, "length"
        else:
            content, reasoning_used = answer, reasoning
            completion, finish = reasoning + answer_tokens, "stop"
        usage = SimpleNamespace(
            prompt_tokens=self.prompt_tokens, completion_tokens=completion,
            total_tokens=self.prompt_tokens + completion,
            prompt_tokens_details=(None if self.cached_tokens is None
                                   else {"cached_tokens": self.cached_tokens}),
            completion_tokens_details=({"reasoning_tokens": reasoning_used}
                                       if self.report_reasoning else None))
        message = SimpleNamespace(role="assistant", content=content,
                                  reasoning_content=f"{REASONING_MARKER} x{reasoning_used}")
        return SimpleNamespace(id="chatcmpl-reasoning-fake", model=request.get("model"),
                               choices=[SimpleNamespace(index=0, message=message,
                                                        finish_reason=finish)],
                               usage=usage)


def reasoning_client(provider: ReasoningProvider) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=provider))
