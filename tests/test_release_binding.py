"""The release-binding verifiers kept in scripts/release/pins/ (cleanup D8).

policy_envelope.py (the reviewed policy pin and the content binding),
verify_caps.py (the deployed caps, limits, flags, keys and release identity
against the policy) and verify_images.py (the accepted image digests) were the
Stage D toolkit's verifiers. When the rest of that toolkit was deleted, their
tests moved here unchanged from tests/test_stage_d_toolkit.py (sections C and
L), running against the permanent copies.
"""


from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.runtime_policy import (
    CAP_ENV_PREFIXES,
    ENGINE_ENV_PREFIXES,
    PROVIDER_ENV_PREFIXES,
    reviewed_first_run_policy,
)

REPO = Path(__file__).resolve().parents[1]


# The permanent copies of policy_envelope.py, verify_caps.py and
# verify_images.py (cleanup D8).
PINS = REPO / "scripts" / "release" / "pins"


RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"


# The commit this checkout is actually at. verify_caps.py REFUSES unless
# backend/runtime_policy.py here is byte-for-byte the file at
# STAGE_D_RELEASE_SHA; HEAD is simply the most convenient commit that
# satisfies that, since the working tree is committed in CI. It is NOT a
# requirement that the checkout BE the release --
# `test_a_later_authorization_commit_may_reference_an_earlier_release` proves
# the opposite, which is what makes re-authorization possible at all.
CHECKOUT_SHA = subprocess.run(
    ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"],
    capture_output=True, text=True, timeout=60).stdout.strip()


REGISTRY = "us-central1-docker.pkg.dev/big-cabinet-457321-t7/milo-agent"


# The ACCEPTED release digests. The release identity is the digest, not
# the tag: this repository cannot reproduce a build byte-for-byte
# (mutable python:3.12-slim base, unpinned openai>=1.30.0, no lockfile),
# so a rebuild pushed under the same tag would replace the accepted
# image rather than re-prove it.
API_DIGEST = "sha256:04275e81995d7bbaf23d0e71e71c2ac83adf37f45eca8686ddb812050a18caa6"


WORKER_DIGEST = "sha256:d3743e5a8dabc3f663970abe83886ea91b030ad7b339e1178d0ab5efad8f64b5"


# A digest the Worker tag really resolved to earlier in this project
# (execution milo-agent-worker-bw8kj) — proof the hazard is not theoretical.
STALE_WORKER_DIGEST = "sha256:2314852868a8abca211731960a178e7372997cc64674407d1c5119c86de9b265"


# The operating envelope, READ from the ONE canonical runtime policy exactly
# as the Stage D env generated it. This file used to carry its own
# transcription of all three groups, which made the whole suite a proof that
# one transcription matched another: it passed while the toolkit pinned
# MILO_PROVIDER_RPM_LIMIT=350 against MILO's organization ceiling of 80 --
# a posture `ProviderLimitsConfig.from_env` refuses, so no Worker could have
# started under it.
POLICY = reviewed_first_run_policy()



def _rendered(prefixes) -> str:
    return ",".join(f"{k}={v}" for k, v in POLICY.env_expectations(prefixes=prefixes).items())



CAPS = _rendered(CAP_ENV_PREFIXES)


PROVIDER_LIMITS = _rendered(PROVIDER_ENV_PREFIXES)


ENGINE_LIMITS = _rendered(ENGINE_ENV_PREFIXES)


POLICY_FINGERPRINT = POLICY.fingerprint()



def parse_pairs(raw: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in raw.split(","))



def env_entries(pairs: str) -> list[dict]:
    return [{"name": k, "value": v} for k, v in parse_pairs(pairs).items()]



def worker_spec(*, caps=CAPS, provider=PROVIDER_LIMITS, engine=ENGINE_LIMITS,
                image=None, bind_key=True, extra=None, release_sha=None):
    env = env_entries(caps) + env_entries(provider) + env_entries(engine) + [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "true"},
        {"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": "false"},
        # The release this deployment STATES it is serving. It is what
        # `backend/run_identity.py` binds onto every run the deployment
        # creates, so an absent value would make every run record no release
        # and the evidence gate would refuse the run as unbindable.
        {"name": "MILO_RELEASE_SHA", "value": release_sha or CHECKOUT_SHA},
    ]
    if bind_key:
        env.append({"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}})
    env.extend(extra or [])
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/worker:{release_sha or CHECKOUT_SHA}", "env": env}
    ]}}}}}}



