#!/usr/bin/env python3
"""Diagnostic for Moonshot/Kimi builtin $web_search tool_calls.

This script is intentionally standalone and is not imported by the app.
It makes one request each to kimi-k2.6 and kimi-k2.5, prints the full raw
response JSON for each model, then prints a side-by-side summary indicating
whether the model triggered the builtin web search tool.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from openai import OpenAI


BASE_URL = os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
MODELS = ("kimi-k2.6", "kimi-k2.5")
MESSAGES = [
    {
        "role": "system",
        "content": "You are a helpful assistant. Always use web search to answer questions.",
    },
    {
        "role": "user",
        "content": "Search the web: what is the official website of Hyundai Israel?",
    },
]
TOOLS = [{"type": "builtin_function", "function": {"name": "$web_search"}}]


def to_plain_json(value: Any) -> Any:
    """Convert OpenAI SDK response objects into JSON-serializable data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def first_choice(response_json: dict[str, Any]) -> dict[str, Any]:
    choices = response_json.get("choices") or []
    if not choices:
        return {}
    return choices[0] or {}


def summarize_response(model: str, response_json: dict[str, Any]) -> dict[str, Any]:
    choice = first_choice(response_json)
    message = choice.get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    usage = response_json.get("usage") or {}
    finish_reason = choice.get("finish_reason")
    total_tokens = usage.get("total_tokens")

    if finish_reason == "tool_calls":
        verdict = "SUCCESS: Model triggered web search"
    elif finish_reason == "stop" and not tool_calls:
        verdict = "FAILURE: Model did not use web search tool"
    elif finish_reason == "length":
        verdict = "FAILURE: Model hit token limit without searching"
    else:
        verdict = f"UNKNOWN: finish_reason={finish_reason!r}, tool_calls={bool(tool_calls)}"

    return {
        "model": model,
        "finish_reason": finish_reason,
        "tool_calls_exist": bool(tool_calls),
        "content_first_500_chars": content[:500],
        "total_tokens_used": total_tokens,
        "verdict": verdict,
    }


def print_model_result(summary: dict[str, Any]) -> None:
    print(f"\n--- Specific checks for {summary['model']} ---")
    print(f"finish_reason: {summary['finish_reason']}")
    print(f"tool_calls exist: {summary['tool_calls_exist']}")
    print("content text (first 500 chars):")
    print(summary["content_first_500_chars"])
    print(f"total tokens used: {summary['total_tokens_used']}")
    print(summary["verdict"])


def print_side_by_side(summaries: list[dict[str, Any]]) -> None:
    print("\n=== Side-by-side summary ===")
    headers = ["model", "finish_reason", "tool_calls", "total_tokens", "verdict"]
    rows = [
        [
            str(item["model"]),
            str(item["finish_reason"]),
            str(item["tool_calls_exist"]),
            str(item["total_tokens_used"]),
            str(item["verdict"]),
        ]
        for item in summaries
    ]
    widths = [
        max(len(headers[col]), *(len(row[col]) for row in rows))
        for col in range(len(headers))
    ]

    def fmt(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

    print(fmt(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(fmt(row))


def call_model(client: OpenAI, model: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        messages=MESSAGES,
        tools=TOOLS,
        max_tokens=2048,
        temperature=0.6,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return to_plain_json(response)


def main() -> int:
    api_key = os.environ.get("MOONSHOT_API_KEY")
    if not api_key:
        print("ERROR: MOONSHOT_API_KEY environment variable is not set.", file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key, base_url=BASE_URL)
    summaries: list[dict[str, Any]] = []

    for model in MODELS:
        print(f"\n=== Raw API response for {model} ===")
        try:
            response_json = call_model(client, model)
        except Exception as exc:  # noqa: BLE001 - diagnostics should show API/client failures clearly.
            response_json = {"error": type(exc).__name__, "message": str(exc)}
            print(json.dumps(response_json, ensure_ascii=False, indent=2))
            summaries.append(
                {
                    "model": model,
                    "finish_reason": None,
                    "tool_calls_exist": False,
                    "content_first_500_chars": "",
                    "total_tokens_used": None,
                    "verdict": f"ERROR: {type(exc).__name__}: {exc}",
                }
            )
            print_model_result(summaries[-1])
            continue

        print(json.dumps(response_json, ensure_ascii=False, indent=2))
        summary = summarize_response(model, response_json)
        summaries.append(summary)
        print_model_result(summary)

    print_side_by_side(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
