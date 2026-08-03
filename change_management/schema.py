from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any


class ChangeStatus(str, Enum):
    DRAFT = "DRAFT"
    BLOCKED = "BLOCKED"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    phase: str
    action: str
    route_table_id: str
    availability_zone: str
    destination: str
    from_next_hop: str
    to_next_hop: str
    validation_thresholds: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    validator: str
    status: str
    message: str
    hard_gate: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)
    phase: str = "PRECHECK"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionRecord:
    ticket_id: str
    run_id: str
    outcome: str
    started_at: str
    finished_at: str
    before_state_hash: str
    after_state_hash: str
    applied_steps: list[str]
    skipped_steps: list[str]
    rollback_steps: list[str]
    validations: list[dict[str, Any]]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeedbackRecord:
    ticket_id: str
    outcome: str
    planned_steps: int
    applied_steps: int
    rollback_steps: int
    deviations: list[str]
    lessons: list[str]
    knowledge_candidate_id: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChangeTicket:
    ticket_id: str
    revision: int
    status: ChangeStatus
    synthetic: bool
    change_type: str
    risk_level: str
    title: str
    summary: str
    requested_by: str
    region: str
    environment: str
    vpc_id: str
    affected_services: list[str]
    change_window: dict[str, str]
    environment_snapshot_version: int
    environment_snapshot_hash: str
    knowledge_references: list[dict[str, Any]]
    plan_steps: list[PlanStep]
    rollback_triggers: list[str]
    rollback_steps: list[str]
    communication_plan: list[str]
    risk_score: int
    generator_mode: str
    generation_notes: list[str]
    plan_hash: str = ""

    def execution_contract(self) -> dict[str, Any]:
        """Return every immutable field covered by the human approval."""

        return {
            "ticket_id": self.ticket_id,
            "revision": self.revision,
            "synthetic": self.synthetic,
            "change_type": self.change_type,
            "risk_level": self.risk_level,
            "title": self.title,
            "summary": self.summary,
            "requested_by": self.requested_by,
            "region": self.region,
            "environment": self.environment,
            "vpc_id": self.vpc_id,
            "affected_services": self.affected_services,
            "change_window": self.change_window,
            "environment_snapshot_version": self.environment_snapshot_version,
            "environment_snapshot_hash": self.environment_snapshot_hash,
            "knowledge_references": self.knowledge_references,
            "plan_steps": [step.to_dict() for step in self.plan_steps],
            "rollback_triggers": self.rollback_triggers,
            "rollback_steps": self.rollback_steps,
            "communication_plan": self.communication_plan,
            "risk_score": self.risk_score,
            "generator_mode": self.generator_mode,
            "generation_notes": self.generation_notes,
        }

    def compute_plan_hash(self) -> str:
        encoded = json.dumps(
            self.execution_contract(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def seal(self) -> "ChangeTicket":
        self.plan_hash = self.compute_plan_hash()
        return self

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ChangeTicket":
        return cls(
            ticket_id=str(payload["ticket_id"]),
            revision=int(payload["revision"]),
            status=ChangeStatus(str(payload["status"])),
            synthetic=bool(payload["synthetic"]),
            change_type=str(payload["change_type"]),
            risk_level=str(payload["risk_level"]),
            title=str(payload["title"]),
            summary=str(payload["summary"]),
            requested_by=str(payload["requested_by"]),
            region=str(payload["region"]),
            environment=str(payload["environment"]),
            vpc_id=str(payload["vpc_id"]),
            affected_services=[str(item) for item in payload["affected_services"]],
            change_window=dict(payload["change_window"]),
            environment_snapshot_version=int(payload["environment_snapshot_version"]),
            environment_snapshot_hash=str(payload["environment_snapshot_hash"]),
            knowledge_references=[dict(item) for item in payload["knowledge_references"]],
            plan_steps=[PlanStep(**dict(item)) for item in payload["plan_steps"]],
            rollback_triggers=[str(item) for item in payload["rollback_triggers"]],
            rollback_steps=[str(item) for item in payload["rollback_steps"]],
            communication_plan=[str(item) for item in payload["communication_plan"]],
            risk_score=int(payload["risk_score"]),
            generator_mode=str(payload["generator_mode"]),
            generation_notes=[str(item) for item in payload["generation_notes"]],
            plan_hash=str(payload.get("plan_hash") or ""),
        )
