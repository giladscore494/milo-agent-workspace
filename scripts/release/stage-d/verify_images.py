"""Read-only IMMUTABLE image verification for Stage D.

Stage D never rebuilds and never redeploys. It proves, by DIGEST, that
production is still serving the exact accepted release images — and
BLOCKS if it is not.

Why a tag match is not acceptance
---------------------------------
A rebuild of the same Git SHA is not reproducible in this repository:
`Dockerfile.api`/`Dockerfile.worker` start FROM the mutable base tag
`python:3.12-slim`, `backend/requirements.txt` carries the unpinned floor
`openai>=1.30.0`, and there is no lockfile or `--require-hashes`. A
rebuild can therefore produce different bytes and, pushed under the same
`:<sha>` tag, would silently replace the accepted image while every
tag-based check still reported success. That is not theoretical in this
project: the Worker tag has resolved to several distinct digests over
time. The digest is the release identity; the tag is only a label.

What this module verifies
-------------------------
1. REGISTRY — the tag `:<release sha>` on each of api/worker still
   resolves to exactly the pinned digest, and to exactly one image.
2. API SERVING REVISION — the latest ready revision serves 100% of
   traffic and runs the pinned API digest. A Cloud Run *service* resolves
   the tag to a digest when the revision is created and the revision is
   immutable, so this is a strong, point-in-time-independent proof.
3. WORKER JOB — the job's image reference resolves to the pinned Worker
   digest. A Cloud Run *job* is NOT a revision: its template may hold a
   mutable tag that is resolved afresh at every execution. When the job
   references a tag, this check is only as strong as the registry check
   at this instant, and the module says so explicitly in its output. The
   evidence gate closes that window by re-verifying the digest that the
   authorized execution ACTUALLY ran.
4. RELEASE CORRESPONDENCE — every verified reference is under the pinned
   registry and carries/holds the pinned release SHA tag.
5. ACTUAL EXECUTION (optional, `--execution-json`) — the digest a Worker
   execution REALLY ran. A Cloud Run execution records the resolved
   digest, so this is the only check that cannot be invalidated by a tag
   moving afterwards. The evidence gate passes the authorized execution
   here, which closes the tag-resolution window left by check 3.

Any mismatch, any missing input, any ambiguous registry listing and any
unparseable field fails closed. A mismatch is never auto-repaired: it
means the accepted release is no longer what production serves, which is
a separate reviewed release, never a Stage D action.

Usage:
  python3 verify_images.py \
    --registry-json <file> --api-service-json <file> \
    --api-revision-json <file> --worker-job-json <file>

Env (exported by stage-d-env.sh): STAGE_D_REGISTRY, STAGE_D_RELEASE_SHA,
STAGE_D_API_IMAGE_DIGEST, STAGE_D_WORKER_IMAGE_DIGEST.
Prints one structured JSON verdict. Exit 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

DIGEST_PREFIX = "sha256:"


def load(path: str, problems: list[str], label: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{label}: could not be read as JSON ({exc.__class__.__name__}) — failing closed")
        return None


def registry_digest_for(listing: object, package: str, release_sha: str, problems: list[str]) -> str | None:
    """The digest the release tag resolves to for one package, else None.

    Accepts the `gcloud artifacts docker images list --include-tags
    --format=json` shape. Zero matches and more than one match both fail
    closed: an ambiguous listing must never be reduced to a guess.
    """
    if not isinstance(listing, list):
        problems.append("registry listing was not a JSON list — failing closed")
        return None
    matches = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("package", "")) != package:
            continue
        tags = entry.get("tags")
        # gcloud renders tags as a list or as a comma-separated string.
        if isinstance(tags, str):
            tag_set = {t.strip() for t in tags.split(",") if t.strip()}
        elif isinstance(tags, list):
            tag_set = {str(t).strip() for t in tags}
        else:
            tag_set = set()
        if release_sha in tag_set:
            matches.append(entry)
    if not matches:
        problems.append(
            f"registry: no image in {package} carries the release tag {release_sha} — "
            "the accepted release is not present; failing closed"
        )
        return None
    if len(matches) > 1:
        problems.append(
            f"registry: {len(matches)} images in {package} carry the release tag {release_sha} — "
            "the tag is ambiguous; failing closed"
        )
        return None
    digest = str(matches[0].get("version", ""))
    if not digest.startswith(DIGEST_PREFIX):
        problems.append(f"registry: {package} tag {release_sha} has no usable digest — failing closed")
        return None
    return digest


def reference_digest(image: str, repo: str, pinned_digest: str, release_sha: str,
                     registry_digest: str | None, surface: str, problems: list[str]) -> tuple[str | None, str]:
    """Resolve a container image reference to a digest.

    Returns (digest, how) where `how` is "digest" (the reference pins the
    digest directly — strongest) or "tag" (the reference is a mutable tag
    resolved through the registry at this instant — weaker; the caller
    must say so).
    """
    if not isinstance(image, str) or not image:
        problems.append(f"{surface}: image reference is missing — failing closed")
        return None, "unknown"
    if "@" in image:
        ref_repo, _, digest = image.partition("@")
        if ref_repo != repo:
            problems.append(f"{surface}: image {image!r} is not from the pinned repository {repo!r}")
            return None, "digest"
        if digest != pinned_digest:
            problems.append(
                f"{surface}: runs digest {digest} but the accepted release digest is {pinned_digest} — "
                "production is NOT serving the accepted image; this requires a separate reviewed release"
            )
            return None, "digest"
        return digest, "digest"
    ref_repo, _, tag = image.partition(":")
    if ref_repo != repo:
        problems.append(f"{surface}: image {image!r} is not from the pinned repository {repo!r}")
        return None, "tag"
    if tag != release_sha:
        problems.append(f"{surface}: image tag {tag!r} is not the pinned release {release_sha!r}")
        return None, "tag"
    if registry_digest is None:
        problems.append(
            f"{surface}: references the mutable tag {tag!r} and the registry resolution is unavailable — "
            "the running digest cannot be established; failing closed"
        )
        return None, "tag"
    if registry_digest != pinned_digest:
        problems.append(
            f"{surface}: mutable tag {tag!r} now resolves to {registry_digest}, not the accepted release digest "
            f"{pinned_digest} — the tag was moved; this requires a separate reviewed release"
        )
        return None, "tag"
    return registry_digest, "tag"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-json", required=True)
    parser.add_argument("--api-service-json", required=True)
    parser.add_argument("--api-revision-json", required=True)
    parser.add_argument("--worker-job-json", required=True)
    parser.add_argument(
        "--execution-json",
        help="optional: a Worker execution describe, to prove the digest it ACTUALLY ran",
    )
    args = parser.parse_args()

    problems: list[str] = []
    notes: list[str] = []

    registry = os.environ.get("STAGE_D_REGISTRY", "")
    release_sha = os.environ.get("STAGE_D_RELEASE_SHA", "")
    api_digest = os.environ.get("STAGE_D_API_IMAGE_DIGEST", "")
    worker_digest = os.environ.get("STAGE_D_WORKER_IMAGE_DIGEST", "")
    for name, value in (("STAGE_D_REGISTRY", registry), ("STAGE_D_RELEASE_SHA", release_sha),
                        ("STAGE_D_API_IMAGE_DIGEST", api_digest),
                        ("STAGE_D_WORKER_IMAGE_DIGEST", worker_digest)):
        if not value:
            problems.append(f"{name} is not set — cannot verify the accepted release; failing closed")
    for name, value in (("STAGE_D_API_IMAGE_DIGEST", api_digest),
                        ("STAGE_D_WORKER_IMAGE_DIGEST", worker_digest)):
        if value and not value.startswith(DIGEST_PREFIX):
            problems.append(f"{name}={value!r} is not a sha256 digest — failing closed")
    if problems:
        print(json.dumps({"stage_d_image_gate": "BLOCKED", "ok": False, "problems": problems}, indent=2))
        return 1

    api_repo = f"{registry}/api"
    worker_repo = f"{registry}/worker"

    # -- 1. Registry: the release tag still resolves to the accepted digests.
    listing = load(args.registry_json, problems, "registry listing")
    registry_api = registry_digest_for(listing, api_repo, release_sha, problems) if listing is not None else None
    registry_worker = registry_digest_for(listing, worker_repo, release_sha, problems) if listing is not None else None
    if registry_api is not None and registry_api != api_digest:
        problems.append(
            f"registry: the api tag {release_sha} resolves to {registry_api}, not the accepted digest "
            f"{api_digest} — the tag was re-pushed; this requires a separate reviewed release"
        )
    if registry_worker is not None and registry_worker != worker_digest:
        problems.append(
            f"registry: the worker tag {release_sha} resolves to {registry_worker}, not the accepted digest "
            f"{worker_digest} — the tag was re-pushed; this requires a separate reviewed release"
        )

    # -- 2. API serving revision: immutable, must run the accepted digest.
    service = load(args.api_service_json, problems, "api service describe")
    revision = load(args.api_revision_json, problems, "api revision describe")
    api_running: str | None = None
    ready_name = ""
    if isinstance(service, dict):
        status = service.get("status") or {}
        ready_name = str(status.get("latestReadyRevisionName") or "")
        traffic = status.get("traffic") or []
        if not ready_name:
            problems.append("api: the service has no latest ready revision — failing closed")
        elif not any(
            isinstance(item, dict) and item.get("revisionName") == ready_name
            and int(item.get("percent") or 0) == 100
            for item in traffic
        ):
            problems.append(f"api: the latest ready revision {ready_name} is not serving 100% of traffic")
    if isinstance(revision, dict):
        described = str(((revision.get("metadata") or {}).get("name")) or "")
        if ready_name and described != ready_name:
            problems.append(
                f"api: the described revision {described!r} is not the serving revision {ready_name!r} — "
                "the wrong revision was inspected; failing closed"
            )
        try:
            image = revision["spec"]["containers"][0].get("image", "")
        except (KeyError, IndexError, TypeError):
            image = ""
            problems.append("api revision: container image could not be read — failing closed")
        if image:
            api_running, _how = reference_digest(
                image, api_repo, api_digest, release_sha, registry_api, "api serving revision", problems)

    # -- 3. Worker job: must resolve to the accepted digest.
    job = load(args.worker_job_json, problems, "worker job describe")
    worker_running: str | None = None
    worker_how = "unknown"
    if isinstance(job, dict):
        try:
            image = job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].get("image", "")
        except (KeyError, IndexError, TypeError):
            image = ""
            problems.append("worker job: container image could not be read — failing closed")
        if image:
            worker_running, worker_how = reference_digest(
                image, worker_repo, worker_digest, release_sha, registry_worker, "worker job", problems)
            if worker_how == "tag" and worker_running is not None:
                notes.append(
                    "worker job references the MUTABLE tag rather than the digest. A Cloud Run job is not a "
                    "revision: it resolves that tag afresh at every execution, so this proof is point-in-time "
                    "only. 05-execute-run.sh re-verifies immediately before run creation and the evidence gate "
                    "verifies the digest the authorized execution actually ran."
                )

    # -- 5. The digest an authorized execution ACTUALLY ran (optional).
    # An execution records its resolved digest, so unlike the job template
    # this cannot be invalidated by the tag moving later.
    execution_running: str | None = None
    execution_name = ""
    if args.execution_json:
        execution = load(args.execution_json, problems, "worker execution describe")
        if isinstance(execution, dict):
            execution_name = str(((execution.get("metadata") or {}).get("name")) or "")
            try:
                image = execution["spec"]["template"]["spec"]["containers"][0].get("image", "")
            except (KeyError, IndexError, TypeError):
                image = ""
                problems.append("worker execution: container image could not be read — failing closed")
            if image:
                execution_running, how = reference_digest(
                    image, worker_repo, worker_digest, release_sha, registry_worker,
                    f"worker execution {execution_name or '<unnamed>'}", problems)
                if how != "digest":
                    problems.append(
                        f"worker execution {execution_name or '<unnamed>'}: recorded image {image!r} is not a "
                        "digest reference, so what it actually ran cannot be established; failing closed")

    ok = not problems
    print(json.dumps({
        "stage_d_image_gate": "verify",
        "ok": ok,
        "release_sha": release_sha,
        "expected": {"api": api_digest, "worker": worker_digest},
        "registry_resolution": {"api": registry_api, "worker": registry_worker},
        "api_serving_revision": {"name": ready_name, "digest": api_running},
        "worker_job": {"digest": worker_running, "reference_kind": worker_how},
        "worker_execution": {"name": execution_name, "digest": execution_running},
        "notes": notes,
        "problems": problems,
    }, indent=2))
    if not ok:
        print(
            "\nSTAGE D IMAGE VERIFICATION FAILED — do NOT proceed.\n"
            "Stage D never rebuilds and never redeploys: a rebuild of this release is not byte-reproducible\n"
            "(mutable python:3.12-slim base, unpinned openai>=1.30.0, no lockfile), so re-pushing the tag\n"
            "would replace the accepted image rather than restore it. A digest mismatch means production is\n"
            "no longer serving the accepted release, which requires a SEPARATE REVIEWED RELEASE.",
            file=sys.stderr,
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
