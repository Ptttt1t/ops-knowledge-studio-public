from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http import HTTPStatus
from pathlib import Path
import threading
from typing import Any
import unicodedata
from uuid import uuid4

from harness.api_client import APIError, DeepSeekClient
from harness.config import Settings
from harness.trace import TraceLogger

from .documents import (
    DocumentLimits,
    DocumentChunk,
    EvidenceSpan,
    SourceDocument,
    chunk_text,
    ground_evidence_quote,
)
from .change_order_adapter import (
    ChangeOrderExtractionPlan,
    ChangeOrderExtractionUnit,
    build_change_order_extraction_plan,
)
from .prompts import (
    ANSWER_SYSTEM_PROMPT,
    CHANGE_ORDER_EXTRACTION_SYSTEM_PROMPT,
    COMPARISON_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    answer_user_prompt,
    change_order_extraction_user_prompt,
    comparison_user_prompt,
    extraction_user_prompt,
)
from .long_term_memory import MindMemOSBridge, MindMemOSError
from .retrieval import HybridRetriever, SearchHit, tokenize
from .schema import CardStatus, ComparisonResult, KnowledgeCardDraft
from .safe_documents import read_document_safely
from .store import KnowledgeStore


class KnowledgeServiceError(RuntimeError):
    """Raised when a knowledge pipeline operation cannot be completed."""


class KnowledgeRequestError(KnowledgeServiceError):
    def __init__(self, message: str, *, status: int, code: str):
        super().__init__(message)
        self.status = int(status)
        self.code = code


@dataclass
class ModelCallBudget:
    maximum: int
    used: int = 0

    def consume(self, purpose: str) -> None:
        if self.used >= self.maximum:
            raise KnowledgeRequestError(
                f"单次知识导入模型调用预算已用尽（{self.maximum} 次）",
                status=HTTPStatus.TOO_MANY_REQUESTS,
                code="model_call_budget_exceeded",
            )
        self.used += 1


@dataclass(frozen=True)
class ExtractedCard:
    chunk: DocumentChunk
    draft: KnowledgeCardDraft
    evidence_span: EvidenceSpan | None
    quality_score: float
    quality_issues: list[str]
    comparison: ComparisonResult


