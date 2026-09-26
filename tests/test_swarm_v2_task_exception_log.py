"""PR-W S2: an unclassified task exception leaves ONE structured log line.

Run 280fc9e5 lost 11/11 tasks to TASK_FAILED after successful model calls, and
nothing in the logs said why: the worker's generic ``except Exception`` folded
a bare KeyError from the runtime schema validator into TASK_FAILED silently.
The fold is unchanged -- same static code, same message, other tasks proceed --
but it now writes one line naming the task and the exception CLASS. Never the
exception message, never provider material.
"""

from __future__ import annotations

import json
import logging

from backend.engines.swarm_v2.contracts import DynamicTask
from backend.engines.swarm_v2.worker import GenericWorker
from backend.tools import ToolContext, ToolRegistry
from test_swarm_v2 import task

LOGGER = "milo.swarm_v2.worker"
PROVIDER_SENTINEL = "PROVIDER_SECRET_SENTINEL sk-live-000 https://leaky.example.test"


class _ExplodingGateway:
    """Delivers a completion, then the output path raises an unclassified error."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def call(self, **_: object):
        self.calls += 1
        raise self.exc


def _worker(gateway) -> GenericWorker:
    return GenericWorker(gateway=gateway, tools=ToolRegistry([]), model="kimi-k2.6",
                         tool_context=ToolContext())


def _task(task_id: str = "t02") -> DynamicTask:
    item = task(task_id, "resolve register variants")
    item["tools"] = []
    return DynamicTask.model_validate(item)


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


def test_an_unclassified_exception_is_task_failed_with_one_structured_line(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    result = _worker(_ExplodingGateway(KeyError(PROVIDER_SENTINEL))).execute(_task(), {})
    # Behaviour is unchanged: the same static failure.
    assert result.status == "failed"
    assert result.error == {"code": "TASK_FAILED", "message": "task execution failed"}
    (line,) = _lines(caplog)
    assert json.loads(line) == {"event": "task_exception", "task_id": "t02",
                                "exception_class": "KeyError"}


def test_the_line_carries_the_class_name_only_never_the_message(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    class ProviderLeak(RuntimeError):
        pass

    _worker(_ExplodingGateway(ProviderLeak(PROVIDER_SENTINEL))).execute(_task("t07"), {})
    (line,) = _lines(caplog)
    assert json.loads(line) == {"event": "task_exception", "task_id": "t07",
                                "exception_class": "ProviderLeak"}
    rendered = "\n".join(record.getMessage() + repr(record.args) + str(record.exc_info)
                         for record in caplog.records)
    assert "SENTINEL" not in rendered and "sk-live" not in rendered and "leaky" not in rendered


def test_a_classified_failure_writes_no_task_exception_line(caplog):
    """Only the generic fold logs: a typed outcome already names itself."""
    caplog.set_level(logging.INFO, logger=LOGGER)

    class _BadJsonGateway:
        def call(self, **_: object):
            return "{not json"

    result = _worker(_BadJsonGateway()).execute(_task(), {})
    assert result.status == "failed"
    assert result.error["code"] != "TASK_FAILED"
    assert not [line for line in _lines(caplog) if "task_exception" in line]
