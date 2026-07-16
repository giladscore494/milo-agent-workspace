"""Reporting: human summary and JSON report from the single RunResult.

Nothing in this module decides outcomes; it renders the authoritative
RunResult. Redaction here is only a backup safeguard — secrets must never
reach the RunResult in the first place.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .model import RunResult

_REDACT_PATTERNS = (
    re.compile(
        r"(?i)\w*(?:token|secret|password|credential|api_?key)\w*\s*[=:]\s*\S+"
    ),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"https://[^/\s:@]+:[^@\s]+@"),
)


def redact(text: str) -> str:
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def run_result_to_dict(result: RunResult) -> dict[str, object]:
    return {
        "schema": "milo-bootstrap-v2-report",
        "mode": result.mode.value,
        "final_status": result.status.value,
        "starting_sha": result.starting_sha,
        "trusted_ref": result.trusted_ref,
        "plan_digest": result.plan_digest,
        "last_completed_stage": result.last_completed_stage.value,
        "metadata_status": result.metadata_status.value,
        "exit_code": result.exit_code(),
        "findings": [
            {
                "code": f.code,
                "severity": f.severity.value,
                "message": redact(f.message),
                "stage": f.stage.value,
                "critical": f.critical,
                "requires_manual": f.requires_manual,
                "unknown": f.unknown,
            }
            for f in result.findings
        ],
        "reads": [
            {
                "sequence": r.sequence,
                "provider": r.provider.value,
                "description": redact(r.description),
                "outcome": r.outcome.value,
                "resource": r.resource.key() if r.resource else "",
                "critical": r.critical,
            }
            for r in result.reads
        ],
        "mutations": [
            {
                "sequence": m.sequence,
                "provider": m.provider.value,
                "operation_type": m.operation_type.value,
                "resource": m.resource.key(),
                "idempotency_key": m.idempotency_key,
                "declared": m.declared,
                "executed": m.executed,
                "succeeded": m.succeeded,
                "error_class": m.error_class,
            }
            for m in result.mutations
        ],
        "post_write_verifications": [
            {
                "idempotency_key": v.idempotency_key,
                "verified": v.verified,
                "observed_post_state_digest": v.observed_post_state_digest,
                "expected_post_state_digest": v.expected_post_state_digest,
                "detail": redact(v.detail),
            }
            for v in result.verifications
        ],
        "resources_created_before_failure": [
            resource.key() for resource in result.created_resources
        ],
        "recovery_steps": [
            {"order": step.order, "description": redact(step.description)}
            for step in sorted(result.recovery_steps, key=lambda s: s.order)
        ],
    }


def render_human_summary(result: RunResult) -> str:
    lines = [
        "MILO bootstrap v2 report",
        f"  mode:                 {result.mode.value}",
        f"  final status:         {result.status.value.upper()}",
        f"  last completed stage: {result.last_completed_stage.value}",
        f"  starting sha:         {result.starting_sha}",
        f"  plan digest:          {result.plan_digest or '(none)'}",
        f"  metadata:             {result.metadata_status.value}",
        f"  reads:                {len(result.reads)}",
        f"  mutations executed:   {sum(1 for m in result.mutations if m.executed)}",
        f"  exit code:            {result.exit_code()}",
    ]
    blocking = [f for f in result.findings if f.blocks_apply() or f.severity.value == "blocked"]
    if blocking:
        lines.append("  blocking findings:")
        for finding in blocking:
            lines.append(f"    [{finding.severity.value.upper()}] {finding.code}: {redact(finding.message)}")
    if result.created_resources:
        lines.append("  resources created before failure:")
        for resource in result.created_resources:
            lines.append(f"    {resource.key()}")
    if result.recovery_steps:
        lines.append("  recovery steps:")
        for step in sorted(result.recovery_steps, key=lambda s: s.order):
            lines.append(f"    {step.order}. {redact(step.description)}")
    return "\n".join(lines) + "\n"


def write_json_report(result: RunResult, output_dir: Path) -> Path:
    """Atomically write the JSON report (0600) inside a private directory."""

    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    final_path = output_dir / "bootstrap-v2-report.json"
    payload = json.dumps(run_result_to_dict(result), indent=2, sort_keys=True)

    fd, tmp_name = tempfile.mkstemp(dir=output_dir, prefix=".report-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return final_path
