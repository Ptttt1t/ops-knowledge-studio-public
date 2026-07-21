from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CardStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ComparisonDecision(str, Enum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    NEW_VERSION = "NEW_VERSION"


LIST_FIELDS = (
    "applicable_versions",
    "prerequisites",
    "procedure_steps",
    "risks",
    "rollback_steps",
    "validation_steps",
    "keywords",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = [value]
    result: list[str] = []
    for item in candidates:
        normalized = _text(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


@dataclass
class KnowledgeCardDraft:
    title: str
    summary: str
    knowledge_type: str = "procedure"
    scenario: str = ""
    object_type: str = ""
    object_name: str = ""
    applicable_versions: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    procedure_steps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    rollback_steps: list[str] = field(default_factory=list)
    validation_steps: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    evidence_quote: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "KnowledgeCardDraft":
        if not isinstance(payload, dict):
            raise ValueError("知识卡片必须是 JSON 对象")
        return cls(
            title=_text(payload.get("title")),
            summary=_text(payload.get("summary")),
            knowledge_type=_text(payload.get("knowledge_type")) or "procedure",
            scenario=_text(payload.get("scenario")),
            object_type=_text(payload.get("object_type")),
            object_name=_text(payload.get("object_name")),
            applicable_versions=_string_list(payload.get("applicable_versions")),
            prerequisites=_string_list(payload.get("prerequisites")),
            procedure_steps=_string_list(payload.get("procedure_steps")),
            risks=_string_list(payload.get("risks")),
            rollback_steps=_string_list(payload.get("rollback_steps")),
            validation_steps=_string_list(payload.get("validation_steps")),
            keywords=_string_list(payload.get("keywords")),
            evidence_quote=_text(payload.get("evidence_quote")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def quality(self, source_text: str) -> tuple[float, list[str]]:
        score = 100.0
        issues: list[str] = []

        common_checks = [
            (not self.title, 20, "缺少标题"),
            (not self.summary, 20, "缺少摘要"),
            (not self.scenario, 8, "缺少适用场景"),
            (not self.object_name, 6, "缺少操作对象"),
            (not self.evidence_quote, 25, "缺少原文证据"),
        ]
        knowledge_type = self.knowledge_type.strip().lower()
        type_checks = {
            "procedure": [
                (not self.procedure_steps, 18, "缺少操作步骤"),
                (not self.risks, 7, "缺少风险说明"),
                (not self.rollback_steps, 8, "缺少回退步骤"),
                (not self.validation_steps, 5, "缺少验证步骤"),
            ],
            "rollback": [
                (not self.rollback_steps, 18, "缺少回退步骤"),
                (not self.risks, 7, "缺少风险说明"),
                (not self.validation_steps, 5, "缺少验证步骤"),
            ],
            "risk": [
                (not self.risks, 18, "缺少风险说明"),
            ],
            "case": [
                (not self.risks, 7, "缺少事件影响或风险说明"),
            ],
            "compatibility": [
                (not self.applicable_versions, 15, "缺少兼容版本或适用范围"),
            ],
            "constraint": [],
        }.get(
            knowledge_type,
            [
                (not self.procedure_steps, 18, "缺少操作步骤"),
                (not self.risks, 7, "缺少风险说明"),
                (not self.rollback_steps, 8, "缺少回退步骤"),
                (not self.validation_steps, 5, "缺少验证步骤"),
            ],
        )

        for failed, penalty, message in [*common_checks, *type_checks]:
            if failed:
                score -= penalty
                issues.append(message)

        if self.evidence_quote and self.evidence_quote not in source_text:
            score -= 25
            issues.append("证据原文无法在来源分片中精确定位")
        return max(0.0, score), issues


@dataclass(frozen=True)
class ComparisonResult:
    decision: ComparisonDecision = ComparisonDecision.NEW
    related_card_id: int | None = None
    confidence: float = 0.0
    reason: str = "未发现高相似候选知识"

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        allowed_card_ids: set[int],
    ) -> "ComparisonResult":
        raw_decision = _text(payload.get("decision")).upper()
        try:
            decision = ComparisonDecision(raw_decision)
        except ValueError:
            decision = ComparisonDecision.NEW

        raw_id = payload.get("related_card_id")
        try:
            related_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            related_id = None
        if related_id not in allowed_card_ids:
            related_id = None
        if decision is not ComparisonDecision.NEW and related_id is None:
            decision = ComparisonDecision.NEW

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            decision=decision,
            related_card_id=related_id,
            confidence=max(0.0, min(1.0, confidence)),
            reason=_text(payload.get("reason")) or "模型未提供判断理由",
        )
