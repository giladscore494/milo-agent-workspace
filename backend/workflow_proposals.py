from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


REPAIR_CAP = 2
MAX_PLANNED_AGENTS = 8
MAX_MODEL_CALLS = 40
MAX_TOKEN_CEILING = 250_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _contains_any(text: str, words: list[str]) -> bool:
    lower = text.lower()
    return any(word in lower for word in words)


def build_task_spec(user_text: str, budget_preference: str | None = None) -> dict[str, Any]:
    lower = user_text.lower()
    ambiguity = []
    if len(user_text.split()) < 8 or _contains_any(lower, ["something", "stuff", "whatever", "unclear"]):
        ambiguity.append("Request is underspecified; clarify goal, deliverables, and target audience.")
    freshness = "current" if _contains_any(lower, ["latest", "current", "today", "recent", "2026", "news"]) else "stable"
    output_format = "report" if _contains_any(lower, ["report", "brief", "memo"]) else "workflow proposal"
    evidence = "citations required" if _contains_any(lower, ["source", "citation", "evidence", "research", "market", "latest", "current"]) else "internal reasoning acceptable"
    privacy = "sensitive" if _contains_any(lower, ["private", "confidential", "pii", "customer", "medical", "legal"]) else "standard"
    constraints = []
    if "no internet" in lower or "offline" in lower:
        constraints.append("offline_only")
    if "cheap" in lower or "low cost" in lower:
        constraints.append("minimize_cost")
    return {
        "goal": user_text.strip(),
        "deliverables": [output_format, "final assembly with verification notes"],
        "entities": [token.strip(".,") for token in user_text.split() if token[:1].isupper()][:8],
        "constraints": constraints,
        "freshness": freshness,
        "output_format": output_format,
        "evidence_requirements": evidence,
        "privacy": privacy,
        "ambiguity_questions": ambiguity,
        "budget_preference": budget_preference or ("low" if "minimize_cost" in constraints else "standard"),
    }


def _internet_policy(role: str, spec: dict[str, Any]) -> tuple[str, str]:
    if "offline_only" in spec["constraints"]:
        return "disabled", "User requested offline/no internet work."
    if spec["freshness"] == "current" or "citations" in spec["evidence_requirements"]:
        if role in {"researcher", "verifier"}:
            return "enabled", "Fresh or cited evidence is required for this role."
        return "disabled", "Role can use compiled evidence without additional browsing."
    return "disabled", "Task appears stable and does not require current sources."


def draft_workflow(spec: dict[str, Any]) -> dict[str, Any]:
    roles = ["architect", "researcher", "synthesizer", "verifier", "final_assembler"]
    if spec["privacy"] == "sensitive":
        roles.insert(1, "privacy_reviewer")
    if spec["budget_preference"] == "excessive" or "huge" in spec["goal"].lower():
        roles.extend([f"researcher_{i}" for i in range(10)])
    agents = []
    for idx, role in enumerate(roles, start=1):
        policy, reason = _internet_policy(role if not role.startswith("researcher_") else "researcher", spec)
        agents.append({
            "id": f"agent_{idx}", "role": role,
            "template": "approved_research" if "researcher" in role else "approved_review",
            "internet_policy": policy, "internet_reason": reason,
            "depends_on": [] if idx == 1 else [f"agent_{idx-1}"],
            "chunk_size": 4 if "researcher" in role else 1,
            "source_policy": spec["evidence_requirements"],
        })
    return {"agents": agents, "completion": "final_assembler compiles verified answer", "validation_enabled": True, "auto_run": False, "model_generated_code_execution": False}


def estimate(draft: dict[str, Any]) -> dict[str, Any]:
    agents = draft["agents"]
    max_calls = len(agents) * 4
    token_ceiling = len(agents) * 20_000
    return {
        "planned_agents": len(agents),
        "max_model_calls": max_calls,
        "search_enabled_agents": sum(1 for a in agents if a["internet_policy"] == "enabled"),
        "token_ceiling": token_ceiling,
        "duration_range": f"{max(2, len(agents) * 2)}-{max(5, len(agents) * 5)} minutes",
        "cost_warning": "high" if max_calls > 24 or token_ceiling > 160_000 else "normal",
    }


