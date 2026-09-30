"""PR-D3: manufacturer name normalisation, offline (the in-memory mirror).

Every model answer here is RECORDED / FAKE: no provider is constructed, no
socket is opened. Proves: the deterministic rules; the strict output contract
(valid, not JSON, wrong shape, invented name, duplicate member, low
confidence); the capture job's ONE call records a proposal or a static
refusal and never runs without its own switch; the API's view, request and
owner approval (high-confidence groups together, a low-confidence or
conflicting group alone, owner only, a stale version refused); the canonical
names beside the exact tozar on the Register page and the catalog browser,
whose reads and filters keep the source value.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from backend import capture_invocation as ci
from backend.budget import BudgetExceeded
from backend.catalog import operator_capture as entrypoint
from backend.catalog.register import normalization as norm
from backend.catalog.register import service as register_service
from backend.engines.swarm_v2 import model_gateway
from backend.testing.memory_repository import MemoryRepository
from tests.test_register_capture import (LEXUS, TOYOTA, USER, FakeTrigger, api_env, as_user,  # noqa: F401
                                         claimed_lease, directory, no_sockets, world)
from tests.test_register_capture import client as register_client


def client(repo, trigger):
    """The Register page's client; the normalisation routes execute their own job."""
    from backend.dependencies import get_normalisation_trigger
    from backend.main import app

    api = register_client(repo, FakeTrigger())
    app.dependency_overrides[get_normalisation_trigger] = lambda: trigger
    return api

HONDA, HONDA_SPACED, HONDA_JP = "הונדה", "הונדה ", "הונדה יפן"
MERCEDES, MERCEDES_DASH = "מרצדס בנץ", "מרצדס-בנץ"


@pytest.fixture(autouse=True)
def normalisation_env(monkeypatch):
    monkeypatch.setenv(norm.FLAG, "true")


def names() -> dict[str, int]:
    return {TOYOTA: 28, LEXUS: 50, MERCEDES: 10, MERCEDES_DASH: 3, HONDA: 7, HONDA_JP: 2}


# -- 1. the deterministic rules --------------------------------------------------------

def test_r1_joins_names_equal_up_to_case_space_and_punctuation():
    assert norm.spelling_key(MERCEDES) == norm.spelling_key(MERCEDES_DASH) == norm.spelling_key(" מרצדס  בנץ.")
    assert norm.spelling_key("Mercedes-Benz") == norm.spelling_key("MERCEDES BENZ")
    assert norm.spelling_key(TOYOTA) != norm.spelling_key(LEXUS)
    (group,) = norm.deterministic_groups(names(), {})
    assert group == {"canonical": MERCEDES, "members": sorted([MERCEDES, MERCEDES_DASH], key=str.encode),
                     "confidence": "high", "rule_id": "R1_SPELLING", "reason": norm.RULES["R1_SPELLING"]}


def test_r2_joins_names_with_the_same_register_codes_and_mapped_names_are_left_alone():
    evidence = {HONDA: {"tozeret_cd": [430, 431]}, HONDA_JP: {"tozeret_cd": [431, 430]},
                TOYOTA: {"tozeret_cd": [413]}, LEXUS: {"tozeret_cd": [413, 999]}}
    groups = {g["rule_id"]: g for g in norm.deterministic_groups(names(), evidence)}
    assert groups["R2_TOZERET_CD"]["members"] == sorted([HONDA, HONDA_JP], key=str.encode)
    assert groups["R2_TOZERET_CD"]["canonical"] == HONDA          # the most rows
    # One maker's codes may carry two brands: an R2 group is approved alone.
    assert groups["R2_TOZERET_CD"]["confidence"] == "low"
    # Toyota and Lexus share one code but not the SAME set: not joined.
    assert all(TOYOTA not in g["members"] for g in groups.values())
    # A mapped name is never proposed again.
    assert [g["rule_id"] for g in norm.deterministic_groups(names(), evidence, mapped=[HONDA_JP])] == ["R1_SPELLING"]


# -- 2. the output contract (recorded answers) -------------------------------------------

INPUT = [MERCEDES, MERCEDES_DASH, TOYOTA, LEXUS]
VALID = {"groups": [{"canonical": "Mercedes-Benz", "members": [MERCEDES, MERCEDES_DASH], "confidence": "high",
                     "reason": "the same marque"},
                    {"canonical": "Toyota", "members": [TOYOTA], "confidence": "low", "reason": "one name"}]}