def api_spec(*, caps=CAPS, image=None, extra=None, release_sha=None):
    env = env_entries(caps) + [
        {"name": "MILO_ENABLE_PAID_EXECUTION", "value": "false"},
        {"name": "MILO_ENABLE_RUN_CREATION", "value": "true"},
        {"name": "JOB_LAUNCHER", "value": "cloud_run"},
        {"name": "MILO_ENABLE_PROPOSAL_MUTATIONS", "value": "false"},
        {"name": "MILO_ENABLE_PROPOSAL_READS", "value": "false"},
        {"name": "MILO_ENABLE_RUN_CANCELLATION", "value": "false"},
        {"name": "MILO_ENABLE_EXECUTION_CONTROL", "value": "false"},
        {"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": "false"},
        {"name": "MILO_RELEASE_SHA", "value": release_sha or CHECKOUT_SHA},
    ]
    env.extend(extra or [])
    return {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{REGISTRY}/api:{release_sha or CHECKOUT_SHA}", "env": env}
    ]}}}}



_UNSET = object()



def run_verify_caps(tmp_path, worker, api, caps=CAPS, provider_limits=PROVIDER_LIMITS,
                    engine_limits=ENGINE_LIMITS, fingerprint=None, release_sha=_UNSET):
    worker_path = tmp_path / "worker.json"
    api_path = tmp_path / "api.json"
    worker_path.write_text(json.dumps(worker))
    api_path.write_text(json.dumps(api))
    return subprocess.run(
        [sys.executable, str(PINS / "verify_caps.py"),
         "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True, text=True,
        env={**os.environ, "STAGE_D_CAPS": caps, "STAGE_D_WORKER_PROVIDER_LIMITS": provider_limits,
             "STAGE_D_WORKER_ENGINE_LIMITS": engine_limits,
             "STAGE_D_POLICY_FINGERPRINT": fingerprint or POLICY_FINGERPRINT,
             "STAGE_D_REGISTRY": REGISTRY,
             "STAGE_D_RELEASE_SHA": (CHECKOUT_SHA if release_sha is _UNSET
                                     else release_sha),
             "STAGE_D_API_IMAGE_DIGEST": API_DIGEST, "STAGE_D_WORKER_IMAGE_DIGEST": WORKER_DIGEST},
        timeout=60,
    )



def test_verify_caps_refuses_a_deployment_that_states_no_release(tmp_path):
    """The run identity is the last link of the release chain, and it is bound
    from `MILO_RELEASE_SHA`. A deployment that states none would create runs
    recording no release, and an unpinned run cannot be bound to the accepted
    release -- so it is refused BEFORE the run is created rather than after it
    has been paid for."""
    for surface, worker, api in (
        ("worker", worker_spec(extra=[{"name": "MILO_RELEASE_SHA", "value": ""}]), api_spec()),
        ("api", worker_spec(), api_spec(extra=[{"name": "MILO_RELEASE_SHA", "value": ""}])),
    ):
        result = run_verify_caps(tmp_path, worker, api)
        assert result.returncode == 1, surface
        assert f"{surface}: MILO_RELEASE_SHA is MISSING" in result.stdout, surface



def test_verify_caps_refuses_a_deployment_pinned_to_another_release(tmp_path):
    other = "b" * 40
    result = run_verify_caps(
        tmp_path, worker_spec(extra=[{"name": "MILO_RELEASE_SHA", "value": other}]), api_spec())
    assert result.returncode == 1
    assert "is not the accepted release" in result.stdout



def test_verify_caps_passes_on_the_exact_authorized_posture(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec())
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"release {CHECKOUT_SHA[:12]}" in result.stdout
    assert "bound to the accepted release" in result.stdout



@pytest.mark.parametrize("surface,bad", [
    ("worker", {"image": f"{REGISTRY}/worker:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("worker", {"image": f"{REGISTRY}/worker:latest"}),
    ("worker", {"image": f"{REGISTRY}/worker@{STALE_WORKER_DIGEST}"}),
    ("api", {"image": f"{REGISTRY}/api:88224bccc836f80f3dc1d173306a1aa63cddcc7a"}),
    ("api", {"image": "us-central1-docker.pkg.dev/attacker/evil/api:" + CHECKOUT_SHA}),
    ("api", {"image": f"{REGISTRY}/api@sha256:" + "0" * 64}),
])
def test_verify_caps_refuses_a_wrong_release_image(tmp_path, surface, bad):
    """WRONG RELEASE IMAGE — wrong tag, wrong registry, or wrong digest."""
    worker = worker_spec(**bad) if surface == "worker" else worker_spec()
    api = api_spec(**bad) if surface == "api" else api_spec()
    result = run_verify_caps(tmp_path, worker, api)
    assert result.returncode != 0
    assert "is neither the accepted release digest" in result.stdout
    assert "do NOT create the run" in result.stdout



@pytest.mark.parametrize("surface", ["worker", "api"])
def test_verify_caps_accepts_the_accepted_digest_reference(tmp_path, surface):
    """Pinning the digest directly is the strongest form and must pass."""
    worker = worker_spec(image=f"{REGISTRY}/worker@{WORKER_DIGEST}") if surface == "worker" else worker_spec()
    api = api_spec(image=f"{REGISTRY}/api@{API_DIGEST}") if surface == "api" else api_spec()
    assert run_verify_caps(tmp_path, worker, api).returncode == 0



def test_verify_caps_fails_closed_without_the_accepted_digests(tmp_path):
    """The release identity is the digest; without it there is nothing to verify."""
    result = subprocess.run(
        [sys.executable, str(PINS / "verify_caps.py"),
         "--worker-json", str(tmp_path / "w.json"), "--api-json", str(tmp_path / "a.json")],
        capture_output=True, text=True,
        env={**os.environ, "STAGE_D_CAPS": CAPS, "STAGE_D_WORKER_PROVIDER_LIMITS": PROVIDER_LIMITS,
             "STAGE_D_WORKER_ENGINE_LIMITS": ENGINE_LIMITS,
             "STAGE_D_POLICY_FINGERPRINT": POLICY_FINGERPRINT,
             "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": CHECKOUT_SHA,
             "STAGE_D_API_IMAGE_DIGEST": "", "STAGE_D_WORKER_IMAGE_DIGEST": ""},
        timeout=60,
    )
    assert result.returncode != 0



@pytest.mark.parametrize("surface", ["worker", "api"])
@pytest.mark.parametrize("enabled", ["true", "TRUE", "1"])
def test_verify_caps_refuses_catalog_execution_enabled(tmp_path, surface, enabled):
    """CATALOG FLAG ENABLED — on either surface, in any spelling."""
    entry = [{"name": "MILO_ENABLE_CATALOG_EXECUTION", "value": enabled}]
    if surface == "worker":
        worker = worker_spec()
        worker["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e for e in worker["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] != "MILO_ENABLE_CATALOG_EXECUTION"
        ] + entry
        api = api_spec()
    else:
        worker = worker_spec()
        api = api_spec()
        api["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e for e in api["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] != "MILO_ENABLE_CATALOG_EXECUTION"
        ] + entry
    result = run_verify_caps(tmp_path, worker, api)
    assert result.returncode != 0
    assert "MILO_ENABLE_CATALOG_EXECUTION" in result.stdout
    assert "requires its own explicit authorization" in result.stdout



@pytest.mark.parametrize("entry", [
    {"name": "KIMI_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}},
    {"name": "MOONSHOT_API_KEY", "valueFrom": {"secretKeyRef": {"key": "latest", "name": "KIMI_API_KEY"}}},
    {"name": "KIMI_API_KEY", "value": "sk-not-a-real-key"},
    {"name": "MOONSHOT_API_KEY", "value": "sk-not-a-real-key"},
])
def test_verify_caps_refuses_a_provider_key_on_the_api(tmp_path, entry):
    """PROVIDER KEY ON API — binding or literal, either alias."""
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(extra=[entry]))
    assert result.returncode != 0
    assert "must NEVER" in result.stdout
    # The secret VALUE is never echoed back.
    assert "sk-not-a-real-key" not in result.stdout



def test_verify_caps_refuses_the_live_provider_concurrency_drift(tmp_path):
    """Production currently carries MILO_PROVIDER_MAX_CONCURRENCY=8."""
    drifted = PROVIDER_LIMITS.replace("MILO_PROVIDER_MAX_CONCURRENCY=2", "MILO_PROVIDER_MAX_CONCURRENCY=8")
    result = run_verify_caps(tmp_path, worker_spec(provider=drifted), api_spec())
    assert result.returncode != 0
    assert "MILO_PROVIDER_MAX_CONCURRENCY" in result.stdout
    assert "differs from pinned" in result.stdout



def test_verify_caps_refuses_any_provider_variable_on_the_api(tmp_path):
    api = api_spec(extra=[{"name": "MILO_PROVIDER_MAX_CONCURRENCY", "value": "2"}])
    result = run_verify_caps(tmp_path, worker_spec(), api)
    assert result.returncode != 0
    assert "must NEVER be set on the API service" in result.stdout



@pytest.mark.parametrize("cap,loosened", [
    # PR-R reviewed 3.00; anything above it is a loosening.
    ("MILO_MAX_COST_PER_RUN=3.00", "MILO_MAX_COST_PER_RUN=4.00"),
    ("MILO_MAX_MODEL_CALLS_PER_RUN=150", "MILO_MAX_MODEL_CALLS_PER_RUN=200"),
])
def test_verify_caps_refuses_a_loosened_cap_on_either_surface(tmp_path, cap, loosened):
    live = CAPS.replace(cap, loosened)
    assert run_verify_caps(tmp_path, worker_spec(caps=live), api_spec()).returncode != 0
    assert run_verify_caps(tmp_path, worker_spec(), api_spec(caps=live)).returncode != 0



def test_verify_caps_refuses_an_unexpected_extra_budget_variable(tmp_path):
    extra = [{"name": "MILO_MAX_SOMETHING_ELSE", "value": "999999"}]
    result = run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec())
    assert result.returncode != 0
    assert "unexpected budget/cap variable" in result.stdout



