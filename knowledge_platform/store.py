from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .schema import CardStatus, ComparisonResult, KnowledgeCardDraft, LIST_FIELDS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StoreError(RuntimeError):
    """Raised when a knowledge lifecycle operation is invalid."""


class KnowledgeStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            checksum TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        );

        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            knowledge_type TEXT NOT NULL,
            scenario TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_name TEXT NOT NULL,
            applicable_versions TEXT NOT NULL,
            prerequisites TEXT NOT NULL,
            procedure_steps TEXT NOT NULL,
            risks TEXT NOT NULL,
            rollback_steps TEXT NOT NULL,
            validation_steps TEXT NOT NULL,
            keywords TEXT NOT NULL,
            source_document_id INTEGER NOT NULL REFERENCES documents(id),
            source_chunk_id INTEGER NOT NULL REFERENCES chunks(id),
            evidence_quote TEXT NOT NULL,
            evidence_locator TEXT NOT NULL,
            status TEXT NOT NULL,
            quality_score REAL NOT NULL,
            quality_issues TEXT NOT NULL,
            comparison_label TEXT NOT NULL,
            comparison_confidence REAL NOT NULL,
            comparison_reason TEXT NOT NULL,
            supersedes_id INTEGER REFERENCES cards(id),
            reviewer TEXT,
            review_comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
        CREATE INDEX IF NOT EXISTS idx_cards_object ON cards(object_name);
        CREATE INDEX IF NOT EXISTS idx_cards_source ON cards(source_document_id);

        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            related_card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            relation_type TEXT NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(card_id, related_card_id, relation_type)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)

    def add_document(
        self,
        source_name: str,
        source_type: str,
        source_ref: str,
        checksum: str,
        content: str,
    ) -> tuple[int, bool]:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM documents WHERE checksum = ?", (checksum,)
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO documents
                    (source_name, source_type, source_ref, checksum, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_name, source_type, source_ref, checksum, content, utc_now()),
            )
            return int(cursor.lastrowid), True

    def find_document_by_checksum(self, checksum: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE checksum = ?", (checksum,)
            ).fetchone()
        return dict(row) if row is not None else None

    def card_ids_for_document(self, document_id: int) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM cards WHERE source_document_id = ? ORDER BY id",
                (document_id,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def add_chunk(
        self,
        document_id: int,
        chunk_index: int,
        char_start: int,
        char_end: int,
        content: str,
    ) -> int:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO chunks
                    (document_id, chunk_index, char_start, char_end, content)
                VALUES (?, ?, ?, ?, ?)
                """,
                (document_id, chunk_index, char_start, char_end, content),
            )
            row = connection.execute(
                "SELECT id FROM chunks WHERE document_id = ? AND chunk_index = ?",
                (document_id, chunk_index),
            ).fetchone()
            if row is None:
                raise StoreError("无法保存文档分片")
            return int(row["id"])

    def get_chunk(self, chunk_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def add_card(
        self,
        draft: KnowledgeCardDraft,
        *,
        document_id: int,
        chunk_id: int,
        evidence_locator: str,
        status: CardStatus,
        quality_score: float,
        quality_issues: list[str],
        comparison: ComparisonResult,
    ) -> int:
        now = utc_now()
        values = draft.to_dict()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cards (
                    title, summary, knowledge_type, scenario, object_type, object_name,
                    applicable_versions, prerequisites, procedure_steps, risks,
                    rollback_steps, validation_steps, keywords,
                    source_document_id, source_chunk_id, evidence_quote, evidence_locator,
                    status, quality_score, quality_issues,
                    comparison_label, comparison_confidence, comparison_reason,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    values["title"],
                    values["summary"],
                    values["knowledge_type"],
                    values["scenario"],
                    values["object_type"],
                    values["object_name"],
                    json.dumps(values["applicable_versions"], ensure_ascii=False),
                    json.dumps(values["prerequisites"], ensure_ascii=False),
                    json.dumps(values["procedure_steps"], ensure_ascii=False),
                    json.dumps(values["risks"], ensure_ascii=False),
                    json.dumps(values["rollback_steps"], ensure_ascii=False),
                    json.dumps(values["validation_steps"], ensure_ascii=False),
                    json.dumps(values["keywords"], ensure_ascii=False),
                    document_id,
                    chunk_id,
                    values["evidence_quote"],
                    evidence_locator,
                    status.value,
                    quality_score,
                    json.dumps(quality_issues, ensure_ascii=False),
                    comparison.decision.value,
                    comparison.confidence,
                    comparison.reason,
                    now,
                    now,
                ),
            )
            card_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_log (card_id, action, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    card_id,
                    "CARD_CREATED",
                    "knowledge_pipeline",
                    json.dumps(
                        {
                            "status": status.value,
                            "quality_score": quality_score,
                            "comparison": comparison.decision.value,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            if comparison.related_card_id is not None:
                relation_type = {
                    "DUPLICATE": "DUPLICATE_OF",
                    "CONFLICT": "CONFLICTS_WITH",
                    "NEW_VERSION": "CANDIDATE_VERSION_OF",
                }.get(comparison.decision.value, "RELATED_TO")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO relations
                        (card_id, related_card_id, relation_type, confidence, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        card_id,
                        comparison.related_card_id,
                        relation_type,
                        comparison.confidence,
                        comparison.reason,
                        now,
                    ),
                )
            return card_id

    @staticmethod
    def _decode_card(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field in LIST_FIELDS:
            result[field] = json.loads(result[field] or "[]")
        result["quality_issues"] = json.loads(result["quality_issues"] or "[]")
        return result

    def get_card(self, card_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT cards.*, documents.source_name, documents.source_ref,
                       documents.checksum AS source_checksum,
                       chunks.char_start, chunks.char_end
                FROM cards
                JOIN documents ON documents.id = cards.source_document_id
                JOIN chunks ON chunks.id = cards.source_chunk_id
                WHERE cards.id = ?
                """,
                (card_id,),
            ).fetchone()
        return self._decode_card(row)

    def update_card_quality(
        self,
        card_id: int,
        *,
        evidence_quote: str,
        evidence_locator: str,
        quality_score: float,
        quality_issues: list[str],
        status: CardStatus,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT id FROM cards WHERE id = ?", (card_id,)
            ).fetchone()
            if current is None:
                raise StoreError(f"知识卡片不存在: {card_id}")
            connection.execute(
                """
                UPDATE cards
                SET evidence_quote = ?, evidence_locator = ?, quality_score = ?,
                    quality_issues = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    evidence_quote,
                    evidence_locator,
                    quality_score,
                    json.dumps(quality_issues, ensure_ascii=False),
                    status.value,
                    now,
                    card_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_log (card_id, action, actor, detail, created_at)
                VALUES (?, 'CARD_REGRADED', 'knowledge_pipeline', ?, ?)
                """,
                (card_id, json.dumps(detail, ensure_ascii=False), now),
            )
        card = self.get_card(card_id)
        if card is None:
            raise StoreError("重新评分后无法读取知识卡片")
        return card

    def list_cards(
        self,
        status: CardStatus | str | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            value = status.value if isinstance(status, CardStatus) else str(status).upper()
            where = "WHERE cards.status = ?"
            params.append(value)
        params.extend([min(max(limit, 1), 2000), max(offset, 0)])
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT cards.*, documents.source_name, documents.source_ref
                FROM cards
                JOIN documents ON documents.id = cards.source_document_id
                {where}
                ORDER BY cards.updated_at DESC, cards.id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [self._decode_card(row) for row in rows if row is not None]

    def list_relations(self, card_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT relations.*, cards.title AS related_title,
                       cards.status AS related_status
                FROM relations
                JOIN cards ON cards.id = relations.related_card_id
                WHERE relations.card_id = ?
                ORDER BY relations.confidence DESC
                """,
                (card_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_audit(self, card_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE card_id = ? ORDER BY id DESC", (card_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def review_card(
        self,
        card_id: int,
        *,
        action: str,
        reviewer: str,
        comment: str = "",
        supersedes_id: int | None = None,
    ) -> dict[str, Any]:
        action = action.upper()
        reviewer = reviewer.strip()
        if not reviewer:
            raise StoreError("审核人不能为空")
        if action not in {"APPROVE", "REJECT", "SUPERSEDE"}:
            raise StoreError("审核动作必须是 APPROVE、REJECT 或 SUPERSEDE")

        now = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT id, status, evidence_quote, quality_issues FROM cards WHERE id = ?",
                (card_id,),
            ).fetchone()
            if current is None:
                raise StoreError(f"知识卡片不存在: {card_id}")
            reviewable_statuses = {
                CardStatus.DRAFT.value,
                CardStatus.PENDING_REVIEW.value,
            }
            if current["status"] not in reviewable_statuses:
                raise StoreError(
                    f"仅 DRAFT 或 PENDING_REVIEW 卡片可以审核；"
                    f"当前状态为 {current['status']}"
                )
            if action in {"APPROVE", "SUPERSEDE"}:
                quality_issues = json.loads(current["quality_issues"] or "[]")
                evidence_issues = [issue for issue in quality_issues if "证据" in str(issue)]
                if not current["evidence_quote"] or evidence_issues:
                    raise StoreError(
                        "该知识缺少可定位的原文证据，不能批准发布；请补充来源后重新抽取。"
                    )

            if action == "SUPERSEDE":
                if supersedes_id is None or supersedes_id == card_id:
                    raise StoreError("SUPERSEDE 必须提供另一个 supersedes_id")
                target = connection.execute(
                    "SELECT id, status FROM cards WHERE id = ?", (supersedes_id,)
                ).fetchone()
                if target is None:
                    raise StoreError(f"被替代知识不存在: {supersedes_id}")
                if target["status"] != CardStatus.APPROVED.value:
                    raise StoreError(
                        "只能替代 APPROVED 知识；"
                        f"目标卡片当前状态为 {target['status']}"
                    )
                connection.execute(
                    "UPDATE cards SET status = ?, updated_at = ? WHERE id = ?",
                    (CardStatus.SUPERSEDED.value, now, supersedes_id),
                )
                connection.execute(
                    """
                    UPDATE cards
                    SET status = ?, supersedes_id = ?, reviewer = ?, review_comment = ?,
                        published_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        CardStatus.APPROVED.value,
                        supersedes_id,
                        reviewer,
                        comment,
                        now,
                        now,
                        card_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO relations
                        (card_id, related_card_id, relation_type, confidence, reason, created_at)
                    VALUES (?, ?, 'SUPERSEDES', 1.0, ?, ?)
                    """,
                    (card_id, supersedes_id, comment or "人工确认版本替代", now),
                )
                connection.execute(
                    """
                    INSERT INTO audit_log (card_id, action, actor, detail, created_at)
                    VALUES (?, 'SUPERSEDED', ?, ?, ?)
                    """,
                    (
                        supersedes_id,
                        reviewer,
                        json.dumps(
                            {
                                "comment": comment,
                                "superseded_by": card_id,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                new_status = CardStatus.APPROVED.value
            else:
                new_status = (
                    CardStatus.APPROVED.value
                    if action == "APPROVE"
                    else CardStatus.REJECTED.value
                )
                published_at = now if action == "APPROVE" else None
                connection.execute(
                    """
                    UPDATE cards
                    SET status = ?, reviewer = ?, review_comment = ?,
                        published_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status, reviewer, comment, published_at, now, card_id),
                )

            connection.execute(
                "INSERT INTO audit_log (card_id, action, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    card_id,
                    action,
                    reviewer,
                    json.dumps(
                        {"comment": comment, "supersedes_id": supersedes_id},
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        card = self.get_card(card_id)
        if card is None:
            raise StoreError("审核后无法读取知识卡片")
        return card

    def stats(self) -> dict[str, Any]:
        with self.connect() as connection:
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM cards GROUP BY status"
            ).fetchall()
            document_count = connection.execute(
                "SELECT COUNT(*) AS count FROM documents"
            ).fetchone()["count"]
            relation_count = connection.execute(
                "SELECT COUNT(*) AS count FROM relations"
            ).fetchone()["count"]
            average_quality = connection.execute(
                "SELECT COALESCE(AVG(quality_score), 0) AS value FROM cards"
            ).fetchone()["value"]
        statuses = {status.value: 0 for status in CardStatus}
        statuses.update({row["status"]: row["count"] for row in status_rows})
        return {
            "documents": document_count,
            "cards": sum(statuses.values()),
            "relations": relation_count,
            "average_quality": round(float(average_quality), 1),
            "statuses": statuses,
        }
