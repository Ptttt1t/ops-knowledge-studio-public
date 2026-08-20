from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from difflib import SequenceMatcher
from enum import Enum
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

from .change_order_adapter import (
    ChangeOrderExtractionPlan,
    ChangeOrderExtractionUnit,
    SourceEvidenceRef,
)
from .schema import KnowledgeCardDraft


CARD_MODEL_VERSION = "change_order_card_model_v1"
BUILDER_VERSION = "change_order_semantic_builder_v1"


class CardType(str, Enum):
    CASE_CONTEXT = "CASE_CONTEXT"
    PROCEDURE_STEP = "PROCEDURE_STEP"
    EXECUTION_OUTCOME = "EXECUTION_OUTCOME"


class DedupStatus(str, Enum):
    NEW = "NEW"
    REUSED = "REUSED"
    DUPLICATE = "DUPLICATE"


class PublishStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    SKIPPED = "SKIPPED"


CONTEXT_ROLES = {
    "IDENTITY",
    "SERVICE_SCOPE",
    "CHANGE_CONTEXT",
    "RISK_IMPACT",
    "EXECUTION_CONTEXT",
    "GOVERNANCE_CONTEXT",
    "IDENTITY_METADATA_CONTEXT",
}
PROCEDURE_ROLES = {
    "PRECHECK_STEPS",
    "IMPLEMENTATION_STEPS",
    "VALIDATION_STEPS",
    "ROLLBACK_STEPS",
    "UNMAPPED_PROCEDURE_STEPS",
}
ROLE_TO_PHASE = {
    "PRECHECK_STEPS": "PRECHECK",
    "IMPLEMENTATION_STEPS": "IMPLEMENTATION",
    "VALIDATION_STEPS": "VALIDATION",
    "ROLLBACK_STEPS": "ROLLBACK",
}

_HTML_RESIDUE = re.compile(r"</?[a-zA-Z][^>]*>|&(?:nbsp|amp|lt|gt|quot|#\d+);", re.I)
_RAW_JSON = re.compile(
    r"(?:^|\n)\s*(?:/[^:\n]+:\s*)?[\[{]\s*[\"']?[^\n{}\[\]:,]+[\"']?\s*:",
    re.M,
)
_URL = re.compile(r"https?://\S+", re.I)
_TOP_SECTION = re.compile(
    r"(?m)^\s*(?P<label>(?:\d{1,2}|[一二三四五六七八九十百]+)[、.．])\s*(?P<title>[^\n]+)"
)
_INSTANCE_KEY = re.compile(
    r"(?:region|cluster|node[_ -]?pool|nodepool|workload|container|cpu|memory|"
    r"instance[_ -]?count|replica|namespace|pod|实例数|容器|集群|节点池|工作负载|"
    r"内存|处理器|地域|区域)",
    re.I,
)
_INLINE_INSTANCE = re.compile(
    r"(?P<key>region|cluster|node[_ -]?pool|workload|container|cpu(?:_before|_after)?|"
    r"memory(?:_before|_after)?|instance[_ -]?count|replica|namespace)\s*[:=]\s*"
    r"(?P<value>[^,，;；\s]+)",
    re.I,
)


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _timezone(value: str) -> tzinfo:
    normalized = str(value or "Asia/Shanghai").strip()
    if normalized in {"Asia/Shanghai", "UTC+8", "+08:00"}:
        return timezone(timedelta(hours=8), name="Asia/Shanghai")
    if normalized.upper() in {"UTC", "Z", "+00:00"}:
        return timezone.utc
    matched = re.fullmatch(r"([+-])(\d{2}):(\d{2})", normalized)
    if not matched:
        raise ValueError(
            "CHANGE_ORDER_CARD_TIMEZONE 仅支持 Asia/Shanghai、UTC 或显式 ±HH:MM"
        )
    sign = 1 if matched.group(1) == "+" else -1
    hours, minutes = int(matched.group(2)), int(matched.group(3))
    if hours > 23 or minutes > 59:
        raise ValueError("显式时区偏移无效")
    return timezone(sign * timedelta(hours=hours, minutes=minutes), name=normalized)


def normalize_timestamp(value: Any, *, timezone_name: str) -> dict[str, Any]:
    """Deterministically normalize epoch or ISO timestamps.

    The model never receives an unconverted epoch. The raw value is retained as
    provenance while the normalized local value and timezone are explicit.
    """

    zone = _timezone(timezone_name)
    if isinstance(value, bool):
        raise ValueError("boolean 不是 timestamp")
    if isinstance(value, (int, float)):
        numeric = float(value)
        seconds = numeric / 1000.0 if abs(numeric) >= 100_000_000_000 else numeric
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(zone)
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return normalize_timestamp(float(text), timezone_name=timezone_name)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        else:
            parsed = parsed.astimezone(zone)
    else:
        raise ValueError("timestamp 必须是 epoch 数值或 ISO 字符串")
    return {
        "raw": value,
        "normalized": parsed.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": timezone_name,
        "iso8601": parsed.isoformat(timespec="seconds"),
    }


