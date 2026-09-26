"""The frontend's vehicle-view fixture IS what the backend builds.

`frontend/tests/fixtures/swarmV2VehicleResult.json` is what the Final result
panel's vehicle tests render. It is committed output of the real FinalBuilder
on the run 6825eb96 replay; if the builder or the assembler changes what it
emits, this fails until the fixture is regenerated, so the browser contract is
never tested against a payload the backend no longer produces.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.engines.swarm_v2 import validate_product_outcome
from backend.engines.swarm_v2.builder import FinalBuilder
from replay_6825eb96 import replay_inputs

FIXTURE = (Path(__file__).resolve().parents[1] / "frontend" / "tests" / "fixtures"
           / "swarmV2VehicleResult.json")


def test_the_frontend_fixture_is_the_backend_replay_output():
    inputs = replay_inputs()
    final = FinalBuilder().build(inputs["evidence"], inputs["verdicts"],
                                 task_failures=inputs["task_failures"],
                                 coverage_gaps=inputs["coverage_gaps"],
                                 candidate_outcomes=inputs["candidate_outcomes"])
    validate_product_outcome(final)
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert committed == {"replay_6825eb96": json.loads(json.dumps(final))}
