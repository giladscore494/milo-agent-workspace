"""Verify the DISPOSABLE Stage D probe jobs are exactly what was reviewed.

The db probe runs with `SUPABASE_SERVICE_ROLE_KEY` bound. Whatever image
that job runs executes arbitrary code with service-role access to
production, so the image is a privileged supply-chain input and must be
pinned by DIGEST, never by a tag.

An earlier revision created both jobs with `--image=python:3.12-slim`.
That is a MUTABLE upstream tag: Docker Hub re-publishes it, so the job
could silently start executing different code with service-role
credentials between one execution and the next, with nothing in the
toolkit noticing.

This module checks, for both jobs:

  * the image is EXACTLY the pinned `<repo>@<digest>` reference — a tag
    reference is refused outright, a different digest is refused, and an
    image from any other repository is refused;
  * the service account is the expected operator-controlled identity;
  * the db probe's secret REFERENCES are exactly the reviewed ones —
    SUPABASE_URL -> SUPABASE_URL:latest and SUPABASE_SERVICE_ROLE_KEY ->
    SUPABASE_SECRET_KEY:latest, with no extras — because the env name is
    only a label and the reference is what is actually read; the gateway
    probe holds NO secret reference at all;
  * neither job carries a provider-key alias in any form.

It runs after the jobs are created AND again immediately before every
probe execution, because a Cloud Run job template can be updated between
the two and the credentials are what make that worth checking.

Usage:
  python3 verify_probe_jobs.py [--db-json <file>] [--gw-json <file>]
  (at least one; each job is verified independently)

Env (exported by stage-d-env.sh): STAGE_D_PROBE_IMAGE_REPO,
STAGE_D_PROBE_IMAGE_DIGEST, STAGE_D_API_SA, STAGE_D_GATEWAY_SA.
Prints one structured JSON verdict. Exit 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

#: The exact secret REFERENCES the db probe may hold: env name -> the
#: Secret Manager secret and version behind it. Checking only the env
#: names would accept `SUPABASE_SERVICE_ROLE_KEY` wired to some other
#: secret entirely, which is the interesting attack — the name is the
#: label, the reference is what is actually read. These are the two the
#: API runtime identity already accesses; no new grant, and nothing else.
DB_PROBE_EXPECTED_SECRET_REFS = {
    "SUPABASE_URL": ("SUPABASE_URL", "latest"),
    "SUPABASE_SERVICE_ROLE_KEY": ("SUPABASE_SECRET_KEY", "latest"),
}

#: Every provider-key alias the Worker accepts. A probe must never hold one.
PROVIDER_SECRET_ALIASES = ("KIMI_API_KEY", "MOONSHOT_API_KEY")


def load(path: str, label: str, problems: list[str]):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{label}: could not be read as JSON ({exc.__class__.__name__}) — failing closed")
        return None


def container_of(job: object, label: str, problems: list[str]) -> dict | None:
    try:
        return job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    except (KeyError, IndexError, TypeError):
        problems.append(f"{label}: container spec could not be read — failing closed")
        return None


def service_account_of(job: object) -> str:
    try:
        return str(job["spec"]["template"]["spec"]["template"]["spec"].get("serviceAccountName") or "")
    except (KeyError, TypeError):
        return ""


#: Registry prefixes Docker Hub images may legitimately carry. Cloud Run
#: may render `python@sha256:…` in its canonical `docker.io/library/…`
#: form (or the reverse), so both sides are normalised before comparison.
#: This makes the check robust to that rewriting WITHOUT loosening it:
#: the digest is still compared exactly, and any other repository is
#: still refused.
_DOCKER_HUB_PREFIXES = ("docker.io/library/", "index.docker.io/library/", "registry-1.docker.io/library/")


def canonical_repo(repo: str) -> str:
    """One spelling for a repository, so normalisation cannot fail a check."""
    for prefix in _DOCKER_HUB_PREFIXES:
        if repo.startswith(prefix):
            return "docker.io/library/" + repo[len(prefix):]
    # A bare, single-segment name is a Docker Hub official image.
    if "/" not in repo and "." not in repo.split(":")[0]:
        return "docker.io/library/" + repo
    return repo


def secret_refs_of(env: list) -> dict[str, tuple[str, str]]:
    """env name -> (secret name, version) for every secret-backed variable."""
    refs: dict[str, tuple[str, str]] = {}
    for entry in env:
        if not isinstance(entry, dict) or "valueFrom" not in entry:
            continue
        source = entry.get("valueFrom")
        if not isinstance(source, dict):
            continue
        ref = source.get("secretKeyRef")
        if not isinstance(ref, dict):
            # A secret-backed variable whose reference cannot be read is
            # not a variable whose reference has been verified.
            refs[str(entry.get("name"))] = ("<unreadable>", "<unreadable>")
            continue
        refs[str(entry.get("name"))] = (str(ref.get("name") or ""), str(ref.get("key") or ""))
    return refs


def check_secret_refs(label: str, refs: dict[str, tuple[str, str]],
                      expected: dict[str, tuple[str, str]], problems: list[str]) -> None:
    for name, (secret, version) in sorted(expected.items()):
        actual = refs.get(name)
        if actual is None:
            problems.append(f"{label}: expected secret-backed variable {name} is missing")
            continue
        if actual != (secret, version):
            problems.append(
                f"{label}: {name} is backed by {actual[0]}:{actual[1]}, expected {secret}:{version} — "
                "the env NAME is only a label; the reference is what is actually read")
    for name in sorted(set(refs) - set(expected)):
        problems.append(
            f"{label}: unexpected secret reference {name} -> {refs[name][0]}:{refs[name][1]}")


def check_image(label: str, container: dict, expected: str, repo: str, problems: list[str]) -> str:
    image = container.get("image")
    if not isinstance(image, str) or not image:
        problems.append(f"{label}: image reference is missing — failing closed")
        return ""
    if "@" not in image:
        problems.append(
            f"{label}: image {image!r} is a TAG reference. A probe that holds production credentials must "
            f"be pinned by digest — expected {expected!r}")
        return image
    ref_repo, _, digest = image.partition("@")
    if canonical_repo(ref_repo) != canonical_repo(repo):
        problems.append(
            f"{label}: image {image!r} comes from {ref_repo!r}, not the reviewed probe repository {repo!r}")
        return image
    if digest != expected.partition("@")[2]:
        problems.append(
            f"{label}: image digest {digest} is not the reviewed probe runtime digest — expected {expected!r}")
    return image


def check_job(label: str, job: object, expected_image: str, repo: str,
              expected_sa: str, expected_secret_refs: dict[str, tuple[str, str]],
              problems: list[str]) -> dict:
    evidence: dict = {}
    container = container_of(job, label, problems)
    if container is None:
        return evidence
    evidence["image"] = check_image(label, container, expected_image, repo, problems)

    actual_sa = service_account_of(job)
    evidence["service_account"] = actual_sa
    if expected_sa and actual_sa != expected_sa:
        problems.append(f"{label}: runs as {actual_sa!r}, expected the operator-controlled {expected_sa!r}")

    env = container.get("env") or []
    values = {e["name"] for e in env if isinstance(e, dict) and "value" in e}
    refs = secret_refs_of(env)
    evidence["secret_references"] = {k: f"{v[0]}:{v[1]}" for k, v in sorted(refs.items())}
    check_secret_refs(label, refs, expected_secret_refs, problems)
    for alias in PROVIDER_SECRET_ALIASES:
        if alias in refs or alias in values:
            problems.append(f"{label}: provider alias {alias} must NEVER be present on a probe job")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-json")
    parser.add_argument("--gw-json")
    args = parser.parse_args()
    if not args.db_json and not args.gw_json:
        print("usage: at least one of --db-json / --gw-json is required", file=sys.stderr)
        return 2

    problems: list[str] = []
    repo = os.environ.get("STAGE_D_PROBE_IMAGE_REPO", "")
    digest = os.environ.get("STAGE_D_PROBE_IMAGE_DIGEST", "")
    if not repo or not digest:
        problems.append(
            "STAGE_D_PROBE_IMAGE_REPO / STAGE_D_PROBE_IMAGE_DIGEST are not both set — the privileged probe "
            "runtime is unpinned; failing closed")
    if digest and not digest.startswith("sha256:"):
        problems.append(f"STAGE_D_PROBE_IMAGE_DIGEST={digest!r} is not a sha256 digest — failing closed")
    if problems:
        print(json.dumps({"stage_d_probe_job_gate": "BLOCKED", "ok": False, "problems": problems}, indent=2))
        return 1

    expected_image = f"{repo}@{digest}"
    verdict: dict = {"stage_d_probe_job_gate": "verify", "expected_image": expected_image, "jobs": {}}

    db_job = load(args.db_json, "db probe", problems) if args.db_json else None
    if db_job is not None:
        verdict["jobs"]["db"] = check_job(
            "db probe", db_job, expected_image, repo,
            os.environ.get("STAGE_D_API_SA", ""), DB_PROBE_EXPECTED_SECRET_REFS, problems)

    if args.gw_json:
        gw_job = load(args.gw_json, "gw probe", problems)
        if gw_job is not None:
            verdict["jobs"]["gw"] = check_job(
                "gw probe", gw_job, expected_image, repo,
                # The gateway probe must hold ZERO secret references.
                os.environ.get("STAGE_D_GATEWAY_SA", ""), {}, problems)

    verdict["problems"] = problems
    verdict["ok"] = not problems
    print(json.dumps(verdict, indent=2))
    if problems:
        print(
            "\nSTAGE D PROBE JOB VERIFICATION FAILED — do NOT execute the probe.\n"
            "The db probe holds SUPABASE_SERVICE_ROLE_KEY: an unpinned or unexpected image would run\n"
            "arbitrary code with service-role access to production.",
            file=sys.stderr)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
