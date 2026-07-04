from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import urlparse


class InternetPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    ALLOWED = "allowed"
    REQUIRED = "required"
    CONDITIONAL = "conditional"


class GovernanceError(PermissionError):
    pass


@dataclass
class AccessRequest:
    run_id: UUID
    agent: str
    tool: str
    reason: str
    scope: dict[str, Any]
    requested_limits: dict[str, Any]
    trigger: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    status: str = "pending"


@dataclass
class ToolGrant:
    run_id: UUID
    request_id: UUID | None
    agent: str
    tool: str
    max_searches: int
    max_rounds: int
    expires_at: datetime
    approver_policy: str
    domains: list[str] | None = None
    revoked_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def allows_domain(self, url: str) -> bool:
        if not self.domains:
            return True
        hostname = (urlparse(url).hostname or "").lower()
        return any(hostname == d.lower() or hostname.endswith(f".{d.lower()}") for d in self.domains)


@dataclass
class ToolUsage:
    run_id: UUID
    grant_id: UUID
    agent: str
    tool: str
    operation: str
    query: str | None = None
    url: str | None = None
    status: str = "succeeded"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Source:
    run_id: UUID
    agent: str
    url: str
    title: str
    source_type: str
    source_strength: str
    query: str
    tool_operation: str
    source_date: str | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: UUID = field(default_factory=uuid4)

    @property
    def domain(self) -> str:
        return (urlparse(self.url).hostname or "").lower()


@dataclass
class Claim:
    run_id: UUID
    agent: str
    entity_key: str
    field_key: str
    value: Any
    source_id: UUID
    source_strength: str
    confidence: float
    unit: str | None = None
    time_scope: dict[str, Any] = field(default_factory=dict)
    geography: str | None = None
    market: str | None = None
    status: str = "active"
    id: UUID = field(default_factory=uuid4)


@dataclass
class Conflict:
    run_id: UUID
    entity_key: str
    field_key: str
    claim_ids: list[UUID]
    outcome: str = "unresolved_needs_review"
    id: UUID = field(default_factory=uuid4)


def scopes_overlap(a: Claim, b: Claim) -> bool:
    if (a.market or "") != (b.market or "") or (a.geography or "") != (b.geography or ""):
        return False
    if a.unit and b.unit and a.unit != b.unit:
        return False
    return _time_overlaps(a.time_scope, b.time_scope)


def materially_different(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) > max(0.01 * max(abs(float(a)), abs(float(b))), 1e-9)
    return a != b


def _time_overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("variant") and b.get("variant") and a["variant"] != b["variant"]:
        return False
    if a.get("year") is not None or b.get("year") is not None:
        return a.get("year") == b.get("year")
    return True


class InternetGovernanceEngine:
    def __init__(self, run_id: UUID, policy: InternetPolicy, min_successful_searches: int = 0, per_agent_limit: int | None = None, per_run_limit: int | None = None) -> None:
        self.run_id = run_id
        self.policy = InternetPolicy(policy)
        self.min_successful_searches = min_successful_searches if self.policy == InternetPolicy.REQUIRED else 0
        self.per_agent_limit = per_agent_limit
        self.per_run_limit = per_run_limit
        self.requests: list[AccessRequest] = []
        self.grants: list[ToolGrant] = []
        self.usage: list[ToolUsage] = []
        self.sources: list[Source] = []
        self.claims: list[Claim] = []
        self.links: list[dict[str, UUID]] = []
        self.conflicts: list[Conflict] = []

    def request_access(self, agent: str, tool: str, reason: str, scope: dict[str, Any], requested_limits: dict[str, Any], trigger: dict[str, Any] | None = None) -> AccessRequest:
        if self.policy == InternetPolicy.FORBIDDEN:
            raise GovernanceError("internet access is forbidden by policy")
        if self.policy == InternetPolicy.CONDITIONAL and not trigger:
            raise GovernanceError("conditional internet access requires a structured trigger")
        req = AccessRequest(self.run_id, agent, tool, reason, scope, requested_limits, trigger)
        self.requests.append(req)
        return req

    def grant(self, request_id: UUID, max_searches: int, max_rounds: int, expires_at: datetime, approver_policy: str, domains: list[str] | None = None) -> ToolGrant:
        req = next((r for r in self.requests if r.id == request_id), None)
        if req is None:
            raise GovernanceError("unknown access request")
        req.status = "granted"
        grant = ToolGrant(self.run_id, req.id, req.agent, req.tool, max_searches, max_rounds, expires_at, approver_policy, domains)
        self.grants.append(grant)
        return grant

    def deny(self, request_id: UUID) -> None:
        req = next((r for r in self.requests if r.id == request_id), None)
        if req:
            req.status = "denied"

    def record_tool_use(self, grant_id: UUID, operation: str, query: str | None = None, url: str | None = None, status: str = "succeeded") -> ToolUsage:
        grant = self._valid_grant(grant_id, url)
        if self.per_run_limit is not None and len(self.usage) >= self.per_run_limit:
            raise GovernanceError("run internet quota exceeded")
        if self.per_agent_limit is not None and sum(u.agent == grant.agent for u in self.usage) >= self.per_agent_limit:
            raise GovernanceError("agent internet quota exceeded")
        if sum(u.grant_id == grant.id and u.status == "succeeded" for u in self.usage) >= grant.max_searches:
            raise GovernanceError("grant search quota exceeded")
        usage = ToolUsage(self.run_id, grant.id, grant.agent, grant.tool, operation, query, url, status)
        self.usage.append(usage)
        return usage

    def add_source(self, agent: str, url: str, title: str, source_type: str, source_strength: str, query: str, tool_operation: str, source_date: str | None = None) -> Source:
        source = Source(self.run_id, agent, url, title, source_type, source_strength, query, tool_operation, source_date)
        self.sources.append(source)
        return source

    def add_claim(self, agent: str, entity_key: str, field_key: str, value: Any, source_id: UUID, source_strength: str, confidence: float, **scope: Any) -> Claim:
        if not any(s.id == source_id for s in self.sources):
            raise GovernanceError("claim source must be registered")
        claim = Claim(self.run_id, agent, entity_key, field_key, value, source_id, source_strength, confidence, **scope)
        self.claims.append(claim)
        self.links.append({"source_id": source_id, "claim_id": claim.id})
        self._detect_conflicts_for(claim)
        return claim

    def assert_can_complete(self) -> None:
        if self.policy == InternetPolicy.REQUIRED:
            successful = [u for u in self.usage if u.status == "succeeded"]
            if len(successful) < self.min_successful_searches:
                raise GovernanceError("required internet policy did not meet minimum successful searches")
            if not self.sources:
                raise GovernanceError("required internet policy produced no acceptable source")

    def _valid_grant(self, grant_id: UUID, url: str | None) -> ToolGrant:
        grant = next((g for g in self.grants if g.id == grant_id), None)
        if grant is None:
            raise GovernanceError("no grant for tool call")
        now = datetime.now(UTC)
        if grant.revoked_at or grant.expires_at <= now:
            raise GovernanceError("grant is expired or revoked")
        if url and not grant.allows_domain(url):
            raise GovernanceError("domain is outside grant scope")
        return grant

    def _detect_conflicts_for(self, claim: Claim) -> None:
        for other in self.claims[:-1]:
            if other.entity_key == claim.entity_key and other.field_key == claim.field_key and scopes_overlap(other, claim) and materially_different(other.value, claim.value):
                self.conflicts.append(Conflict(self.run_id, claim.entity_key, claim.field_key, [other.id, claim.id]))
