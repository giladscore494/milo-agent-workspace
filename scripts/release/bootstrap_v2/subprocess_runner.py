"""Operation execution layer: mutation ledger, plan gate, safe subprocess.

Every external write — subprocess or HTTP — must pass through
:class:`MutationGate`, which rejects any mutating operation that was not
declared in the frozen plan and records every attempt in the append-only
:class:`MutationLedger`. There is no second ledger and no competing failure
flag; the ledger's records flow into the single RunResult.

Secret handling: secrets never appear in argv. Callers register known
secret values with :class:`SecretRegistry`; the runner refuses to execute
any command whose argv contains a registered secret and never copies
registered secrets into a child environment. Secret payloads travel via
stdin only. Output sizes are capped.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .model import (
    MutationOperation,
    MutationPlan,
    MutationRecord,
    OperationType,
    Provider,
    ResourceIdentity,
)

MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120

#: Minimal child environment allowlist. Token-bearing variables never pass.
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CLOUDSDK_CONFIG",
    "CLOUDSDK_CORE_DISABLE_PROMPTS",
)


class UndeclaredMutationError(Exception):
    """An adapter attempted a write that is not in the frozen plan."""


class SecretInArgvError(Exception):
    """A registered secret value was found in a command line."""


class MutationAfterBlockError(Exception):
    """A write was attempted after the run was blocked."""


class SecretRegistry:
    """In-memory registry of secret values for leak prevention.

    Values are stored only to be able to refuse their appearance in argv
    and to strip them from child environments; they are cleared with
    :meth:`clear` as soon as the run no longer needs them.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> None:
        if value:
            self._values.add(value)

    def clear(self) -> None:
        self._values.clear()

    def contains_secret(self, text: str) -> bool:
        return any(value in text for value in self._values if value)

    def assert_argv_clean(self, argv: tuple[str, ...]) -> None:
        for arg in argv:
            if self.contains_secret(arg):
                raise SecretInArgvError(
                    "refusing to execute: a registered secret value appears in "
                    "argv (secrets must travel via stdin)"
                )

    def redact(self, text: str) -> str:
        for value in self._values:
            if value:
                text = text.replace(value, "[REDACTED]")
        return text


class MutationLedger:
    """Append-only record of every attempted external write."""

    def __init__(self) -> None:
        self._records: list[MutationRecord] = []
        self._sequence = 0

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def append(self, record: MutationRecord) -> None:
        self._records.append(record)

    def records(self) -> tuple[MutationRecord, ...]:
        return tuple(self._records)

    def executed_count(self) -> int:
        return sum(1 for record in self._records if record.executed)


class MutationGate:
    """The only path to an external write.

    ``authorize`` must be called (and must not raise) before any adapter
    performs a mutation. It refuses undeclared operations and any operation
    after the gate has been closed by a blocker, and it records refused
    attempts in the ledger so they surface in the RunResult.
    """

    def __init__(self, plan: MutationPlan | None, ledger: MutationLedger) -> None:
        self._plan = plan
        self._declared: dict[str, MutationOperation] = (
            {op.idempotency_key: op for op in plan.operations}
            if plan is not None
            else {}
        )
        self._ledger = ledger
        self._closed = False

    @property
    def ledger(self) -> MutationLedger:
        return self._ledger

    def close(self) -> None:
        """Close the gate permanently for this run (first blocker)."""
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def authorize(
        self,
        operation_type: OperationType,
        resource: ResourceIdentity,
        idempotency_key: str,
    ) -> MutationOperation:
        if self._closed:
            raise MutationAfterBlockError(
                f"mutation {idempotency_key} attempted after the run was blocked"
            )
        declared = self._declared.get(idempotency_key)
        if declared is None:
            self._ledger.append(
                MutationRecord(
                    sequence=self._ledger.next_sequence(),
                    provider=resource.provider,
                    operation_type=operation_type,
                    resource=resource,
                    idempotency_key=idempotency_key,
                    declared=False,
                    executed=False,
                    succeeded=False,
                    error_class="undeclared",
                )
            )
            self._closed = True
            raise UndeclaredMutationError(
                f"mutation {idempotency_key} is not declared in the frozen plan"
            )
        if declared.operation_type is not operation_type or (
            declared.resource != resource
        ):
            self._ledger.append(
                MutationRecord(
                    sequence=self._ledger.next_sequence(),
                    provider=resource.provider,
                    operation_type=operation_type,
                    resource=resource,
                    idempotency_key=idempotency_key,
                    declared=False,
                    executed=False,
                    succeeded=False,
                    error_class="declared-identity-mismatch",
                )
            )
            self._closed = True
            raise UndeclaredMutationError(
                f"mutation {idempotency_key} does not match its declared "
                "operation type or resource identity"
            )
        return declared

    def record_execution(
        self,
        operation: MutationOperation,
        succeeded: bool,
        error_class: str = "",
    ) -> MutationRecord:
        record = MutationRecord(
            sequence=self._ledger.next_sequence(),
            provider=operation.provider,
            operation_type=operation.operation_type,
            resource=operation.resource,
            idempotency_key=operation.idempotency_key,
            declared=True,
            executed=True,
            succeeded=succeeded,
            error_class=error_class,
        )
        self._ledger.append(record)
        if not succeeded:
            self._closed = True
        return record


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SubprocessRunner:
    """Executes provider CLI commands with secret and size discipline."""

    def __init__(self, secrets: SecretRegistry | None = None) -> None:
        self._secrets = secrets or SecretRegistry()

    @property
    def secrets(self) -> SecretRegistry:
        return self._secrets

    def _child_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in _CHILD_ENV_ALLOWLIST:
            value = os.environ.get(name)
            if value is not None and not self._secrets.contains_secret(value):
                env[name] = value
        return env

    def run(
        self,
        argv: tuple[str, ...],
        stdin_data: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> CommandResult:
        self._secrets.assert_argv_clean(argv)
        try:
            completed = subprocess.run(
                list(argv),
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(argv=argv, returncode=-1, stdout="", stderr="", timed_out=True)
        except FileNotFoundError as exc:
            return CommandResult(
                argv=argv,
                returncode=127,
                stdout="",
                stderr=self._secrets.redact(str(exc.filename or exc)),
            )
        stdout = completed.stdout[:MAX_OUTPUT_BYTES]
        stderr = completed.stderr[:MAX_OUTPUT_BYTES]
        return CommandResult(
            argv=argv,
            returncode=completed.returncode,
            stdout=self._secrets.redact(stdout),
            stderr=self._secrets.redact(stderr),
        )
