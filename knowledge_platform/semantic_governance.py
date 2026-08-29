from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
import threading
import time
from typing import Any, TYPE_CHECKING

from .change_order_cards import normalize_rich_text
from .retrieval import tokenize
from .schema import CardStatus, KnowledgeCardDraft

if TYPE_CHECKING:
    from .service import KnowledgeService


ALGORITHM_VERSION = "semantic_cluster_v3"
CANONICAL_CARD_MODEL_VERSION = "semantic_cluster_v3"
PROPOSAL_STATUSES = {
    "PENDING_REVIEW",
    "ACCEPTED",
    "REJECTED",
    "DEFERRED",
    "STALE",
}
_PHASE_PREFIX = re.compile(
    r"^(?:前检|预检|变更前检查|实施|变更实施|验证|变更后检查|回退|回滚)\s*[：:—-]\s*",
    re.IGNORECASE,
)
_IDENTITY_NOISE = re.compile(r"[^0-9a-z\u3400-\u9fff]+", re.IGNORECASE)
_RAW_JSON = re.compile(r"(?:^|\n)\s*[\[{]\s*[\"']?[^\n{}\[\]:,]+[\"']?\s*:", re.M)
_GENERIC_TOKENS = {
    "操作",
    "检查",
    "确认",
    "变更",
    "步骤",
    "系统",
    "实施",
    "验证",
    "回退",
    "变更前",
    "变更后",
    "进行",
    "相关",
    "处理",
    "结果",
    "状态",
    "the",
    "check",
    "verify",
    "validation",
    "operation",
}
_PHASE_ALIASES = {
    "PRECHECK": "PRECHECK",
    "PRECHECK_STEPS": "PRECHECK",
    "CHECK_BEFORE_CHANGE": "PRECHECK",
    "IMPLEMENTATION": "IMPLEMENTATION",
    "IMPLEMENTATION_STEPS": "IMPLEMENTATION",
    "CHANGE_IMPLEMENT": "IMPLEMENTATION",
    "VALIDATION": "VALIDATION",
    "VALIDATION_STEPS": "VALIDATION",
    "CHANGE_VERIFIED": "VALIDATION",
    "ROLLBACK": "ROLLBACK",
    "ROLLBACK_STEPS": "ROLLBACK",
    "CHANGE_ROLLBACK": "ROLLBACK",
}
_ANCHOR_PHRASES = {
    "告警检测": ("告警检测", "告警检查", "活动告警", "当前告警"),
    "客户影响": ("客户影响",),
    "业务影响": ("业务影响",),
    "爆炸半径": ("爆炸半径",),
    "变更回退": ("变更回退", "回退方案", "回滚方案"),
    "系统参数": ("系统参数", "配置参数"),
    "白名单": ("白名单",),
    "CPU扩容": ("cpu扩容", "cpu 扩容", "cpu资源扩容"),
    "内存扩容": ("内存扩容", "memory扩容", "memory 扩容"),
    "节点池": ("节点池", "node pool", "nodepool"),
    "服务探针": ("服务探针", "健康探针", "probe check"),
    "关键业务指标": ("关键业务指标", "业务指标"),
    "健康检查": ("健康检查", "健康状态"),
    "配置回读": ("配置回读",),
    "容量分析": ("容量分析", "容量检查"),
    "版本分析": ("版本分析", "版本检查"),
    "API分析": ("api分析", "api检查"),
    "监控分析": ("监控分析", "监控检查"),
    "高危操作": ("高危操作",),
    "配置备份": ("配置备份", "备份检查", "备份规则"),
    "风险控制": ("风险控制", "风险项"),
    "验证条件": ("验证条件", "验证要求"),
}
_WEAK_ANCHORS = {
    "业务",
    "分析",
    "列表",
    "功能",
    "操作",
    "检查",
    "确认",
    "变更",
    "系统",
    "实施",
    "验证",
    "回退",
    "进行",
    "处理",
    "结果",
    "状态",
    "影响",
    "以及",
    "由于",
    "其中",
    "无需",
    "不涉及",
    "无影响",
}
_STRUCTURED_SPLIT = re.compile(r"[\s,，;；、|/\\()（）\[\]{}<>《》:：=]+")
_TECHNICAL_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.:-]{1,31}(?![A-Za-z0-9_])")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = stable_json(value)
        if marker in seen or value in (None, "", [], {}):
            continue
        seen.add(marker)
        result.append(value)
    return result


def _clean_text(value: Any, *, limit: int = 12_000) -> str:
    text = normalize_rich_text(value).text.strip()
    return text[:limit]


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return _unique([_clean_text(item, limit=4000) for item in items])


def _phase_neutral_title(value: Any) -> str:
    return _PHASE_PREFIX.sub("", _clean_text(value, limit=200)).strip()


def _identity(value: Any) -> str:
    return _IDENTITY_NOISE.sub("", _clean_text(value).casefold())


