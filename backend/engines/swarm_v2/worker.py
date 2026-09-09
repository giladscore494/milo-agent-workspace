"""Taxonomy-neutral worker using only injected gateway and tools."""
from __future__ import annotations
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol
from backend.budget import BudgetExceeded
from backend.errors import AppError
from backend.provider_scheduler import ProviderBackpressureExceeded
from backend.runtime import CancellationRequested
from backend.tools import ToolContext, ToolError, ToolRegistry
from backend.tools.registry import validate_json_schema
from .contracts import DynamicTask
from .model_gateway import ModelGateway
from .tool_calls import (MAX_TASK_OUTPUT_JSON_BYTES, MAX_TOOL_MATERIAL_JSON_BYTES,
                         MAX_TOOL_OUTPUT_JSON_BYTES, ToolCallError, ToolCallRecord,
                         ToolResultCallback, check_material, resolve_tool_arguments)


# One initial worker completion plus AT MOST one bounded semantic repair.
# This constant is the only bound on semantic worker-output model calls per
# task execution: there is no recursion, no retry-until-valid loop and no
# second repair. Provider transport retries (429 backpressure) happen inside
# the ProviderScheduler and are a different concept entirely; they never add
# a semantic attempt here.
MAX_WORKER_OUTPUT_MODEL_ATTEMPTS = 2

# The ONLY failure family eligible for that repair: the provider delivered a
# completion, but the structured worker output could not be decoded as JSON,
# or decoded and failed the declared task.output_schema. Tool, provider,
# budget, cancellation, lease and infrastructure failures keep their existing
# semantics and are never repaired.
WORKER_OUTPUT_REASONS = frozenset({
    "WORKER_OUTPUT_JSON_INVALID",
    "WORKER_OUTPUT_SCHEMA_INVALID",
})


# R5: the static reasons a TRUSTED deterministic task-output strategy can fail
# with. They are deliberately NOT part of WORKER_OUTPUT_REASONS: those are the
# failures a bounded model repair may answer, and there is no model in this
# path to answer anything. A deterministic strategy that raises or returns a
# value the task's own closed output_schema rejects is a server defect, so it
# fails the task closed instead of silently falling back to a model call.
DETERMINISTIC_OUTPUT_REASONS = frozenset({
    "WORKER_OUTPUT_STRATEGY_FAILED",
    "WORKER_OUTPUT_STRATEGY_INVALID",
})


