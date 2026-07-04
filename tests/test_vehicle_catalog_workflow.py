import pytest
from pydantic import ValidationError

from backend.engines.vehicle_catalog_v1 import core
from backend.engines.vehicle_catalog_v1.workflow import (
    AgentSpec,
    InternetPolicy,
    QualityContract,
    TEMPLATES,
    ToolPolicy,
    WorkflowSpec,
    agent_template,
    build_milo_blueprint,
    compile_project,
    compile_workflow,
)


def _workflow_with(tasks):
    return WorkflowSpec(
        key="test_workflow",
        name="Test workflow",
        tasks=tuple(tasks),
        quality_contract=QualityContract(),
        completion=TEMPLATES["web_discovery"].completion,
    )


def test_compile_milo_blueprint_exact_order_and_limits_no_network():
    blueprint = build_milo_blueprint()
    compiled = compile_project(blueprint)[0]

    assert compiled.ordered_task_keys == (
        "current_official_lineup_agent",
        "historical_used_market_agent",
        "ev_hybrid_edge_cases_agent",
        "python_discovery_merge",
        "normalizer_deduper",
        "trims_years_agent",
        "engines_fuel_power_agent",
        "transmission_drivetrain_performance_agent",
        "dimensions_safety_equipment_agent",
        "source_verifier",
        "final_builder",
        "hebrew_summary",
    )

    tasks = {task.key: task for task in blueprint.workflows[0].tasks}
    assert all(isinstance(tasks[key], AgentSpec) and tasks[key].internet_policy for key in compiled.agents)
    assert compiled.agents[:3] == (
        "current_official_lineup_agent",
        "historical_used_market_agent",
        "ev_hybrid_edge_cases_agent",
    )
    assert tasks["trims_years_agent"].execution.chunk_size == core.TECHNICAL_MODEL_CHUNK_SIZE == 4
    assert tasks["source_verifier"].execution.chunk_size == core.VERIFIER_MODEL_CHUNK_SIZE == 6
    assert tasks["current_official_lineup_agent"].execution.max_tokens == core.MAX_DISCOVERY_TOKENS == 1800
    assert tasks["trims_years_agent"].execution.max_tokens == 2500
    assert tasks["engines_fuel_power_agent"].execution.max_tokens == 3000
    assert tasks["source_verifier"].execution.max_tokens == core.MAX_VERIFIER_TOKENS == 3500
    assert tasks["hebrew_summary"].execution.max_tokens == core.MAX_SUMMARY_TOKENS == 1200
    assert tasks["source_verifier"].internet_policy == InternetPolicy.DISABLED


def test_malformed_workflows_rejected():
    blueprint = build_milo_blueprint()
    good_tasks = list(blueprint.workflows[0].tasks)
    with pytest.raises(ValueError, match="duplicate"):
        compile_workflow(_workflow_with([good_tasks[0], good_tasks[0], *good_tasks[-2:]]))
    with pytest.raises(ValueError, match="source_verifier"):
        compile_workflow(_workflow_with([task for task in good_tasks if task.key != "source_verifier"]))
    with pytest.raises(ValueError, match="final_builder"):
        compile_workflow(_workflow_with([task for task in good_tasks if task.key != "final_builder"]))

    cyclic_a = TEMPLATES["web_discovery"].model_copy(update={"key": "a", "dependencies": ("b",)})
    cyclic_b = TEMPLATES["claim_verifier"].model_copy(update={"dependencies": ("a",)})
    cyclic_a = cyclic_a.model_copy(update={"dependencies": ("source_verifier",)})
    final = TEMPLATES["deterministic_builder"].model_copy(update={"dependencies": ("source_verifier",)})
    with pytest.raises(ValueError, match="circular"):
        compile_workflow(_workflow_with([cyclic_a, cyclic_b, final]))


def test_invalid_policy_schema_budget_and_tools_rejected():
    with pytest.raises(ValidationError):
        QualityContract(source_attribution=False)
    with pytest.raises(ValidationError):
        ToolPolicy(allowed_tools=("$web_search",), internet_policy=InternetPolicy.DISABLED, max_tool_rounds=1)

    bad_tool = TEMPLATES["web_discovery"].model_copy(update={"tools": ToolPolicy(allowed_tools=("unknown",), internet_policy=InternetPolicy.OPTIONAL, max_tool_rounds=0)})
    verifier = TEMPLATES["claim_verifier"].model_copy(update={"dependencies": (bad_tool.key,)})
    final = TEMPLATES["deterministic_builder"].model_copy(update={"dependencies": (verifier.key,)})
    with pytest.raises(ValueError, match="unknown tools"):
        compile_workflow(_workflow_with([bad_tool, verifier, final]))

    unbounded = agent_template(
        key="big",
        name="Big",
        responsibility="Big budget",
        instructions="test",
        output_schema=TEMPLATES["web_discovery"].output_schema,
        internet_policy=InternetPolicy.REQUIRED,
        max_tokens=100_001,
    )
    verifier = TEMPLATES["claim_verifier"].model_copy(update={"dependencies": ("big",)})
    final = TEMPLATES["deterministic_builder"].model_copy(update={"dependencies": ("source_verifier",)})
    with pytest.raises(ValueError, match="unbounded budget"):
        compile_workflow(_workflow_with([unbounded, verifier, final]))


def test_templates_have_no_hidden_domain_assumptions():
    for template in TEMPLATES.values():
        text = " ".join((template.name, template.responsibility, template.instructions)).lower()
        assert "hyundai" not in text
        assert "israel" not in text
