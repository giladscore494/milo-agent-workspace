"""The canonical event vocabulary, and the drift it exists to make impossible.

Before ``backend/event_registry.py`` the same question -- "is this a legitimate
run event?" -- had four independently maintained answers:

* the API accepted a set that named no Swarm V2 type at all, so it answered 422
  to ``task_started``;
* the durable Supabase sink validated nothing, so eighteen further types
  reached ``run_events`` unchecked;
* the in-process sink used by tests validated against the API's narrower set,
  so the three boundaries disagreed about one event;
* the browser hand-maintained a Swarm V2 set with no backend counterpart, and
  five of the engine's real types were missing from it.

These tests prove the registry is now the only answer, that every emitter in
the repository is inside it, and that the browser's mirror cannot move
independently of it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from uuid import uuid4

import pytest

from backend import event_registry as registry
from backend.event_registry import (CAPTURE_PROGRESS_EVENT_TYPES, CATALOG_EVENT_TYPES,
                                    EVENT_TYPES, OPERATIONAL_EVENT_TYPES,
                                    RUN_LEVEL_EVENT_TYPES, SWARM_V2_EVENT_TYPES,
                                    UnknownEventType, V1_EVENT_TYPES,
                                    is_known_event_type, require_known_event_type)
from backend.runtime import InMemoryEventSink, RunEventRecord, SupabaseEventSink

MANIFEST = Path("frontend/lib/eventRegistry.generated.json")
VOCABULARY = Path("frontend/lib/eventVocabulary.ts")


# ---------------------------------------------------------------------------
# the registry's own shape
# ---------------------------------------------------------------------------
def test_the_acceptance_set_is_exactly_the_four_durable_groups():
    assert EVENT_TYPES == (V1_EVENT_TYPES | SWARM_V2_EVENT_TYPES
                           | CATALOG_EVENT_TYPES | OPERATIONAL_EVENT_TYPES)


def test_the_capture_vocabulary_is_declared_and_deliberately_not_durable():
    """Naming the Government capture's progress signals must not make any of
    them appendable: the capture keeps them in memory and writes none."""
    assert CAPTURE_PROGRESS_EVENT_TYPES
    assert not (CAPTURE_PROGRESS_EVENT_TYPES & EVENT_TYPES)
    for event_type in CAPTURE_PROGRESS_EVENT_TYPES:
        assert not is_known_event_type(event_type)


def test_membership_grants_a_projection_so_the_groups_stay_separate():
    """The F5 rule. A catalog, swarm or operational type must never acquire the
    V1 AGENT projection.

    Membership is about which projection a name may write, not about disjoint
    name sets. The canonical worker emits the SAME run lifecycle around either
    engine, so those names are deliberately granted to the Swarm slice as well
    (see SWARM_V2_EVENT_TYPES). What must never be shared is the ability to
    create or mutate a V1 agent row -- that is what would let an `agent` field
    on a run-level or Swarm event invent an agent that did no work.
    """
    for group in (SWARM_V2_EVENT_TYPES, CATALOG_EVENT_TYPES, OPERATIONAL_EVENT_TYPES):
        for event_type in group:
            assert not registry.owns_agent_projection(event_type), event_type

    # The catalog and operational slices are wholly their own.
    assert not (CATALOG_EVENT_TYPES & V1_EVENT_TYPES)
    assert not (OPERATIONAL_EVENT_TYPES & V1_EVENT_TYPES)
    for group in (CATALOG_EVENT_TYPES, OPERATIONAL_EVENT_TYPES):
        for event_type in group:
            assert not registry.owns_v1_projection(event_type), event_type

    # The swarm slice shares ONLY run-level names with V1, and nothing
    # agent-, phase- or chunk-shaped.
    assert (SWARM_V2_EVENT_TYPES & V1_EVENT_TYPES) <= RUN_LEVEL_EVENT_TYPES
    assert RUN_LEVEL_EVENT_TYPES <= V1_EVENT_TYPES


def test_recognition_is_exact_membership_never_a_prefix_or_substring():
    assert is_known_event_type("catalog_variant_promoted")
    assert not is_known_event_type("catalog_variant_promoted_v2")
    assert not is_known_event_type("run_")
    assert not is_known_event_type("")
    assert not is_known_event_type(None)
    assert not is_known_event_type(["run_started"])


# ---------------------------------------------------------------------------
# every emitter in the repository is inside the registry
# ---------------------------------------------------------------------------
#: Where an event type is named as the first argument of an emit call.
_EMIT_CALLEES = {"emit", "_emit", "event_sink", "forward_event",
                 "emit_budget_event", "diagnostic_sink"}


def _emitted_event_types() -> dict[str, set[str]]:
    """Every literal event type the backend emits, and where from.

    A static scan, so it sees emitters no test happens to exercise -- which is
    exactly how eighteen real types stayed outside every vocabulary while being
    written to the database on every run.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(Path("backend").rglob("*.py")):
        relative = str(path)
        if relative.startswith("backend/testing/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None)
            if name not in _EMIT_CALLEES:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, set()).add(relative)
        # `RunEventRecord(type="...")` is the other emit shape.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            if not (isinstance(callee, ast.Name) and callee.id == "RunEventRecord"):
                continue
            for keyword in node.keywords:
                if keyword.arg == "type" and isinstance(keyword.value, ast.Constant) \
                        and isinstance(keyword.value.value, str):
                    found.setdefault(keyword.value.value, set()).add(relative)
    return found


