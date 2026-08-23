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


ALGORITHM_VERSION = "semantic_candidate_v1"
CANONICAL_CARD_MODEL_VERSION = "semantic_merge_v1"
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
    if (
        left.get("semantic_fingerprint")
        and left.get("semantic_fingerprint") == right.get("semantic_fingerprint")
    ):
        return 1.0, {
            "exact_fingerprint": True,
            "shared_core_tokens": [],
            "token_dice": 1.0,
            "token_containment": 1.0,
            "sequence_similarity": 1.0,
        }
    left_parts = _semantic_parts(left)
    right_parts = _semantic_parts(right)
    shared = left_parts["tokens"] & right_parts["tokens"]
    if len(shared) < 2:
        return 0.0, {
            "exact_fingerprint": False,
            "shared_core_tokens": sorted(shared),
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
        "shared_core_tokens": sorted(shared)[:30],
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
            [*(left.get("applicable_phases") or []), *(right.get("applicable_phases") or [])]
        ),
        "source_card_ids": sorted((int(left["id"]), int(right["id"]))),
    }


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
                for card in self.knowledge.store.list_cards(
                    CardStatus.APPROVED,
                    limit=self.knowledge.settings.semantic_scan_max_cards,
                )
                if self._eligible(card)
            ]
            eligible_fingerprints = {
                int(card["id"]): str(card["semantic_fingerprint"]) for card in cards
            }
            stale_count = self.knowledge.store.stale_semantic_merge_proposals(
                eligible_fingerprints
            )
            candidates: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            comparisons = 0
            for left_index, left in enumerate(cards):
                for right in cards[left_index + 1 :]:
                    if self._same_lineage(left, right):
                        continue
                    comparisons += 1
                    score, signals = semantic_similarity(left, right)
                    if score >= self.knowledge.settings.semantic_proposal_min_score:
                        candidates.append((score, left, right, signals))
            candidates.sort(
                key=lambda item: (item[0], int(item[1]["id"]), int(item[2]["id"])),
                reverse=True,
            )
            selected = candidates[
                : self.knowledge.settings.semantic_scan_max_proposals
            ]
            created_count = 0
            refreshed_count = 0
            pending_ids: list[str] = []
            for score, first, second, signals in selected:
                left, right = (
                    (first, second)
                    if int(first["id"]) < int(second["id"])
                    else (second, first)
                )
                pair_key = self._pair_key(left, right)
                reason = (
                    "canonical semantic fingerprint 完全一致"
                    if signals.get("exact_fingerprint")
                    else (
                        "通用操作内容高度相似；共享锚点："
                        + "、".join(signals.get("shared_core_tokens") or [])
                    )
                )
                proposal, created = self.knowledge.store.upsert_semantic_merge_proposal(
                    proposal_id=f"SMP-{pair_key[:16].upper()}",
                    pair_key=pair_key,
                    left_card_id=int(left["id"]),
                    right_card_id=int(right["id"]),
                    left_fingerprint=str(left["semantic_fingerprint"]),
                    right_fingerprint=str(right["semantic_fingerprint"]),
                    algorithm_version=ALGORITHM_VERSION,
                    similarity_score=score,
                    signals=signals,
                    reason=reason,
                    proposed_merge=proposed_canonical_merge(left, right),
                )
                created_count += int(created)
                refreshed_count += int(not created)
                if proposal["status"] in {"PENDING_REVIEW", "DEFERRED"}:
                    pending_ids.append(str(proposal["proposal_id"]))
            report = {
                "status": "COMPLETED",
                "algorithm_version": ALGORITHM_VERSION,
                "actor": actor,
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "eligible_card_count": len(cards),
                "pair_comparison_count": comparisons,
                "candidate_count": len(candidates),
                "selected_candidate_count": len(selected),
                "created_proposal_count": created_count,
                "refreshed_proposal_count": refreshed_count,
                "stale_proposal_count": stale_count,
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
            "semantic_fingerprint",
            "source_procedure_pointer",
        )
        return {field: card.get(field) for field in fields}

    def _enrich(self, proposal: dict[str, Any]) -> dict[str, Any]:
        result = dict(proposal)
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

    def list_proposals(
        self, *, status: str = "PENDING_REVIEW", limit: int = 100
    ) -> dict[str, Any]:
        normalized = status.strip().upper() or "PENDING_REVIEW"
        if normalized != "ALL" and normalized not in PROPOSAL_STATUSES:
            raise SemanticGovernanceError("未知的相似知识候选状态")
        proposals = self.knowledge.store.list_semantic_merge_proposals(
            status=normalized, limit=limit
        )
        return {
            "enabled": self.enabled,
            "proposals": [self._enrich(item) for item in proposals],
            "counts": self.knowledge.store.semantic_merge_proposal_counts(),
            "last_scan": self.knowledge.store.get_semantic_scan_report(),
            "policy": {
                "automatic_merge": False,
                "model_content_export": False,
                "minimum_score": self.knowledge.settings.semantic_proposal_min_score,
                "algorithm_version": ALGORITHM_VERSION,
            },
        }

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
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
        base["source_card_ids"] = sorted(
            (int(proposal["left_card_id"]), int(proposal["right_card_id"]))
        )
        return base

    def accept(
        self,
        proposal_id: str,
        *,
        actor: str,
        comment: str,
        merged_card: Any = None,
    ) -> dict[str, Any]:
        proposal = self.knowledge.store.get_semantic_merge_proposal(proposal_id)
        if proposal is None:
            raise SemanticGovernanceError("相似知识候选不存在")
        payload = self._canonical_payload(proposal, merged_card)
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
    ) -> dict[str, Any]:
        normalized = decision.strip().upper()
        if normalized == "ACCEPT":
            return self.accept(
                proposal_id,
                actor=actor,
                comment=comment,
                merged_card=merged_card,
            )
        if normalized not in {"REJECT", "DEFER"}:
            raise SemanticGovernanceError("决策必须是 ACCEPT、REJECT 或 DEFER")
        result = self.knowledge.store.decide_semantic_merge_proposal(
            proposal_id,
            decision=normalized,
            actor=actor,
            comment=comment,
        )
        self.knowledge.trace.log(
            "semantic_governance_proposal_decided",
            proposal_id=proposal_id,
            actor=actor,
            decision=normalized,
        )
        return self._enrich(result)


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

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

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
                    f"proposals={report['created_proposal_count']}"
                )
            except Exception as exc:
                print(f"[SEMANTIC GOVERNANCE] scan failed error={exc}")
            if self._stop.wait(self.interval_seconds):
                return