def _duration(value: Any) -> dict[str, Any]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"raw": value, "normalized": value, "unit": "source_defined"}
    seconds = float(value)
    if seconds.is_integer():
        seconds = int(seconds)
    return {"raw": value, "normalized": seconds, "unit": "seconds"}


def normalize_deterministic_value(
    key: str, value: Any, *, timezone_name: str
) -> Any:
    normalized_key = str(key).strip().casefold().replace("-", "_")
    if value is None:
        return None
    if isinstance(value, bool):
        return {"raw": value, "normalized": value, "type": "boolean"}
    if any(
        marker in normalized_key
        for marker in ("timestamp", "start_time", "end_time", "create_time", "update_time")
    ):
        try:
            return normalize_timestamp(value, timezone_name=timezone_name)
        except (OverflowError, OSError, ValueError):
            return {"raw": value, "normalization_error": "INVALID_TIMESTAMP"}
    if any(marker in normalized_key for marker in ("duration", "total_time", "elapsed")):
        return _duration(value)
    if isinstance(value, (int, float)):
        return {"raw": value, "normalized": value, "type": "number"}
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [
            normalize_deterministic_value(key, item, timezone_name=timezone_name)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            str(child_key): normalize_deterministic_value(
                str(child_key), child_value, timezone_name=timezone_name
            )
            for child_key, child_value in value.items()
        }
    return str(value)


class _RichTextParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.attachments: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        attributes = {str(key).casefold(): value for key, value in attrs}
        if name == "br":
            self.parts.append("\n")
        elif name in self.BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        elif name == "img":
            source = str(attributes.get("src") or "").strip()
            self.attachments.append(
                {
                    "type": "image",
                    "source": source,
                    "alt": str(attributes.get("alt") or "").strip(),
                }
            )
            self.parts.append("[图片证据]")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class RichText:
    text: str
    attachments: tuple[dict[str, Any], ...] = ()


def normalize_rich_text(value: Any) -> RichText:
    if value is None:
        return RichText("")
    if isinstance(value, (list, tuple)):
        normalized = [normalize_rich_text(item) for item in value]
        return RichText(
            "\n".join(item.text for item in normalized if item.text),
            tuple(attachment for item in normalized for attachment in item.attachments),
        )
    if isinstance(value, dict):
        # Dictionaries are provenance/metadata, never rendered into card body.
        return RichText("")
    raw = str(value)
    try:
        raw = json.loads(f'"{raw}"') if "\\" in raw else raw
    except json.JSONDecodeError:
        pass
    parser = _RichTextParser()
    parser.feed(unescape(raw).replace("\u00a0", " "))
    parser.close()
    text = "".join(parser.parts)
    text = _URL.sub("", text)
    text = text.replace('\"', '"')
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return RichText(text.strip(), tuple(parser.attachments))


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]


def resolve_pointer(payload: Any, pointer: str) -> Any:
    current = payload
    for part in _pointer_parts(pointer):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(pointer)
    return current


def _normalized_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = stable_json(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result


def _source_identity(
    unit: ChangeOrderExtractionUnit,
    *,
    section_index: int | None = None,
) -> dict[str, Any]:
    pointer = unit.source_pointers[0] if unit.source_pointers else unit.pointer
    identity = {
        "source_pointer": pointer,
        "procedure_group": unit.procedure_group,
        "procedure_step_index": unit.step_start_index,
        "source_hashes": [item.content_sha256 for item in unit.source_evidence_refs],
    }
    if section_index is not None:
        identity["semantic_section_index"] = section_index
    identity["identity"] = content_sha256(identity)
    return identity


def _semantic_fingerprint(card_type: CardType, payload: dict[str, Any]) -> str:
    if card_type is CardType.PROCEDURE_STEP:
        content = {
            "title": _normalized_identity(payload.get("title", "")),
            "generalized_operation": _normalized_identity(
                payload.get("generalized_operation", "")
            ),
            "validation": _normalized_identity(payload.get("validation", "")),
            "rollback": _normalized_identity(payload.get("rollback", "")),
            "impact_analysis": _normalized_identity(payload.get("impact_analysis", "")),
            "operate_command": _normalized_identity(payload.get("operate_command", "")),
            "command_list": [
                _normalized_identity(item) for item in payload.get("command_list", [])
            ],
        }
    else:
        content = payload
    return content_sha256({"card_type": card_type.value, "content": content})


def _comparison_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(value)
        for value in (
            payload.get("title"),
            payload.get("generalized_operation"),
            payload.get("validation"),
            payload.get("rollback"),
            payload.get("impact_analysis"),
            payload.get("operate_command"),
            *(payload.get("command_list") or []),
        )
        if value
    )