def test_verify_caps_fails_closed_without_expected_values(tmp_path):
    assert run_verify_caps(tmp_path, worker_spec(), api_spec(), caps="").returncode != 0
    assert run_verify_caps(tmp_path, worker_spec(), api_spec(), provider_limits="").returncode != 0



def test_verify_caps_requires_the_worker_secret_binding(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(bind_key=False), api_spec())
    assert result.returncode != 0
    assert "provider key is not bound" in result.stdout



def test_verify_caps_refuses_a_release_whose_policy_is_not_this_one(tmp_path):
    """CHECKOUT-POLICY DRIFT — the whole reason the binding exists.

    Generating the envelope from the local checkout and verifying against the
    same local checkout only ever proves the checkout agrees with itself. The
    run executes separately pinned release IMAGES, which may carry a different
    policy entirely, so Stage D refuses unless the policy here is byte-for-byte
    the policy at the accepted release.

    Run end to end against a purpose-built repository rather than a commit of
    this one: CI checks out at depth 1, so a test that names a real historical
    SHA asserts a message the environment cannot produce and proves nothing
    about the case it claims to cover.
    """
    root, release_sha = build_release_toolkit_repo(tmp_path, change_policy_after=True)
    result = run_verify_caps_in(root, tmp_path, release_sha)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "is not byte-for-byte the policy at the accepted release" in result.stdout