def normalize_procedure_group(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_")
    return _PHASE_ALIASES.get(normalized, "")


def procedure_group(card: dict[str, Any]) -> str:
    explicit = normalize_procedure_group(card.get("procedure_group"))
    if explicit:
        return explicit
    phases = {
        normalized
        for item in card.get("applicable_phases") or []
        if (normalized := normalize_procedure_group(item))
    }
    return next(iter(phases)) if len(phases) == 1 else ""


def _structured_anchor_values(card: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for field in ("keywords", "object_type", "object_name"):
        raw = card.get(field) or []
        values.extend(str(item) for item in (raw if isinstance(raw, list) else [raw]))
    parameters = card.get("instance_parameters") or {}
    if isinstance(parameters, dict):
        for key, value in parameters.items():
            values.extend((str(key), str(value)))
    actions = card.get("actions") or []
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                values.extend(
                    str(value)
                    for key, value in action.items()
                    if str(key).casefold()
                    not in {"id", "unique_id", "tool_unique_id", "action_unique_id"}
                    and not isinstance(value, (dict, list))
                )
            elif not isinstance(action, (dict, list)):
                values.append(str(action))
    return values


def explainable_anchors(card: dict[str, Any]) -> list[str]:
    """Extract complete, explainable phrases without exposing Chinese n-grams."""

    operation = (
        card.get("generalized_operation")
        or card.get("operation")
        or card.get("procedure_steps")
        or ""
    )
    searchable = "\n".join(
        _clean_text(value)
        for value in (
            card.get("title"),
            operation,
            card.get("validation") or card.get("validation_steps"),
            card.get("rollback") or card.get("rollback_steps"),
            card.get("impact_analysis"),
            *_structured_anchor_values(card),
        )
        if _clean_text(value)
    )
    folded = searchable.casefold()
    anchors: list[str] = []
    for canonical, aliases in _ANCHOR_PHRASES.items():
        if any(alias.casefold() in folded for alias in aliases):
            anchors.append(canonical)

    # Structured metadata is already a semantic boundary. Keep complete field
    # values or technical identifiers; never manufacture a Chinese sliding
    # window merely to increase the anchor count.
    for value in _structured_anchor_values(card):
        clean = _clean_text(value, limit=160).strip("-_.:：")
        for part in _STRUCTURED_SPLIT.split(clean):
            candidate = part.strip("-_.:：")
            if not 2 <= len(candidate) <= 32:
                continue
            if candidate.casefold() in _GENERIC_TOKENS or candidate in _WEAK_ANCHORS:
                continue
            if re.fullmatch(r"[\u3400-\u9fff]+", candidate) and len(candidate) == 2:
                # Two-character values are only trusted when they came from the
                # curated semantic phrase set above.
                continue
            anchors.append(candidate)
    anchors.extend(match.group(0) for match in _TECHNICAL_TOKEN.finditer(searchable))
    return sorted(_unique(anchors), key=lambda item: (-len(item), item.casefold()))[:30]


def _flatten_actions(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(
            _flatten_actions(child)
            for key, child in value.items()
            if str(key).casefold()
            not in {"id", "unique_id", "tool_unique_id", "action_unique_id"}
        )
    if isinstance(value, list):
        return " ".join(_flatten_actions(item) for item in value)
    return str(value or "")


def _semantic_parts(card: dict[str, Any]) -> dict[str, Any]:
    operation = (
        card.get("generalized_operation")
        or card.get("operation")
        or card.get("procedure_steps")
        or ""
    )
    fields = [
        operation,
        card.get("validation") or card.get("validation_steps") or "",
        card.get("rollback") or card.get("rollback_steps") or "",
        card.get("impact_analysis") or "",
    ]
    core = "\n".join(_clean_text(field) for field in fields if _clean_text(field))
    tokens = [
        token
        for token in tokenize(core)
        if token not in _GENERIC_TOKENS and len(token.strip()) > 1
    ]
    action_tokens = {
        token
        for token in tokenize(_flatten_actions(card.get("actions") or []))
        if token not in _GENERIC_TOKENS and len(token.strip()) > 1
    }
    object_tokens = {
        token
        for token in tokenize(
            f"{card.get('object_type', '')} {card.get('object_name', '')}"
        )
        if token not in _GENERIC_TOKENS and len(token.strip()) > 1
    }
    return {
        "title": _phase_neutral_title(card.get("title")),
        "core": core,
        "identity": _identity(core),
        "tokens": set(tokens),
        "token_counts": Counter(tokens),
        "action_tokens": action_tokens,
        "object_tokens": object_tokens,
        "anchors": set(explainable_anchors(card)),
    }


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return (2.0 * len(left & right)) / (len(left) + len(right))


def _containment(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _optional_similarity(left: set[str], right: set[str]) -> float | None:
    if not left or not right:
        return None
    return _dice(left, right)


def semantic_similarity(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[float, dict[str, Any]]:
    left_parts = _semantic_parts(left)
    right_parts = _semantic_parts(right)
    shared_anchors = sorted(
        left_parts["anchors"] & right_parts["anchors"],
        key=lambda item: (-len(item), item.casefold()),
    )[:30]
    if (
        left.get("semantic_fingerprint")
        and left.get("semantic_fingerprint") == right.get("semantic_fingerprint")
    ):
        return 1.0, {
            "exact_fingerprint": True,
            "shared_core_tokens": shared_anchors,
            "shared_anchors": shared_anchors,
            "token_dice": 1.0,
            "token_containment": 1.0,
            "sequence_similarity": 1.0,
        }
    shared = left_parts["tokens"] & right_parts["tokens"]
    if len(shared) < 2:
        return 0.0, {
            "exact_fingerprint": False,
            "shared_core_tokens": shared_anchors,
            "shared_anchors": shared_anchors,
            "insufficient_shared_anchors": True,
        }
    dice = _dice(left_parts["tokens"], right_parts["tokens"])
    containment = _containment(left_parts["tokens"], right_parts["tokens"])
    sequence = SequenceMatcher(
        None, left_parts["identity"], right_parts["identity"]
    ).ratio()
    action = _optional_similarity(
        left_parts["action_tokens"], right_parts["action_tokens"]
    )
    object_similarity = _optional_similarity(
        left_parts["object_tokens"], right_parts["object_tokens"]
    )
    weighted = [(containment, 0.45), (dice, 0.30), (sequence, 0.15)]
    if action is not None:
        weighted.append((action, 0.06))
    if object_similarity is not None:
        weighted.append((object_similarity, 0.04))
    score = sum(value * weight for value, weight in weighted) / sum(
        weight for _value, weight in weighted
    )
    return round(score, 6), {
        "exact_fingerprint": False,
        "shared_core_tokens": shared_anchors,
        "shared_anchors": shared_anchors,
        "token_dice": round(dice, 6),
        "token_containment": round(containment, 6),
        "sequence_similarity": round(sequence, 6),
        "action_similarity": None if action is None else round(action, 6),
        "object_similarity": (
            None if object_similarity is None else round(object_similarity, 6)
        ),
        "left_phase_neutral_title": left_parts["title"],
        "right_phase_neutral_title": right_parts["title"],
    }


def _prefer_text(values: list[Any], *, limit: int = 12_000) -> str:
    normalized = _unique([_clean_text(value, limit=limit) for value in values])
    if not normalized:
        return ""
    return max(normalized, key=lambda item: (len(_identity(item)), len(item)))


def proposed_canonical_merge(
    left: dict[str, Any], right: dict[str, Any]
) -> dict[str, Any]:
    left_group = procedure_group(left)
    right_group = procedure_group(right)
    if not left_group or left_group != right_group:
        raise SemanticGovernanceError("不同执行阶段的 Procedure Knowledge 不允许合并")
    titles = [_phase_neutral_title(left.get("title")), _phase_neutral_title(right.get("title"))]
    operation = _prefer_text(
        [
            left.get("generalized_operation") or left.get("operation"),
            right.get("generalized_operation") or right.get("operation"),
            left.get("procedure_steps"),
            right.get("procedure_steps"),
        ]
    )
    title = _prefer_text(titles, limit=200) or "合并后的通用操作知识"
    return {
        "title": title,
        "summary": _prefer_text([left.get("summary"), right.get("summary")]),
        "generalized_operation": operation,
        "validation": _prefer_text(
            [
                left.get("validation") or left.get("validation_steps"),
                right.get("validation") or right.get("validation_steps"),
            ]
        ),
        "rollback": _prefer_text(
            [
                left.get("rollback") or left.get("rollback_steps"),
                right.get("rollback") or right.get("rollback_steps"),
            ]
        ),
        "impact_analysis": _prefer_text(
            [left.get("impact_analysis"), right.get("impact_analysis")]
        ),
        "risk_level": _prefer_text([left.get("risk_level"), right.get("risk_level")], limit=100),
        "risk_control": _prefer_text([left.get("risk_control"), right.get("risk_control")]),
        "scenario": _prefer_text([left.get("scenario"), right.get("scenario")], limit=1000),
        "object_type": _prefer_text([left.get("object_type"), right.get("object_type")], limit=200),
        "object_name": _prefer_text([left.get("object_name"), right.get("object_name")], limit=300),
        "applicable_versions": _unique(
            [*(left.get("applicable_versions") or []), *(right.get("applicable_versions") or [])]
        ),
        "prerequisites": _unique(
            [*(left.get("prerequisites") or []), *(right.get("prerequisites") or [])]
        ),
        "keywords": _unique([*(left.get("keywords") or []), *(right.get("keywords") or [])]),
        "applicable_phases": _unique(
            [left_group]
        ),
        "procedure_group": left_group,
        "source_card_ids": sorted((int(left["id"]), int(right["id"]))),
    }


def proposed_canonical_cluster(cards: list[dict[str, Any]]) -> dict[str, Any]:
    if len(cards) < 2:
        raise SemanticGovernanceError("canonical 知识簇至少需要两张来源卡片")
    groups = {procedure_group(card) for card in cards}
    if "" in groups or len(groups) != 1:
        raise SemanticGovernanceError("不同执行阶段的 Procedure Knowledge 不允许合并")
    group = next(iter(groups))
    operation = _prefer_text(
        [
            card.get("generalized_operation")
            or card.get("operation")
            or card.get("procedure_steps")
            for card in cards
        ]
    )
    return {
        "title": _prefer_text(
            [_phase_neutral_title(card.get("title")) for card in cards], limit=200
        )
        or "跨案例通用操作知识",
        "summary": _prefer_text([card.get("summary") for card in cards]),
        "generalized_operation": operation,
        "validation": _prefer_text(
            [card.get("validation") or card.get("validation_steps") for card in cards]
        ),
        "rollback": _prefer_text(
            [card.get("rollback") or card.get("rollback_steps") for card in cards]
        ),
        "impact_analysis": _prefer_text(
            [card.get("impact_analysis") for card in cards]
        ),
        "risk_level": _prefer_text(
            [card.get("risk_level") for card in cards], limit=100
        ),
        "risk_control": _prefer_text([card.get("risk_control") for card in cards]),
        "scenario": _prefer_text([card.get("scenario") for card in cards], limit=1000),
        "object_type": _prefer_text(
            [card.get("object_type") for card in cards], limit=200
        ),
        "object_name": _prefer_text(
            [card.get("object_name") for card in cards], limit=300
        ),
        "applicable_versions": _unique(
            [item for card in cards for item in (card.get("applicable_versions") or [])]
        ),
        "prerequisites": _unique(
            [item for card in cards for item in (card.get("prerequisites") or [])]
        ),
        "keywords": _unique(
            [item for card in cards for item in (card.get("keywords") or [])]
        ),
        "applicable_phases": [group],
        "procedure_group": group,
        "source_card_ids": sorted(int(card["id"]) for card in cards),
    }


def _member_differences(cards: list[dict[str, Any]]) -> dict[str, Any]:
    fields = {
        "title": lambda card: _phase_neutral_title(card.get("title")),
        "generalized_operation": lambda card: _clean_text(
            card.get("generalized_operation")
            or card.get("operation")
            or card.get("procedure_steps"),
            limit=1200,
        ),
        "validation": lambda card: _clean_text(
            card.get("validation") or card.get("validation_steps"), limit=800
        ),
        "rollback": lambda card: _clean_text(
            card.get("rollback") or card.get("rollback_steps"), limit=800
        ),
        "impact_analysis": lambda card: _clean_text(
            card.get("impact_analysis"), limit=800
        ),
        "object": lambda card: " / ".join(
            value
            for value in (
                _clean_text(card.get("object_type"), limit=120),
                _clean_text(card.get("object_name"), limit=160),
            )
            if value
        ),
    }
    differences: dict[str, Any] = {}
    for field, getter in fields.items():
        values = [getter(card) for card in cards]
        identities = {_identity(value) for value in values if value}
        if len(identities) <= 1:
            continue
        differences[field] = [
            {
                "card_id": int(card["id"]),
                "case_id": str(card.get("case_id") or ""),
                "value": value,
            }
            for card, value in zip(cards, values)
            if value
        ]
    return differences


class SemanticGovernanceError(RuntimeError):
    pass


class SemanticGovernanceService:
    def __init__(self, knowledge: "KnowledgeService") -> None:
        self.knowledge = knowledge
        self._scan_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.knowledge.settings.semantic_governance_enabled)

    @staticmethod
    def _eligible(card: dict[str, Any]) -> bool:
        return (
            card.get("status") == CardStatus.APPROVED.value
            and str(card.get("card_type") or "").upper() == "PROCEDURE_STEP"
            and str(card.get("publish_status") or "").upper() == "INDEXED"
            and card.get("retrieval_enabled") is not False
            and bool(str(card.get("semantic_fingerprint") or "").strip())
        )

    @staticmethod
    def _same_lineage(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_pointer = str(left.get("source_procedure_pointer") or "")
        right_pointer = str(right.get("source_procedure_pointer") or "")
        return bool(left_pointer and left_pointer == right_pointer)

    @staticmethod
    def _case_id(card: dict[str, Any]) -> str:
        return str(card.get("case_id") or "").strip()

    @classmethod
    def _same_case(cls, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_case = cls._case_id(left)
        right_case = cls._case_id(right)
        return bool(left_case and left_case == right_case)

    @staticmethod
    def _structured_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_parts = _semantic_parts(left)
        right_parts = _semantic_parts(right)
        left_objects = left_parts["object_tokens"]
        right_objects = right_parts["object_tokens"]
        if left_objects and right_objects and not (left_objects & right_objects):
            left_type = _identity(left.get("object_type"))
            right_type = _identity(right.get("object_type"))
            if left_type and right_type and left_type != right_type:
                return False
        left_actions = left_parts["action_tokens"]
        right_actions = right_parts["action_tokens"]
        if left_actions and right_actions and not (left_actions & right_actions):
            return False
        return True

    @classmethod
    def _build_complete_link_clusters(
        cls,
        candidates: list[
            tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]
        ],
    ) -> tuple[list[list[dict[str, Any]]], dict[tuple[int, int], tuple[float, dict[str, Any]]]]:
        edge_map: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}
        cards_by_id: dict[int, dict[str, Any]] = {}
        for score, left, right, signals in candidates:
            left_id, right_id = sorted((int(left["id"]), int(right["id"])))
            edge_map[(left_id, right_id)] = (score, signals)
            cards_by_id[left_id] = left if int(left["id"]) == left_id else right
            cards_by_id[right_id] = right if int(right["id"]) == right_id else left

        clusters: list[set[int]] = []

        def cluster_index(card_id: int) -> int | None:
            return next(
                (index for index, members in enumerate(clusters) if card_id in members),
                None,
            )

        def complete_link(member_ids: set[int]) -> bool:
            ordered = sorted(member_ids)
            cases = [cls._case_id(cards_by_id[card_id]) for card_id in ordered]
            if any(not case_id for case_id in cases) or len(cases) != len(set(cases)):
                return False
            return all(
                (left_id, right_id) in edge_map
                for left_index, left_id in enumerate(ordered)
                for right_id in ordered[left_index + 1 :]
            )

        for _score, left, right, _signals in sorted(
            candidates,
            key=lambda item: (item[0], -int(item[1]["id"]), -int(item[2]["id"])),
            reverse=True,
        ):
            left_id = int(left["id"])
            right_id = int(right["id"])
            left_cluster = cluster_index(left_id)
            right_cluster = cluster_index(right_id)
            if left_cluster is None and right_cluster is None:
                proposed = {left_id, right_id}
                if complete_link(proposed):
                    clusters.append(proposed)
                continue
            if left_cluster is not None and right_cluster is None:
                proposed = clusters[left_cluster] | {right_id}
                if complete_link(proposed):
                    clusters[left_cluster] = proposed
                continue
            if left_cluster is None and right_cluster is not None:
                proposed = clusters[right_cluster] | {left_id}
                if complete_link(proposed):
                    clusters[right_cluster] = proposed
                continue
            if left_cluster == right_cluster:
                continue
            assert left_cluster is not None and right_cluster is not None
            proposed = clusters[left_cluster] | clusters[right_cluster]
            if complete_link(proposed):
                keep, remove = sorted((left_cluster, right_cluster))
                clusters[keep] = proposed
                clusters.pop(remove)

        return (
            [
                [cards_by_id[card_id] for card_id in sorted(member_ids)]
                for member_ids in clusters
                if len(member_ids) >= 2
            ],
            edge_map,
        )

    @staticmethod
    def _pair_key(left: dict[str, Any], right: dict[str, Any]) -> str:
        left_id, right_id = sorted((int(left["id"]), int(right["id"])))
        return content_hash(
            {
                "algorithm_version": ALGORITHM_VERSION,
                "left_card_id": left_id,
                "right_card_id": right_id,
                "left_fingerprint": (
                    left["semantic_fingerprint"]
                    if int(left["id"]) == left_id
                    else right["semantic_fingerprint"]
                ),
                "right_fingerprint": (
                    right["semantic_fingerprint"]
                    if int(right["id"]) == right_id
                    else left["semantic_fingerprint"]
                ),
                "procedure_group": procedure_group(left),
            }
        )

    def scan(self, *, actor: str = "semantic-governance-worker") -> dict[str, Any]:
        if not self.enabled:
            raise SemanticGovernanceError(
                "相似知识治理未启用，请设置 SEMANTIC_GOVERNANCE_ENABLED=true"
            )
        if not self._scan_lock.acquire(blocking=False):
            raise SemanticGovernanceError("相似知识扫描正在运行")
        started_at = utc_now()
        started = time.monotonic()
        try:
            cards = [
                card
                for card in self.knowledge.store.list_semantic_governance_cards(
                    limit=self.knowledge.settings.semantic_scan_max_cards
                )
                if self._eligible(card) and self._case_id(card)
            ]
            eligible_fingerprints = {
                int(card["id"]): str(card["semantic_fingerprint"]) for card in cards
            }
            eligible_groups = {
                int(card["id"]): procedure_group(card) for card in cards
            }
            eligible_case_ids = {
                int(card["id"]): self._case_id(card) for card in cards
            }
            stale_legacy_count = self.knowledge.store.stale_semantic_merge_proposals(
                eligible_fingerprints,
                eligible_groups=eligible_groups,
                eligible_case_ids=eligible_case_ids,
                algorithm_version=ALGORITHM_VERSION,
            )
            candidates: list[
                tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]
            ] = []
            cross_phase_relations: list[
                tuple[int, int, float, str]
            ] = []
            comparisons = 0
            same_case_skipped = 0
            structured_conflict_skipped = 0
            for left_index, left in enumerate(cards):
                for right in cards[left_index + 1 :]:
                    if self._same_case(left, right):
                        same_case_skipped += 1
                        continue
                    if self._same_lineage(left, right):
                        continue
                    comparisons += 1
                    score, signals = semantic_similarity(left, right)
                    if score >= self.knowledge.settings.semantic_proposal_min_score:
                        left_group = procedure_group(left)
                        right_group = procedure_group(right)
                        signals["left_procedure_group"] = left_group
                        signals["right_procedure_group"] = right_group
                        signals["phase_merge_compatible"] = bool(
                            left_group and left_group == right_group
                        )
                        if left_group and right_group and left_group != right_group:
                            anchors = "、".join(
                                signals.get("shared_anchors") or []
                            )
                            reason = (
                                f"跨阶段相似：{left_group} 与 {right_group}；"
                                "保留独立阶段知识，不合并 semantic identity"
                            )
                            if anchors:
                                reason += f"；共享锚点：{anchors}"
                            cross_phase_relations.append(
                                (
                                    min(int(left["id"]), int(right["id"])),
                                    max(int(left["id"]), int(right["id"])),
                                    score,
                                    reason,
                                )
                            )
                            continue
                        if left_group and left_group == right_group:
                            if not self._structured_compatible(left, right):
                                structured_conflict_skipped += 1
                                continue
                            candidates.append((score, left, right, signals))
            self.knowledge.store.replace_semantic_governance_relations(
                "CROSS_PHASE_SIMILAR",
                cross_phase_relations,
                scope_card_ids=[int(card["id"]) for card in cards],
            )
            raw_clusters, edge_map = self._build_complete_link_clusters(candidates)
            cluster_specs: list[dict[str, Any]] = []
            for cluster_cards in raw_clusters:
                member_ids = sorted(int(card["id"]) for card in cluster_cards)
                pair_scores = [
                    edge_map[(left_id, right_id)][0]
                    for left_index, left_id in enumerate(member_ids)
                    for right_id in member_ids[left_index + 1 :]
                ]
                average_by_member = {
                    card_id: sum(
                        edge_map[tuple(sorted((card_id, other_id)))][0]
                        for other_id in member_ids
                        if other_id != card_id
                    )
                    / (len(member_ids) - 1)
                    for card_id in member_ids
                }
                representative_id = max(
                    member_ids,
                    key=lambda card_id: (average_by_member[card_id], -card_id),
                )
                cards_by_id = {int(card["id"]): card for card in cluster_cards}
                anchor_sets = [set(explainable_anchors(card)) for card in cluster_cards]
                shared_anchors = sorted(
                    set.intersection(*anchor_sets) if anchor_sets else set(),
                    key=lambda item: (-len(item), item.casefold()),
                )[:30]
                fingerprints = {
                    str(card.get("semantic_fingerprint") or "")
                    for card in cluster_cards
                }
                recommended_mode = (
                    "FULL_EQUIVALENT" if len(fingerprints) == 1 else "SHARED_CORE"
                )
                cluster_key = content_hash(
                    {
                        "algorithm_version": ALGORITHM_VERSION,
                        "card_type": "PROCEDURE_STEP",
                        "procedure_group": procedure_group(cluster_cards[0]),
                        "members": [
                            {
                                "card_id": card_id,
                                "case_id": self._case_id(cards_by_id[card_id]),
                                "semantic_fingerprint": str(
                                    cards_by_id[card_id]["semantic_fingerprint"]
                                ),
                            }
                            for card_id in member_ids
                        ],
                    }
                )
                reason = (
                    f"跨 {len(member_ids)} 个案例包的同阶段 Procedure Knowledge 形成"
                    " complete-link 知识簇"
                )
                if shared_anchors:
                    reason += f"；共享锚点：{'、'.join(shared_anchors)}"
                cluster_specs.append(
                    {
                        "cluster_id": f"SMC-{cluster_key[:16].upper()}",
                        "cluster_key": cluster_key,
                        "cards": cluster_cards,
                        "representative_card_id": representative_id,
                        "minimum_score": min(pair_scores),
                        "average_score": sum(pair_scores) / len(pair_scores),
                        "maximum_score": max(pair_scores),
                        "shared_anchors": shared_anchors,
                        "member_differences": _member_differences(cluster_cards),
                        "reason": reason,
                        "proposed_merge": proposed_canonical_cluster(cluster_cards),
                        "recommended_merge_mode": recommended_mode,
                        "average_by_member": average_by_member,
                    }
                )
            active_cluster_keys = {
                str(spec["cluster_key"]) for spec in cluster_specs
            }
            stale_cluster_count = self.knowledge.store.stale_semantic_merge_clusters(
                active_cluster_keys,
                eligible_fingerprints,
                eligible_case_ids,
                algorithm_version=ALGORITHM_VERSION,
            )
            cluster_specs.sort(
                key=lambda item: (
                    float(item["average_score"]),
                    len(item["cards"]),
                    str(item["cluster_id"]),
                ),
                reverse=True,
            )
            selected = cluster_specs[: self.knowledge.settings.semantic_scan_max_proposals]
            created_count = 0
            refreshed_count = 0
            pending_ids: list[str] = []
            for spec in selected:
                representative_id = int(spec["representative_card_id"])
                proposal, created = self.knowledge.store.upsert_semantic_merge_cluster(
                    cluster_id=str(spec["cluster_id"]),
                    cluster_key=str(spec["cluster_key"]),
                    card_type="PROCEDURE_STEP",
                    procedure_group=procedure_group(spec["cards"][0]),
                    algorithm_version=ALGORITHM_VERSION,
                    representative_card_id=representative_id,
                    minimum_score=float(spec["minimum_score"]),
                    average_score=float(spec["average_score"]),
                    maximum_score=float(spec["maximum_score"]),
                    shared_anchors=list(spec["shared_anchors"]),
                    member_differences=dict(spec["member_differences"]),
                    reason=str(spec["reason"]),
                    proposed_merge=dict(spec["proposed_merge"]),
                    recommended_merge_mode=str(spec["recommended_merge_mode"]),
                    members=[
                        {
                            "card_id": int(card["id"]),
                            "case_id": self._case_id(card),
                            "semantic_fingerprint": str(card["semantic_fingerprint"]),
                            "representative_score": (
                                1.0
                                if int(card["id"]) == representative_id
                                else edge_map[
                                    tuple(sorted((int(card["id"]), representative_id)))
                                ][0]
                            ),
                            "signals": (
                                {"representative": True}
                                if int(card["id"]) == representative_id
                                else edge_map[
                                    tuple(sorted((int(card["id"]), representative_id)))
                                ][1]
                            ),
                        }
                        for card in spec["cards"]
                    ],
                )
                created_count += int(created)
                refreshed_count += int(not created)
                if proposal["status"] in {"PENDING_REVIEW", "DEFERRED"}:
                    pending_ids.append(str(proposal["cluster_id"]))
            report = {
                "status": "COMPLETED",
                "algorithm_version": ALGORITHM_VERSION,
                "actor": actor,
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "eligible_card_count": len(cards),
                "pair_comparison_count": comparisons,
                "same_case_pair_skipped_count": same_case_skipped,
                "structured_conflict_skipped_count": structured_conflict_skipped,
                "pair_candidate_count": len(candidates),
                "candidate_count": len(cluster_specs),
                "cluster_candidate_count": len(cluster_specs),
                "cross_phase_relation_count": len(cross_phase_relations),
                "selected_candidate_count": len(selected),
                "created_proposal_count": created_count,
                "created_cluster_count": created_count,
                "refreshed_proposal_count": refreshed_count,
                "refreshed_cluster_count": refreshed_count,
                "stale_proposal_count": stale_legacy_count + stale_cluster_count,
                "stale_legacy_proposal_count": stale_legacy_count,
                "stale_cluster_count": stale_cluster_count,
                "pending_proposal_ids": pending_ids,
                "minimum_score": self.knowledge.settings.semantic_proposal_min_score,
                "model_content_export": False,
            }
            self.knowledge.store.save_semantic_scan_report(report)
            self.knowledge.trace.log("semantic_governance_scan_completed", **report)
            return report
        except Exception as exc:
            report = {
                "status": "FAILED",
                "algorithm_version": ALGORITHM_VERSION,
                "actor": actor,
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "error": str(exc),
                "model_content_export": False,
            }
            self.knowledge.store.save_semantic_scan_report(report)
            self.knowledge.trace.log("semantic_governance_scan_failed", **report)
            raise
        finally:
            self._scan_lock.release()

    @staticmethod
    def _card_projection(card: dict[str, Any] | None) -> dict[str, Any] | None:
        if card is None:
            return None
        fields = (
            "id",
            "title",
            "summary",
            "status",
            "card_type",
            "dedup_status",
            "publish_status",
            "retrieval_enabled",
            "source_name",
            "source_ref",
            "source_document_id",
            "scenario",
            "object_type",
            "object_name",
            "applicable_versions",
            "prerequisites",
            "procedure_steps",
            "risks",
            "rollback_steps",
            "validation_steps",
            "keywords",
            "generalized_operation",
            "validation",
            "rollback",
            "impact_analysis",
            "risk_level",
            "risk_control",
            "applicable_phases",
            "procedure_group",
            "semantic_fingerprint",
            "source_procedure_pointer",
            "case_id",
            "case_title",
        )
        return {field: card.get(field) for field in fields}

    def _enrich(self, proposal: dict[str, Any]) -> dict[str, Any]:
        result = dict(proposal)
        result["proposal_type"] = "PAIR"
        result["left_card"] = self._card_projection(
            self.knowledge.store.get_card(int(proposal["left_card_id"]))
        )
        result["right_card"] = self._card_projection(
            self.knowledge.store.get_card(int(proposal["right_card_id"]))
        )
        canonical_id = proposal.get("canonical_card_id")
        result["canonical_card"] = self._card_projection(
            self.knowledge.store.get_card(int(canonical_id))
            if canonical_id is not None
            else None
        )
        return result

    def _enrich_cluster(self, cluster: dict[str, Any]) -> dict[str, Any]:
        result = dict(cluster)
        result["proposal_id"] = str(cluster["cluster_id"])
        result["proposal_type"] = "CLUSTER"
        result["similarity_score"] = float(cluster.get("average_score") or 0.0)
        result["signals"] = {
            "shared_anchors": list(cluster.get("shared_anchors") or []),
            "shared_core_tokens": list(cluster.get("shared_anchors") or []),
            "minimum_score": cluster.get("minimum_score"),
            "average_score": cluster.get("average_score"),
            "maximum_score": cluster.get("maximum_score"),
            "complete_link": True,
        }
        member_cards: list[dict[str, Any]] = []
        for member in cluster.get("members") or []:
            projection = self._card_projection(
                self.knowledge.store.get_card(int(member["card_id"]))
            )
            if projection is not None:
                projection["case_id"] = str(member.get("case_id") or "")
                projection["representative_score"] = member.get(
                    "representative_score"
                )
                projection["is_representative"] = int(member["card_id"]) == int(
                    cluster["representative_card_id"]
                )
            member_cards.append(projection or {"id": int(member["card_id"]), "missing": True})
        result["member_cards"] = member_cards
        result["left_card"] = member_cards[0] if member_cards else None
        result["right_card"] = member_cards[1] if len(member_cards) > 1 else None
        result["left_card_id"] = (
            int(member_cards[0]["id"]) if member_cards else None
        )
        result["right_card_id"] = (
            int(member_cards[1]["id"]) if len(member_cards) > 1 else None
        )
        canonical_id = cluster.get("canonical_card_id")
        result["canonical_card"] = self._card_projection(
            self.knowledge.store.get_card(int(canonical_id))
            if canonical_id is not None
            else None
        )
        return result

    def list_proposals(
        self, *, status: str = "PENDING_REVIEW", limit: int = 100
    ) -> dict[str, Any]:
        normalized = status.strip().upper() or "PENDING_REVIEW"
        if normalized != "ALL" and normalized not in PROPOSAL_STATUSES:
            raise SemanticGovernanceError("未知的相似知识候选状态")
        clusters = self.knowledge.store.list_semantic_merge_clusters(
            status=normalized, limit=limit
        )
        legacy = self.knowledge.store.list_semantic_merge_proposals(
            status=normalized, limit=limit
        )
        proposals = [self._enrich_cluster(item) for item in clusters]
        proposals.extend(self._enrich(item) for item in legacy)
        proposals.sort(
            key=lambda item: (
                {"PENDING_REVIEW": 0, "DEFERRED": 1}.get(
                    str(item.get("status") or ""), 2
                ),
                -float(item.get("similarity_score") or 0.0),
                str(item.get("updated_at") or ""),
            ),
        )
        proposals = proposals[: max(1, min(int(limit), 500))]
        cluster_counts = self.knowledge.store.semantic_merge_cluster_counts()
        legacy_counts = self.knowledge.store.semantic_merge_proposal_counts()
        counts = {
            status_name: cluster_counts.get(status_name, 0)
            + legacy_counts.get(status_name, 0)
            for status_name in set(cluster_counts) | set(legacy_counts)
        }
        return {
            "enabled": self.enabled,
            "proposals": proposals,
            "clusters": [self._enrich_cluster(item) for item in clusters],
            "counts": counts,
            "last_scan": self.knowledge.store.get_semantic_scan_report(),
            "policy": {
                "automatic_merge": False,
                "same_case_merge": False,
                "cross_phase_merge": False,
                "cross_phase_relation": "CROSS_PHASE_SIMILAR",
                "governance_unit": "CASE_AWARE_COMPLETE_LINK_CLUSTER",
                "merge_modes": ["FULL_EQUIVALENT", "SHARED_CORE"],
                "model_content_export": False,
                "minimum_score": self.knowledge.settings.semantic_proposal_min_score,
                "algorithm_version": ALGORITHM_VERSION,
            },
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        cluster = self.knowledge.store.get_semantic_merge_cluster(proposal_id)
        if cluster is not None:
            return self._enrich_cluster(cluster)
        proposal = self.knowledge.store.get_semantic_merge_proposal(proposal_id)
        return self._enrich(proposal) if proposal is not None else None

    @staticmethod
    def _canonical_payload(
        proposal: dict[str, Any], overrides: Any
    ) -> dict[str, Any]:
        base = dict(proposal.get("proposed_merge") or {})
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise SemanticGovernanceError("merged_card 必须是 JSON 对象")
            allowed = {
                "title",
                "summary",
                "generalized_operation",
                "validation",
                "rollback",
                "impact_analysis",
                "risk_level",
                "risk_control",
                "scenario",
                "object_type",
                "object_name",
                "applicable_versions",
                "prerequisites",
                "keywords",
                "applicable_phases",
                "procedure_group",
            }
            unknown = sorted(set(overrides) - allowed)
            if unknown:
                raise SemanticGovernanceError(f"merged_card 包含未知字段: {unknown}")
            base.update(overrides)
        for field in (
            "title",
            "summary",
            "generalized_operation",
            "validation",
            "rollback",
            "impact_analysis",
            "risk_level",
            "risk_control",
            "scenario",
            "object_type",
            "object_name",
        ):
            base[field] = _clean_text(base.get(field), limit=12_000)
        for field in (
            "applicable_versions",
            "prerequisites",
            "keywords",
            "applicable_phases",
        ):
            base[field] = _text_list(base.get(field))
        base["procedure_group"] = normalize_procedure_group(
            base.get("procedure_group")
        )
        normalized_phases = {
            phase
            for item in base["applicable_phases"]
            if (phase := normalize_procedure_group(item))
        }
        if len(normalized_phases) != 1:
            raise SemanticGovernanceError(
                "canonical Procedure Knowledge 必须且只能属于一个执行阶段"
            )
        canonical_group = next(iter(normalized_phases))
        if base["procedure_group"] and base["procedure_group"] != canonical_group:
            raise SemanticGovernanceError("procedure_group 与 applicable_phases 不一致")
        base["procedure_group"] = canonical_group
        base["applicable_phases"] = [canonical_group]
        if not base["title"]:
            raise SemanticGovernanceError("合并后的 canonical 卡标题不能为空")
        if not base["generalized_operation"]:
            raise SemanticGovernanceError("合并后的 canonical 卡缺少通用化操作")
        for field in (
            "summary",
            "generalized_operation",
            "validation",
            "rollback",
            "impact_analysis",
        ):
            if _RAW_JSON.search(base[field]):
                raise SemanticGovernanceError(f"canonical 卡字段 {field} 含 raw JSON")
        if not base["summary"]:
            base["summary"] = base["generalized_operation"][:600]
        source_card_ids = proposal.get("member_card_ids")
        if not source_card_ids:
            source_card_ids = (proposal["left_card_id"], proposal["right_card_id"])
        base["source_card_ids"] = sorted(int(card_id) for card_id in source_card_ids)
        return base

    def _accept_cluster(
        self,
        cluster: dict[str, Any],
        *,
        actor: str,
        comment: str,
        merged_card: Any,
        merge_mode: str,
    ) -> dict[str, Any]:
        # Existing API callers did not send merge_mode and historically meant a
        # full merge. The new UI always sends an explicit mode.
        normalized_mode = str(merge_mode or "FULL_EQUIVALENT").strip().upper()
        if normalized_mode not in {"FULL_EQUIVALENT", "SHARED_CORE"}:
            raise SemanticGovernanceError(
                "知识簇确认方式必须是 FULL_EQUIVALENT 或 SHARED_CORE"
            )
        member_cards: list[dict[str, Any]] = []
        source_case_ids: list[str] = []
        for member in cluster.get("members") or []:
            card = self.knowledge.store.get_card(int(member["card_id"]))
            if card is None:
                raise SemanticGovernanceError("知识簇来源卡片已不存在")
            card["case_id"] = str(member.get("case_id") or "")
            member_cards.append(card)
            source_case_ids.append(str(member.get("case_id") or ""))
        if len(member_cards) < 2:
            raise SemanticGovernanceError("知识簇至少需要两张来源卡片")
        if any(not case_id for case_id in source_case_ids) or len(source_case_ids) != len(
            set(source_case_ids)
        ):
            raise SemanticGovernanceError("同一案例包最多只能贡献一张知识簇成员卡")
        groups = {procedure_group(card) for card in member_cards}
        if "" in groups or len(groups) != 1:
            raise SemanticGovernanceError(
                "不同或未知执行阶段的 Procedure Knowledge 不允许合并"
            )
        payload = self._canonical_payload(cluster, merged_card)
        group = next(iter(groups))
        if payload["procedure_group"] != group:
            raise SemanticGovernanceError("canonical 卡不得改变来源知识的执行阶段")
        source_card_ids = payload["source_card_ids"]
        source_content = "\n".join(
            [
                f"跨案例相似知识簇：{cluster['cluster_id']}",
                f"治理方式：{normalized_mode}",
                f"来源案例数：{len(source_case_ids)}",
                f"来源卡片：{', '.join('K' + str(item) for item in source_card_ids)}",
                f"标题：{payload['title']}",
                f"摘要：{payload['summary']}",
                f"通用化操作：{payload['generalized_operation']}",
                f"验证：{payload['validation']}",
                f"回退：{payload['rollback']}",
                f"影响分析：{payload['impact_analysis']}",
            ]
        )
        fingerprint = content_hash(
            {
                "card_type": "PROCEDURE_STEP",
                "title": _identity(payload["title"]),
                "generalized_operation": _identity(payload["generalized_operation"]),
                "validation": _identity(payload["validation"]),
                "rollback": _identity(payload["rollback"]),
                "impact_analysis": _identity(payload["impact_analysis"]),
                "procedure_group": payload["procedure_group"],
            }
        )
        draft = KnowledgeCardDraft(
            title=payload["title"],
            summary=payload["summary"],
            knowledge_type="procedure",
            scenario=payload["scenario"],
            object_type=payload["object_type"],
            object_name=payload["object_name"],
            applicable_versions=payload["applicable_versions"],
            prerequisites=payload["prerequisites"],
            procedure_steps=[payload["generalized_operation"]],
            risks=_unique([payload["impact_analysis"], payload["risk_control"]]),
            rollback_steps=_text_list(payload["rollback"]),
            validation_steps=_text_list(payload["validation"]),
            keywords=payload["keywords"],
            evidence_quote=payload["summary"],
        )
        semantic_payload = {
            **payload,
            "canonical": True,
            "merge_cluster_id": str(cluster["cluster_id"]),
            "merge_mode": normalized_mode,
            "case_evidence_card_ids": source_card_ids,
            "source_case_ids": source_case_ids,
            "instance_parameters": {},
            "inferred_facts": [],
        }
        result = self.knowledge.store.accept_semantic_merge_cluster(
            str(cluster["cluster_id"]),
            actor=actor,
            comment=comment,
            merge_mode=normalized_mode,
            draft=draft,
            semantic_metadata={
                "card_type": "PROCEDURE_STEP",
                "card_model_version": CANONICAL_CARD_MODEL_VERSION,
                "dedup_status": "CANONICAL",
                "publish_status": "INDEXED",
                "content_quality": 95.0,
                "semantic_fingerprint": fingerprint,
                "semantic_payload": semantic_payload,
            },
            source_content=source_content,
            source_checksum=content_hash(
                {
                    "cluster_id": cluster["cluster_id"],
                    "merge_mode": normalized_mode,
                    "canonical_payload": payload,
                    "source_fingerprints": [
                        str(member["semantic_fingerprint"])
                        for member in cluster.get("members") or []
                    ],
                }
            ),
        )
        canonical = self.knowledge.store.get_card(int(result["canonical_card_id"]))
        memory_sync: dict[str, Any] | None = None
        cleanup: dict[str, Any] | None = None
        if canonical is not None:
            try:
                memory_sync = self.knowledge.memory.sync_card(canonical)
            except Exception as exc:
                memory_sync = {"status": "FAILED", "error": str(exc)}
        if result.get("retired_memory_ids") and self.knowledge.memory.configured:
            try:
                cleanup = self.knowledge.memory.cleanup_retired_memories(
                    limit=len(result["retired_memory_ids"])
                )
            except Exception as exc:
                cleanup = {"status": "FAILED", "error": str(exc)}
        enriched = self._enrich_cluster(result)
        enriched["memory_sync"] = memory_sync
        enriched["memory_retirement_cleanup"] = cleanup
        self.knowledge.trace.log(
            "semantic_governance_cluster_accepted",
            cluster_id=cluster["cluster_id"],
            actor=actor,
            merge_mode=normalized_mode,
            canonical_card_id=result["canonical_card_id"],
            source_card_ids=source_card_ids,
            source_case_ids=source_case_ids,
        )
        return enriched

    def accept(
        self,
        proposal_id: str,
        *,
        actor: str,
        comment: str,
        merged_card: Any = None,
        merge_mode: str = "",
    ) -> dict[str, Any]:
        cluster = self.knowledge.store.get_semantic_merge_cluster(proposal_id)
        if cluster is not None:
            return self._accept_cluster(
                cluster,
                actor=actor,
                comment=comment,
                merged_card=merged_card,
                merge_mode=merge_mode,
            )
        proposal = self.knowledge.store.get_semantic_merge_proposal(proposal_id)
        if proposal is None:
            raise SemanticGovernanceError("相似知识候选不存在")
        left = self.knowledge.store.get_card(int(proposal["left_card_id"]))
        right = self.knowledge.store.get_card(int(proposal["right_card_id"]))
        left_group = procedure_group(left or {})
        right_group = procedure_group(right or {})
        if not left_group or left_group != right_group:
            raise SemanticGovernanceError(
                "不同或未知执行阶段的 Procedure Knowledge 不允许合并；"
                "请保留独立卡片并使用 CROSS_PHASE_SIMILAR 关系"
            )
        payload = self._canonical_payload(proposal, merged_card)
        if payload["procedure_group"] != left_group:
            raise SemanticGovernanceError("canonical 卡不得改变来源知识的执行阶段")
        source_card_ids = payload["source_card_ids"]
        source_content = "\n".join(
            [
                f"相似知识人工合并提案：{proposal_id}",
                f"来源卡片：{', '.join('K' + str(item) for item in source_card_ids)}",
                f"标题：{payload['title']}",
                f"摘要：{payload['summary']}",
                f"通用化操作：{payload['generalized_operation']}",
                f"验证：{payload['validation']}",
                f"回退：{payload['rollback']}",
                f"影响分析：{payload['impact_analysis']}",
            ]
        )
        fingerprint = content_hash(
            {
                "card_type": "PROCEDURE_STEP",
                "title": _identity(payload["title"]),
                "generalized_operation": _identity(payload["generalized_operation"]),
                "validation": _identity(payload["validation"]),
                "rollback": _identity(payload["rollback"]),
                "impact_analysis": _identity(payload["impact_analysis"]),
                "procedure_group": payload["procedure_group"],
            }
        )
        draft = KnowledgeCardDraft(
            title=payload["title"],
            summary=payload["summary"],
            knowledge_type="procedure",
            scenario=payload["scenario"],
            object_type=payload["object_type"],
            object_name=payload["object_name"],
            applicable_versions=payload["applicable_versions"],
            prerequisites=payload["prerequisites"],
            procedure_steps=[payload["generalized_operation"]],
            risks=_unique(
                [payload["impact_analysis"], payload["risk_control"]]
            ),
            rollback_steps=_text_list(payload["rollback"]),
            validation_steps=_text_list(payload["validation"]),
            keywords=payload["keywords"],
            evidence_quote=payload["summary"],
        )
        semantic_payload = {
            **payload,
            "canonical": True,
            "merge_proposal_id": proposal_id,
            "case_evidence_card_ids": source_card_ids,
            "instance_parameters": {},
            "inferred_facts": [],
        }
        result = self.knowledge.store.accept_semantic_merge_proposal(
            proposal_id,
            actor=actor,
            comment=comment,
            draft=draft,
            semantic_metadata={
                "card_type": "PROCEDURE_STEP",
                "card_model_version": CANONICAL_CARD_MODEL_VERSION,
                "dedup_status": "CANONICAL",
                "publish_status": "INDEXED",
                "content_quality": 95.0,
                "semantic_fingerprint": fingerprint,
                "semantic_payload": semantic_payload,
            },
            source_content=source_content,
            source_checksum=content_hash(
                {
                    "proposal_id": proposal_id,
                    "canonical_payload": payload,
                    "source_fingerprints": [
                        proposal["left_fingerprint"], proposal["right_fingerprint"]
                    ],
                }
            ),
        )
        canonical = self.knowledge.store.get_card(int(result["canonical_card_id"]))
        memory_sync: dict[str, Any] | None = None
        cleanup: dict[str, Any] | None = None
        if canonical is not None:
            try:
                memory_sync = self.knowledge.memory.sync_card(canonical)
            except Exception as exc:
                memory_sync = {"status": "FAILED", "error": str(exc)}
        if result.get("retired_memory_ids") and self.knowledge.memory.configured:
            try:
                cleanup = self.knowledge.memory.cleanup_retired_memories(
                    limit=len(result["retired_memory_ids"])
                )
            except Exception as exc:
                cleanup = {"status": "FAILED", "error": str(exc)}
        enriched = self._enrich(result)
        enriched["memory_sync"] = memory_sync
        enriched["memory_retirement_cleanup"] = cleanup
        self.knowledge.trace.log(
            "semantic_governance_merge_accepted",
            proposal_id=proposal_id,
            actor=actor,
            canonical_card_id=result["canonical_card_id"],
            source_card_ids=source_card_ids,
        )
        return enriched

    def decide(
        self,
        proposal_id: str,
        *,
        decision: str,
        actor: str,
        comment: str = "",
        merged_card: Any = None,
        merge_mode: str = "",
    ) -> dict[str, Any]:
        normalized = decision.strip().upper()
        if normalized == "ACCEPT":
            return self.accept(
                proposal_id,
                actor=actor,
                comment=comment,
                merged_card=merged_card,
                merge_mode=merge_mode,
            )
        if normalized not in {"REJECT", "DEFER"}:
            raise SemanticGovernanceError("决策必须是 ACCEPT、REJECT 或 DEFER")
        cluster = self.knowledge.store.get_semantic_merge_cluster(proposal_id)
        if cluster is not None:
            result = self.knowledge.store.decide_semantic_merge_cluster(
                proposal_id,
                decision=normalized,
                actor=actor,
                comment=comment,
            )
            enriched = self._enrich_cluster(result)
        else:
            result = self.knowledge.store.decide_semantic_merge_proposal(
                proposal_id,
                decision=normalized,
                actor=actor,
                comment=comment,
            )
            enriched = self._enrich(result)
        self.knowledge.trace.log(
            "semantic_governance_proposal_decided",
            proposal_id=proposal_id,
            actor=actor,
            decision=normalized,
        )
        return enriched


class SemanticGovernanceWorker:
    def __init__(
        self,
        governance: SemanticGovernanceService,
        *,
        interval_seconds: int,
        initial_delay_seconds: float = 3.0,
    ) -> None:
        self.governance = governance
        self.interval_seconds = max(int(interval_seconds), 60)
        self.initial_delay_seconds = max(float(initial_delay_seconds), 0.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if not self.governance.enabled or self.running:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="semantic-governance-worker",
            daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        self.request_stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(timeout_seconds, 0.0))
        return not self.running

    def _loop(self) -> None:
        if self._stop.wait(self.initial_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                print("[SEMANTIC GOVERNANCE] scan started")
                report = self.governance.scan()
                print(
                    "[SEMANTIC GOVERNANCE] scan completed "
                    f"eligible={report['eligible_card_count']} "
                    f"clusters={report['created_cluster_count']} "
                    f"same_case_skipped={report['same_case_pair_skipped_count']}"
                )
            except Exception as exc:
                print(f"[SEMANTIC GOVERNANCE] scan failed error={exc}")
            if self._stop.wait(self.interval_seconds):
                return