class DeterministicOutputError(ValueError):
    """A trusted deterministic-strategy failure carrying ONLY a static code.

    The strategy's traceback and its rejected value never travel with the
    classification, exactly like every other failure this module raises, so
    the safe representation is fit for a durable task result, a run event and
    telemetry alike.
    """

    MESSAGES = {
        "WORKER_OUTPUT_STRATEGY_FAILED": "the deterministic task-output strategy failed",
        "WORKER_OUTPUT_STRATEGY_INVALID": "the deterministic task output does not satisfy the declared output schema",
    }

    def __init__(self, reason_code: str):
        if reason_code not in DETERMINISTIC_OUTPUT_REASONS:
            raise ValueError("deterministic output reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


class WorkerOutputValidationError(ValueError):
    """A structurally invalid worker completion carrying ONLY a static code.

    The malformed completion, the schema diagnostic (which embeds
    model-chosen property names) and the provider payload never reach this
    exception, so its safe representation is fit for durable task results,
    run events and telemetry.
    """

    MESSAGES = {
        "WORKER_OUTPUT_JSON_INVALID": "worker output is not valid JSON",
        "WORKER_OUTPUT_SCHEMA_INVALID": "worker output does not satisfy the declared output schema",
    }

    def __init__(self, reason_code: str):
        if reason_code not in WORKER_OUTPUT_REASONS:
            raise ValueError("worker output reason must come from the static allowlist")
        self.reason_code = reason_code
        self.safe_message = self.MESSAGES[reason_code]
        super().__init__(self.safe_message)


def validate_worker_output(completion: Any, output_schema: Mapping[str, Any]) -> Any:
    """Decode and schema-validate ONE worker completion.

    The single parsing path for both the initial completion and the repair
    completion, so the two attempts cannot drift apart. Only two failures are
    distinguished — JSON versus schema — and every raw diagnostic is dropped
    at this boundary.
    """
    if isinstance(completion, (str, bytes, bytearray)) and len(completion) > MAX_TASK_OUTPUT_JSON_BYTES:
        # Refused BEFORE decoding: an oversized completion must never be
        # parsed into memory, repaired, or written to durable state. This is
        # deliberately NOT a repairable worker-output failure.
        raise ToolCallError("TASK_OUTPUT_TOO_LARGE")
    if isinstance(completion, Mapping):
        parsed: Any = dict(completion)
    elif not isinstance(completion, (str, bytes, bytearray)):
        # A null, list or object completion body is a delivered response the
        # structured worker output still cannot be decoded from.
        raise WorkerOutputValidationError("WORKER_OUTPUT_JSON_INVALID")
    else:
        try:
            parsed = json.loads(completion)
        except (TypeError, ValueError):
            # `from None`: the decoder exception carries the raw document and
            # must never travel with the safe classification.
            raise WorkerOutputValidationError("WORKER_OUTPUT_JSON_INVALID") from None
    check_material(parsed, MAX_TASK_OUTPUT_JSON_BYTES, "TASK_OUTPUT_TOO_LARGE")
    try:
        validate_json_schema(output_schema, parsed, "$model_output")
    except (TypeError, ValueError):
        # Dropped deliberately: the validation message can quote model-chosen
        # property names, so it is provider material, not a safe reason.
        raise WorkerOutputValidationError("WORKER_OUTPUT_SCHEMA_INVALID") from None
    return parsed


def build_worker_request(task: DynamicTask, tool_outputs: Mapping[str, Any],
                         dependency_outputs: Mapping[str, Any], *,
                         repair_reason: str | None = None) -> list[dict[str, Any]]:
    """Build the messages for one worker-output model call.

    Both attempts are built here from the SAME trusted material — the task
    goal and scope, the dependency context the first call already used, the
    already validated tool outputs (keyed by call_id, so two calls to the same
    tool stay distinguishable) and the declared output_schema — so the
    repair can never silently see more or less than the initial call.

    The repair carries only a static reason code. The malformed completion is
    deliberately NOT sent back: it can contain provider artifacts, unbounded
    prose and accidental sensitive text, and regenerating the answer from
    trusted material is safer than asking the model to edit untrusted text.
    """
    # The explicit JSON system instruction is required: OpenAI-compatible
    # providers (including Moonshot) may reject response_format
    # json_object when no message mentions JSON output.
    system = ("Return only one JSON object that validates exactly against the "
              "provided output_schema. No markdown, no extra fields.")
    if repair_reason is not None:
        if repair_reason not in WORKER_OUTPUT_REASONS:
            raise ValueError("worker repair reason must come from the static allowlist")
        system += (" The previous response was rejected by deterministic server "
                   f"validation with static reason code {repair_reason}. Return one "
                   "corrected JSON value built only from the supplied goal, scope, "
                   "dependencies and tools material. Do not explain the previous "
                   "failure and do not include any reasoning. This is the final attempt.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"goal": task.goal, "scope": task.scope,
            "dependencies": dependency_outputs, "tools": tool_outputs,
            "output_schema": task.output_schema}, sort_keys=True)},
    ]


