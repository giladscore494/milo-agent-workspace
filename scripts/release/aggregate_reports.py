"""Consolidate release sub-audit JSON reports into one accurate aggregate.

Reads the orchestrator's own top-level report plus every sub-audit report and
produces a single aggregate whose summary totals equal the sum of ALL checks
(top-level and nested), with every blocking finding, warning and manual action
surfaced (manual actions de-duplicated by their prerequisite text).

The aggregate is valid JSON even when a sub-audit failed before producing a
report: an unreadable or unparseable sub-report path becomes a blocking
finding rather than crashing the aggregation.

Exit status: 1 when the consolidated blocked total is greater than zero, else
0. Usage errors exit 64.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

STATUS_KEY = {
    "PASS": "pass",
    "WARN": "warn",
    "BLOCKED": "blocked",
    "MANUAL": "manual",
    "NOT_APPLICABLE": "not_applicable",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-level", required=True, help="orchestrator top-level report JSON")
    parser.add_argument("--sub-report", action="append", default=[], help="name=path (repeatable)")
    parser.add_argument("--head-sha", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    summary = {"pass": 0, "warn": 0, "blocked": 0, "manual": 0, "not_applicable": 0}
    blocking_findings: list[dict] = []
    warnings: list[dict] = []
    manual_actions: list[dict] = []
    sub_reports: dict[str, dict] = {}

    def ingest(source: str, report: dict) -> None:
        for check in report.get("checks", []):
            status = check.get("status", "")
            key = STATUS_KEY.get(status)
            if key is not None:
                summary[key] += 1
            entry = {
                "source": source,
                "check": check.get("name", ""),
                "detail": check.get("detail", ""),
            }
            if status == "BLOCKED":
                blocking_findings.append(entry)
            elif status == "WARN":
                warnings.append(entry)
            elif status == "MANUAL":
                manual_actions.append(entry)

    # Top-level report. If even this is unreadable the aggregate still emits
    # valid JSON and records the failure as blocking.
    try:
        top = json.loads(Path(args.top_level).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        summary["blocked"] += 1
        blocking_findings.append(
            {
                "source": "production-readiness",
                "check": "aggregate:top-level-unreadable",
                "detail": f"top-level report could not be read/parsed: {exc}",
            }
        )
        top = {}
    else:
        ingest(top.get("script", "production-readiness"), top)

    for spec in args.sub_report:
        name, sep, path = spec.partition("=")
        if not sep:
            print(f"invalid --sub-report (expected name=path): {spec}", file=sys.stderr)
            return 64
        try:
            report = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Missing or corrupt sub-report is itself a blocking finding.
            summary["blocked"] += 1
            blocking_findings.append(
                {
                    "source": name,
                    "check": f"{name}:report-unreadable",
                    "detail": f"sub-report could not be read/parsed: {exc}",
                }
            )
            continue
        sub_reports[name] = report
        ingest(name, report)

    # De-duplicate manual actions that name the same prerequisite (same detail).
    seen: set[str] = set()
    manual_dedup: list[dict] = []
    for action in manual_actions:
        fingerprint = action["detail"].strip()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        manual_dedup.append(action)

    aggregate = {
        "script": "production-readiness",
        "head_sha": args.head_sha,
        "branch": args.branch,
        "summary": summary,
        "blocking_findings": blocking_findings,
        "warnings": warnings,
        "manual_actions_remaining": manual_dedup,
        "sub_reports": sub_reports,
    }
    Path(args.output).write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")

    print(
        "CONSOLIDATED TOTALS: "
        f"{summary['pass']} PASS, {summary['warn']} WARN, "
        f"{summary['blocked']} BLOCKED, {summary['manual']} MANUAL, "
        f"{summary['not_applicable']} NOT_APPLICABLE"
    )
    if blocking_findings:
        print(f"BLOCKING FINDINGS ({len(blocking_findings)}):")
        for finding in blocking_findings:
            print(f"  [{finding['source']}] {finding['check']} — {finding['detail']}")
    if manual_dedup:
        print(f"MANUAL ACTIONS REMAINING ({len(manual_dedup)} unique):")
        for action in manual_dedup:
            print(f"  [{action['source']}] {action['check']} — {action['detail']}")

    return 1 if summary["blocked"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
