"""PR-OBS: scripts/ops/sentry_report.py -- one failed-workflow event, only with a DSN."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "sentry_report.py"
spec = importlib.util.spec_from_file_location("milo_sentry_report", TOOL)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

DSN = "https://publickey@o1.ingest.sentry.io/4242"
SECRET = "SECRET-SENTINEL"


class Opener:
    def __init__(self, fail: bool = False):
        self.requests = []
        self.fail = fail

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail:
            raise OSError("network down " + SECRET)

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()


def _env(**extra):
    return {"GITHUB_SHA": "c" * 40, "GITHUB_RUN_ID": "987", "GITHUB_EVENT_NAME": "schedule",
            "MILO_BACKUP_PASSPHRASE": SECRET, "MILO_BACKUP_DB_URL": f"postgresql://x:{SECRET}@h/db", **extra}


def test_no_dsn_reports_nothing(capsys):
    opener = Opener()
    assert report.main(["--workflow", "backup-supabase-scheduled", "--job", "backup"], _env(), opener) == 0
    assert opener.requests == []
    assert "not configured" in capsys.readouterr().out


def test_an_invalid_dsn_reports_nothing_and_is_not_printed(capsys):
    opener = Opener()
    assert report.main(["--workflow", "w", "--job", "j"], _env(SENTRY_DSN=f"junk-{SECRET}"), opener) == 0
    assert opener.requests == []
    assert SECRET not in capsys.readouterr().out


def test_one_event_with_names_only(capsys):
    opener = Opener()
    assert report.main(["--workflow", "backup-supabase-scheduled", "--job", "restore-test"],
                       _env(SENTRY_DSN=DSN), opener) == 0
    assert len(opener.requests) == 1
    request = opener.requests[0]
    assert request.full_url == "https://o1.ingest.sentry.io/api/4242/envelope/"
    body = request.data.decode()
    assert SECRET not in body
    event = json.loads(body.splitlines()[2])
    assert event["message"] == {"message": "backup-supabase-scheduled / restore-test failed"}
    assert event["release"] == "c" * 40
    assert event["tags"] == {"service": "backup-supabase-scheduled", "job": "restore-test",
                             "workflow_run_id": "987", "event": "schedule"}
    assert "publickey" in request.headers["X-sentry-auth"]
    assert "publickey" not in capsys.readouterr().out


def test_a_delivery_failure_never_fails_the_step(capsys):
    assert report.main(["--workflow", "w", "--job", "j"], _env(SENTRY_DSN=DSN), Opener(fail=True)) == 0
    assert SECRET not in capsys.readouterr().out


def test_the_envelope_url_keeps_a_path_prefix():
    assert report.parse_dsn("https://k@sentry.example/prefix/7") == ("https://sentry.example/prefix/api/7/envelope/", "k")
    assert report.parse_dsn("https://sentry.example/7") is None