def _qa(card_type: CardType, payload: dict[str, Any], body: str) -> dict[str, Any]:
    has_raw_json = bool(_RAW_JSON.search(body))
    has_html_residue = bool(_HTML_RESIDUE.search(body)) or bool(_URL.search(body))
    if card_type is CardType.PROCEDURE_STEP:
        empty_required = not payload.get("title") or not any(
            payload.get(field)
            for field in (
                "operation",
                "validation",
                "rollback",
                "impact_analysis",
                "operate_command",
                "command_list",
            )
        )
        title_consistent = payload.get("title") == payload.get("check_name")
    elif card_type is CardType.CASE_CONTEXT:
        empty_required = not payload.get("title") or not payload.get("context")
        title_consistent = bool(payload.get("title"))
    else:
        empty_required = not payload.get("title") or not payload.get("outcome")
        title_consistent = bool(payload.get("title"))
    score = 100.0
    issues: list[str] = []
    for failed, penalty, issue in (
        (has_raw_json, 40, "RAW_JSON_IN_BODY"),
        (has_html_residue, 30, "HTML_RESIDUE_IN_BODY"),
        (empty_required, 25, "EMPTY_REQUIRED_SECTION"),
        (not title_consistent, 10, "TITLE_CONTENT_INCONSISTENT"),
    ):
        if failed:
            score -= penalty
            issues.append(issue)
    return {
        "has_raw_json": has_raw_json,
        "has_html_residue": has_html_residue,
        "has_empty_required_section": empty_required,
        "title_content_consistent": title_consistent,
        "source_step_count": 0,
        "semantic_unit_count": 1,
        "source_fact_count": len(payload.get("source_facts") or []),
        "inferred_fact_count": len(payload.get("inferred_facts") or []),
        "content_quality": max(0.0, score),
        "quality_issues": issues,
    }


@dataclass
class SemanticKnowledgeCard:
    card_type: CardType
    title: str
    semantic_payload: dict[str, Any]
    source_identities: list[dict[str, Any]]
    source_evidence_refs: list[SourceEvidenceRef]
    source_order: int
    source_chunk_index: int
    procedure_group: str | None = None
    procedure_step_index: int | None = None
    applicable_phases: list[str] = field(default_factory=list)
    semantic_fingerprint: str = ""
    dedup_status: str = DedupStatus.NEW.value
    publish_status: str = PublishStatus.CANDIDATE.value
    planning_rag_enabled: bool = True
    qa: dict[str, Any] = field(default_factory=dict)

    def finalize(self) -> None:
        self.semantic_payload["applicable_phases"] = list(self.applicable_phases)
        self.semantic_payload["source_identities"] = list(self.source_identities)
        self.semantic_fingerprint = _semantic_fingerprint(
            self.card_type, self.semantic_payload
        )
        body = self.body_text()
        self.qa = _qa(self.card_type, self.semantic_payload, body)
        self.qa["source_step_count"] = len(
            {
                (item.get("source_pointer"), item.get("procedure_step_index"))
                for item in self.source_identities
                if item.get("procedure_step_index") is not None
            }
        )
        self.qa["semantic_fingerprint"] = self.semantic_fingerprint
        self.qa["dedup_status"] = self.dedup_status
        self.qa["publish_status"] = self.publish_status

    def body_text(self) -> str:
        payload = self.semantic_payload
        if self.card_type is CardType.PROCEDURE_STEP:
            values: list[Any] = [
                self.title,
                payload.get("generalized_operation"),
                payload.get("operation"),
                payload.get("validation"),
                payload.get("rollback"),
                payload.get("impact_analysis"),
                payload.get("risk_level"),
                payload.get("risk_control"),
                payload.get("inferred_risk"),
                payload.get("operate_command"),
                *(payload.get("command_list") or []),
            ]
        elif self.card_type is CardType.CASE_CONTEXT:
            values = [self.title, payload.get("summary")]
            for section in (payload.get("context") or {}).values():
                if isinstance(section, dict):
                    values.extend(
                        value for value in section.values() if isinstance(value, str)
                    )
        else:
            values = [self.title, payload.get("summary")]
            outcome = payload.get("outcome") or {}
            values.extend(value for value in outcome.values() if isinstance(value, str))
        return "\n".join(str(value) for value in values if value)

    def evidence_excerpt(self, *, limit: int = 300) -> str:
        """Build a readable preview while exact pointer/span/hash stays metadata."""

        payload = self.semantic_payload
        if self.card_type is CardType.PROCEDURE_STEP:
            candidates = (
                payload.get("check_name"),
                payload.get("operation"),
                payload.get("validation"),
                payload.get("rollback"),
                payload.get("impact_analysis"),
            )
        else:
            candidates = (payload.get("summary"), self.title)
        excerpt = next(
            (
                normalized.text
                for value in candidates
                if value
                and (normalized := normalize_rich_text(value)).text
            ),
            self.title,
        )
        return excerpt[: max(1, int(limit))].rstrip()

    def to_legacy_draft(self) -> KnowledgeCardDraft:
        payload = self.semantic_payload
        if self.card_type is CardType.CASE_CONTEXT:
            context = payload.get("context") or {}
            risk = context.get("risk_impact") or {}
            summary = str(payload.get("summary") or self.title)
            return KnowledgeCardDraft(
                title=self.title,
                summary=summary,
                knowledge_type="case_context",
                scenario=str((context.get("change_context") or {}).get("change_scene") or ""),
                object_type="ChangeOrder",
                object_name=str(payload.get("case_identity") or self.title),
                prerequisites=_string_values(context.get("governance") or {}),
                risks=_string_values(risk),
                keywords=_context_keywords(context),
                evidence_quote="",
            )
        if self.card_type is CardType.EXECUTION_OUTCOME:
            return KnowledgeCardDraft(
                title=self.title,
                summary=str(payload.get("summary") or self.title),
                knowledge_type="execution_outcome",
                scenario="historical execution outcome",
                object_type="ChangeOrder",
                object_name=str(payload.get("case_identity") or self.title),
                validation_steps=_string_values(payload.get("outcome") or {}),
                keywords=["execution", "outcome", "historical"],
                evidence_quote="",
            )
        operation = str(payload.get("generalized_operation") or payload.get("operation") or "")
        procedure_steps = [operation] if operation else []
        operate_command = str(payload.get("operate_command") or "")
        if operate_command:
            procedure_steps.append(operate_command)
        procedure_steps.extend(str(item) for item in payload.get("command_list") or [] if item)
        risk_values = [
            str(payload.get(field) or "")
            for field in ("risk_level", "risk_control", "inferred_risk")
            if payload.get(field)
        ]
        impact = str(payload.get("impact_analysis") or "")
        summary = operation or impact or self.title
        if impact:
            summary = f"{summary}\n影响分析：{impact}" if summary != impact else f"影响分析：{impact}"
        return KnowledgeCardDraft(
            title=self.title,
            summary=summary,
            knowledge_type="procedure_step",
            scenario=str(self.procedure_group or ""),
            object_type="ChangeOrder ProcedureStep",
            object_name=str(payload.get("case_identity") or "ChangeOrder"),
            procedure_steps=_unique(procedure_steps),
            risks=_unique(risk_values),
            rollback_steps=[str(payload["rollback"])] if payload.get("rollback") else [],
            validation_steps=[str(payload["validation"])] if payload.get("validation") else [],
            keywords=_unique(
                [
                    "procedure",
                    str(self.procedure_group or "").casefold(),
                    *[str(key) for key in (payload.get("instance_parameters") or {})],
                ]
            ),
            evidence_quote="",
        )

    def report_row(self) -> dict[str, Any]:
        first = self.source_evidence_refs[0] if self.source_evidence_refs else None
        return {
            "card_id": None,
            "card_type": self.card_type.value,
            "title": self.title,
            "procedure_group": self.procedure_group,
            "procedure_step_index": self.procedure_step_index,
            "source_pointer": (
                self.source_identities[0].get("source_pointer")
                if self.source_identities
                else None
            ),
            "source_hash": first.content_sha256 if first else None,
            **self.qa,
        }


