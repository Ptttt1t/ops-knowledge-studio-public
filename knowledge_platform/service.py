from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

from harness.api_client import APIError, DeepSeekClient
from harness.config import Settings
from harness.trace import TraceLogger

from .documents import (
    DocumentChunk,
    EvidenceSpan,
    SourceDocument,
    chunk_text,
    ground_evidence_quote,
    read_document,
)
from .prompts import (
    ANSWER_SYSTEM_PROMPT,
    COMPARISON_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    answer_user_prompt,
    comparison_user_prompt,
    extraction_user_prompt,
)
from .retrieval import HybridRetriever, SearchHit
from .schema import CardStatus, ComparisonResult, KnowledgeCardDraft
from .store import KnowledgeStore


class KnowledgeServiceError(RuntimeError):
    """Raised when a knowledge pipeline operation cannot be completed."""


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
    ANSWER_CATEGORIES = {
        "适用条件",
        "执行步骤",
        "风险",
        "回退",
        "验证",
        "结论",
        "知识不足",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        store: KnowledgeStore | None = None,
        client: Any | None = None,
        trace: TraceLogger | None = None,
    ):
        self.settings = settings
        self.store = store or KnowledgeStore(settings.database_path)
        self.store.initialize()
        self.client = client or DeepSeekClient(settings)
        self.trace = trace or TraceLogger(settings.project_root / "artifacts")
        self.retriever = HybridRetriever(self.store)

    def ingest_file(
        self, path: Path, *, source_name: str | None = None
    ) -> dict[str, Any]:
        document = read_document(path)
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
        checksum = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
        existing = self.store.find_document_by_checksum(checksum)
        if existing is not None:
            return {
                "document_id": existing["id"],
                "duplicate_document": True,
                "card_ids": self.store.card_ids_for_document(int(existing["id"])),
                "message": "相同内容已经导入，本次未重复调用模型。",
            }

        chunks = chunk_text(
            document.content,
            self.settings.chunk_size,
            self.settings.chunk_overlap,
        )
        extracted: list[ExtractedCard] = []
        self.trace.log(
            "knowledge_ingest_started",
            source_name=document.name,
            checksum=checksum,
            chunks=len(chunks),
        )

        for chunk in chunks:
            for extracted_chunk, payload, usage, split_depth in self._extract_chunk(
                document.name, chunk
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

                for raw_card in raw_cards[: self.MAX_CARDS_PER_EXTRACTION]:
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
                    score, issues = draft.quality(extracted_chunk.content)
                    comparison = self._compare(draft)
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
            return {
                "document_id": document_id,
                "duplicate_document": True,
                "card_ids": self.store.card_ids_for_document(document_id),
                "message": "相同内容已经导入。",
            }

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
            card_ids.append(card_id)

        result = {
            "document_id": document_id,
            "duplicate_document": False,
            "chunks": len(chunks),
            "extracted_cards": len(card_ids),
            "card_ids": card_ids,
            "pending_review": sum(
                1
                for card_id in card_ids
                if self.store.get_card(card_id)["status"] == CardStatus.PENDING_REVIEW.value
            ),
            "message": "知识抽取完成，正式发布前必须人工审核。",
        }
        self.trace.log("knowledge_ingest_completed", **result)
        return result

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
    ) -> list[tuple[DocumentChunk, Any, dict[str, Any] | None, int]]:
        locator = f"字符 {chunk.char_start}-{chunk.char_end}"
        try:
            payload, usage = self.client.chat_json(
                EXTRACTION_SYSTEM_PROMPT,
                extraction_user_prompt(source_name, locator, chunk.content),
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
                    )
                )
            return results

    def _compare(self, draft: KnowledgeCardDraft) -> ComparisonResult:
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
        hits = self.retriever.search(
            query,
            statuses=[status],
            top_k=top_k or self.settings.retrieval_top_k,
            min_score=self.settings.retrieval_min_score,
            min_query_coverage=self.settings.retrieval_min_coverage,
        )
        return [hit.to_dict() for hit in hits]

    def _validate_answer_claims(
        self, payload: Any, retrieved_ids: set[int]
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
            raise KnowledgeServiceError("模型答案不是规定的 claims JSON 对象")

        claims: list[dict[str, Any]] = []
        for index, raw_claim in enumerate(payload["claims"], start=1):
            if not isinstance(raw_claim, dict):
                raise KnowledgeServiceError(f"第 {index} 条结论不是 JSON 对象")
            category = str(raw_claim.get("category") or "").strip()
            text = str(raw_claim.get("text") or "").strip()
            raw_ids = raw_claim.get("card_ids")
            if category not in self.ANSWER_CATEGORIES:
                raise KnowledgeServiceError(f"第 {index} 条结论类别无效: {category!r}")
            if not text:
                raise KnowledgeServiceError(f"第 {index} 条结论内容为空")
            if re.search(r"(?:\[\s*)?K\d+(?:\s*\])?", text, flags=re.IGNORECASE):
                raise KnowledgeServiceError(
                    f"第 {index} 条结论在正文中自行写入了 K 编号，无法安全渲染"
                )
            if not isinstance(raw_ids, list) or not raw_ids:
                raise KnowledgeServiceError(f"第 {index} 条结论缺少 card_ids")
            card_ids: list[int] = []
            for raw_id in raw_ids:
                try:
                    card_id = int(raw_id)
                except (TypeError, ValueError) as exc:
                    raise KnowledgeServiceError(
                        f"第 {index} 条结论包含无效 card_id: {raw_id!r}"
                    ) from exc
                if card_id not in retrieved_ids:
                    raise KnowledgeServiceError(
                        f"第 {index} 条结论引用了未检索或未批准的知识卡片: K{card_id}"
                    )
                if card_id not in card_ids:
                    card_ids.append(card_id)
            claims.append(
                {"category": category, "text": text, "card_ids": card_ids}
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
        retrieved_ids = {int(card["id"]) for card in cards}
        claims = self._validate_answer_claims(payload, retrieved_ids)
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
        hits = self.retriever.search(
            question,
            statuses=[CardStatus.APPROVED],
            top_k=self.settings.retrieval_top_k,
            min_score=self.settings.retrieval_min_score,
            min_query_coverage=self.settings.retrieval_min_coverage,
        )
        return self._answer_from_hits(question, hits)

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
        return self.store.review_card(
            card_id,
            action=action,
            reviewer=reviewer,
            comment=comment,
            supersedes_id=supersedes_id,
        )

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
        return card

    def stats(self) -> dict[str, Any]:
        result = self.store.stats()
        result["config"] = self.settings.public_config()
        return result
