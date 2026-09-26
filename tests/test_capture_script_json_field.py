"""PR-U2 S2: `json_field` reads the capture document Cloud Run actually logs.

Cloud Run appends its own line after the entrypoint's JSON document --
"Container called exit(0)." -- and `json.loads` refuses that as extra data.
In production `--prepare` then reported "did not report a run id" although
the document it had read said status=prepared.

These tests extract the real `json_field` definition from
`scripts/catalog/government-production-capture.sh` and run it under the
script's own shell options, and drive the whole `--prepare` path with the
trailing line present.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = REPO / "scripts" / "catalog" / "government-production-capture.sh"
CLOUD_RUN_TRAILER = "Container called exit(0)."

DOCUMENT = {"entrypoint": "catalog-capture", "status": "prepared",
            "preparation": {"run_id": "00000000-0000-4000-8000-000000000099"},
            "work_scope": {"queued_item_count": 25, "batch_count": 3}}


def _json_field_definition() -> str:
    script = CAPTURE_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^json_field\(\) \{\n.*?^\}\n", script, re.M | re.S)
    assert match, f"json_field() not found in {CAPTURE_SCRIPT}"
    return match.group(0)


def json_field(text: str, path: str) -> subprocess.CompletedProcess:
    harness = "set -euo pipefail\n" + _json_field_definition() + 'json_field "$1"\n'
    return subprocess.run(["bash", "-c", harness, "json_field", path], input=text,
                          capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.parametrize("text", [
    json.dumps(DOCUMENT),
    json.dumps(DOCUMENT) + "\n",
    json.dumps(DOCUMENT, indent=2) + "\n",
    json.dumps(DOCUMENT) + "\n" + CLOUD_RUN_TRAILER + "\n",
    json.dumps(DOCUMENT) + CLOUD_RUN_TRAILER,
    json.dumps(DOCUMENT, indent=2) + "\n" + CLOUD_RUN_TRAILER + "\n",
    "Starting capture\n" + json.dumps(DOCUMENT) + "\n" + CLOUD_RUN_TRAILER + "\n",
], ids=["plain", "plain-newline", "indented", "trailer-line", "trailer-inline",
        "indented-trailer", "leading-and-trailer"])
def test_the_first_document_is_read_whatever_follows_it(text):
    for path, expected in (("status", "prepared"),
                           ("preparation.run_id", "00000000-0000-4000-8000-000000000099"),
                           ("work_scope.batch_count", "3")):
        result = json_field(text, path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected + "\n"


def test_only_the_first_object_is_read():
    text = json.dumps({"status": "prepared"}) + "\n" + json.dumps({"status": "failed"}) + "\n"
    assert json_field(text, "status").stdout == "prepared\n"


@pytest.mark.parametrize("text,path", [
    (json.dumps(DOCUMENT) + "\n" + CLOUD_RUN_TRAILER + "\n", "preparation.missing"),
    (json.dumps(DOCUMENT) + "\n" + CLOUD_RUN_TRAILER + "\n", "status.nested"),
    (CLOUD_RUN_TRAILER + "\n", "status"),
    ("", "status"),
    ('{"status": "prepared"\n' + CLOUD_RUN_TRAILER + "\n", "status"),
], ids=["missing-key", "not-an-object", "trailer-only", "empty", "truncated"])
def test_a_field_that_is_not_there_is_still_a_refusal(text, path):
    result = json_field(text, path)
    assert result.returncode == 1
    assert result.stdout == ""


def test_prepare_reports_the_run_id_when_cloud_run_appends_its_exit_line(tmp_path):
    """The production failure, end to end: `gcloud logging read` answers
    newest-first, so after the script's `tac` the document is followed by
    Cloud Run's own exit line."""
    from tests.test_work_scope_preparation import SUCCEEDED_DOCUMENT, _capture_script

    prepared = dict(SUCCEEDED_DOCUMENT,
                    preparation={"run_id": "00000000-0000-4000-8000-000000000099"})
    newest_first = CLOUD_RUN_TRAILER + "\n" + json.dumps(prepared)
    result, _log = _capture_script(tmp_path, "--enable-catalog-execution", gcloud=True,
                                   mode="--prepare", document=prepared,
                                   env={"MOCK_GCLOUD_DOCUMENT": newest_first})
    assert result.returncode == 0, result.stderr
    assert "did not report a run id" not in result.stderr
    assert "PREPARED_RUN_ID=00000000-0000-4000-8000-000000000099" in result.stdout
