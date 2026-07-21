from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Any, Iterable

from .schema import CardStatus
from .store import KnowledgeStore


_LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*")
_CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]+")

# Single-character function words are especially dangerous in a tiny corpus:
# one match can otherwise make an unrelated APPROVED card look retrievable.
_CJK_STOPWORDS = {
    "的",
    "了",
    "和",
    "与",
    "或",
    "及",
    "在",
    "对",
    "将",
    "为",
    "是",
    "有",
    "中",
    "前",
    "后",
    "把",
    "被",
    "这",
    "那",
    "能",
    "可",
}
_LATIN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "by",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "with",
}


def tokenize(text: str) -> list[str]:
    normalized = text.lower()
    tokens = [
        token
        for match in _LATIN_TOKEN.finditer(normalized)
        if (token := match.group(0)) not in _LATIN_STOPWORDS
    ]
    for match in _CJK_SEQUENCE.finditer(normalized):
        sequence = match.group(0)
        if len(sequence) == 1:
            if sequence not in _CJK_STOPWORDS:
                tokens.append(sequence)
        else:
            tokens.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def _flatten(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


@dataclass(frozen=True)
class SearchHit:
    card: dict[str, Any]
    score: float
    matched_terms: list[str]
    query_coverage: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "card": self.card,
            "score": round(self.score, 4),
            "matched_terms": self.matched_terms,
            "query_coverage": round(self.query_coverage, 4),
        }


class HybridRetriever:
    """Small-corpus lexical retriever optimized for Chinese ops terminology.

    It combines weighted fields, alphanumeric tokens, and Chinese bigrams. This
    intentionally avoids an embedding dependency in the first local version.
    """

    FIELD_WEIGHTS = {
        "title": 5.0,
        "summary": 3.0,
        "scenario": 2.5,
        "object_name": 3.0,
        "applicable_versions": 3.0,
        "keywords": 3.5,
        "prerequisites": 1.5,
        "procedure_steps": 1.5,
        "risks": 1.5,
        "rollback_steps": 1.2,
        "validation_steps": 1.2,
    }

    def __init__(self, store: KnowledgeStore):
        self.store = store

    def search(
        self,
        query: str,
        *,
        statuses: Iterable[CardStatus | str] | None = None,
        top_k: int = 6,
        cards: list[dict[str, Any]] | None = None,
        min_score: float = 0.0,
        min_query_coverage: float = 0.0,
    ) -> list[SearchHit]:
        if cards is None:
            if statuses is None:
                cards = self.store.list_cards(limit=2000)
            else:
                allowed = {
                    item.value if isinstance(item, CardStatus) else str(item).upper()
                    for item in statuses
                }
                cards = [
                    card
                    for card in self.store.list_cards(limit=2000)
                    if card["status"] in allowed
                ]
        if not cards:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        query_counts = Counter(query_tokens)

        documents: list[dict[str, Counter[str]]] = []
        document_frequency: Counter[str] = Counter()
        for card in cards:
            fields: dict[str, Counter[str]] = {}
            seen: set[str] = set()
            for field in self.FIELD_WEIGHTS:
                counts = Counter(tokenize(_flatten(card.get(field))))
                fields[field] = counts
                seen.update(counts)
            documents.append(fields)
            document_frequency.update(seen)

        total_docs = len(cards)
        hits: list[SearchHit] = []
        query_lower = query.lower().strip()
        for card, fields in zip(cards, documents):
            score = 0.0
            matched: set[str] = set()
            for term, query_tf in query_counts.items():
                idf = math.log((total_docs + 1) / (document_frequency[term] + 0.5)) + 1
                for field, weight in self.FIELD_WEIGHTS.items():
                    tf = fields[field][term]
                    if tf:
                        matched.add(term)
                        score += weight * (1 + math.log(tf)) * idf * (1 + math.log(query_tf))
            title = str(card.get("title", "")).lower()
            object_name = str(card.get("object_name", "")).lower()
            if query_lower and query_lower in title:
                score += 12.0
            if object_name and object_name in query_lower:
                score += 6.0
            coverage = len(matched) / len(query_counts)
            if score > 0 and score >= max(0.0, min_score) and coverage >= max(
                0.0, min(1.0, min_query_coverage)
            ):
                hits.append(
                    SearchHit(
                        card=card,
                        score=score,
                        matched_terms=sorted(matched),
                        query_coverage=coverage,
                    )
                )

        hits.sort(key=lambda hit: (hit.score, hit.card["id"]), reverse=True)
        return hits[: max(1, top_k)]