class TaskOutputStrategy(Protocol):
    """R5: trusted server code that produces ONE task's structured output.

    The seam exists so a TOOL-COMPLETE structured task can finish without a
    worker model call. It is deliberately narrow:

    *   It is selected by CONSTRUCTOR INJECTION from trusted server/test
        wiring only. There is no run-input field, no plan field and no model
        output that can reach, name or enable it, so a client can never ask a
        run to skip the model.
    *   It receives only material the server already validated: the approved
        task, the Registry-validated tool results (keyed by call_id) and the
        direct dependency outputs -- the exact same trusted material
        `build_worker_request` would otherwise put in a prompt.
    *   Its return value is validated against the task's own closed
        `output_schema` through the SAME `validate_worker_output` path a model
        completion goes through, so it cannot widen a contract or exceed a
        durable bound.
    *   It has no evidence capability whatsoever. Evidence acquisition already
        happened inside the tool loop, through the trusted mapper and the
        lease-guarded Evidence Board; a strategy returns a task OUTPUT and can
        neither create a source, alter source metadata nor influence a verdict.

    When no strategy is injected the worker's behaviour is byte-for-byte the
    current model-backed one.
    """

    def __call__(self, *, task: DynamicTask, tool_outputs: Mapping[str, Any],
                 dependency_outputs: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: str
    output: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

class GenericWorker:
    def __init__(self, *, gateway: ModelGateway, tools: ToolRegistry, model: str, tool_context: ToolContext,
                 cancellation_checker: Callable[[], bool] | None = None,
                 event_sink: Callable[[str, dict[str, Any]], None] | None = None,
                 retry_callback: Callable[[str, str, str], None] | None = None,
                 tool_result_sink: ToolResultCallback | None = None,
                 task_output_strategy: TaskOutputStrategy | None = None):
        self._gateway, self._tools, self._model = gateway, tools, model
        self._tool_context = (replace(tool_context, cancellation_checker=cancellation_checker)
                              if cancellation_checker is not None else tool_context)
        self._cancelled = cancellation_checker
        self._event_sink = event_sink
        self._retry_callback = retry_callback
        # The trusted post-execution seam (see .tool_calls.ToolCallRecord).
        # Unwired in production until Y4/G3 connect a domain mapping to a real
        # evidence grant; a validated result is material, never a fact.
        self._tool_result_sink = tool_result_sink
        # R5: the OPTIONAL deterministic task-output strategy. `None` -- the
        # production default -- keeps the model-backed behaviour exactly as it
        # is; nothing outside this constructor can set it.
        self._task_output_strategy = task_output_strategy

    def _check_cancelled(self) -> None:
        if self._cancelled and self._cancelled():
            raise CancellationRequested("RUN_CANCELLED")

    def execute(self, task: DynamicTask, dependency_outputs: Mapping[str, Any]) -> TaskResult:
        try:
            tool_outputs = self._run_planned_calls(task, dependency_outputs)
            self._check_cancelled()
            # Tool execution is COMPLETE and final at this line. Everything
            # below is the model-output boundary: the bounded repair re-uses
            # exactly these captured outputs and never re-enters the loop
            # above, so each planned call is invoked exactly once per task
            # execution whether or not a repair happens.
            output = self._resolve_output(task, dependency_outputs, tool_outputs)
            return TaskResult(task.task_id, "completed", output=output)
        except CancellationRequested:
            raise
        except BudgetExceeded:
            # A tripped hard budget limit is a run-terminal outcome, never a
            # per-task failure: it must reach the worker's terminal handler.
            # This includes a budget refusal of the repair call, which must
            # never be laundered into a worker-output failure.
            raise
        except AppError:
            # Persistence/lease failures from the guarded call path are
            # infrastructure outcomes, never per-task failures.
            raise
        except ToolError as exc:
            return TaskResult(task.task_id, "failed", error=exc.as_dict()["error"])
        except ToolCallError as exc:
            # A binding that could not be resolved, or material that exceeded a
            # deterministic bound. Only the static code travels; the rejected
            # value never reaches a durable result or a run event.
            return TaskResult(task.task_id, "failed",
                              error={"code": exc.code, "message": exc.safe_message})
        except ProviderBackpressureExceeded:
            return TaskResult(task.task_id, "failed", error={"code": "PROVIDER_BACKPRESSURE_EXCEEDED", "message": "provider backpressure did not clear"})
        except DeterministicOutputError as exc:
            # A trusted deterministic strategy raised, or produced output the
            # task's own closed schema rejects. There is deliberately NO
            # fallback to a model call: silently paying for a completion here
            # would turn a server defect into an invisible provider charge and
            # would make a zero-model-call guarantee unprovable.
            return TaskResult(task.task_id, "failed",
                              error={"code": exc.reason_code, "message": exc.safe_message})
        except WorkerOutputValidationError as exc:
            # A structural model-output failure that already exhausted
            # MAX_WORKER_OUTPUT_MODEL_ATTEMPTS. The static code of the FINAL
            # attempt is what tells a structural worker failure apart from a
            # tool failure without exposing any provider material.
            return TaskResult(task.task_id, "failed",
                              error={"code": exc.reason_code, "message": exc.safe_message})
        except Exception:
            return TaskResult(task.task_id, "failed", error={"code": "TASK_FAILED", "message": "task execution failed"})

    def _run_planned_calls(self, task: DynamicTask,
                           dependency_outputs: Mapping[str, Any]) -> dict[str, Any]:
        """Execute exactly the approved calls, once each, in declaration order.

        There is no fallback payload and no dynamic loop: every call, its
        operation and its arguments come from the already-validated plan, and
        a task that declares no calls performs none. Results are keyed by
        `call_id`, so the same tool may legitimately be called more than once
        in one task without either result overwriting the other.

        The first failure stops the task: later calls are never attempted
        after one fails, so a task can never be completed from a partial set
        of tool material.
        """
        outputs: dict[str, Any] = {}
        for call in task.tools:
            self._check_cancelled()
            if call.call_id in outputs:
                # Defence in depth: the firewall already rejects duplicates.
                raise ToolCallError("DUPLICATE_TOOL_CALL_ID")
            # Bindings are resolved HERE, in trusted code, from the direct
            # dependency outputs the executor supplied -- never by the model
            # and never from the wider run -- and the resolver bounds the
            # FINAL payload's shape and size before it can reach a tool.
            arguments = resolve_tool_arguments(call, dependency_outputs)
            result = self._tools.execute(call.name, call.operation,
                                         self._tool_context, arguments)
            check_material(result, MAX_TOOL_OUTPUT_JSON_BYTES, "TOOL_OUTPUT_TOO_LARGE")
            outputs[call.call_id] = result
            if self._tool_result_sink is not None:
                # Reached only with a Registry-validated result and
                # server-resolved identity; the worker model cannot call it.
                self._tool_result_sink(ToolCallRecord(
                    task_id=task.task_id, call_id=call.call_id, tool=call.name,
                    operation=call.operation, result=result))
            if self._event_sink:
                # Identifiers only. Arguments and results are deliberately
                # absent: a public run event is not an evidence channel.
                self._event_sink("tool_called", {
                    "task_id": task.task_id, "tool": call.name,
                    "call_id": call.call_id, "operation": call.operation})
            self._check_cancelled()
        # The combined material is bounded BEFORE it can reach a model prompt.
        check_material(outputs, MAX_TOOL_MATERIAL_JSON_BYTES, "TOOL_MATERIAL_TOO_LARGE")
        return outputs

    def _resolve_output(self, task: DynamicTask, dependency_outputs: Mapping[str, Any],
                        tool_outputs: Mapping[str, Any]) -> Any:
        """Return validated worker output within the bounded attempt budget.

        Attempt 1 is the normal completion. ONLY a JSON or output_schema
        failure earns attempt 2, and there is no attempt 3: a second
        structural failure is re-raised for the caller to classify. Every
        attempt is an ordinary guarded ModelGateway call, so reservation,
        budget authority, provider scheduling, cancellation and usage
        accounting are identical for the repair and for the first call.
        """
        if self._task_output_strategy is not None:
            # R5: the task is tool-complete and trusted server code can state
            # its output. Nothing below this line runs, so this task produces
            # no reservation, no provider call and no model usage at all.
            return self._deterministic_output(task, dependency_outputs, tool_outputs)
        repair_reason: str | None = None
        attempt = 1
        while True:
            if repair_reason is not None:
                # The same cancellation gate the initial call passes through:
                # a run cancelled after an invalid completion never pays for
                # a repair.
                self._check_cancelled()
                # Announce the decision to repair BEFORE any accounting, so a
                # repair the retry allowance or the budget then refuses is
                # still visible; the refusal has its own budget event.
                if self._event_sink:
                    self._event_sink("worker_output_repair_started",
                                     {"task_id": task.task_id, "reason_code": repair_reason,
                                      "attempt_number": attempt})
                # A repair IS a semantic retry and consumes the run's semantic
                # retry allowance — unlike a 429, which the scheduler absorbs
                # as transport backpressure and never charges here.
                if self._retry_callback is not None:
                    self._retry_callback(f"worker:{task.task_id}", "execute", repair_reason)
            response = self._gateway.call(model=self._model, agent=f"worker:{task.task_id}", phase="execute",
                messages=build_worker_request(task, tool_outputs, dependency_outputs,
                                              repair_reason=repair_reason),
                response_format={"type": "json_object"})
            content = response if isinstance(response, dict) else response.choices[0].message.content
            try:
                return validate_worker_output(content, task.output_schema)
            except WorkerOutputValidationError as exc:
                if attempt >= MAX_WORKER_OUTPUT_MODEL_ATTEMPTS:
                    raise
                repair_reason, attempt = exc.reason_code, attempt + 1

    def _deterministic_output(self, task: DynamicTask, dependency_outputs: Mapping[str, Any],
                              tool_outputs: Mapping[str, Any]) -> Any:
        """Produce and re-validate ONE task output with no model call at all.

        The strategy's return value is never trusted for being the strategy's:
        it goes through the SAME `validate_worker_output` the model path uses,
        so a deterministic answer is held to the task's declared closed
        `output_schema` and to the identical durable size bound. Both failure
        shapes -- the strategy raising, and the strategy returning something
        invalid -- collapse to a static code here, so neither a traceback nor
        the rejected value can reach a durable task result.
        """
        try:
            produced = self._task_output_strategy(
                task=task, tool_outputs=tool_outputs, dependency_outputs=dependency_outputs)
        except Exception:
            # `from None`: a strategy traceback can quote tool material.
            raise DeterministicOutputError("WORKER_OUTPUT_STRATEGY_FAILED") from None
        try:
            return validate_worker_output(produced, task.output_schema)
        except (WorkerOutputValidationError, ToolCallError):
            # Deliberately NOT repairable: see DETERMINISTIC_OUTPUT_REASONS.
            raise DeterministicOutputError("WORKER_OUTPUT_STRATEGY_INVALID") from None