def test_verify_caps_refuses_a_release_that_predates_the_policy(tmp_path):
    """The currently pinned Stage D release is exactly this case."""
    root, release_sha = build_release_toolkit_repo(tmp_path, policy_at_release=False)
    result = run_verify_caps_in(root, tmp_path, release_sha)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "does not contain backend/runtime_policy.py" in result.stdout



def test_verify_caps_accepts_a_later_authorization_commit_end_to_end(tmp_path):
    """The re-authorization property, proven through verify_caps itself."""
    root, release_sha = build_release_toolkit_repo(tmp_path)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    assert head != release_sha
    result = run_verify_caps_in(root, tmp_path, release_sha)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "bound to the accepted release" in result.stdout



@pytest.mark.parametrize("bad_sha", ["", "not-a-sha", "84cd8696", "z" * 40])
def test_verify_caps_refuses_an_unprovable_release_sha(tmp_path, bad_sha):
    """Cannot PROVE the binding is a refusal, never a pass."""
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(), release_sha=bad_sha)
    assert result.returncode != 0
    assert "not a full 40-character commit SHA" in result.stdout



def test_verify_caps_refuses_a_release_commit_this_checkout_cannot_read(tmp_path):
    result = run_verify_caps(tmp_path, worker_spec(), api_spec(), release_sha="f" * 40)
    assert result.returncode != 0
    assert "is not a commit this checkout can read" in result.stdout



def test_the_pinned_policy_fingerprint_is_the_checkouts_policy():
    """The literal reviewed pin, kept honest by CI rather than by a run.

    Editing a reviewed value without re-pinning would otherwise be discovered
    by a production gate. It is discovered here instead.
    """
    sys.path.insert(0, str(PINS))
    import policy_envelope

    assert policy_envelope.PINNED_POLICY_FINGERPRINT == POLICY.fingerprint()
    assert policy_envelope.fingerprint_problems() == []



def test_a_drifted_checkout_policy_cannot_even_print_an_envelope(monkeypatch):
    """Every selector refuses, so a drifted checkout produces no pins at all."""
    sys.path.insert(0, str(PINS))
    import policy_envelope

    monkeypatch.setattr(policy_envelope, "PINNED_POLICY_FINGERPRINT", "0" * 64)
    assert policy_envelope.fingerprint_problems()
    for selector in ("caps", "provider-limits", "engine-limits", "fingerprint",
                     "execution-increment", "document", "binding"):
        assert policy_envelope.main(["policy_envelope.py", selector]) != 0, selector



def test_the_binding_refuses_when_it_cannot_read_the_checkout(monkeypatch):
    """Cannot prove is a refusal, never a pass: no git, no Stage D."""
    sys.path.insert(0, str(PINS))
    import policy_envelope

    monkeypatch.setattr(policy_envelope, "_git", lambda *a, **k: None)
    problems = policy_envelope.release_binding_problems(CHECKOUT_SHA)
    assert any("is not a commit this checkout can read" in problem
               for problem in problems)