@pytest.mark.parametrize("answer, code", [
    ("not json", "NORMALIZATION_OUTPUT_NOT_JSON"),
    ("[1, 2]", "NORMALIZATION_OUTPUT_NOT_JSON"),
    (json.dumps({"groups": [], "notes": "x"}), "NORMALIZATION_OUTPUT_SHAPE_INVALID"),
    (json.dumps({"groups": [{"canonical": "X", "members": [TOYOTA], "confidence": "certain", "reason": ""}]}),
     "NORMALIZATION_OUTPUT_SHAPE_INVALID"),
    (json.dumps({"groups": [{"canonical": " X", "members": [TOYOTA], "confidence": "high", "reason": ""}]}),
     "NORMALIZATION_OUTPUT_SHAPE_INVALID"),
    (json.dumps({"groups": [{"canonical": "Toyota", "members": ["טויוטה יפן"], "confidence": "high",
                             "reason": ""}]}), "NORMALIZATION_MEMBER_INVENTED"),
    (json.dumps({"groups": [{"canonical": "A", "members": [TOYOTA], "confidence": "high", "reason": ""},
                            {"canonical": "B", "members": [TOYOTA, LEXUS], "confidence": "low", "reason": ""}]}),
     "NORMALIZATION_MEMBER_DUPLICATED"),
    (json.dumps({"groups": [{"canonical": "A", "members": [LEXUS, LEXUS], "confidence": "high", "reason": ""}]}),
     "NORMALIZATION_MEMBER_DUPLICATED"),
    # Text the database's JSON cannot store: refused, never a failed write after the call.
    (json.dumps({"groups": [{"canonical": "X\u0000", "members": [TOYOTA], "confidence": "high", "reason": ""}]}),
     "NORMALIZATION_OUTPUT_SHAPE_INVALID"),
    ('{"groups": [{"canonical": "X", "members": ["%s"], "confidence": "high", "reason": "\\ud800"}]}' % TOYOTA,
     "NORMALIZATION_OUTPUT_SHAPE_INVALID"),
])
def test_the_contract_refuses_anything_but_groups_of_input_names(answer, code):
    with pytest.raises(norm.NormalizationRefused) as refused:
        norm.validate_groups(answer, INPUT)
    assert refused.value.code == code


def test_a_valid_answer_with_low_confidence_is_accepted_as_it_is():
    assert norm.validate_groups(json.dumps(VALID), INPUT) == VALID["groups"]


# -- 3. the capture job's ONE call -------------------------------------------------------

