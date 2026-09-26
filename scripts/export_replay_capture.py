#!/usr/bin/env python3
"""Operator tool: export ONE run's replay capture as a committed replay fixture.

A run executed with the ``MILO_CAPTURE_REPLAY`` capture switched on carries a bounded capture of
its inert provider outputs and Government tool results on its own checkpoints
(``artifacts.replay_capture``; ``run_checkpoints`` is service-only). This tool
reads the run's LATEST checkpoint, the run's own Government preparation record
and exactly the register rows the recorded tool results name, and writes::

    tests/replay/<name>/manifest.json      (format replay/1, every artifact "captured")

Usage (operator workstation, service credentials in the environment)::

    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python scripts/export_replay_capture.py --run-id <run uuid> \\
        --out tests/replay/<first 8 hex of the run id> [--write-expected]

Read-only: nothing is written anywhere but ``--out``. It REFUSES to write when
the capture is missing, was truncated at its bound, or when the sanitizer
(``backend.replay_capture.sanitization_findings``) finds anything -- a secret,
a user id, an e-mail address, a URL outside data.gov.il, an unexplained UUID.
The run's objective text is never exported.

``--write-expected`` replays the export against the current code
(tests/replay_harness.py) and writes that outcome as ``expected``; review it
before committing -- it is what every future PR will be held to.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.catalog.government.preparation import ARTIFACT_KEY as PREPARATION_KEY  # noqa: E402
from backend.replay_capture import (CAPTURE_ARTIFACT_KEY, CAPTURE_FORMAT, CAPTURED,  # noqa: E402
                                    REPLAY_FORMAT, sanitization_findings)

PREPARATION_FIELDS = ("snapshot_key", "resource_id", "upstream_version",
                      "upstream_version_kind", "queue", "total_candidates", "bounded")


class ExportRefused(Exception):
    """A static reason the export will not be written."""


def _captured(source: str) -> dict[str, str]:
    return {"kind": CAPTURED, "source": source}


def build_manifest(repository: Any, run_id: str, *, name: str,
                   today: str | None = None) -> dict[str, Any]:
    """The replay/1 manifest for one captured run (no `expected` outcome yet)."""
    checkpoint = repository.latest_checkpoint(run_id)
    artifacts = (checkpoint or {}).get("artifacts") or {}
    capture = artifacts.get(CAPTURE_ARTIFACT_KEY)
    if not isinstance(capture, Mapping) or capture.get("format") != CAPTURE_FORMAT:
        raise ExportRefused("REPLAY_CAPTURE_MISSING")
    if capture.get("truncated"):
        raise ExportRefused("REPLAY_CAPTURE_TRUNCATED")
    record = artifacts.get(PREPARATION_KEY)
    if not isinstance(record, Mapping):
        raise ExportRefused("PREPARATION_RECORD_MISSING")
    stamp = today or dt.date.today().isoformat()
    origin = f"MILO_CAPTURE_REPLAY capture of run {name}, exported {stamp}"

    commander, verifier, workers = [], [], {}
    for entry in capture.get("completions") or []:
        item = {"content": entry.get("content") if isinstance(entry.get("content"), str)
                else "", "finish_reason": entry.get("finish_reason") or "stop"}
        if entry.get("role") == "commander":
            commander.append({"phase": entry["phase"], **item})
        elif entry.get("role") == "verifier":
            verifier.append({"phase": entry["phase"], **item})
        elif entry.get("role") == "worker":
            workers.setdefault(str(entry["task_id"]), []).append(item)
    tool_results = [{key: entry[key] for key in ("task_id", "call_id", "tool", "operation",
                                                 "arguments", "result")}
                    for entry in capture.get("tool_calls") or []]
    if any(item["arguments"] is None for item in tool_results):
        raise ExportRefused("TOOL_ARGUMENTS_NOT_CAPTURED")

    referenced = sorted({str(variant["upstream_record_id"])
                         for item in tool_results
                         for variant in (item["result"].get("variants") or [])} |
                        {str(item["result"]["source_record"]["upstream_record_id"])
                         for item in tool_results
                         if isinstance(item["result"].get("source_record"), Mapping)},
                        key=lambda value: (len(value), value))
    rows = []
    for record_id in referenced:
        row = repository.catalog_raw_record_by_upstream_id(record["snapshot_id"], record_id)
        payload = (row or {}).get("payload")
        if not isinstance(payload, Mapping):
            raise ExportRefused("SNAPSHOT_ROW_UNREADABLE")
        rows.append(dict(payload))

    provenance: dict[str, dict[str, str]] = {"preparation": _captured(
        f"the run's durable preparation record (artifacts.government); {origin}")}
    provenance.update({f"commander[{index}]": _captured(origin)
                       for index in range(len(commander))})
    for task_id, attempts in workers.items():
        provenance.update({f"workers.{task_id}[{index}]": _captured(origin)
                           for index in range(len(attempts))})
    provenance.update({f"verifier[{index}]": _captured(origin)
                       for index in range(len(verifier))})
    provenance.update({f"tool_results.{item['task_id']}/{item['call_id']}": _captured(origin)
                       for item in tool_results})
    provenance.update({f"snapshot_rows.{row['_id']}": _captured(
        f"register row read from the run's pinned snapshot {record['snapshot_key']}")
        for row in rows})
    return {"format": REPLAY_FORMAT, "run_id": str(run_id),
            "description": f"captured run {name}",
            "models": dict(capture.get("models") or {}),
            # Never exported: the objective is run input and can carry user text.
            "objective": "(objective not exported)",
            "preparation": {key: record[key] for key in PREPARATION_FIELDS},
            "commander": commander, "workers": dict(sorted(workers.items())),
            "verifier": verifier, "tool_results": tool_results, "snapshot_rows": rows,
            "provenance": dict(sorted(provenance.items())),
            "expected": {"terminal": "result",
                         "note": "fill with --write-expected and review"}}


def write_manifest(manifest: Mapping[str, Any], out: Path, *, write_expected: bool) -> Path:
    if write_expected:
        sys.path.insert(0, str(ROOT / "tests"))
        from replay_harness import replay

        manifest = {**manifest, "expected": replay(manifest).outcome()}
    findings = sanitization_findings(manifest)
    if findings:
        raise ExportRefused("SANITIZATION_FAILED: " + "; ".join(findings))
    out.mkdir(parents=True, exist_ok=True)
    target = out / "manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    return target


def main(argv: list[str] | None = None, *, repository: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--write-expected", action="store_true")
    args = parser.parse_args(argv)
    if repository is None:
        from backend.dependencies import get_repository

        repository = get_repository()
    try:
        manifest = build_manifest(repository, args.run_id, name=args.out.name)
        target = write_manifest(manifest, args.out, write_expected=args.write_expected)
    except ExportRefused as refusal:
        print(f"export refused: {refusal}", file=sys.stderr)
        return 1
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