def test_the_binding_refuses_a_policy_imported_from_outside_the_checkout(monkeypatch):
    """The bytes compared must be the bytes in use."""
    sys.path.insert(0, str(PINS))
    import policy_envelope

    monkeypatch.setattr(policy_envelope, "_imported_policy_source",
                        lambda: Path("/somewhere/else/runtime_policy.py"))
    problems = policy_envelope.release_binding_problems(CHECKOUT_SHA)
    assert any("imported from outside this checkout" in problem for problem in problems)



def test_the_binding_accepts_this_checkout_against_its_own_head():
    """A committed checkout verifying against its own HEAD is one accept path.

    This fails on a working tree with uncommitted changes to
    `backend/runtime_policy.py`, and that is the point: the envelope may only
    be generated from a policy that is identical to a released one. CI always
    runs against a clean checkout.
    """
    sys.path.insert(0, str(PINS))
    import policy_envelope

    assert policy_envelope.release_binding_problems(CHECKOUT_SHA) == []



def build_release_repo(tmp_path, *, change_policy_after=False):
    """A miniature repo with release R, then a LATER authorization commit.

    Deterministic and independent of this repository's own history: R carries
    the real policy source, and the commit after it edits a runbook — exactly
    the shape of a reviewed authorization commit that pins R.
    """
    root = tmp_path / "release-repo"
    (root / "backend").mkdir(parents=True)
    (root / "scripts" / "release" / "pins").mkdir(parents=True)

    def run(*args):
        subprocess.run(("git", "-C", str(root), *args), check=True,
                       capture_output=True, timeout=60)

    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    run("config", "user.email", "release@invalid")
    run("config", "user.name", "Release")
    policy = root / "backend" / "runtime_policy.py"
    policy.write_bytes((REPO / "backend" / "runtime_policy.py").read_bytes())
    (root / "scripts" / "release" / "pins" / "AUTHORIZATION.md").write_text("# v1\n")
    run("add", "-A")
    run("commit", "-qm", "release R")
    release_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()

    # The later, reviewed authorization commit. It references R; it is not R.
    (root / "scripts" / "release" / "pins" / "AUTHORIZATION.md").write_text(
        "# v2 — pins release R\n")
    if change_policy_after:
        policy.write_bytes(policy.read_bytes() + b"\n# an authorization commit changed the policy\n")
    run("add", "-A")
    run("commit", "-qm", "authorize release R")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, timeout=60).stdout.strip()
    assert head != release_sha
    return root, release_sha



def build_release_toolkit_repo(tmp_path, *, policy_at_release=True,
                               change_policy_after=False):
    """A miniature repo carrying BOTH the policy and the Stage D verifiers.

    `verify_caps.py` resolves its repository root from its own location, so a
    copy of the toolkit inside this repo imports THIS repo's policy and binds
    against THIS repo's history. That makes the end-to-end refusal paths
    testable without depending on how deeply the CI runner cloned us.
    """
    root = tmp_path / "release-toolkit"
    toolkit = root / "scripts" / "release" / "pins"
    (root / "backend").mkdir(parents=True)
    toolkit.mkdir(parents=True)

    def run(*args):
        subprocess.run(("git", "-C", str(root), *args), check=True,
                       capture_output=True, timeout=60)

    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    run("config", "user.email", "release@invalid")
    run("config", "user.name", "Release")
    for name in ("policy_envelope.py", "verify_caps.py"):
        (toolkit / name).write_bytes((PINS / name).read_bytes())
    (root / "backend" / "__init__.py").write_bytes(
        (REPO / "backend" / "__init__.py").read_bytes())
    policy = root / "backend" / "runtime_policy.py"
    released_policy = (REPO / "backend" / "runtime_policy.py").read_bytes()
    if policy_at_release:
        policy.write_bytes(released_policy)
    (toolkit / "AUTHORIZATION.md").write_text("# v1\n")
    run("add", "-A")
    run("commit", "-qm", "release R")
    release_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()

    # The later, reviewed authorization commit. It references R; it is not R.
    (toolkit / "AUTHORIZATION.md").write_text("# v2 — pins release R\n")
    if not policy_at_release:
        # The policy only appears AFTER the release, so R cannot carry it.
        policy.write_bytes(released_policy)
    if change_policy_after:
        # A comment-only change: the DOCUMENT is identical, so the fingerprint
        # pin still matches and only the byte comparison can refuse. That
        # isolates the message this test is about.
        policy.write_bytes(released_policy + b"\n# an authorization commit touched the policy\n")
    run("add", "-A")
    run("commit", "-qm", "authorize release R")
    return root, release_sha