def _string_values(value: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for item in value.values():
        if isinstance(item, str) and item:
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("normalized"), str):
            result.append(str(item["normalized"]))
    return _unique(result)


def _context_keywords(context: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key, section in context.items():
        values.append(key)
        if isinstance(section, dict):
            values.extend(str(field) for field in section)
    return _unique(values)


@dataclass
class CardBuildResult:
    cards: list[SemanticKnowledgeCard]
    report: dict[str, Any]


@dataclass(frozen=True)
class ChangeOrderCardBuilderConfig:
    timezone_name: str = "Asia/Shanghai"
    long_step_chars: int = 6000
    semantic_section_threshold: int = 5
    semantic_reuse_threshold: float = 0.92


class ChangeOrderCardBuilder:
    """Build business-semantic cards from an Adapter-confirmed ChangeOrder.

    Extraction units remain evidence transport units. They never determine card
    boundaries directly: context is merged, actions are metadata-only, source
    ProcedureSteps are normalized one by one, and outcomes are isolated.
    """

    CONTEXT_FIELDS: tuple[tuple[str, set[str]], ...] = (
        ("identity", {"ticket_id", "title", "original_system", "create_time"}),
        (
            "service_scope",
            {"cloud_service", "service", "micro_service", "affected_service", "object"},
        ),
        (
            "change_context",
            {"change_scene", "change_type", "change_reason", "change_content", "current_state", "target_state"},
        ),
        ("region", {"region", "az", "zone", "site", "location"}),
        (
            "risk_impact",
            {"risk_level", "impact_risk_level", "impact_scope", "affected_customer", "customer_sensed", "severity"},
        ),
        (
            "grayscale_policy",
            {"grayscale_policy", "gray_policy", "canary_policy", "batch_policy"},
        ),
        (
            "rollback_requirement",
            {"rollback_requirement", "rollback_policy", "rollback_condition"},
        ),
        (
            "schedule",
            {"expected_start_time", "expected_end_time", "expected_total_time", "change_window", "schedule"},
        ),
        (
            "governance",
            {"approval_status", "approvers", "reviewers", "executors", "cooperators", "authorization", "notification"},
        ),
        ("tools", {"tool_list", "tools", "tool_unique_ids"}),
    )

    def __init__(self, config: ChangeOrderCardBuilderConfig | None = None):
        self.config = config or ChangeOrderCardBuilderConfig()
        _timezone(self.config.timezone_name)
        if self.config.long_step_chars <= 0:
            raise ValueError("long_step_chars 必须大于 0")
        if self.config.semantic_section_threshold <= 0:
            raise ValueError("semantic_section_threshold 必须大于 0")
        if not 0.0 <= self.config.semantic_reuse_threshold <= 1.0:
            raise ValueError("semantic_reuse_threshold 必须在 0 到 1 之间")

    def build(
        self,
        source_text: str,
        plan: ChangeOrderExtractionPlan,
        *,
        source_name: str,
    ) -> CardBuildResult:
        source = json.loads(source_text)
        units = list(plan.units)
        cards: list[SemanticKnowledgeCard] = []
        skipped: list[dict[str, Any]] = []

        context_card = self._context_card(source, units, skipped, source_name)
        if context_card is not None:
            cards.append(context_card)

        procedure_source_count = 0
        procedure_units: list[SemanticKnowledgeCard] = []
        for unit in units:
            if unit.role not in PROCEDURE_ROLES:
                continue
            procedure_source_count += 1
            built = self._procedure_cards(source, unit, source_name)
            if not built:
                skipped.append(self._skip(unit, "EMPTY_AFTER_NORMALIZATION"))
                continue
            procedure_units.extend(built)

        canonical_procedures, reuse_count, reused_skips = self._reuse(procedure_units)
        skipped.extend(reused_skips)
        cards.extend(canonical_procedures)

        outcome = self._outcome_card(source, units, source_name)
        if outcome is not None:
            cards.append(outcome)

        for unit in units:
            if unit.role == "TASKS_GROUPED_UNRECONCILED":
                skipped.append(self._skip(unit, "STRUCTURAL_METADATA_ONLY"))

        assigned = {
            (ref.pointer, ref.char_start, ref.char_end)
            for card in cards
            for ref in card.source_evidence_refs
        }
        explicitly_skipped = {
            (reference["pointer"], reference["char_start"], reference["char_end"])
            for item in skipped
            for reference in item.get("source_evidence_refs", [])
        }
        all_refs = {
            (ref.pointer, ref.char_start, ref.char_end)
            for unit in units
            for ref in unit.source_evidence_refs
        }
        accounted = assigned | explicitly_skipped
        expressed_steps = {
            (identity.get("source_pointer"), identity.get("procedure_step_index"))
            for card in canonical_procedures
            for identity in card.source_identities
            if identity.get("procedure_step_index") is not None
        }
        ratio = (
            len(expressed_steps) / procedure_source_count
            if procedure_source_count
            else 1.0
        )
        report = {
            "builder": BUILDER_VERSION,
            "card_model_version": CARD_MODEL_VERSION,
            "source_name": source_name,
            "case_context_count": sum(
                card.card_type is CardType.CASE_CONTEXT for card in cards
            ),
            "procedure_source_step_count": procedure_source_count,
            "procedure_unit_count": len(canonical_procedures),
            "semantic_reuse_count": reuse_count,
            "execution_outcome_count": sum(
                card.card_type is CardType.EXECUTION_OUTCOME for card in cards
            ),
            "skipped_unit_count": len(skipped),
            "structural_source_coverage": {
                "status": "COMPLETE" if all_refs <= accounted else "INCOMPLETE",
                "expected_source_items": len(all_refs),
                "accounted_source_items": len(all_refs & accounted),
                "unaccounted_source_items": len(all_refs - accounted),
            },
            "semantic_content_coverage": {
                "status": "COMPLETE" if ratio == 1.0 else "INCOMPLETE",
                "source_procedure_steps": procedure_source_count,
                "expressed_or_reused_steps": len(expressed_steps),
                "ratio": round(ratio, 6),
            },
            "cards": [card.report_row() for card in cards],
            "skipped_units": skipped,
        }
        return CardBuildResult(cards, report)

    def _context_card(
        self,
        source: dict[str, Any],
        units: list[ChangeOrderExtractionUnit],
        skipped: list[dict[str, Any]],
        source_name: str,
    ) -> SemanticKnowledgeCard | None:
        data = source.get("data") if isinstance(source, dict) else None
        if not isinstance(data, dict):
            return None
        context: dict[str, dict[str, Any]] = {
            section: {} for section, _fields in self.CONTEXT_FIELDS
        }
        recognized: set[str] = set()
        attachments: list[dict[str, Any]] = []
        source_facts: list[dict[str, Any]] = []
        for section, fields in self.CONTEXT_FIELDS:
            for key in fields:
                if key not in data:
                    continue
                recognized.add(key)
                value = data[key]
                if isinstance(value, str):
                    rich = normalize_rich_text(value)
                    normalized: Any = rich.text
                    attachments.extend(rich.attachments)
                    if any(
                        marker in key.casefold()
                        for marker in ("time", "timestamp", "duration")
                    ):
                        normalized = normalize_deterministic_value(
                            key, value, timezone_name=self.config.timezone_name
                        )
                else:
                    normalized = normalize_deterministic_value(
                        key, value, timezone_name=self.config.timezone_name
                    )
                context[section][key] = normalized
                source_facts.append(
                    {"field": key, "pointer": f"/data/{key}", "value": normalized}
                )
        context = {key: value for key, value in context.items() if value}
        if not context:
            return None

        context_units = [unit for unit in units if unit.role in CONTEXT_ROLES]
        refs: list[SourceEvidenceRef] = []
        identities: list[dict[str, Any]] = []
        for unit in context_units:
            unit_keys = {
                part
                for pointer in unit.source_pointers
                for part in _pointer_parts(pointer)
                if part in data
            }
            if unit_keys & recognized:
                refs.extend(unit.source_evidence_refs)
                identities.append(_source_identity(unit))
                skipped.append(self._skip(unit, "MERGED_INTO_CASE_CONTEXT"))
            else:
                skipped.append(self._skip(unit, "STRUCTURAL_METADATA_ONLY"))
        first_unit = min(context_units, key=lambda item: item.chunk.index) if context_units else units[0]
        title = normalize_rich_text(data.get("title")).text
        ticket_id = normalize_rich_text(data.get("ticket_id")).text
        if title and ticket_id:
            display_title = f"{title} · {ticket_id}"
        else:
            display_title = title or ticket_id or source_name
        actions = self._normalize_actions(data.get("action_list"))
        for index, action in enumerate(actions):
            source_facts.append(
                {
                    "field": "actions",
                    "pointer": f"/data/action_list/{index}",
                    "value": action,
                }
            )
        for unit in units:
            if unit.role != "TASKS_CANONICAL":
                continue
            refs.extend(unit.source_evidence_refs)
            identities.append(_source_identity(unit))
            skipped.append(self._skip(unit, "MERGED_INTO_CASE_CONTEXT"))
        payload = {
            "title": display_title,
            "summary": normalize_rich_text(data.get("change_content") or data.get("change_reason") or title).text,
            "case_identity": ticket_id or content_sha256({"source": source_name})[:16],
            "context": context,
            "actions": actions,
            "attachments": _unique(attachments),
            "source_facts": source_facts,
            "inferred_facts": [],
        }
        card = SemanticKnowledgeCard(
            card_type=CardType.CASE_CONTEXT,
            title=display_title,
            semantic_payload=payload,
            source_identities=_unique(identities),
            source_evidence_refs=_unique_refs(refs),
            source_order=first_unit.chunk.index,
            source_chunk_index=first_unit.chunk.index,
        )
        card.finalize()
        return card

    def _normalize_actions(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        actions: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            normalized: dict[str, Any] = {}
            for key, raw in item.items():
                if isinstance(raw, (dict, list)):
                    normalized[str(key)] = normalize_deterministic_value(
                        str(key), raw, timezone_name=self.config.timezone_name
                    )
                else:
                    rich = normalize_rich_text(raw)
                    normalized[str(key)] = rich.text if isinstance(raw, str) else raw
            actions.append(normalized)
        return actions

    def _procedure_cards(
        self,
        source: dict[str, Any],
        unit: ChangeOrderExtractionUnit,
        source_name: str,
    ) -> list[SemanticKnowledgeCard]:
        pointer = unit.source_pointers[0] if unit.source_pointers else unit.pointer
        try:
            record = resolve_pointer(source, pointer)
        except (KeyError, IndexError, ValueError):
            return []
        if not isinstance(record, dict):
            return []
        fields: dict[str, RichText] = {
            key: normalize_rich_text(record.get(key))
            for key in (
                "check_name",
                "operate_description",
                "operate_verified",
                "operate_rollback",
                "impact_analysis",
                "operate_commond",
            )
        }
        command_value = record.get("command_list")
        command_list = [
            item.text
            for item in (
                normalize_rich_text(raw)
                for raw in (command_value if isinstance(command_value, list) else [command_value])
            )
            if item.text
        ]
        if not any(item.text for item in fields.values()) and not command_list:
            return []

        operation = fields["operate_description"].text
        instance_parameters = self._instance_parameters(record, operation)
        generalized = operation
        for key, value in sorted(
            instance_parameters.items(), key=lambda item: len(str(item[1])), reverse=True
        ):
            text = str(value)
            if len(text) >= 2:
                generalized = re.sub(
                    re.escape(text), f"{{{{{key}}}}}", generalized, flags=re.I
                )
        title = fields["check_name"].text or f"{unit.procedure_group or 'PROCEDURE'} 步骤 {(unit.step_start_index or 0) + 1}"
        phase = unit.procedure_group or ROLE_TO_PHASE.get(unit.role, "UNMAPPED")
        attachments = _unique(
            attachment
            for rich in fields.values()
            for attachment in rich.attachments
        )
        source_facts = [
            {"field": field, "pointer": f"{pointer}/{field}", "value": rich.text}
            for field, rich in fields.items()
            if rich.text
        ]
        source_facts.extend(
            {
                "field": "command_list",
                "pointer": f"{pointer}/command_list/{index}",
                "value": value,
            }
            for index, value in enumerate(command_list)
        )
        risk_level = normalize_rich_text(record.get("action_risk_level")).text
        if risk_level:
            source_facts.append(
                {
                    "field": "action_risk_level",
                    "pointer": f"{pointer}/action_risk_level",
                    "value": risk_level,
                }
            )
        payload = {
            "title": title,
            "check_name": title,
            "case_identity": normalize_rich_text(
                (source.get("data") or {}).get("ticket_id")
                if isinstance(source.get("data"), dict)
                else source_name
            ).text,
            "operation": operation,
            "generalized_operation": generalized,
            "validation": fields["operate_verified"].text,
            "rollback": fields["operate_rollback"].text,
            "impact_analysis": fields["impact_analysis"].text,
            "risk_level": risk_level,
            "risk_control": "",
            "inferred_risk": "",
            "operate_command": fields["operate_commond"].text,
            "command_list": command_list,
            "instance_parameters": instance_parameters,
            "actions": self._procedure_actions(record),
            "attachments": attachments,
            "source_facts": source_facts,
            "inferred_facts": [],
            "source_metadata": {
                key: normalize_deterministic_value(
                    key, value, timezone_name=self.config.timezone_name
                )
                for key, value in record.items()
                if key
                not in {
                    "check_name",
                    "operate_description",
                    "operate_verified",
                    "operate_rollback",
                    "impact_analysis",
                    "operate_commond",
                    "command_list",
                }
            },
        }
        parent = self._make_procedure_card(unit, payload)
        sections = self._semantic_sections(operation)
        should_split = len(operation) > self.config.long_step_chars or len(sections) >= self.config.semantic_section_threshold
        if not should_split or not sections:
            return [parent]
        children: list[SemanticKnowledgeCard] = []
        for index, section in enumerate(sections):
            child_payload = dict(payload)
            child_payload.update(
                {
                    "title": f"{title} · {section['title']}",
                    "check_name": f"{title} · {section['title']}",
                    "operation": section["content"],
                    "generalized_operation": section["content"],
                    "parent_source_identity": parent.source_identities[0]["identity"],
                    "semantic_section": {
                        "index": index,
                        "label": section["label"],
                        "title": section["title"],
                    },
                }
            )
            child = self._make_procedure_card(unit, child_payload, section_index=index)
            children.append(child)
        parent.semantic_payload["child_semantic_fingerprints"] = [
            child.semantic_fingerprint for child in children
        ]
        parent.finalize()
        return [parent, *children]

    def _make_procedure_card(
        self,
        unit: ChangeOrderExtractionUnit,
        payload: dict[str, Any],
        *,
        section_index: int | None = None,
    ) -> SemanticKnowledgeCard:
        card = SemanticKnowledgeCard(
            card_type=CardType.PROCEDURE_STEP,
            title=str(payload["title"]),
            semantic_payload=payload,
            source_identities=[_source_identity(unit, section_index=section_index)],
            source_evidence_refs=list(unit.source_evidence_refs),
            source_order=unit.chunk.index,
            source_chunk_index=unit.chunk.index,
            procedure_group=unit.procedure_group,
            procedure_step_index=unit.step_start_index,
            applicable_phases=[unit.procedure_group or ROLE_TO_PHASE.get(unit.role, "UNMAPPED")],
        )
        card.finalize()
        return card

    @staticmethod
    def _procedure_actions(record: dict[str, Any]) -> list[Any]:
        for key in ("tool_related_actions", "action_unique_ids", "actions"):
            value = record.get(key)
            if isinstance(value, list):
                return value
            if value not in (None, ""):
                return [value]
        return []

    @staticmethod
    def _instance_parameters(record: dict[str, Any], operation: str) -> dict[str, Any]:
        result = {
            str(key): value
            for key, value in record.items()
            if _INSTANCE_KEY.search(str(key)) and value not in (None, "", [], {})
        }
        for matched in _INLINE_INSTANCE.finditer(operation):
            key = matched.group("key").casefold().replace("-", "_").replace(" ", "_")
            result.setdefault(key, matched.group("value"))
        return result

    @staticmethod
    def _semantic_sections(operation: str) -> list[dict[str, str]]:
        matches = list(_TOP_SECTION.finditer(operation))
        if not matches:
            return []
        sections: list[dict[str, str]] = []
        for index, matched in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(operation)
            content = operation[matched.start() : end].strip()
            sections.append(
                {
                    "label": matched.group("label"),
                    "title": matched.group("title").strip(),
                    "content": content,
                }
            )
        return sections

    def _reuse(
        self, cards: list[SemanticKnowledgeCard]
    ) -> tuple[list[SemanticKnowledgeCard], int, list[dict[str, Any]]]:
        canonical: list[SemanticKnowledgeCard] = []
        skipped: list[dict[str, Any]] = []
        reuse_count = 0
        for candidate in cards:
            matched: SemanticKnowledgeCard | None = None
            matched_similarity = 0.0
            candidate_text = _normalized_identity(
                _comparison_text(candidate.semantic_payload)
            )
            for existing in canonical:
                if candidate.semantic_fingerprint == existing.semantic_fingerprint:
                    matched, matched_similarity = existing, 1.0
                    break
                if not candidate_text:
                    continue
                existing_text = _normalized_identity(
                    _comparison_text(existing.semantic_payload)
                )
                similarity = SequenceMatcher(None, candidate_text, existing_text).ratio()
                if similarity >= self.config.semantic_reuse_threshold and similarity > matched_similarity:
                    matched, matched_similarity = existing, similarity
            if matched is None:
                canonical.append(candidate)
                continue
            reuse_count += 1
            matched.dedup_status = DedupStatus.REUSED.value
            matched.applicable_phases = _unique(
                [*matched.applicable_phases, *candidate.applicable_phases]
            )
            matched.source_identities = _unique(
                [*matched.source_identities, *candidate.source_identities]
            )
            matched.source_evidence_refs = _unique_refs(
                [*matched.source_evidence_refs, *candidate.source_evidence_refs]
            )
            matched.semantic_payload.setdefault("reuse", []).append(
                {
                    "source_identity": candidate.source_identities[0],
                    "relationship": "REUSES",
                    "similarity": round(matched_similarity, 6),
                }
            )
            matched.finalize()
            first_ref = candidate.source_evidence_refs[0]
            skipped.append(
                {
                    "unit_role": CardType.PROCEDURE_STEP.value,
                    "source_pointer": candidate.source_identities[0].get("source_pointer"),
                    "char_start": first_ref.char_start,
                    "char_end": first_ref.char_end,
                    "source_hash": first_ref.content_sha256,
                    "source_evidence_refs": [
                        item.to_dict() for item in candidate.source_evidence_refs
                    ],
                    "skip_reason": "DUPLICATE",
                    "reuse_target_fingerprint": matched.semantic_fingerprint,
                    "similarity": round(matched_similarity, 6),
                }
            )
        return canonical, reuse_count, skipped

    def _outcome_card(
        self,
        source: dict[str, Any],
        units: list[ChangeOrderExtractionUnit],
        source_name: str,
    ) -> SemanticKnowledgeCard | None:
        outcome_units = [unit for unit in units if unit.role == "EXECUTION_RESULT"]
        if not outcome_units:
            return None
        pointer = outcome_units[0].pointer
        try:
            result = resolve_pointer(source, pointer)
        except (KeyError, IndexError, ValueError):
            return None
        if not isinstance(result, dict):
            return None
        normalized: dict[str, Any] = {}
        source_facts: list[dict[str, Any]] = []
        attachments: list[dict[str, Any]] = []
        for key, value in result.items():
            if isinstance(value, str):
                rich = normalize_rich_text(value)
                normalized_value: Any = rich.text
                attachments.extend(rich.attachments)
                if "time" in key.casefold() or "timestamp" in key.casefold():
                    normalized_value = normalize_deterministic_value(
                        key, value, timezone_name=self.config.timezone_name
                    )
            else:
                normalized_value = normalize_deterministic_value(
                    key, value, timezone_name=self.config.timezone_name
                )
            normalized[key] = normalized_value
            source_facts.append(
                {"field": key, "pointer": f"{pointer}/{key}", "value": normalized_value}
            )
        data = source.get("data") if isinstance(source, dict) else {}
        case_identity = normalize_rich_text(
            data.get("ticket_id") if isinstance(data, dict) else source_name
        ).text
        result_text = next(
            (
                value
                for key in ("change_result", "verified_note", "result", "status")
                if isinstance((value := normalized.get(key)), str) and value
            ),
            "历史执行结果",
        )
        title = f"{case_identity or source_name} · 执行结果"
        payload = {
            "title": title,
            "summary": result_text,
            "case_identity": case_identity,
            "outcome": normalized,
            "attachments": _unique(attachments),
            "source_facts": source_facts,
            "inferred_facts": [],
        }
        refs = _unique_refs(
            ref for unit in outcome_units for ref in unit.source_evidence_refs
        )
        card = SemanticKnowledgeCard(
            card_type=CardType.EXECUTION_OUTCOME,
            title=title,
            semantic_payload=payload,
            source_identities=[_source_identity(unit) for unit in outcome_units],
            source_evidence_refs=refs,
            source_order=min(unit.chunk.index for unit in outcome_units),
            source_chunk_index=min(unit.chunk.index for unit in outcome_units),
            planning_rag_enabled=False,
        )
        card.finalize()
        return card

    @staticmethod
    def _skip(unit: ChangeOrderExtractionUnit, reason: str) -> dict[str, Any]:
        if not unit.source_evidence_refs:
            return {
                "unit_role": unit.role,
                "source_pointer": unit.pointer,
                "skip_reason": reason,
            }
        first = unit.source_evidence_refs[0]
        return {
            "unit_role": unit.role,
            "source_pointer": first.pointer,
            "char_start": first.char_start,
            "char_end": first.char_end,
            "source_hash": first.content_sha256,
            "source_evidence_refs": [
                item.to_dict() for item in unit.source_evidence_refs
            ],
            "skip_reason": reason,
        }


def _unique_refs(values: Iterable[SourceEvidenceRef]) -> list[SourceEvidenceRef]:
    result: list[SourceEvidenceRef] = []
    seen: set[tuple[str, int, int]] = set()
    for value in values:
        marker = (value.pointer, value.char_start, value.char_end)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