class RecordedGateway:
    """A recorded K3 answer behind the gateway's call signature."""

    def __init__(self, content: str | None = None, *, failure: BaseException | None = None) -> None:
        self.content, self.failure, self.calls = content, failure, []

    def call(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        message = SimpleNamespace(content=self.content, reasoning_content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


def world_with_proposal(repo: MemoryRepository | None = None) -> tuple[MemoryRepository, dict, Any, str]:
    repo, w = world(repo)
    directory(repo, names())
    trigger = FakeTrigger()
    answer = register_service.request_normalization(repo, USER, UUID(w["project"]),
                                                    conversation_id=UUID(w["conversation"]), trigger=trigger)
    assert answer["started"] is True
    (invocation,) = trigger.calls
    lease = claimed_lease(repo, answer["run_id"])
    return repo, w, lease, answer["proposal_id"]


def test_the_api_starts_one_capture_job_execution_with_its_own_switch():
    repo, w = world()
    directory(repo, names())
    trigger = FakeTrigger()
    answer = register_service.request_normalization(repo, USER, UUID(w["project"]),
                                                    conversation_id=UUID(w["conversation"]), trigger=trigger)
    (invocation,) = trigger.calls
    assert invocation.env_overrides == ((ci.NORMALISATION_SWITCH, "true"),)
    assert invocation.entrypoint_args[-2:] == ("--normalisation-proposal-id", answer["proposal_id"])
    proposal = repo.manufacturer_normalization_proposal(answer["proposal_id"])
    assert [row["name"] for row in proposal["input"]] == sorted(names(), key=str.encode)
    assert all(set(row) == {"name", "rows", "tozeret_nm", "samples"} for row in proposal["input"])
    # One live at a time: a second press starts nothing.
    again = register_service.request_normalization(repo, USER, UUID(w["project"]),
                                                   conversation_id=UUID(w["conversation"]), trigger=trigger)
    assert again == {"started": False, "proposal_id": answer["proposal_id"]} and len(trigger.calls) == 1


def test_a_valid_recorded_answer_is_proposed_through_the_role_policy(monkeypatch):
    repo, _w, lease, proposal_id = world_with_proposal()
    gateway = RecordedGateway(json.dumps({"groups": [VALID["groups"][0]]}))
    outcome = norm.propose(repo, lease, proposal_id, env={norm.JOB_SWITCH: "true"},
                           gateway_factory=lambda *_a: gateway)
    assert outcome == {"status": "proposed", "reason_code": None, "groups": 1}
    (call,) = gateway.calls
    assert (call["model"], call["agent"], call["phase"]) == ("kimi-k3", "normaliser", "manufacturers")
    assert "tools" not in call and model_gateway.role_policy("normaliser", "manufacturers").max_output == 16_000
    assert repo.manufacturer_normalization_proposal(proposal_id)["groups"] == [VALID["groups"][0]]


@pytest.mark.parametrize("gateway, code", [
    (RecordedGateway("```json\n{}\n```"), "NORMALIZATION_OUTPUT_NOT_JSON"),
    (RecordedGateway(json.dumps({"groups": [{"canonical": "Honda", "members": ["הונדה מוטורס"],
                                             "confidence": "high", "reason": "x"}]})),
     "NORMALIZATION_MEMBER_INVENTED"),
    (RecordedGateway(failure=BudgetExceeded("BUDGET_EXCEEDED", "the run cap", "budget_exceeded",
                                            "budget_exhausted")), "NORMALIZATION_BUDGET_REFUSED"),
    (RecordedGateway(failure=RuntimeError("provider down")), "NORMALIZATION_MODEL_FAILED"),
])
def test_anything_else_is_recorded_as_a_static_refusal(gateway, code):
    repo, _w, lease, proposal_id = world_with_proposal()
    outcome = norm.propose(repo, lease, proposal_id, env={norm.JOB_SWITCH: "true"},
                           gateway_factory=lambda *_a: gateway)
    assert outcome == {"status": "refused", "reason_code": code, "groups": 0}
    stored = repo.manufacturer_normalization_proposal(proposal_id)
    assert stored["status"] == "refused" and stored["groups"] is None


def test_no_call_without_the_executions_own_switch_or_for_another_run():
    repo, _w, lease, proposal_id = world_with_proposal()
    gateway = RecordedGateway(json.dumps(VALID))
    for env, pid in (({}, proposal_id), ({norm.JOB_SWITCH: "true"}, "0" * 8 + "-0000-4000-8000-" + "0" * 12)):
        with pytest.raises(norm.NormalizationRefused):
            norm.propose(repo, lease, pid, env=env, gateway_factory=lambda *_a: gateway)
    assert gateway.calls == []
    # The entrypoint refuses the mode without the switch, or beside another mode.
    args = entrypoint.build_parser().parse_args(
        ["--execute", *ci.manufacturer_normalisation(project_ref="abc", run_id=lease.run_id,
                                                     proposal_id=proposal_id).entrypoint_args[1:]])
    from tests.test_catalog_operator_capture import capture_env

    ready = capture_env()
    args = entrypoint.build_parser().parse_args(
        ["--execute", *ci.manufacturer_normalisation(project_ref=entrypoint.configured_project_ref(ready),
                                                     run_id=lease.run_id, proposal_id=proposal_id).entrypoint_args[1:]])
    assert entrypoint._refusal(args, [], ready) == "CAPTURE_NORMALISATION_DISABLED"
    assert entrypoint._refusal(args, [], {**ready, norm.JOB_SWITCH: "true"}) == ""
    assert entrypoint._refusal(args, [], {**ready, norm.JOB_SWITCH: "true",
                                          "MILO_ENABLE_PAID_EXECUTION": "true"}) != ""


# -- 4. the owner's approval, and the canonical names on the pages -----------------------

def proposed(repo: MemoryRepository, lease: Any, proposal_id: str, groups: list[dict]) -> None:
    norm.propose(repo, lease, proposal_id, env={norm.JOB_SWITCH: "true"},
                 gateway_factory=lambda *_a: RecordedGateway(json.dumps({"groups": groups})))


def test_high_confidence_groups_together_and_any_other_alone():
    repo, w, lease, proposal_id = world_with_proposal()
    proposed(repo, lease, proposal_id, [
        {"canonical": "Toyota", "members": [TOYOTA], "confidence": "high", "reason": "x"},
        {"canonical": "Lexus", "members": [LEXUS], "confidence": "low", "reason": "y"},
        # Conflicts with the R1 group of the two Mercedes spellings.
        {"canonical": "Mercedes", "members": [MERCEDES], "confidence": "high", "reason": "z"}])
    api = client(repo, FakeTrigger())
    view = api.get(f"/projects/{w['project']}/register/normalisation", headers=as_user()).json()
    pending = {(g["canonical"], g.get("rule_id")): g for g in view["pending"]}
    assert view["version"] == 0 and view["can_approve"] is True and view["unmapped"] == len(names())
    assert pending[("Toyota", None)]["bulk_approvable"] is True
    assert pending[("Lexus", None)]["bulk_approvable"] is False
    assert pending[("Mercedes", None)]["conflicting"] is True
    assert pending[(MERCEDES, "R1_SPELLING")]["conflicting"] is True

    def approve(groups: list[dict], version: int = 0):
        body = {"expected_version": version, "groups": [
            {k: g[k] for k in ("canonical", "members", "rule_id", "proposal_id") if g.get(k)} for g in groups]}
        return api.post(f"/projects/{w['project']}/register/normalisation/approvals", headers=as_user(), json=body)

    # A low-confidence group may not ride with others; a conflicting one neither.
    assert approve([pending[("Toyota", None)], pending[("Lexus", None)]]).status_code == 422
    assert approve([pending[("Toyota", None)], pending[("Mercedes", None)]]).status_code == 422
    # Something the server did not propose is refused.
    assert approve([dict(pending[("Toyota", None)], canonical="Toyota Motor")]).status_code == 422
    assert approve([pending[("Toyota", None)]]).json() == {"version": 1, "entry_count": 1}
    # An approved group is no longer pending: it cannot be approved into a new version again.
    again = api.get(f"/projects/{w['project']}/register/normalisation", headers=as_user()).json()["pending"]
    assert ("Toyota", None) not in {(g["canonical"], g.get("rule_id")) for g in again}
    assert approve([pending[("Toyota", None)]], version=1).status_code == 422
    assert approve([pending[("Lexus", None)]]).status_code == 409          # stale version
    assert approve([pending[("Lexus", None)]], version=1).json()["version"] == 2
    assert approve([pending[(MERCEDES, "R1_SPELLING")]], version=2).json() == {"version": 3, "entry_count": 4}
    current = repo.manufacturer_normalization_current()
    provenance = {e["source_tozar"]: (e["provenance"], e["rule_id"], e["confidence"]) for e in current["entries"]}
    assert provenance[TOYOTA] == ("model", None, "high") and provenance[LEXUS] == ("model", None, "low")
    assert provenance[MERCEDES_DASH] == ("rule", "R1_SPELLING", None)
    # The Register page: the canonical name beside the EXACT tozar.
    page = api.get(f"/projects/{w['project']}/register", headers=as_user()).json()
    units = {u["tozar"]: u for u in page["units"]}
    assert units[TOYOTA]["canonical_manufacturer"] == "Toyota" and units[HONDA]["canonical_manufacturer"] is None


def test_the_owner_rejects_a_pending_group_and_it_is_no_longer_pending():
    repo, w, lease, proposal_id = world_with_proposal()
    proposed(repo, lease, proposal_id, [
        {"canonical": "Toyota", "members": [TOYOTA], "confidence": "high", "reason": "x"},
        {"canonical": "Lexus", "members": [LEXUS], "confidence": "low", "reason": "y"}])
    api = client(repo, FakeTrigger())
    url = f"/projects/{w['project']}/register/normalisation"
    pending = {g["canonical"]: g for g in api.get(url, headers=as_user()).json()["pending"]}

    def reject(group: dict):
        body = {"group": {k: group[k] for k in ("canonical", "members", "rule_id", "proposal_id") if group.get(k)}}
        return api.post(f"{url}/rejections", headers=as_user(), json=body)

    # Only a pending group, exactly as proposed.
    refused = reject(dict(pending["Toyota"], canonical="Toyota Motor"))
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "CATALOG_NORMALIZATION_REJECTION_INVALID"
    assert reject(pending["Toyota"]).json() == {"rejected": True}
    after = {g["canonical"] for g in api.get(url, headers=as_user()).json()["pending"]}
    assert "Toyota" not in after and "Lexus" in after
    # A rejected group is not approved either, and nothing was mapped.
    body = {"expected_version": 0, "groups": [{"canonical": "Toyota", "members": [TOYOTA], "proposal_id": proposal_id}]}
    assert api.post(f"{url}/approvals", headers=as_user(), json=body).status_code == 422
    assert repo.manufacturer_normalization_current()["version"] == 0
    # A single HIGH-confidence group may be approved alone too.
    assert api.post(f"{url}/approvals", headers=as_user(), json={"expected_version": 0, "groups": [
        {"canonical": "Lexus", "members": [LEXUS], "proposal_id": proposal_id}]}).json()["version"] == 1
    # Only an owner rejects.
    repo._norm_state()["roles"][(w["project"], str(USER))] = "member"
    denied = reject(pending["Lexus"])
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "CATALOG_NORMALIZATION_OWNER_ONLY"


def test_only_an_owner_approves_and_the_button_needs_its_flag(monkeypatch):
    repo, w, lease, proposal_id = world_with_proposal()
    proposed(repo, lease, proposal_id, [{"canonical": "Toyota", "members": [TOYOTA], "confidence": "high",
                                         "reason": "x"}])
    repo._norm_state()["roles"][(w["project"], str(USER))] = "member"
    api = client(repo, FakeTrigger())
    view = api.get(f"/projects/{w['project']}/register/normalisation", headers=as_user()).json()
    assert view["can_approve"] is False
    body = {"expected_version": 0, "groups": [{"canonical": "Toyota", "members": [TOYOTA],
                                               "proposal_id": proposal_id}]}
    refused = api.post(f"/projects/{w['project']}/register/normalisation/approvals", headers=as_user(), json=body)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "CATALOG_NORMALIZATION_OWNER_ONLY"
    # The paid call spends the owner's daily budget: only an owner starts it.
    assert view["can_normalise"] is False
    pressed = api.post(f"/projects/{w['project']}/register/normalisation", headers=as_user(),
                       json={"conversation_id": w["conversation"]})
    assert pressed.status_code == 403 and pressed.json()["error"]["code"] == "CATALOG_NORMALIZATION_OWNER_ONLY"
    monkeypatch.setenv(norm.FLAG, "false")
    closed = api.post(f"/projects/{w['project']}/register/normalisation", headers=as_user(),
                      json={"conversation_id": w["conversation"]})
    assert closed.status_code == 403
    assert api.get(f"/projects/{w['project']}/register/normalisation",
                   headers=as_user()).json()["can_normalise"] is False


def test_the_browser_shows_the_canonical_name_and_filters_keep_the_tozar(monkeypatch):
    from backend.catalog.register import browser
    from tests.test_catalog_variants import fixture_rows
    from tests.test_register_capture import captured_world

    monkeypatch.setenv(browser.BROWSER_FLAG, "true")
    repo, w, _version, _report, _writer = captured_world(fixture_rows())
    repo._norm_state()["versions"].append({"version": 1, "approved_by": str(USER), "entries": {
        TOYOTA: {"source_tozar": TOYOTA, "canonical_name": "Toyota", "provenance": "rule",
                 "rule_id": "R1_SPELLING", "proposal_id": None, "confidence": None, "approved_at": "t"}}})
    api = client(repo, FakeTrigger())
    listed = api.get(f"/projects/{w['project']}/catalog/browser/manufacturers", headers=as_user()).json()
    assert listed["items"] == [{"tozar": TOYOTA, "variants": 16, "canonical_manufacturer": "Toyota"}]
    models = api.get(f"/projects/{w['project']}/catalog/browser/models", params={"tozar": TOYOTA},
                     headers=as_user()).json()
    assert models["total"] > 0
    assert api.get(f"/projects/{w['project']}/catalog/browser/models", params={"tozar": "Toyota"},
                   headers=as_user()).json()["total"] == 0


# -- 3b. the real guarded gateway: the deployment's policy, the daily budget ------------

class RecordedCompletions:
    """The provider's chat completions, recorded: ONE valid answer each call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        from tests.test_swarm_v2_smoke_offline import kimi_response

        self.calls.append(kwargs)
        return kimi_response(json.dumps({"groups": [VALID["groups"][0]]}))


@pytest.fixture
def recorded_provider(monkeypatch):
    """guarded_gateway's own wiring, with the provider client replaced by a
    recorded one (no socket): the SAME tracker, reservations and settlement."""
    from backend import budget as budget_module
    from tests.test_swarm_v2_smoke_offline import fake_kimi_client

    completions = RecordedCompletions()
    original = budget_module.build_guarded_client_factory
    monkeypatch.setattr(budget_module, "build_guarded_client_factory", lambda tracker, **kw: original(
        tracker, inner_factory=lambda *_a: fake_kimi_client(completions), **kw))
    monkeypatch.setenv("KIMI_API_KEY", "offline-recorded-key")
    return completions


def deployment_policy(**overrides: str) -> dict[str, str]:
    from backend.runtime_policy import reviewed_first_run_policy

    return {**reviewed_first_run_policy().env_expectations(), norm.JOB_SWITCH: "true", **overrides}


def test_the_guarded_gateway_reserves_and_settles_under_the_deployments_policy(recorded_provider):
    repo, _w, lease, proposal_id = world_with_proposal()
    outcome = norm.propose(repo, lease, proposal_id, env=deployment_policy())
    assert outcome == {"status": "proposed", "reason_code": None, "groups": 1}
    (call,) = recorded_provider.calls
    assert call["model"] == "kimi-k3" and "tools" not in call
    # The daily reservation was made and settled, and the usage recorded, under the run's lease.
    rows = [r for r in repo.usage_ledger if r.get("run_id") == str(lease.run_id)]
    assert [(r["call_seq"], r["status"]) for r in rows if r.get("status")] == [(1, "reserved"), (1, "settled")]
    assert repo.get_run_usage_ledger(lease.run_id)["ledger"]["model_calls"] == 1


def test_the_daily_pre_check_refuses_before_the_provider(recorded_provider, monkeypatch):
    repo, _w, lease, proposal_id = world_with_proposal()
    monkeypatch.setattr(repo, "sum_daily_ledger_cost", lambda **_kw: 1_000.0)
    outcome = norm.propose(repo, lease, proposal_id, env=deployment_policy())
    assert outcome == {"status": "refused", "reason_code": "NORMALIZATION_BUDGET_REFUSED", "groups": 0}
    assert recorded_provider.calls == []


def test_a_deployment_without_its_caps_refuses_the_call(recorded_provider):
    repo, _w, lease, proposal_id = world_with_proposal()
    outcome = norm.propose(repo, lease, proposal_id, env={norm.JOB_SWITCH: "true"})
    assert outcome == {"status": "refused", "reason_code": "NORMALIZATION_POLICY_REFUSED", "groups": 0}
    assert recorded_provider.calls == []


def test_the_button_executes_the_normalisation_job_never_the_capture_job():
    """The provider key lives on the normalisation job only: its routes are wired to a
    trigger for CLOUD_RUN_NORMALISATION_JOB, and without one the API normalises nothing."""
    from backend.catalog.scope.prepare_trigger import build_capture_trigger
    from backend.dependencies import get_normalisation_trigger
    from backend.main import get_normalisation, request_normalisation

    settings = SimpleNamespace(gcp_project_id="p", gcp_region="r", cloud_run_worker_job="worker",
                               cloud_run_capture_job="capture", cloud_run_normalisation_job="capture-normalisation")
    trigger = build_capture_trigger(settings, {"MILO_RELEASE_SHA": "a" * 40},
                                    job_setting="cloud_run_normalisation_job")
    assert (trigger.capture_job, trigger.worker_job) == ("capture-normalisation", "worker")
    assert build_capture_trigger(SimpleNamespace(**{**vars(settings), "cloud_run_normalisation_job": ""}), {},
                                 job_setting="cloud_run_normalisation_job") is None
    for route in (get_normalisation, request_normalisation):
        assert route.__defaults__[-1].dependency is get_normalisation_trigger