def run_verify_caps_in(root, tmp_path, release_sha, *, caps=CAPS,
                       provider_limits=PROVIDER_LIMITS, engine_limits=ENGINE_LIMITS):
    """Run the COPY of verify_caps.py that lives inside `root`."""
    worker_path = tmp_path / "sandbox-worker.json"
    api_path = tmp_path / "sandbox-api.json"
    worker_path.write_text(json.dumps(worker_spec(release_sha=release_sha)))
    api_path.write_text(json.dumps(api_spec(release_sha=release_sha)))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update({
        "STAGE_D_CAPS": caps, "STAGE_D_WORKER_PROVIDER_LIMITS": provider_limits,
        "STAGE_D_WORKER_ENGINE_LIMITS": engine_limits,
        "STAGE_D_POLICY_FINGERPRINT": POLICY_FINGERPRINT,
        "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": release_sha,
        "STAGE_D_API_IMAGE_DIGEST": API_DIGEST,
        "STAGE_D_WORKER_IMAGE_DIGEST": WORKER_DIGEST,
    })
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "release" / "pins" / "verify_caps.py"),
         "--worker-json", str(worker_path), "--api-json", str(api_path)],
        capture_output=True, text=True, env=env, cwd=str(root), timeout=120)



def test_a_later_authorization_commit_may_reference_an_earlier_release(tmp_path):
    """THE re-authorization property.

    Requiring HEAD == STAGE_D_RELEASE_SHA made the binding self-referential:
    the reviewed commit that updates the pin to release R cannot itself be R,
    so no authorization commit could ever satisfy its own pin. What has to
    hold is that the POLICY is the released one, and it does here.
    """
    sys.path.insert(0, str(PINS))
    import policy_envelope

    root, release_sha = build_release_repo(tmp_path)
    assert policy_envelope.release_binding_problems(release_sha, repo_root=root) == []



def test_an_authorization_commit_that_changes_the_policy_is_refused(tmp_path):
    """Changing the policy needs a new release, not a new authorization."""
    sys.path.insert(0, str(PINS))
    import policy_envelope

    root, release_sha = build_release_repo(tmp_path, change_policy_after=True)
    problems = policy_envelope.release_binding_problems(release_sha, repo_root=root)
    assert any("is not byte-for-byte the policy at the accepted release" in problem
               for problem in problems)
    assert any("requires a new reviewed release" in problem for problem in problems)



def test_an_earlier_real_commit_with_the_same_policy_is_accepted():
    """The same property over this repository's OWN history.

    The sandbox test proves the rule; this proves it holds for a real
    ancestor, which is what an authorization commit pinning the previous
    release actually looks like. Skipped only when the policy was changed in
    HEAD itself, in which case no ancestor can carry identical bytes.
    """
    sys.path.insert(0, str(PINS))
    import policy_envelope

    current = (REPO / "backend" / "runtime_policy.py").read_bytes()
    ancestors = subprocess.run(
        ["git", "-C", str(REPO), "rev-list", "--max-count=25", "HEAD~1"],
        capture_output=True, text=True, timeout=60).stdout.split()
    match = next(
        (sha for sha in ancestors
         if subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:backend/runtime_policy.py"],
                           capture_output=True, timeout=60).stdout == current),
        None)
    if match is None:
        pytest.skip(
            "no readable ancestor carries this policy — either HEAD changed it, "
            "or this is a shallow clone (CI checks out at depth 1). The same "
            "property is proven without history by "
            "test_a_later_authorization_commit_may_reference_an_earlier_release")
    assert match != CHECKOUT_SHA
    assert policy_envelope.release_binding_problems(match) == []



def test_verify_caps_tolerates_unrelated_live_worker_variables(tmp_path):
    """Model selection is not part of the envelope, so it is not verified."""
    extra = [
        {"name": "MILO_COMMANDER_MODEL", "value": "kimi-k2.6"},
        {"name": "MILO_MODEL_BASE_URL", "value": "https://api.moonshot.ai/v1"},
    ]
    assert run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec()).returncode == 0



def test_verify_caps_refuses_the_swarm_width_that_used_to_be_unverified(tmp_path):
    """MILO_SWARM_MAX_ACTIVE_WORKERS=8 was tolerated as "unrelated".

    It is not unrelated: it is the Swarm V2 queueing width, the canonical
    policy reviews it at 2, and a paid Worker carrying 8 would have run at
    four times the reviewed width with every Stage D check passing.
    """
    extra = [{"name": "MILO_SWARM_MAX_ACTIVE_WORKERS", "value": "8"}]
    result = run_verify_caps(tmp_path, worker_spec(extra=extra), api_spec())
    assert result.returncode != 0
    assert "MILO_SWARM_MAX_ACTIVE_WORKERS" in result.stdout