def critique(spec: dict[str, Any], draft: dict[str, Any], estimates: dict[str, Any]) -> dict[str, Any]:
    findings = []
    roles = [a["role"] for a in draft.get("agents", [])]
    if "verifier" not in roles:
        findings.append("missing verifier role")
    if "final_assembler" not in roles:
        findings.append("missing final assembly")
    if len(roles) != len(set(roles)):
        findings.append("duplicate roles")
    if estimates["planned_agents"] > MAX_PLANNED_AGENTS or estimates["max_model_calls"] > MAX_MODEL_CALLS or estimates["token_ceiling"] > MAX_TOKEN_CEILING:
        findings.append("excessive cost")
    if spec["freshness"] == "current" and not any(a["internet_policy"] == "enabled" for a in draft["agents"]):
        findings.append("wrong internet policy")
    if spec["ambiguity_questions"]:
        findings.append("ambiguity")
    if not draft.get("validation_enabled"):
        findings.append("validation disabled")
    if draft.get("auto_run"):
        findings.append("auto-run is not allowed")
    status = "approved" if not findings else ("rejected" if any(f in findings for f in ["validation disabled", "auto-run is not allowed"]) else "revision_required")
    return {"status": status, "findings": findings, "checked_at": _now()}


def repair(spec: dict[str, Any], draft: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    roles = [a["role"] for a in draft["agents"]]
    if "missing verifier role" in review["findings"]:
        policy, reason = _internet_policy("verifier", spec)
        draft["agents"].append({"id": f"agent_{len(roles)+1}", "role": "verifier", "template": "approved_review", "internet_policy": policy, "internet_reason": reason, "depends_on": [], "chunk_size": 1, "source_policy": spec["evidence_requirements"]})
    if "missing final assembly" in review["findings"]:
        draft["agents"].append({"id": f"agent_{len(draft['agents'])+1}", "role": "final_assembler", "template": "approved_review", "internet_policy": "disabled", "internet_reason": "Compiles already gathered and verified evidence.", "depends_on": [], "chunk_size": 1, "source_policy": spec["evidence_requirements"]})
        draft["completion"] = "final_assembler compiles verified answer"
    if "excessive cost" in review["findings"]:
        draft["agents"] = [a for a in draft["agents"] if not a["role"].startswith("researcher_")][:MAX_PLANNED_AGENTS]
    return draft


def compile_proposal(user_text: str, budget_preference: str | None = None, force_missing_verifier: bool = False, force_bad_internet: bool = False) -> dict[str, Any]:
    spec = build_task_spec(user_text, budget_preference)
    draft = draft_workflow(spec)
    if force_missing_verifier:
        draft["agents"] = [a for a in draft["agents"] if a["role"] != "verifier"]
    if force_bad_internet:
        for agent in draft["agents"]:
            agent["internet_policy"] = "disabled"
            agent["internet_reason"] = "Forced invalid policy for test."
    critiques = []
    repair_count = 0
    while True:
        estimates = estimate(draft)
        review = critique(spec, draft, estimates)
        critiques.append(review)
        if review["status"] != "revision_required" or repair_count >= REPAIR_CAP:
            break
        draft = repair(spec, draft, review)
        repair_count += 1
    final_status = "revision_required" if critiques[-1]["status"] == "revision_required" else critiques[-1]["status"]
    return {"task_spec": spec, "draft": draft, "critiques": critiques, "repair_count": repair_count, "status": final_status, "estimates": estimate(draft), "compiled_at": _now(), "approved_at": None, "rejected_at": None}


def ensure_approved(proposal: dict[str, Any]) -> None:
    if proposal.get("status") != "approved" or not proposal.get("approved_at"):
        from backend.errors import AppError
        raise AppError("PROPOSAL_NOT_APPROVED", "workflow proposal must be approved before project creation or run start", 409)