def test_every_event_type_the_backend_emits_is_in_the_registry():
    emitted = _emitted_event_types()
    known = EVENT_TYPES | CAPTURE_PROGRESS_EVENT_TYPES
    unknown = {name: sorted(where) for name, where in emitted.items()
               if name not in known}
    assert not unknown, (
        "these event types are emitted but named in no vocabulary, so they "
        f"would be refused at the durable boundary: {unknown}")


def test_the_scan_actually_finds_the_engines_events():
    """A scan that found nothing would make the test above vacuous."""
    emitted = set(_emitted_event_types())
    assert {"task_started", "commander_plan_created", "phase_started",
            "provider_backpressure_wait", "retry_limit_checked"} <= emitted


# ---------------------------------------------------------------------------
# the durable boundary fails closed
# ---------------------------------------------------------------------------
class RecordingRepo:
    def __init__(self):
        self.appended = []

    def append_run_event(self, run_id, event_type, payload, **lease):
        self.appended.append(event_type)
        return {"event_type": event_type}


@pytest.mark.parametrize("sink_factory", [
    lambda repo: InMemoryEventSink(),
    lambda repo: SupabaseEventSink(repo, worker_id="w", attempt=1, lease_token="t"),
    lambda repo: SupabaseEventSink(repo),
])
def test_no_sink_will_make_an_unknown_type_durable(sink_factory):
    """REQUIRED REGRESSION 6/7, at the boundary. The Supabase sink is the one
    that actually writes, and it used to check nothing at all."""
    repo = RecordingRepo()
    sink = sink_factory(repo)
    with pytest.raises(UnknownEventType):
        sink.emit(RunEventRecord(run_id=uuid4(), type="invented_event", message="m"))
    assert repo.appended == []


@pytest.mark.parametrize("event_type", ["run_completed", "run_partial_success",
                                        "run_failed", "run_cancelled",
                                        "task_completed", "task_failed",
                                        "commander_plan_created"])
def test_v1_and_v2_terminal_and_task_events_come_from_the_registry(event_type):
    """REQUIRED REGRESSION 6: both engines' terminal vocabulary is the
    canonical one, and a durable sink accepts exactly it."""
    repo = RecordingRepo()
    sink = SupabaseEventSink(repo, worker_id="w", attempt=1, lease_token="t")
    sink.emit(RunEventRecord(run_id=uuid4(), type=event_type, message="m"))
    assert repo.appended == [event_type]
    assert require_known_event_type(event_type) == event_type


def test_the_finalizers_terminal_events_are_all_registry_members():
    from backend.finalization import _TERMINAL_EVENT

    assert set(_TERMINAL_EVENT.values()) <= V1_EVENT_TYPES


def test_the_budget_trackers_stop_events_are_all_registry_members():
    """A rail that trips and then cannot record why it tripped is worse than
    a rail that does not trip."""
    import re

    source = Path("backend/budget.py").read_text()
    emitted = set(re.findall(r'"(budget_exhausted|run_timed_out|token_limit_reached|'
                             r'retry_limit_reached|kill_switch_activated|budget_warning|'
                             r'model_output_cap_missing)"', source))
    assert emitted, "the budget rail scan found nothing"
    assert emitted <= EVENT_TYPES


# ---------------------------------------------------------------------------
# the browser mirror cannot drift
# ---------------------------------------------------------------------------
def test_the_generated_manifest_is_exactly_the_python_registry():
    """REQUIRED REGRESSION 7, backend half. The browser reads this file; if it
    is stale, the two sides describe different vocabularies."""
    assert MANIFEST.read_text(encoding="utf-8") == registry.serialize(), (
        "frontend/lib/eventRegistry.generated.json is stale. Regenerate with:\n"
        "  python3 -c \"import sys; sys.path.insert(0,'.'); "
        "from backend import event_registry as r; "
        "open('frontend/lib/eventRegistry.generated.json','w').write(r.serialize())\"")


def test_the_manifest_states_every_group_and_the_acceptance_set():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["registry_version"] == registry.REGISTRY_VERSION
    assert set(manifest["groups"]) == set(registry.GROUPS)
    assert sorted(manifest["accepted"]) == sorted(EVENT_TYPES)


def test_the_browser_vocabulary_declares_no_event_names_of_its_own():
    """A literal list in the TypeScript is the drift itself. The module must
    DERIVE its sets from the manifest, so there is nothing to keep in step."""
    source = VOCABULARY.read_text(encoding="utf-8")
    assert "eventRegistry.generated.json" in source
    for event_type in sorted(EVENT_TYPES | CAPTURE_PROGRESS_EVENT_TYPES):
        assert f"'{event_type}'" not in source, (
            f"{event_type!r} is written as a literal in eventVocabulary.ts; the "
            "sets must come from the generated manifest")


def test_the_registry_imports_nothing_from_backend_so_every_emitter_can_use_it():
    tree = ast.parse(Path("backend/event_registry.py").read_text())
    modules = {node.module for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module}
    modules |= {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    assert not any(module.startswith("backend") for module in modules)