def test_verify_caps_refuses_an_engine_variable_on_the_api(tmp_path):
    """Engine parallelism belongs to the Worker alone, like provider limits."""
    api = api_spec()
    api["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "MILO_SWARM_MAX_ACTIVE_WORKERS", "value": "2"})
    result = run_verify_caps(tmp_path, worker_spec(), api)
    assert result.returncode != 0
    assert "must NEVER be set on the API service" in result.stdout



API_REPO = f"{REGISTRY}/api"


WORKER_REPO = f"{REGISTRY}/worker"


READY_REVISION = "milo-agent-api-00080-nm8"



def registry_listing(api_digest=API_DIGEST, worker_digest=WORKER_DIGEST, tag=RELEASE_SHA, extra=None):
    listing = [
        {"package": API_REPO, "version": api_digest, "tags": tag},
        {"package": WORKER_REPO, "version": worker_digest, "tags": tag},
    ]
    listing.extend(extra or [])
    return listing



def api_service_doc(ready=READY_REVISION, percent=100):
    return {"status": {"latestReadyRevisionName": ready,
                       "traffic": [{"revisionName": ready, "percent": percent}]}}



def api_revision_doc(image=None, name=READY_REVISION):
    return {"metadata": {"name": name},
            "spec": {"containers": [{"image": image or f"{API_REPO}@{API_DIGEST}"}]}}



def worker_job_doc(image=None):
    return {"spec": {"template": {"spec": {"template": {"spec": {"containers": [
        {"image": image or f"{WORKER_REPO}:{RELEASE_SHA}"}]}}}}}}



def execution_doc(image=None, name="milo-agent-worker-staged1"):
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [
                {"image": image or f"{WORKER_REPO}@{WORKER_DIGEST}"}]}}}}



def run_verify_images(tmp_path, *, registry=None, service=None, revision=None, job=None,
                      execution=None, api_digest=API_DIGEST, worker_digest=WORKER_DIGEST):
    paths = {}
    for label, doc in (("registry", registry if registry is not None else registry_listing()),
                       ("service", service if service is not None else api_service_doc()),
                       ("revision", revision if revision is not None else api_revision_doc()),
                       ("job", job if job is not None else worker_job_doc())):
        path = tmp_path / f"{label}.json"
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        paths[label] = str(path)
    argv = [sys.executable, str(PINS / "verify_images.py"),
            "--registry-json", paths["registry"], "--api-service-json", paths["service"],
            "--api-revision-json", paths["revision"], "--worker-job-json", paths["job"]]
    if execution is not None:
        exec_path = tmp_path / "execution.json"
        exec_path.write_text(json.dumps(execution))
        argv += ["--execution-json", str(exec_path)]
    return subprocess.run(
        argv, capture_output=True, text=True,
        env={**os.environ, "STAGE_D_REGISTRY": REGISTRY, "STAGE_D_RELEASE_SHA": RELEASE_SHA,
             "STAGE_D_API_IMAGE_DIGEST": api_digest, "STAGE_D_WORKER_IMAGE_DIGEST": worker_digest},
        timeout=60)