class KnowledgeService:
    MAX_CARDS_PER_EXTRACTION = 5
    MAX_EXTRACTION_SPLIT_DEPTH = 2
    MAX_ANSWER_CLAIMS = 30
    ANSWER_CATEGORIES = {
        "适用条件",
        "执行步骤",
        "风险",
        "回退",
        "验证",
        "结论",
        "知识不足",
    }
    _SEMANTIC_INTENT_GROUPS = {
        "rollback": (
            ("回退", "撤销", "恢复", "还原", "回滚", "失败", "出问题"),
            ("rollback_steps",),
        ),
        "validation": (
            ("验证", "确认", "检查", "观测", "成功率", "丢包", "时延", "健康"),
            ("validation_steps",),
        ),
        "sequence": (
            ("顺序", "先后", "阶段", "波次", "灰度", "逐批", "分批", "哪边", "开始"),
            ("procedure_steps", "rollback_steps"),
        ),
        "execution": (
            ("迁移", "切换", "替换", "挪走", "换通道", "操作", "执行"),
            ("procedure_steps",),
        ),
        "prerequisite": (
            ("前置", "条件", "准备", "容量"),
            ("prerequisites",),
        ),
        "risk": (
            ("风险", "影响", "故障", "中断"),
            ("risks",),
        ),
    }
    _SEMANTIC_OBJECT_GROUPS = {
        "route": ("路由", "下一跳", "cidr"),
        "nat": ("nat", "出口", "egress"),
        "vpn": ("vpn", "隧道", "合作方", "合作伙伴"),
        "dns": ("dns", "解析", "域名"),
        "firewall": ("防火墙", "firewall", "服务链"),
        "kubernetes": ("kubernetes", "k8s", "容器", "集群"),
        "private_endpoint": ("privatelink", "私网终端", "私有入口", "终端节点"),
        "link": ("专线", "链路", "通道"),
        "database": ("数据库", "mysql", "postgresql", "主从", "索引"),
        "certificate": ("证书", "tls", "certificate", "密钥轮换"),
        "identity": ("账号", "权限", "iam", "用户授权", "登录"),
    }
    _GENERIC_SEMANTIC_TERMS = {
        "回退",
        "失败",
        "切换",
        "变更",
        "验证",
        "步骤",
        "操作",
        "问题",
        "异常",
        "开始",
        "如何",
        "怎么",
    }

    @classmethod
    def _semantic_relevance_gate(
        cls, query: str, card: dict[str, Any], *, minimum_anchors: int
    ) -> dict[str, Any]:
        query_lower = query.lower()
        card_text = " ".join(
            str(value)
            for field in HybridRetriever.FIELD_WEIGHTS
            for value in (
                card.get(field, [])
                if isinstance(card.get(field), list)
                else [card.get(field, "")]
            )
        ).lower()
        query_tokens = set(tokenize(query_lower))
        card_tokens = set(tokenize(card_text))
        lexical_terms = sorted(
            term
            for term in query_tokens & card_tokens
            if term not in cls._GENERIC_SEMANTIC_TERMS
        )
        intent_anchors: list[str] = []
        for name, (cues, fields) in cls._SEMANTIC_INTENT_GROUPS.items():
            if any(cue in query_lower for cue in cues) and any(
                card.get(field) for field in fields
            ):
                intent_anchors.append(name)
        query_object_groups: list[str] = []
        object_anchors: list[str] = []
        for name, cues in cls._SEMANTIC_OBJECT_GROUPS.items():
            if any(cue in query_lower for cue in cues):
                query_object_groups.append(name)
                if any(cue in card_text for cue in cues):
                    object_anchors.append(name)
        anchor_count = min(len(lexical_terms), 3) + len(intent_anchors) + len(
            object_anchors
        )
        minimum = max(1, minimum_anchors)
        unmatched_object_groups = set(query_object_groups) - set(object_anchors)
        return {
            "accepted": anchor_count >= minimum and not unmatched_object_groups,
            "anchor_count": anchor_count,
            "minimum_anchors": minimum,
            "lexical_terms": lexical_terms[:12],
            "intent_anchors": intent_anchors,
            "object_anchors": object_anchors,
            "query_object_groups": query_object_groups,
            "unmatched_object_groups": sorted(unmatched_object_groups),
            "query_coverage": (
                len(lexical_terms) / len(query_tokens) if query_tokens else 0.0
            ),
        }
    CLAIM_FIELD_CATEGORIES = {
        "summary": "结论",
        "scenario": "适用条件",
        "object_name": "适用条件",
        "applicable_versions": "适用条件",
        "prerequisites": "适用条件",
        "procedure_steps": "执行步骤",
        "risks": "风险",
        "rollback_steps": "回退",
        "validation_steps": "验证",
    }
    CLAIM_FIELD_LABELS = {
        "summary": "结论摘要",
        "scenario": "适用场景",
        "object_name": "适用对象",
        "applicable_versions": "适用版本",
        "prerequisites": "前置条件",
        "procedure_steps": "执行步骤",
        "risks": "风险说明",
        "rollback_steps": "回退步骤",
        "validation_steps": "验证步骤",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        store: KnowledgeStore | None = None,
        client: Any | None = None,
        trace: TraceLogger | None = None,
        memory_bridge: MindMemOSBridge | None = None,
    ):
        self.settings = settings
        self.store = store or KnowledgeStore(settings.database_path)
        self.store.initialize()
        self.client = client or DeepSeekClient(settings)
        self.trace = trace or TraceLogger(
            settings.project_root / "artifacts",
            retention_days=settings.trace_retention_days,
            max_files=settings.trace_max_files,
            hmac_key=settings.trace_hmac_key,
        )
        self.retriever = HybridRetriever(self.store)
        self.memory = memory_bridge or MindMemOSBridge(settings, self.store)
        self._ingestion_slots = threading.BoundedSemaphore(
            settings.max_concurrent_ingestions
        )

    def ingest_file(
        self, path: Path, *, source_name: str | None = None
    ) -> dict[str, Any]:
        document = read_document_safely(
            path,
            limits=DocumentLimits.from_settings(self.settings),
            timeout_seconds=self.settings.document_parse_timeout_seconds,
        )
        if source_name and source_name.strip():
            document = SourceDocument(
                name=source_name.strip(),
                source_type=document.source_type,
                source_ref=document.source_ref,
                content=document.content,
            )
        return self.ingest_document(document)

    def ingest_text(
        self,
        *,
        source_name: str,
        content: str,
        source_ref: str = "manual://web-input",
        source_type: str = "text",
    ) -> dict[str, Any]:
        name = source_name.strip() or "未命名文本"
        text = content.strip()
        if not text:
            raise KnowledgeServiceError("来源内容不能为空")
        return self.ingest_document(
            SourceDocument(
                name=name,
                source_type=source_type,
                source_ref=source_ref.strip() or "manual://web-input",
                content=text,
            )
        )

    def ingest_document(self, document: SourceDocument) -> dict[str, Any]:
        self.settings.require_api()
        json_candidate = document.source_type.lower() == "json" or document.content.lstrip().startswith(
            "{"
        )
        if len(document.content) > self.settings.max_text_chars:
            if (
                not json_candidate
                or len(document.content) > self.settings.max_change_order_json_chars
            ):
                raise KnowledgeRequestError(
                    f"文档文本超过 {self.settings.max_text_chars} 字符限制；"
                    "只有结构指纹匹配的变更单 JSON 可使用更高边界",
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    code="document_text_too_large",
                )
            preflight_plan, preflight_report = build_change_order_extraction_plan(
                document.content,
                chunk_size=self.settings.change_order_chunk_size,
            )
            if preflight_plan is None:
                raise KnowledgeRequestError(
                    "大 JSON 未通过变更单结构指纹识别，不能进入模型抽取："
                    + str(preflight_report.get("reason") or "结构未知"),
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    code="large_json_schema_not_recognized",
                )
        checksum = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
        existing = self.store.find_document_by_checksum(checksum)
        if existing is not None:
            extraction_report = self.store.get_extraction_report(int(existing["id"]))
            return {
                "document_id": existing["id"],
                "duplicate_document": True,
                "card_ids": self.store.card_ids_for_document(int(existing["id"])),
                "case_id": (
                    extraction_report.get("case_id")
                    if extraction_report is not None
                    else None
                ),
                "cards_by_role": (
                    extraction_report.get("cards_by_role", {})
                    if extraction_report is not None
                    else {}
                ),
                "extraction_strategy": (
                    extraction_report.get("strategy")
                    if extraction_report is not None
                    else "unknown"
                ),
                "extraction_report": extraction_report,
                "message": "相同内容已经导入，本次未重复调用模型。",
            }

        acquired = self._ingestion_slots.acquire(blocking=False)
        if not acquired:
            raise KnowledgeRequestError(
                "知识导入并发额度已满，请稍后重试",
                status=HTTPStatus.TOO_MANY_REQUESTS,
                code="ingestion_concurrency_exceeded",
            )
        owner_token = uuid4().hex
        try:
            claim = self.store.claim_ingestion(
                checksum,
                owner_token,
                lease_seconds=max(7200, self.settings.timeout_seconds * 10),
            )
            if claim["state"] == "COMPLETED":
                document_id = int(claim["document_id"])
                extraction_report = self.store.get_extraction_report(document_id)
                return {
                    "document_id": document_id,
                    "duplicate_document": True,
                    "card_ids": self.store.card_ids_for_document(document_id),
                    "case_id": (
                        extraction_report.get("case_id")
                        if extraction_report is not None
                        else None
                    ),
                    "cards_by_role": (
                        extraction_report.get("cards_by_role", {})
                        if extraction_report is not None
                        else {}
                    ),
                    "extraction_strategy": (
                        extraction_report.get("strategy")
                        if extraction_report is not None
                        else "unknown"
                    ),
                    "extraction_report": extraction_report,
                    "message": "相同内容已经导入，本次未重复调用模型。",
                }
            if claim["state"] == "PROCESSING":
                raise KnowledgeRequestError(
                    "相同文档正在处理，请等待当前导入完成",
                    status=HTTPStatus.CONFLICT,
                    code="ingestion_in_progress",
                )
            try:
                return self._ingest_claimed_document(
                    document,
                    checksum=checksum,
                    owner_token=owner_token,
                )
            except Exception as exc:
                self.store.fail_ingestion(checksum, owner_token, str(exc))
                raise
        finally:
            self._ingestion_slots.release()

    def _ingest_claimed_document(
        self,
        document: SourceDocument,
        *,
        checksum: str,
        owner_token: str,
    ) -> dict[str, Any]:

        change_order_plan: ChangeOrderExtractionPlan | None = None
        adapter_diagnostics: dict[str, Any] | None = None
        if document.source_type.lower() == "json" or document.content.lstrip().startswith(
            "{"
        ):
            change_order_plan, adapter_diagnostics = build_change_order_extraction_plan(
                document.content,
                chunk_size=self.settings.change_order_chunk_size,
            )
            if (
                document.source_type.lower() == "json"
                and adapter_diagnostics.get("valid_json") is False
            ):
                raise KnowledgeRequestError(
                    "JSON 格式无效或含重复 Key，不能可靠抽取："
                    + str(adapter_diagnostics.get("reason") or "解析失败"),
                    status=HTTPStatus.BAD_REQUEST,
                    code="invalid_or_ambiguous_json",
                )
            if (
                change_order_plan is None
                and adapter_diagnostics.get("possible_change_order") is True
            ):
                raise KnowledgeRequestError(
                    "JSON 疑似变更单，但关键结构缺失或候选不唯一，已在调用模型前阻断："
                    + str(adapter_diagnostics.get("reason") or "结构不确定"),
                    status=HTTPStatus.UNPROCESSABLE_ENTITY,
                    code="change_order_schema_ambiguous",
                )
        if change_order_plan is not None:
            chunks = [unit.chunk for unit in change_order_plan.units]
            extraction_units = {
                unit.chunk.index: unit for unit in change_order_plan.units
            }
            extraction_strategy = "change_order_shape_v1"
        else:
            chunks = chunk_text(
                document.content,
                self.settings.chunk_size,
                self.settings.chunk_overlap,
            )
            extraction_units = {}
            extraction_strategy = "generic_text_v1"

        def apply_structural_blockers(
            score: float, issues: list[str]
        ) -> tuple[float, list[str]]:
            if change_order_plan is None or change_order_plan.report.get(
                "safe_to_publish", False
            ):
                return score, issues
            updated = list(issues)
            updated.extend(
                f"阻断：{blocker}"
                for blocker in change_order_plan.report.get("blockers", [])
                if f"阻断：{blocker}" not in updated
            )
            return min(score, 64.0), updated

        chunk_limit = (
            self.settings.max_change_order_chunks
            if change_order_plan is not None
            else self.settings.max_document_chunks
        )
        if len(chunks) > chunk_limit:
            raise KnowledgeRequestError(
                f"文档分片数 {len(chunks)} 超过 {chunk_limit} 个限制",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="document_chunk_limit_exceeded",
            )
        budget = ModelCallBudget(self.settings.max_model_calls_per_ingest)
        extracted: list[ExtractedCard] = []
        batch_seen: dict[str, int] = {}
        batch_duplicates_skipped = 0
        self.trace.log(
            "knowledge_ingest_started",
            source_name=document.name,
            checksum=checksum,
            chunks=len(chunks),
            extraction_strategy=extraction_strategy,
            change_order_adapter=adapter_diagnostics,
        )

        for chunk in chunks:
            unit = extraction_units.get(chunk.index)
            for extracted_chunk, payload, usage, split_depth in self._extract_chunk(
                document.name, chunk, budget=budget, structural_unit=unit
            ):
                self.trace.log(
                    "knowledge_extraction_response",
                    source_name=document.name,
                    chunk_index=chunk.index,
                    char_start=extracted_chunk.char_start,
                    char_end=extracted_chunk.char_end,
                    split_depth=split_depth,
                    usage=usage,
                )
                if isinstance(payload, dict):
                    raw_cards = payload.get("knowledge_cards", [])
                else:
                    raw_cards = []
                if not isinstance(raw_cards, list):
                    raise KnowledgeServiceError("模型返回的 knowledge_cards 不是数组")

                card_limit = 1 if unit is not None else self.MAX_CARDS_PER_EXTRACTION
                if unit is not None and len(raw_cards) > card_limit:
                    self.trace.log(
                        "change_order_unit_card_limit_applied",
                        source_name=document.name,
                        chunk_index=chunk.index,
                        unit_role=unit.role,
                        returned_cards=len(raw_cards),
                        kept_cards=card_limit,
                    )
                for raw_card in raw_cards[:card_limit]:
                    draft = KnowledgeCardDraft.from_dict(raw_card)
                    evidence_span = ground_evidence_quote(
                        extracted_chunk.content, draft.evidence_quote
                    )
                    if evidence_span is not None:
                        draft.evidence_quote = evidence_span.quote
                    self.trace.log(
                        "knowledge_evidence_grounding",
                        source_name=document.name,
                        chunk_index=chunk.index,
                        title=draft.title,
                        grounded=evidence_span is not None,
                        match_method=(
                            evidence_span.match_method
                            if evidence_span is not None
                            else None
                        ),
                        similarity=(
                            round(evidence_span.similarity, 4)
                            if evidence_span is not None
                            else None
                        ),
                    )
                    batch_key = self._batch_card_key(draft)
                    if batch_key in batch_seen:
                        existing_index = batch_seen[batch_key]
                        existing_item = extracted[existing_index]
                        batch_duplicates_skipped += 1
                        score, issues = apply_structural_blockers(
                            *draft.quality(extracted_chunk.content)
                        )
                        replace_existing = (
                            evidence_span is not None,
                            score,
                        ) > (
                            existing_item.evidence_span is not None,
                            existing_item.quality_score,
                        )
                        if replace_existing:
                            extracted[existing_index] = ExtractedCard(
                                chunk=extracted_chunk,
                                draft=draft,
                                evidence_span=evidence_span,
                                quality_score=score,
                                quality_issues=issues,
                                comparison=existing_item.comparison,
                            )
                        self.trace.log(
                            "knowledge_batch_duplicate_skipped",
                            source_name=document.name,
                            title=draft.title,
                            chunk_index=chunk.index,
                            previous_chunk_index=existing_item.chunk.index,
                            kept_chunk_index=(
                                extracted_chunk.index
                                if replace_existing
                                else existing_item.chunk.index
                            ),
                            replaced_existing=replace_existing,
                            fingerprint=batch_key,
                        )
                        continue
                    batch_seen[batch_key] = len(extracted)
                    if len(extracted) >= self.settings.max_cards_per_document:
                        raise KnowledgeRequestError(
                            "单份文档生成的候选知识卡片超过限制",
                            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            code="document_card_limit_exceeded",
                        )
                    score, issues = apply_structural_blockers(
                        *draft.quality(extracted_chunk.content)
                    )
                    comparison = self._compare(draft, budget=budget)
                    extracted.append(
                        ExtractedCard(
                            chunk=extracted_chunk,
                            draft=draft,
                            evidence_span=evidence_span,
                            quality_score=score,
                            quality_issues=issues,
                            comparison=comparison,
                        )
                    )

        document_id, created = self.store.add_document(
            document.name,
            document.source_type,
            document.source_ref,
            checksum,
            document.content,
        )
        if not created:
            self.store.complete_ingestion(checksum, owner_token, document_id)
            extraction_report = self.store.get_extraction_report(document_id)
            return {
                "document_id": document_id,
                "duplicate_document": True,
                "card_ids": self.store.card_ids_for_document(document_id),
                "case_id": (
                    extraction_report.get("case_id")
                    if extraction_report is not None
                    else None
                ),
                "cards_by_role": (
                    extraction_report.get("cards_by_role", {})
                    if extraction_report is not None
                    else {}
                ),
                "extraction_strategy": (
                    extraction_report.get("strategy")
                    if extraction_report is not None
                    else "unknown"
                ),
                "extraction_report": extraction_report,
                "message": "相同内容已经导入。",
            }

        extraction_report: dict[str, Any] = {
            "strategy": extraction_strategy,
            "change_order": adapter_diagnostics,
            "chunks": len(chunks),
        }
        case_id = (
            f"change-order:{checksum}"
            if change_order_plan is not None
            else f"document:{checksum}"
        )
        extraction_report["case_id"] = case_id
        self.store.save_extraction_report(
            document_id, extraction_strategy, extraction_report
        )

        chunk_ids = {
            chunk.index: self.store.add_chunk(
                document_id,
                chunk.index,
                chunk.char_start,
                chunk.char_end,
                chunk.content,
            )
            for chunk in chunks
        }
        card_ids: list[int] = []
        cards_by_role: dict[str, int] = {}
        for item in extracted:
            status = (
                CardStatus.PENDING_REVIEW
                if item.quality_score >= 65 and item.evidence_span is not None
                else CardStatus.DRAFT
            )
            if item.evidence_span is not None:
                evidence_start = item.chunk.char_start + item.evidence_span.start
                evidence_end = item.chunk.char_start + item.evidence_span.end
                evidence_locator = (
                    f"{document.name}#chunk={item.chunk.index + 1};"
                    f"chars={evidence_start}-{evidence_end};"
                    f"match={item.evidence_span.match_method}"
                )
            else:
                evidence_locator = (
                    f"{document.name}#chunk={item.chunk.index + 1};"
                    f"chars={item.chunk.char_start}-{item.chunk.char_end};unverified"
                )
            card_id = self.store.add_card(
                item.draft,
                document_id=document_id,
                chunk_id=chunk_ids[item.chunk.index],
                evidence_locator=evidence_locator,
                status=status,
                quality_score=item.quality_score,
                quality_issues=item.quality_issues,
                comparison=item.comparison,
            )
            lineage_unit = extraction_units.get(item.chunk.index)
            if lineage_unit is not None:
                self.store.save_card_lineage(
                    card_id,
                    case_id=case_id,
                    extraction_strategy=extraction_strategy,
                    unit_role=lineage_unit.role,
                    unit_pointer=lineage_unit.pointer,
                    source_pointers=list(lineage_unit.source_pointers),
                    source_order=lineage_unit.chunk.index,
                )
                cards_by_role[lineage_unit.role] = (
                    cards_by_role.get(lineage_unit.role, 0) + 1
                )
            card_ids.append(card_id)

        extraction_report["generated_cards"] = len(card_ids)
        extraction_report["cards_by_role"] = cards_by_role
        self.store.save_extraction_report(
            document_id, extraction_strategy, extraction_report
        )

        result = {
            "document_id": document_id,
            "case_id": case_id,
            "duplicate_document": False,
            "chunks": len(chunks),
            "extraction_strategy": extraction_strategy,
            "extraction_report": extraction_report,
            "extracted_cards": len(card_ids),
            "cards_by_role": cards_by_role,
            "batch_duplicates_skipped": batch_duplicates_skipped,
            "card_ids": card_ids,
            "pending_review": sum(
                1
                for card_id in card_ids
                if self.store.get_card(card_id)["status"] == CardStatus.PENDING_REVIEW.value
            ),
            "model_calls": budget.used,
            "message": "知识抽取完成，正式发布前必须人工审核。",
        }
        self.store.complete_ingestion(checksum, owner_token, document_id)
        self.trace.log("knowledge_ingest_completed", **result)
        return result

    @staticmethod
    def _normalize_batch_value(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(character for character in normalized if character.isalnum())

    @classmethod
    def _batch_card_key(cls, draft: KnowledgeCardDraft) -> str:
        """Build a stable semantic fingerprint for cards extracted in one document."""

        scalar_fields = (
            draft.knowledge_type,
            draft.summary,
            draft.scenario,
            draft.object_type,
            draft.object_name,
        )
        list_fields = (
            sorted(draft.applicable_versions),
            sorted(draft.prerequisites),
            draft.procedure_steps,
            sorted(draft.risks),
            draft.rollback_steps,
            draft.validation_steps,
        )
        parts = [cls._normalize_batch_value(value) for value in scalar_fields]
        for values in list_fields:
            parts.append(
                "|".join(cls._normalize_batch_value(value) for value in values)
            )
        if not any(parts[1:]):
            parts.extend(
                cls._normalize_batch_value(value)
                for value in (draft.title, draft.evidence_quote)
            )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _split_extraction_chunk(chunk: DocumentChunk) -> list[DocumentChunk]:
        content = chunk.content
        midpoint = len(content) // 2
        lower = max(1, len(content) // 3)
        upper = min(len(content) - 1, (len(content) * 2) // 3)
        candidates = [
            boundary
            for boundary in (
                content.rfind("\n", lower, midpoint + 1),
                content.find("\n", midpoint, upper + 1),
            )
            if boundary > 0
        ]
        split_at = min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint
        parts: list[DocumentChunk] = []
        for relative_start, relative_end in ((0, split_at), (split_at, len(content))):
            raw = content[relative_start:relative_end]
            left_trim = len(raw) - len(raw.lstrip())
            right_trim = len(raw) - len(raw.rstrip())
            start = relative_start + left_trim
            end = relative_end - right_trim
            if end <= start:
                continue
            parts.append(
                DocumentChunk(
                    index=chunk.index,
                    char_start=chunk.char_start + start,
                    char_end=chunk.char_start + end,
                    content=content[start:end],
                )
            )
        return parts

    def _extract_chunk(
        self,
        source_name: str,
        chunk: DocumentChunk,
        *,
        split_depth: int = 0,
        budget: ModelCallBudget | None = None,
        structural_unit: ChangeOrderExtractionUnit | None = None,
    ) -> list[tuple[DocumentChunk, Any, dict[str, Any] | None, int]]:
        budget = budget or ModelCallBudget(self.settings.max_model_calls_per_ingest)
        locator = f"字符 {chunk.char_start}-{chunk.char_end}"
        try:
            budget.consume("extract")
            if structural_unit is None:
                system_prompt = EXTRACTION_SYSTEM_PROMPT
                user_prompt = extraction_user_prompt(
                    source_name, locator, chunk.content
                )
            else:
                system_prompt = CHANGE_ORDER_EXTRACTION_SYSTEM_PROMPT
                user_prompt = change_order_extraction_user_prompt(
                    source_name,
                    locator,
                    structural_unit.prompt_context(),
                    chunk.content,
                )
            payload, usage = self.client.chat_json(
                system_prompt,
                user_prompt,
                retries=0 if len(chunk.content) >= 2000 else 1,
            )
            return [(chunk, payload, usage, split_depth)]
        except APIError as exc:
            if (
                split_depth >= self.MAX_EXTRACTION_SPLIT_DEPTH
                or len(chunk.content) < 800
            ):
                raise KnowledgeServiceError(
                    f"来源 {source_name} 在 {locator} 的结构化抽取失败：{exc}"
                ) from exc
            parts = self._split_extraction_chunk(chunk)
            if len(parts) < 2:
                raise KnowledgeServiceError(
                    f"来源 {source_name} 在 {locator} 的结构化抽取失败且无法继续拆分：{exc}"
                ) from exc
            self.trace.log(
                "knowledge_extraction_split_retry",
                source_name=source_name,
                chunk_index=chunk.index,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                split_depth=split_depth,
                next_depth=split_depth + 1,
                part_lengths=[len(part.content) for part in parts],
                reason=str(exc),
            )
            results: list[
                tuple[DocumentChunk, Any, dict[str, Any] | None, int]
            ] = []
            for part in parts:
                results.extend(
                    self._extract_chunk(
                        source_name,
                        part,
                        split_depth=split_depth + 1,
                        budget=budget,
                        structural_unit=structural_unit,
                    )
                )
            return results

    def _compare(
        self, draft: KnowledgeCardDraft, *, budget: ModelCallBudget
    ) -> ComparisonResult:
        query = " ".join(
            [
                draft.title,
                draft.summary,
                draft.scenario,
                draft.object_name,
                *draft.applicable_versions,
                *draft.keywords,
            ]
        )
        hits = self.retriever.search(
            query,
            statuses=[
                CardStatus.DRAFT,
                CardStatus.PENDING_REVIEW,
                CardStatus.APPROVED,
                CardStatus.SUPERSEDED,
            ],
            top_k=5,
            min_score=self.settings.retrieval_min_score,
            min_query_coverage=self.settings.retrieval_min_coverage,
        )
        if not hits:
            return ComparisonResult()
        candidates = [hit.card for hit in hits]
        budget.consume("compare")
        payload, usage = self.client.chat_json(
            COMPARISON_SYSTEM_PROMPT,
            comparison_user_prompt(draft.to_dict(), candidates),
        )
        self.trace.log(
            "knowledge_comparison_response",
            candidate_ids=[candidate["id"] for candidate in candidates],
            usage=usage,
        )
        if not isinstance(payload, dict):
            return ComparisonResult(reason="模型比较结果不是 JSON 对象")
        return ComparisonResult.from_dict(
            payload, {int(candidate["id"]) for candidate in candidates}
        )

    def search(
        self,
        query: str,
        *,
        status: CardStatus | str = CardStatus.APPROVED,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise KnowledgeServiceError("检索问题不能为空")
        limit = top_k or self.settings.retrieval_top_k
        normalized_status = (
            status.value if isinstance(status, CardStatus) else str(status).upper()
        )
        if normalized_status == CardStatus.APPROVED.value:
            hits, _diagnostics = self.trusted_search_hits(query, top_k=limit)
        else:
            hits = self.retriever.search(
                query,
                statuses=[normalized_status],
                top_k=limit,
                min_score=self.settings.retrieval_min_score,
                min_query_coverage=self.settings.retrieval_min_coverage,
            )
        return [hit.to_dict() for hit in hits]

    def trusted_search_hits(
        self, query: str, *, top_k: int | None = None
    ) -> tuple[list[SearchHit], dict[str, Any]]:
        """Combine local lexical recall with governed MindMemOS recall.

        MindMemOS may only contribute IDs already linked to local cards.  Every
        linked card is loaded again and must still be APPROVED before it can be
        returned to the answer generator.
        """

        text = query.strip()
        if not text:
            raise KnowledgeServiceError("检索问题不能为空")
        limit = max(1, min(top_k or self.settings.retrieval_top_k, 50))
        lexical_candidates = self.retriever.search(
            text,
            statuses=[CardStatus.APPROVED],
            top_k=limit,
            min_score=self.settings.retrieval_min_score,
            min_query_coverage=self.settings.retrieval_min_coverage,
        )
        lexical: list[SearchHit] = []
        lexical_rejected: list[dict[str, Any]] = []
        for hit in lexical_candidates:
            relevance = self._semantic_relevance_gate(
                text,
                hit.card,
                minimum_anchors=self.settings.mindmemos_min_local_anchors,
            )
            if relevance["accepted"]:
                lexical.append(hit)
            else:
                lexical_rejected.append(
                    {
                        "card_id": int(hit.card["id"]),
                        "reason": "INSUFFICIENT_LOCAL_RELEVANCE",
                        **relevance,
                    }
                )
        if lexical:
            diagnostics = {
                "backend": "mindmemos:vanilla",
                "enabled": self.memory.enabled,
                "configured": self.memory.configured,
                "used": False,
                "status": "SKIPPED_LOCAL_SUFFICIENT",
                "memory_hits": 0,
                "mapped_approved_cards": 0,
                "card_ids": [],
                "lexical_card_ids": [int(hit.card["id"]) for hit in lexical],
                "semantic_added_card_ids": [],
                "lexical_rejected": lexical_rejected,
                "final_card_ids": [int(hit.card["id"]) for hit in lexical],
            }
            self.trace.log("governed_memory_retrieval", question=text, **diagnostics)
            return lexical, diagnostics
        recall = self.memory.recall(text)
        hits = list(lexical)
        seen = {int(hit.card["id"]) for hit in hits}
        semantic_added: list[int] = []
        rejected_semantic: list[dict[str, Any]] = []
        for rank, card_id in enumerate(recall.card_ids):
            if (
                card_id in seen
                or len(hits) >= limit
                or len(semantic_added)
                >= self.settings.mindmemos_max_semantic_cards
            ):
                continue
            card = self.store.get_card(card_id)
            if card is None or card["status"] != CardStatus.APPROVED.value:
                continue
            relevance = self._semantic_relevance_gate(
                text,
                card,
                minimum_anchors=self.settings.mindmemos_min_local_anchors,
            )
            if not relevance["accepted"]:
                rejected_semantic.append(
                    {"card_id": card_id, "reason": "INSUFFICIENT_LOCAL_ANCHORS", **relevance}
                )
                continue
            hits.append(
                SearchHit(
                    card=card,
                    score=max(0.0, self.settings.mindmemos_min_relevance_score),
                    matched_terms=["mindmemos:semantic"],
                    query_coverage=float(relevance["query_coverage"]),
                )
            )
            seen.add(card_id)
            semantic_added.append(card_id)
        diagnostics = {
            **recall.diagnostics,
            "lexical_card_ids": [int(hit.card["id"]) for hit in lexical],
            "lexical_rejected": lexical_rejected,
            "semantic_added_card_ids": semantic_added,
            "semantic_rejected": rejected_semantic,
            "relevance_gate": {
                "external_rerank_threshold": self.settings.mindmemos_min_relevance_score,
                "minimum_local_anchors": self.settings.mindmemos_min_local_anchors,
            },
            "final_card_ids": [int(hit.card["id"]) for hit in hits],
        }
        self.trace.log("governed_memory_retrieval", question=text, **diagnostics)
        return hits, diagnostics

    def search_with_diagnostics(
        self,
        query: str,
        *,
        status: CardStatus | str = CardStatus.APPROVED,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        normalized_status = (
            status.value if isinstance(status, CardStatus) else str(status).upper()
        )
        if normalized_status != CardStatus.APPROVED.value:
            return {
                "hits": self.search(query, status=normalized_status, top_k=top_k),
                "memory_retrieval": {
                    "used": False,
                    "status": "SKIPPED_NON_APPROVED_SEARCH",
                },
            }
        hits, diagnostics = self.trusted_search_hits(query, top_k=top_k)
        return {
            "hits": [hit.to_dict() for hit in hits],
            "memory_retrieval": diagnostics,
        }

    def _validate_answer_claims(
        self, payload: Any, retrieved_cards: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
            raise KnowledgeServiceError("模型答案不是规定的 claims JSON 对象")
        if len(payload["claims"]) > self.MAX_ANSWER_CLAIMS:
            raise KnowledgeServiceError(
                f"模型答案超过 {self.MAX_ANSWER_CLAIMS} 条结论限制"
            )

        claims: list[dict[str, Any]] = []
        seen_claims: set[tuple[str, str, int]] = set()
        for index, raw_claim in enumerate(payload["claims"], start=1):
            if not isinstance(raw_claim, dict):
                raise KnowledgeServiceError(f"第 {index} 条结论不是 JSON 对象")
            category = str(raw_claim.get("category") or "").strip()
            if category not in self.ANSWER_CATEGORIES:
                raise KnowledgeServiceError(f"第 {index} 条结论类别无效: {category!r}")
            if "text" in raw_claim or "card_ids" in raw_claim:
                raise KnowledgeServiceError(
                    f"第 {index} 条结论包含模型自由文本或旧式引用；"
                    "可信答案只接受结构化字段指针"
                )
            raw_card_id = raw_claim.get("card_id")
            if isinstance(raw_card_id, bool):
                raise KnowledgeServiceError(f"第 {index} 条结论 card_id 无效")
            try:
                card_id = int(raw_card_id)
            except (TypeError, ValueError) as exc:
                raise KnowledgeServiceError(
                    f"第 {index} 条结论包含无效 card_id: {raw_card_id!r}"
                ) from exc
            card = retrieved_cards.get(card_id)
            if card is None or card.get("status") != CardStatus.APPROVED.value:
                raise KnowledgeServiceError(
                    f"第 {index} 条结论引用了未检索或未批准的知识卡片: K{card_id}"
                )

            support_field = str(raw_claim.get("support_field") or "").strip()
            expected_category = self.CLAIM_FIELD_CATEGORIES.get(support_field)
            if expected_category is None:
                raise KnowledgeServiceError(
                    f"第 {index} 条结论包含不可引用字段: {support_field!r}"
                )
            value = card.get(support_field)
            support_index: int | None = None
            if category == "知识不足":
                if raw_claim.get("support_index") not in (None, ""):
                    raise KnowledgeServiceError(
                        f"第 {index} 条知识不足结论不允许 support_index"
                    )
                if value not in (None, "", []):
                    raise KnowledgeServiceError(
                        f"第 {index} 条知识不足结论指向了非空字段: {support_field}"
                    )
                text = (
                    "现有已审核知识未提供"
                    f"{self.CLAIM_FIELD_LABELS[support_field]}。"
                )
            else:
                if category != expected_category:
                    raise KnowledgeServiceError(
                        f"第 {index} 条结论类别 {category!r} 与字段 "
                        f"{support_field!r} 不匹配"
                    )
                if isinstance(value, list):
                    raw_support_index = raw_claim.get("support_index")
                    if isinstance(raw_support_index, bool):
                        raise KnowledgeServiceError(
                            f"第 {index} 条结论 support_index 无效"
                        )
                    try:
                        support_index = int(raw_support_index)
                    except (TypeError, ValueError) as exc:
                        raise KnowledgeServiceError(
                            f"第 {index} 条结论缺少有效 support_index"
                        ) from exc
                    if not 0 <= support_index < len(value):
                        raise KnowledgeServiceError(
                            f"第 {index} 条结论 support_index 超出字段范围"
                        )
                    text = str(value[support_index]).strip()
                else:
                    if raw_claim.get("support_index") not in (None, ""):
                        raise KnowledgeServiceError(
                            f"第 {index} 条标量字段不允许 support_index"
                        )
                    text = str(value or "").strip()
                if not text:
                    raise KnowledgeServiceError(
                        f"第 {index} 条结论指向了空字段: {support_field}"
                    )

            claim_key = (category, text, card_id)
            if claim_key in seen_claims:
                continue
            seen_claims.add(claim_key)
            claims.append(
                {
                    "category": category,
                    "text": text,
                    "card_ids": [card_id],
                    "support": {
                        "card_id": card_id,
                        "field": support_field,
                        "index": support_index,
                    },
                }
            )
        return claims

    @staticmethod
    def _render_answer(claims: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        categories = [
            "结论",
            "适用条件",
            "执行步骤",
            "风险",
            "回退",
            "验证",
            "知识不足",
        ]
        for category in categories:
            grouped = [claim for claim in claims if claim["category"] == category]
            if not grouped:
                continue
            lines.append(f"### {category}")
            for claim in grouped:
                citations = "".join(f"[K{card_id}]" for card_id in claim["card_ids"])
                lines.append(f"- {claim['text']} {citations}")
            lines.append("")
        return "\n".join(lines).strip()

    def _answer_from_hits(
        self, question: str, hits: list[SearchHit]
    ) -> dict[str, Any]:
        if not hits:
            return {
                "answer": "现有已审核知识不足，无法生成可信方案。请先导入并审核相关知识。",
                "claims": [],
                "sources": [],
                "usage": None,
                "refusal_reason": "no_relevant_approved_knowledge",
            }
        cards = [hit.card for hit in hits]
        payload, usage = self.client.chat_json(
            ANSWER_SYSTEM_PROMPT,
            answer_user_prompt(question, cards),
        )
        retrieved_cards = {int(card["id"]): card for card in cards}
        claims = self._validate_answer_claims(payload, retrieved_cards)
        if not claims:
            return {
                "answer": "现有已审核知识不足，无法生成可信方案。",
                "claims": [],
                "sources": [],
                "usage": usage,
                "refusal_reason": "model_found_insufficient_evidence",
            }
        answer = self._render_answer(claims)
        cited_ids = {
            card_id for claim in claims for card_id in claim["card_ids"]
        }
        hit_by_id = {int(hit.card["id"]): hit for hit in hits}
        cited_cards = [card for card in cards if int(card["id"]) in cited_ids]
        sources = [
            {
                "card_id": card["id"],
                "title": card["title"],
                "source_ref": card["source_ref"],
                "evidence_locator": card["evidence_locator"],
                "evidence_quote": card["evidence_quote"],
                "retrieval_score": round(hit_by_id[int(card["id"])].score, 4),
                "retrieval_channel": (
                    "mindmemos_semantic"
                    if "mindmemos:semantic"
                    in hit_by_id[int(card["id"])].matched_terms
                    else "local_lexical"
                ),
            }
            for card in cited_cards
        ]
        self.trace.log(
            "trusted_query_completed",
            question=question,
            card_ids=sorted(cited_ids),
            usage=usage,
        )
        return {
            "answer": answer,
            "claims": claims,
            "sources": sources,
            "usage": usage,
            "refusal_reason": None,
        }

    def query(self, question: str) -> dict[str, Any]:
        self.settings.require_api()
        hits, diagnostics = self.trusted_search_hits(
            question, top_k=self.settings.retrieval_top_k
        )
        result = self._answer_from_hits(question, hits)
        result["memory_retrieval"] = diagnostics
        return result

    def agent_query(self, question: str) -> dict[str, Any]:
        from .agent import TrustedKnowledgeAgent

        return TrustedKnowledgeAgent(
            self, max_steps=self.settings.agent_max_steps
        ).run(question)

    def review(
        self,
        card_id: int,
        *,
        action: str,
        reviewer: str,
        comment: str = "",
        supersedes_id: int | None = None,
    ) -> dict[str, Any]:
        card = self.store.review_card(
            card_id,
            action=action,
            reviewer=reviewer,
            comment=comment,
            supersedes_id=supersedes_id,
        )
        if card["status"] == CardStatus.APPROVED.value:
            try:
                memory_sync = self.memory.sync_card(card)
            except MindMemOSError as exc:
                memory_sync = {
                    "status": "FAILED",
                    "card_id": card_id,
                    "error": str(exc),
                }
                self.trace.log(
                    "mindmemos_sync_degraded", card_id=card_id, error=str(exc)
                )
            card["memory_sync"] = memory_sync
        return card

    def sync_long_term_memory(self) -> dict[str, Any]:
        result = self.memory.sync_approved()
        self.trace.log(
            "mindmemos_bulk_sync_completed",
            processed=result["processed"],
            stats=result["stats"],
        )
        return result

    def long_term_memory_status(self, *, probe: bool = False) -> dict[str, Any]:
        return self.memory.status(probe=probe)

    def regrade_existing_cards(self) -> dict[str, Any]:
        """Apply current grounding and type-aware quality rules without an API call."""

        processed = 0
        grounded = 0
        status_changes = 0
        cards = self.store.list_cards(limit=2000)
        for card in cards:
            chunk = self.store.get_chunk(int(card["source_chunk_id"]))
            if chunk is None:
                continue
            draft = KnowledgeCardDraft.from_dict(card)
            span = ground_evidence_quote(chunk["content"], draft.evidence_quote)
            if span is not None:
                grounded += 1
                draft.evidence_quote = span.quote
                evidence_start = int(chunk["char_start"]) + span.start
                evidence_end = int(chunk["char_start"]) + span.end
                evidence_locator = (
                    f"{card['source_name']}#chunk={int(chunk['chunk_index']) + 1};"
                    f"chars={evidence_start}-{evidence_end};match={span.match_method}"
                )
            else:
                evidence_locator = (
                    f"{card['source_name']}#chunk={int(chunk['chunk_index']) + 1};"
                    f"chars={chunk['char_start']}-{chunk['char_end']};unverified"
                )
            score, issues = draft.quality(chunk["content"])
            extraction_report = self.store.get_extraction_report(
                int(card["source_document_id"])
            )
            change_report = (
                extraction_report.get("change_order")
                if extraction_report is not None
                else None
            )
            if (
                isinstance(change_report, dict)
                and change_report.get("matched") is True
                and not change_report.get("safe_to_publish", False)
            ):
                issues.extend(
                    f"阻断：{blocker}"
                    for blocker in change_report.get("blockers", [])
                    if f"阻断：{blocker}" not in issues
                )
                score = min(score, 64.0)
            old_status = CardStatus(card["status"])
            if old_status in {CardStatus.DRAFT, CardStatus.PENDING_REVIEW}:
                new_status = (
                    CardStatus.PENDING_REVIEW
                    if score >= 65 and span is not None
                    else CardStatus.DRAFT
                )
            else:
                new_status = old_status
            if new_status is not old_status:
                status_changes += 1
            self.store.update_card_quality(
                int(card["id"]),
                evidence_quote=draft.evidence_quote,
                evidence_locator=evidence_locator,
                quality_score=score,
                quality_issues=issues,
                status=new_status,
                detail={
                    "old_status": old_status.value,
                    "new_status": new_status.value,
                    "old_quality_score": card["quality_score"],
                    "new_quality_score": score,
                    "grounded": span is not None,
                    "match_method": span.match_method if span is not None else None,
                },
            )
            processed += 1
        result = {
            "processed": processed,
            "grounded": grounded,
            "status_changes": status_changes,
            "stats": self.stats(),
        }
        self.trace.log("knowledge_regrade_completed", **result)
        return result

    def card_detail(self, card_id: int) -> dict[str, Any] | None:
        card = self.store.get_card(card_id)
        if card is None:
            return None
        card["relations"] = self.store.list_relations(card_id)
        card["audit_log"] = self.store.list_audit(card_id)
        card["lineage"] = self.store.get_card_lineage(card_id)
        card["extraction_report"] = self.store.get_extraction_report(
            int(card["source_document_id"])
        )
        card["memory_sync"] = self.store.get_memory_sync_state(
            card_id, MindMemOSBridge.BACKEND
        )
        return card

    def stats(self) -> dict[str, Any]:
        result = self.store.stats()
        result["config"] = self.settings.public_config()
        result["long_term_memory"] = self.long_term_memory_status(probe=False)
        return result