def test_verify_images_passes_on_the_accepted_digests(tmp_path):
    result = run_verify_images(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is True
    assert verdict["registry_resolution"] == {"api": API_DIGEST, "worker": WORKER_DIGEST}
    assert verdict["api_serving_revision"]["digest"] == API_DIGEST
    assert verdict["worker_job"]["digest"] == WORKER_DIGEST



def test_verify_images_refuses_a_moved_registry_tag(tmp_path):
    """The exact hazard: same tag, different bytes."""
    result = run_verify_images(
        tmp_path, registry=registry_listing(worker_digest=STALE_WORKER_DIGEST))
    assert result.returncode != 0
    out = result.stdout
    assert "the worker tag" in out and "was re-pushed" in out
    assert "separate reviewed release" in out



def test_verify_images_refuses_a_moved_api_tag(tmp_path):
    result = run_verify_images(tmp_path, registry=registry_listing(api_digest="sha256:" + "a" * 64))
    assert result.returncode != 0
    assert "the api tag" in result.stdout



def test_a_tag_match_alone_is_never_acceptance(tmp_path):
    """Everything still carries the right TAG; only the digest moved."""
    moved = registry_listing(worker_digest=STALE_WORKER_DIGEST)
    result = run_verify_images(tmp_path, registry=moved, job=worker_job_doc(f"{WORKER_REPO}:{RELEASE_SHA}"))
    assert result.returncode != 0
    verdict = json.loads(result.stdout)
    assert verdict["ok"] is False
    assert any("re-pushed" in problem or "was moved" in problem for problem in verdict["problems"])



def test_verify_images_refuses_a_missing_release_tag(tmp_path):
    result = run_verify_images(tmp_path, registry=[])
    assert result.returncode != 0
    assert "no image in" in result.stdout



def test_verify_images_refuses_an_ambiguous_registry_listing(tmp_path):
    duplicate = registry_listing() + [{"package": WORKER_REPO, "version": STALE_WORKER_DIGEST, "tags": RELEASE_SHA}]
    result = run_verify_images(tmp_path, registry=duplicate)
    assert result.returncode != 0
    assert "the tag is ambiguous" in result.stdout



def test_verify_images_refuses_a_serving_revision_on_the_wrong_digest(tmp_path):
    result = run_verify_images(tmp_path, revision=api_revision_doc(f"{API_REPO}@sha256:" + "b" * 64))
    assert result.returncode != 0
    assert "production is NOT serving the accepted image" in result.stdout



def test_verify_images_refuses_when_the_serving_revision_is_not_taking_all_traffic(tmp_path):
    result = run_verify_images(tmp_path, service=api_service_doc(percent=50))
    assert result.returncode != 0
    assert "not serving 100% of traffic" in result.stdout



def test_verify_images_refuses_when_the_wrong_revision_was_inspected(tmp_path):
    result = run_verify_images(tmp_path, revision=api_revision_doc(name="milo-agent-api-00001-aaa"))
    assert result.returncode != 0
    assert "is not the serving revision" in result.stdout



def test_verify_images_refuses_a_worker_job_on_a_foreign_digest(tmp_path):
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}@{STALE_WORKER_DIGEST}"))
    assert result.returncode != 0
    assert "production is NOT serving the accepted image" in result.stdout



def test_verify_images_refuses_a_worker_job_on_an_unpinned_tag(tmp_path):
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}:latest"))
    assert result.returncode != 0
    assert "is not the pinned release" in result.stdout



def test_verify_images_refuses_an_image_from_another_repository(tmp_path):
    result = run_verify_images(
        tmp_path, job=worker_job_doc("us-central1-docker.pkg.dev/attacker/evil/worker:" + RELEASE_SHA))
    assert result.returncode != 0
    assert "not from the pinned repository" in result.stdout



def test_verify_images_says_plainly_when_the_worker_reference_is_a_mutable_tag(tmp_path):
    """A Cloud Run job is not a revision: it resolves the tag every run."""
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}:{RELEASE_SHA}"))
    assert result.returncode == 0
    verdict = json.loads(result.stdout)
    assert verdict["worker_job"]["reference_kind"] == "tag"
    assert any("point-in-time" in note for note in verdict["notes"])



def test_verify_images_reports_a_digest_pinned_worker_as_the_stronger_form(tmp_path):
    result = run_verify_images(tmp_path, job=worker_job_doc(f"{WORKER_REPO}@{WORKER_DIGEST}"))
    assert result.returncode == 0
    assert json.loads(result.stdout)["worker_job"]["reference_kind"] == "digest"



def test_verify_images_fails_closed_without_the_pinned_digests(tmp_path):
    assert run_verify_images(tmp_path, api_digest="").returncode != 0
    assert run_verify_images(tmp_path, worker_digest="").returncode != 0
    assert run_verify_images(tmp_path, worker_digest="not-a-digest").returncode != 0



@pytest.mark.parametrize("field", ["registry", "service", "revision", "job"])
def test_verify_images_fails_closed_on_unreadable_input(tmp_path, field):
    result = run_verify_images(tmp_path, **{field: "{not json"})
    assert result.returncode != 0
    assert "failing closed" in result.stdout



def test_verify_images_proves_what_the_authorized_execution_actually_ran(tmp_path):
    result = run_verify_images(tmp_path, execution=execution_doc())
    assert result.returncode == 0
    assert json.loads(result.stdout)["worker_execution"]["digest"] == WORKER_DIGEST



def test_verify_images_refuses_an_execution_that_ran_a_different_digest(tmp_path):
    """The check a moved tag cannot defeat, because the execution records it."""
    result = run_verify_images(tmp_path, execution=execution_doc(f"{WORKER_REPO}@{STALE_WORKER_DIGEST}"))
    assert result.returncode != 0
    assert "worker execution" in result.stdout
    assert "production is NOT serving the accepted image" in result.stdout



def test_verify_images_refuses_an_execution_recorded_only_as_a_tag(tmp_path):
    """Without a recorded digest, what it ran cannot be established."""
    result = run_verify_images(tmp_path, execution=execution_doc(f"{WORKER_REPO}:{RELEASE_SHA}"))
    assert result.returncode != 0
    assert "cannot be established" in result.stdout
