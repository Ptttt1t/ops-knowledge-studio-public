from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
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
            card_type TEXT NOT NULL DEFAULT 'LEGACY',
            card_model_version TEXT NOT NULL DEFAULT 'legacy_v1',
            review_status TEXT NOT NULL DEFAULT 'DRAFT',
            dedup_status TEXT NOT NULL DEFAULT 'NEW',
            content_quality REAL NOT NULL DEFAULT 0,
            publish_status TEXT NOT NULL DEFAULT 'CANDIDATE',
            retrieval_enabled INTEGER NOT NULL DEFAULT 1,
            planning_rag_enabled INTEGER NOT NULL DEFAULT 1,
            semantic_fingerprint TEXT NOT NULL DEFAULT '',
            semantic_payload TEXT NOT NULL DEFAULT '{}',
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

        CREATE TABLE IF NOT EXISTS ingestion_claims (
            checksum TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            owner_token TEXT NOT NULL,
            document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
            attempt INTEGER NOT NULL,
            lease_expires_at TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ingestion_claims_status
            ON ingestion_claims(status, lease_expires_at);

        CREATE TABLE IF NOT EXISTS extraction_reports (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            strategy TEXT NOT NULL,
            report TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS change_case_bundles (
            case_id TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            extraction_strategy TEXT NOT NULL,
            build_generation INTEGER NOT NULL DEFAULT 1,
            builder_version TEXT NOT NULL DEFAULT '',
            card_model_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_change_case_bundles_updated
            ON change_case_bundles(updated_at DESC);

        CREATE TABLE IF NOT EXISTS card_lineage (
            card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
            case_id TEXT NOT NULL,
            extraction_strategy TEXT NOT NULL,
            unit_role TEXT NOT NULL,
            unit_pointer TEXT NOT NULL,
            source_pointers TEXT NOT NULL,
            source_order INTEGER NOT NULL,
            unit_metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_card_lineage_case
            ON card_lineage(case_id, source_order);

        CREATE TABLE IF NOT EXISTS card_source_items (
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            output_field TEXT NOT NULL,
            output_index INTEGER NOT NULL,
            source_index INTEGER NOT NULL,
            source_pointer TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(card_id, output_field, output_index),
            UNIQUE(card_id, source_pointer, char_start, char_end)
        );

        CREATE INDEX IF NOT EXISTS idx_card_source_items_card
            ON card_source_items(card_id, output_index);

        CREATE TABLE IF NOT EXISTS memory_sync_state (
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            backend TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            memory_count INTEGER NOT NULL DEFAULT 0,
            detail TEXT NOT NULL,
            owner_token TEXT NOT NULL DEFAULT '',
            lease_expires_at TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(card_id, backend)
        );

        CREATE TABLE IF NOT EXISTS memory_links (
            backend TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(backend, memory_id)
        );

        CREATE INDEX IF NOT EXISTS idx_memory_links_card
            ON memory_links(card_id, backend);

        CREATE TABLE IF NOT EXISTS memory_retirements (
            backend TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            card_id INTEGER NOT NULL,
            case_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(backend, memory_id)
        );

        CREATE INDEX IF NOT EXISTS idx_memory_retirements_status
            ON memory_retirements(backend, status, updated_at);
        """
        with self.connect() as connection:
            connection.executescript(schema)
            # Compatibility migration for databases created before sync leases
            # were introduced. Column names and declarations are constants.
            existing_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_sync_state)"
                ).fetchall()
            }
            for column, declaration in (
                ("owner_token", "TEXT NOT NULL DEFAULT ''"),
                ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
                ("attempt", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE memory_sync_state ADD COLUMN {column} {declaration}"
                    )
            retirement_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(memory_retirements)"
                ).fetchall()
            }
            if "case_id" not in retirement_columns:
                connection.execute(
                    "ALTER TABLE memory_retirements "
                    "ADD COLUMN case_id TEXT NOT NULL DEFAULT ''"
                )
            lineage_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(card_lineage)"
                ).fetchall()
            }
            if "unit_metadata" not in lineage_columns:
                connection.execute(
                    "ALTER TABLE card_lineage "
                    "ADD COLUMN unit_metadata TEXT NOT NULL DEFAULT '{}'"
                )
            card_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cards)").fetchall()
            }
            for column, declaration in (
                ("card_type", "TEXT NOT NULL DEFAULT 'LEGACY'"),
                ("card_model_version", "TEXT NOT NULL DEFAULT 'legacy_v1'"),
                ("review_status", "TEXT NOT NULL DEFAULT 'DRAFT'"),
                ("dedup_status", "TEXT NOT NULL DEFAULT 'NEW'"),
                ("content_quality", "REAL NOT NULL DEFAULT 0"),
                ("publish_status", "TEXT NOT NULL DEFAULT 'CANDIDATE'"),
                ("retrieval_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("planning_rag_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("semantic_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                ("semantic_payload", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column not in card_columns:
                    connection.execute(
                        f"ALTER TABLE cards ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                "UPDATE cards SET review_status = status "
                "WHERE review_status != status"
            )
            connection.execute(
                "UPDATE cards SET content_quality = quality_score "
                "WHERE card_model_version = 'legacy_v1'"
            )
            connection.execute(
                "UPDATE cards SET retrieval_enabled = 0 "
                "WHERE publish_status IN ('SKIPPED', 'CONTAINER')"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_cards_semantic_fingerprint "
                "ON cards(semantic_fingerprint)"
            )
            connection.execute("DROP INDEX IF EXISTS idx_cards_publish_status")
            connection.execute(
                "CREATE INDEX idx_cards_publish_status "
                "ON cards(publish_status, retrieval_enabled, planning_rag_enabled)"
            )
            bundle_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(change_case_bundles)"
                ).fetchall()
            }
            for column, declaration in (
                ("build_generation", "INTEGER NOT NULL DEFAULT 1"),
                ("builder_version", "TEXT NOT NULL DEFAULT ''"),
                ("card_model_version", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in bundle_columns:
                    connection.execute(
                        f"ALTER TABLE change_case_bundles ADD COLUMN {column} {declaration}"
                    )
            # Databases created before case bundles existed already carry the
            # authoritative case_id in card_lineage. Promote those structured
            # change orders to first-class bundles without changing card IDs.
            connection.execute(
                """
                INSERT OR IGNORE INTO change_case_bundles
                    (case_id, document_id, title, extraction_strategy,
                     created_at, updated_at)
                SELECT lineage.case_id, cards.source_document_id,
                       documents.source_name, lineage.extraction_strategy,
                       MIN(lineage.created_at), MAX(cards.updated_at)
                FROM card_lineage AS lineage
                JOIN cards ON cards.id = lineage.card_id
                JOIN documents ON documents.id = cards.source_document_id
                WHERE lineage.extraction_strategy = 'change_order_shape_v2'
                GROUP BY lineage.case_id, cards.source_document_id,
                         documents.source_name, lineage.extraction_strategy
                """
            )

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

    def save_extraction_report(
        self, document_id: int, strategy: str, report: dict[str, Any]
    ) -> None:
        now = utc_now()
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO extraction_reports
                    (document_id, strategy, report, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    strategy = excluded.strategy,
                    report = excluded.report,
                    updated_at = excluded.updated_at
                """,
                (document_id, strategy, serialized, now, now),
            )

    def get_extraction_report(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT strategy, report, created_at, updated_at "
                "FROM extraction_reports WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(str(row["report"]) or "{}")
        if not isinstance(result, dict):
            result = {"report": result}
        result.setdefault("strategy", str(row["strategy"]))
        result["created_at"] = str(row["created_at"])
        result["updated_at"] = str(row["updated_at"])
        return result

    def save_case_bundle(
        self,
        *,
        case_id: str,
        document_id: int,
        title: str,
        extraction_strategy: str,
        builder_version: str = "",
        card_model_version: str = "",
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO change_case_bundles
                    (case_id, document_id, title, extraction_strategy,
                     builder_version, card_model_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    document_id = excluded.document_id,
                    title = excluded.title,
                    extraction_strategy = excluded.extraction_strategy,
                    builder_version = excluded.builder_version,
                    card_model_version = excluded.card_model_version,
                    updated_at = excluded.updated_at
                """,
                (
                    case_id,
                    document_id,
                    title.strip() or case_id,
                    extraction_strategy,
                    builder_version,
                    card_model_version,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _case_bundle_status(status_counts: dict[str, int]) -> str:
        total = sum(status_counts.values())
        if total == 0:
            return "EMPTY"
        if status_counts.get(CardStatus.APPROVED.value, 0) == total:
            return CardStatus.APPROVED.value
        if status_counts.get(CardStatus.REJECTED.value, 0) == total:
            return CardStatus.REJECTED.value
        if status_counts.get(CardStatus.SUPERSEDED.value, 0) == total:
            return CardStatus.SUPERSEDED.value
        reviewable = (
            status_counts.get(CardStatus.DRAFT.value, 0)
            + status_counts.get(CardStatus.PENDING_REVIEW.value, 0)
        )
        if reviewable == total:
            return CardStatus.PENDING_REVIEW.value
        return "PARTIAL"

    @classmethod
    def _decode_case_bundle(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_cards: bool,
    ) -> dict[str, Any]:
        card_rows = connection.execute(
            """
            SELECT cards.*, documents.source_name, documents.source_ref,
                   documents.checksum AS source_checksum,
                   lineage.extraction_strategy AS lineage_strategy,
                   lineage.unit_role AS lineage_unit_role,
                   lineage.unit_pointer AS lineage_unit_pointer,
                   lineage.source_pointers AS lineage_source_pointers,
                   lineage.source_order AS lineage_source_order,
                   lineage.unit_metadata AS lineage_unit_metadata,
                   lineage.created_at AS lineage_created_at
            FROM card_lineage AS lineage
            JOIN cards ON cards.id = lineage.card_id
            JOIN documents ON documents.id = cards.source_document_id
            WHERE lineage.case_id = ?
            ORDER BY lineage.source_order, cards.id
            """,
            (str(row["case_id"]),),
        ).fetchall()
        cards: list[dict[str, Any]] = []
        card_ids: list[int] = []
        status_counts: dict[str, int] = {}
        roles: list[str] = []
        for card_row in card_rows:
            card_ids.append(int(card_row["id"]))
            status_value = str(card_row["status"])
            status_counts[status_value] = status_counts.get(status_value, 0) + 1
            role = str(card_row["lineage_unit_role"])
            if role not in roles:
                roles.append(role)
            if not include_cards:
                continue
            card = cls._decode_card(card_row)
            assert card is not None
            metadata = json.loads(str(card.pop("lineage_unit_metadata") or "{}"))
            lineage = {
                "case_id": str(row["case_id"]),
                "extraction_strategy": str(card.pop("lineage_strategy")),
                "unit_role": role,
                "unit_pointer": str(card.pop("lineage_unit_pointer")),
                "source_pointers": json.loads(
                    str(card.pop("lineage_source_pointers") or "[]")
                ),
                "source_order": int(card.pop("lineage_source_order")),
                "created_at": str(card.pop("lineage_created_at")),
                "unit_metadata": metadata,
            }
            if isinstance(metadata, dict):
                lineage.update(metadata)
            card["lineage"] = lineage
            cards.append(card)

        report_row = connection.execute(
            "SELECT report, strategy, created_at, updated_at "
            "FROM extraction_reports WHERE document_id = ?",
            (int(row["document_id"]),),
        ).fetchone()
        extraction_report: dict[str, Any] | None = None
        if report_row is not None:
            decoded = json.loads(str(report_row["report"] or "{}"))
            extraction_report = decoded if isinstance(decoded, dict) else {"report": decoded}
            extraction_report.setdefault("strategy", str(report_row["strategy"]))
            extraction_report["created_at"] = str(report_row["created_at"])
            extraction_report["updated_at"] = str(report_row["updated_at"])

        result = dict(row)
        result.update(
            status=cls._case_bundle_status(status_counts),
            card_count=sum(status_counts.values()),
            reviewable_count=(
                status_counts.get(CardStatus.DRAFT.value, 0)
                + status_counts.get(CardStatus.PENDING_REVIEW.value, 0)
            ),
            status_counts=status_counts,
            card_ids=card_ids,
            roles=roles,
            extraction_report=extraction_report,
        )
        if include_cards:
            result["cards"] = cards
        return result

    def get_case_bundle(
        self, case_id: str, *, include_cards: bool = True
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT bundles.*, documents.source_name, documents.source_type,
                       documents.source_ref, documents.checksum AS source_checksum
                FROM change_case_bundles AS bundles
                JOIN documents ON documents.id = bundles.document_id
                WHERE bundles.case_id = ?
                """,
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            return self._decode_case_bundle(
                connection, row, include_cards=include_cards
            )

    def list_case_bundles(
        self,
        status: str | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip().upper()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT bundles.*, documents.source_name, documents.source_type,
                       documents.source_ref, documents.checksum AS source_checksum
                FROM change_case_bundles AS bundles
                JOIN documents ON documents.id = bundles.document_id
                ORDER BY bundles.updated_at DESC, bundles.case_id
                """
            ).fetchall()
            bundles = [
                self._decode_case_bundle(connection, row, include_cards=False)
                for row in rows
            ]
        if normalized_status:
            bundles = [
                bundle for bundle in bundles if bundle["status"] == normalized_status
            ]
        start = max(offset, 0)
        end = start + min(max(limit, 1), 2000)
        return bundles[start:end]

    def get_case_source(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT bundles.case_id, bundles.build_generation,
                       bundles.builder_version, bundles.card_model_version,
                       documents.id AS document_id, documents.source_name,
                       documents.source_type, documents.source_ref,
                       documents.checksum AS source_checksum, documents.content
                FROM change_case_bundles AS bundles
                JOIN documents ON documents.id = bundles.document_id
                WHERE bundles.case_id = ?
                """,
                (case_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def purge_case_for_rebuild(
        self,
        case_id: str,
        *,
        expected_checksum: str,
        actor: str,
    ) -> dict[str, Any]:
        """Delete only derived state for one ChangeOrder while retaining source."""

        normalized_actor = actor.strip()
        if not normalized_actor:
            raise StoreError("重建操作者不能为空")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                """
                SELECT bundles.case_id, bundles.document_id,
                       bundles.build_generation, bundles.builder_version,
                       bundles.card_model_version, documents.source_name,
                       documents.source_type, documents.source_ref,
                       documents.checksum AS source_checksum, documents.content
                FROM change_case_bundles AS bundles
                JOIN documents ON documents.id = bundles.document_id
                WHERE bundles.case_id = ?
                """,
                (case_id,),
            ).fetchone()
            if source is None:
                raise StoreError(f"变更案例包不存在: {case_id}")
            checksum = str(source["source_checksum"])
            if checksum != expected_checksum:
                raise StoreError("案例包 source_sha256 已变化，拒绝越界清理")
            card_rows = connection.execute(
                """
                SELECT cards.id, cards.status, cards.reviewer,
                       cards.semantic_fingerprint, cards.retrieval_enabled
                FROM card_lineage AS lineage
                JOIN cards ON cards.id = lineage.card_id
                WHERE lineage.case_id = ?
                ORDER BY cards.id
                """,
                (case_id,),
            ).fetchall()
            card_ids = [int(row["id"]) for row in card_rows]
            fingerprints = {
                str(row["semantic_fingerprint"])
                for row in card_rows
                if str(row["semantic_fingerprint"] or "")
            }
            review_count = sum(
                bool(str(row["reviewer"] or ""))
                or str(row["status"])
                in {
                    CardStatus.APPROVED.value,
                    CardStatus.REJECTED.value,
                    CardStatus.SUPERSEDED.value,
                }
                for row in card_rows
            )
            retrieval_count = sum(bool(row["retrieval_enabled"]) for row in card_rows)
            memory_rows: list[sqlite3.Row] = []
            if card_ids:
                placeholders = ",".join("?" for _ in card_ids)
                memory_rows = connection.execute(
                    f"SELECT backend, memory_id, card_id FROM memory_links "
                    f"WHERE card_id IN ({placeholders})",
                    card_ids,
                ).fetchall()
                for row in memory_rows:
                    connection.execute(
                        """
                        INSERT INTO memory_retirements
                            (backend, memory_id, card_id, case_id, status,
                             attempts, last_error, updated_at)
                        VALUES (?, ?, ?, ?, 'PENDING', 0, '', ?)
                        ON CONFLICT(backend, memory_id) DO UPDATE SET
                            card_id = excluded.card_id,
                            case_id = excluded.case_id,
                            status = 'PENDING', attempts = 0,
                            last_error = '', updated_at = excluded.updated_at
                        """,
                        (
                            str(row["backend"]),
                            str(row["memory_id"]),
                            int(row["card_id"]),
                            case_id,
                            now,
                        ),
                    )
                connection.execute(
                    f"UPDATE cards SET supersedes_id = NULL, updated_at = ? "
                    f"WHERE supersedes_id IN ({placeholders})",
                    [now, *card_ids],
                )
                connection.execute(
                    f"DELETE FROM audit_log WHERE card_id IN ({placeholders})",
                    card_ids,
                )
                connection.execute(
                    f"DELETE FROM cards WHERE id IN ({placeholders})",
                    card_ids,
                )
            connection.execute(
                "DELETE FROM extraction_reports WHERE document_id = ?",
                (int(source["document_id"]),),
            )
            previous_generation = max(int(source["build_generation"] or 1), 1)
            current_generation = previous_generation + 1
            connection.execute(
                """
                UPDATE change_case_bundles
                SET build_generation = ?, updated_at = ?
                WHERE case_id = ?
                """,
                (current_generation, now, case_id),
            )
            detail = {
                "case_id": case_id,
                "source_checksum": checksum,
                "previous_generation": previous_generation,
                "current_generation": current_generation,
                "purged_card_count": len(card_ids),
                "purged_review_count": review_count,
                "purged_fingerprint_count": len(fingerprints),
                "purged_index_count": retrieval_count,
                "queued_memory_retirements": len(memory_rows),
                "old_card_ids": card_ids,
            }
            connection.execute(
                """
                INSERT INTO audit_log
                    (card_id, action, actor, detail, created_at)
                VALUES (NULL, 'CASE_REBUILD_PURGED', ?, ?, ?)
                """,
                (normalized_actor, json.dumps(detail, ensure_ascii=False), now),
            )
        return {**dict(source), **detail}

    def save_card_lineage(
        self,
        card_id: int,
        *,
        case_id: str,
        extraction_strategy: str,
        unit_role: str,
        unit_pointer: str,
        source_pointers: list[str],
        source_order: int,
        unit_metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO card_lineage
                    (card_id, case_id, extraction_strategy, unit_role, unit_pointer,
                     source_pointers, source_order, unit_metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    case_id = excluded.case_id,
                    extraction_strategy = excluded.extraction_strategy,
                    unit_role = excluded.unit_role,
                    unit_pointer = excluded.unit_pointer,
                    source_pointers = excluded.source_pointers,
                    source_order = excluded.source_order,
                    unit_metadata = excluded.unit_metadata
                """,
                (
                    card_id,
                    case_id,
                    extraction_strategy,
                    unit_role,
                    unit_pointer,
                    json.dumps(source_pointers, ensure_ascii=False),
                    source_order,
                    json.dumps(unit_metadata or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def get_card_lineage(self, card_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM card_lineage WHERE card_id = ?", (card_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["source_pointers"] = json.loads(result["source_pointers"] or "[]")
        result["unit_metadata"] = json.loads(result.get("unit_metadata") or "{}")
        if isinstance(result["unit_metadata"], dict):
            result.update(result["unit_metadata"])
        return result

    def save_card_source_items(
        self,
        card_id: int,
        items: list[dict[str, Any]],
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM card_source_items WHERE card_id = ?", (card_id,)
            )
            connection.executemany(
                """
                INSERT INTO card_source_items
                    (card_id, output_field, output_index, source_index,
                     source_pointer, source_hash, char_start, char_end, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        card_id,
                        str(item["output_field"]),
                        int(item["output_index"]),
                        int(item["source_index"]),
                        str(item["source_pointer"]),
                        str(item["source_hash"]),
                        int(item["char_start"]),
                        int(item["char_end"]),
                        now,
                    )
                    for item in items
                ],
            )

    def list_card_source_items(self, card_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM card_source_items WHERE card_id = ? "
                "ORDER BY output_field, output_index",
                (card_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_ingestion(
        self,
        checksum: str,
        owner_token: str,
        *,
        lease_seconds: int,
        force: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        lease_text = (now + timedelta(seconds=max(lease_seconds, 1))).isoformat(
            timespec="seconds"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ingestion_claims WHERE checksum = ?", (checksum,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO ingestion_claims
                        (checksum, status, owner_token, document_id, attempt,
                         lease_expires_at, error, created_at, updated_at)
                    VALUES (?, 'PROCESSING', ?, NULL, 1, ?, '', ?, ?)
                    """,
                    (checksum, owner_token, lease_text, now_text, now_text),
                )
                return {"state": "CLAIMED", "attempt": 1}

            payload = dict(row)
            status = str(payload["status"])
            if (
                not force
                and status == "COMPLETED"
                and payload.get("document_id") is not None
            ):
                return {
                    "state": "COMPLETED",
                    "document_id": int(payload["document_id"]),
                    "attempt": int(payload["attempt"]),
                }
            lease_raw = str(payload.get("lease_expires_at") or "")
            try:
                lease_expires = datetime.fromisoformat(lease_raw)
            except ValueError:
                lease_expires = now - timedelta(seconds=1)
            if status == "PROCESSING" and lease_expires > now:
                return {
                    "state": "PROCESSING",
                    "attempt": int(payload["attempt"]),
                    "lease_expires_at": lease_raw,
                }

            attempt = int(payload["attempt"]) + 1
            connection.execute(
                """
                UPDATE ingestion_claims
                SET status = 'PROCESSING', owner_token = ?, document_id = NULL,
                    attempt = ?, lease_expires_at = ?, error = '', updated_at = ?
                WHERE checksum = ?
                """,
                (owner_token, attempt, lease_text, now_text, checksum),
            )
            return {"state": "CLAIMED", "attempt": attempt}

    def complete_ingestion(
        self, checksum: str, owner_token: str, document_id: int
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_claims
                SET status = 'COMPLETED', document_id = ?, error = '', updated_at = ?
                WHERE checksum = ? AND owner_token = ? AND status = 'PROCESSING'
                """,
                (document_id, utc_now(), checksum, owner_token),
            )
            if cursor.rowcount != 1:
                raise StoreError("知识导入声明已失效，不能提交结果")

    def fail_ingestion(self, checksum: str, owner_token: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE ingestion_claims
                SET status = 'FAILED', error = ?, updated_at = ?
                WHERE checksum = ? AND owner_token = ? AND status = 'PROCESSING'
                """,
                (error[:2000], utc_now(), checksum, owner_token),
            )

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
        semantic_metadata: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        values = draft.to_dict()
        semantic = semantic_metadata or {}
        card_type = str(semantic.get("card_type") or "LEGACY")
        card_model_version = str(
            semantic.get("card_model_version") or "legacy_v1"
        )
        dedup_status = str(
            semantic.get("dedup_status") or comparison.decision.value
        )
        publish_status = str(semantic.get("publish_status") or "CANDIDATE")
        retrieval_enabled = bool(semantic.get("retrieval_enabled", True))
        content_quality = float(semantic.get("content_quality", quality_score))
        planning_rag_enabled = bool(
            semantic.get("planning_rag_enabled", True)
        )
        semantic_fingerprint = str(semantic.get("semantic_fingerprint") or "")
        semantic_payload = semantic.get("semantic_payload") or {}
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cards (
                    title, summary, knowledge_type, scenario, object_type, object_name,
                    applicable_versions, prerequisites, procedure_steps, risks,
                    rollback_steps, validation_steps, keywords,
                    card_type, card_model_version, review_status, dedup_status,
                    content_quality, publish_status, planning_rag_enabled,
                    retrieval_enabled, semantic_fingerprint, semantic_payload,
                    source_document_id, source_chunk_id, evidence_quote, evidence_locator,
                    status, quality_score, quality_issues,
                    comparison_label, comparison_confidence, comparison_reason,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    card_type,
                    card_model_version,
                    status.value,
                    dedup_status,
                    content_quality,
                    publish_status,
                    1 if planning_rag_enabled else 0,
                    1 if retrieval_enabled else 0,
                    semantic_fingerprint,
                    json.dumps(semantic_payload, ensure_ascii=False, sort_keys=True),
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
        result["semantic_payload"] = json.loads(
            result.get("semantic_payload") or "{}"
        )
        result["planning_rag_enabled"] = bool(
            result.get("planning_rag_enabled", 1)
        )
        result["retrieval_enabled"] = bool(result.get("retrieval_enabled", 1))
        for field in (
            "operation",
            "generalized_operation",
            "validation",
            "rollback",
            "impact_analysis",
            "risk_level",
            "risk_control",
            "inferred_risk",
            "instance_parameters",
            "operation_sections",
            "subitems",
            "split_decision",
            "applicable_phases",
            "actions",
            "context",
            "outcome",
            "attachments",
            "source_facts",
            "inferred_facts",
            "unit_id",
            "parent_unit_id",
            "section_path",
            "source_procedure_pointer",
            "validates",
            "rollback_of",
        ):
            if field in result["semantic_payload"]:
                result[field] = result["semantic_payload"][field]
        return result

    def find_card_by_semantic_fingerprint(
        self, semantic_fingerprint: str
    ) -> dict[str, Any] | None:
        fingerprint = semantic_fingerprint.strip()
        if not fingerprint:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cards WHERE semantic_fingerprint = ? "
                "AND retrieval_enabled = 1 AND publish_status = 'INDEXED' "
                "ORDER BY CASE WHEN status = 'APPROVED' THEN 0 ELSE 1 END, id LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return self._decode_card(row)

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

    def delete_card(self, card_id: int, *, actor: str) -> dict[str, Any]:
        actor = actor.strip()
        if not actor:
            raise StoreError("删除操作者不能为空")
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT id, title, status, source_document_id FROM cards WHERE id = ?",
                (card_id,),
            ).fetchone()
            if current is None:
                raise StoreError(f"知识卡片不存在: {card_id}")
            lineage = connection.execute(
                "SELECT case_id FROM card_lineage WHERE card_id = ?", (card_id,)
            ).fetchone()

            memory_rows = connection.execute(
                "SELECT backend, memory_id FROM memory_links WHERE card_id = ?",
                (card_id,),
            ).fetchall()
            for row in memory_rows:
                connection.execute(
                    """
                    INSERT INTO memory_retirements
                        (backend, memory_id, card_id, case_id, status, attempts,
                         last_error, updated_at)
                    VALUES (?, ?, ?, ?, 'PENDING', 0, '', ?)
                    ON CONFLICT(backend, memory_id) DO UPDATE SET
                        card_id = excluded.card_id,
                        case_id = excluded.case_id,
                        status = 'PENDING',
                        last_error = '',
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(row["backend"]),
                        str(row["memory_id"]),
                        card_id,
                        str(lineage["case_id"]) if lineage is not None else "",
                        now,
                    ),
                )

            # A deleted historical card must not remain as another card's
            # supersedes target. Relation, lineage and memory mapping rows use
            # ON DELETE CASCADE and are cleaned in the same transaction.
            connection.execute(
                "UPDATE cards SET supersedes_id = NULL, updated_at = ? "
                "WHERE supersedes_id = ?",
                (now, card_id),
            )
            detail = {
                "deleted_card_id": card_id,
                "title": str(current["title"]),
                "status": str(current["status"]),
                "source_document_id": int(current["source_document_id"]),
                "queued_memory_retirements": len(memory_rows),
            }
            connection.execute(
                """
                INSERT INTO audit_log (card_id, action, actor, detail, created_at)
                VALUES (?, 'CARD_DELETED', ?, ?, ?)
                """,
                (card_id, actor, json.dumps(detail, ensure_ascii=False), now),
            )
            connection.execute("DELETE FROM cards WHERE id = ?", (card_id,))
            if lineage is not None:
                connection.execute(
                    "UPDATE change_case_bundles SET updated_at = ? WHERE case_id = ?",
                    (now, str(lineage["case_id"])),
                )
        return detail

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
                    content_quality = ?, quality_issues = ?, status = ?,
                    review_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    evidence_quote,
                    evidence_locator,
                    quality_score,
                    quality_score,
                    json.dumps(quality_issues, ensure_ascii=False),
                    status.value,
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
            connection.execute(
                "UPDATE change_case_bundles SET updated_at = ? "
                "WHERE case_id = (SELECT case_id FROM card_lineage WHERE card_id = ?)",
                (now, card_id),
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

    def add_relation(
        self,
        card_id: int,
        related_card_id: int,
        *,
        relation_type: str,
        confidence: float,
        reason: str,
    ) -> None:
        if card_id == related_card_id:
            return
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO relations
                    (card_id, related_card_id, relation_type, confidence, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    card_id,
                    related_card_id,
                    relation_type.strip().upper(),
                    max(0.0, min(float(confidence), 1.0)),
                    reason.strip(),
                    utc_now(),
                ),
            )

    def knowledge_graph(
        self,
        status: CardStatus | str | None = None,
        *,
        limit: int = 300,
    ) -> dict[str, Any]:
        """Return a bounded graph projection of governed local knowledge."""

        normalized_status = str(
            status.value if isinstance(status, CardStatus) else status or ""
        ).strip().upper()
        where = ""
        params: list[Any] = []
        if normalized_status and normalized_status != "ALL":
            where = "WHERE cards.status = ?"
            params.append(normalized_status)
        params.append(min(max(int(limit), 1), 500))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT cards.id, cards.title, cards.summary, cards.knowledge_type,
                       cards.card_type, cards.dedup_status, cards.publish_status,
                       cards.retrieval_enabled,
                       cards.object_type, cards.object_name, cards.status,
                       cards.quality_score, cards.comparison_label,
                       cards.source_document_id, cards.updated_at,
                       documents.source_name, documents.source_type,
                       documents.source_ref,
                       lineage.case_id, lineage.unit_role,
                       bundles.title AS case_title
                FROM cards
                JOIN documents ON documents.id = cards.source_document_id
                LEFT JOIN card_lineage AS lineage ON lineage.card_id = cards.id
                LEFT JOIN change_case_bundles AS bundles
                       ON bundles.case_id = lineage.case_id
                {where}
                ORDER BY cards.updated_at DESC, cards.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

            card_ids = [int(row["id"]) for row in rows]
            relation_rows: list[sqlite3.Row] = []
            if card_ids:
                placeholders = ",".join("?" for _ in card_ids)
                relation_rows = connection.execute(
                    f"""
                    SELECT card_id, related_card_id, relation_type,
                           confidence, reason
                    FROM relations
                    WHERE card_id IN ({placeholders})
                      AND related_card_id IN ({placeholders})
                    ORDER BY id
                    """,
                    [*card_ids, *card_ids],
                ).fetchall()

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        source_nodes: dict[int, dict[str, Any]] = {}
        object_nodes: dict[str, dict[str, Any]] = {}
        case_nodes: dict[str, dict[str, Any]] = {}

        def add_edge(
            source: str,
            target: str,
            relation_type: str,
            *,
            confidence: float = 1.0,
            reason: str = "",
            explicit: bool = False,
        ) -> None:
            edge_key = f"{source}\0{target}\0{relation_type}"
            edges.append(
                {
                    "id": "edge:" + hashlib.sha256(
                        edge_key.encode("utf-8")
                    ).hexdigest()[:20],
                    "source": source,
                    "target": target,
                    "relation_type": relation_type,
                    "confidence": round(float(confidence), 4),
                    "reason": reason,
                    "explicit": explicit,
                }
            )

        for row in rows:
            card_id = int(row["id"])
            card_node_id = f"card:{card_id}"
            source_document_id = int(row["source_document_id"])
            source_node_id = f"source:{source_document_id}"
            object_key = f"{row['object_type']}\0{row['object_name']}"
            object_node_id = "object:" + hashlib.sha256(
                object_key.encode("utf-8")
            ).hexdigest()[:20]
            nodes.append(
                {
                    "id": card_node_id,
                    "kind": "card",
                    "entity_id": card_id,
                    "label": str(row["title"]),
                    "summary": str(row["summary"]),
                    "status": str(row["status"]),
                    "knowledge_type": str(row["knowledge_type"]),
                    "card_type": str(row["card_type"]),
                    "dedup_status": str(row["dedup_status"]),
                    "publish_status": str(row["publish_status"]),
                    "retrieval_enabled": bool(row["retrieval_enabled"]),
                    "object_type": str(row["object_type"]),
                    "object_name": str(row["object_name"]),
                    "quality_score": round(float(row["quality_score"]), 1),
                    "comparison_label": str(row["comparison_label"]),
                    "unit_role": str(row["unit_role"] or ""),
                    "updated_at": str(row["updated_at"]),
                }
            )
            source_nodes.setdefault(
                source_document_id,
                {
                    "id": source_node_id,
                    "kind": "source",
                    "entity_id": source_document_id,
                    "label": str(row["source_name"]),
                    "source_type": str(row["source_type"]),
                    "source_ref": str(row["source_ref"]),
                },
            )
            object_nodes.setdefault(
                object_node_id,
                {
                    "id": object_node_id,
                    "kind": "object",
                    "label": str(row["object_name"]),
                    "object_type": str(row["object_type"]),
                },
            )
            add_edge(source_node_id, card_node_id, "SOURCE_OF")
            add_edge(card_node_id, object_node_id, "DESCRIBES")
            case_id = str(row["case_id"] or "")
            if case_id:
                case_node_id = "case:" + hashlib.sha256(
                    case_id.encode("utf-8")
                ).hexdigest()[:20]
                case_nodes.setdefault(
                    case_node_id,
                    {
                        "id": case_node_id,
                        "kind": "case",
                        "entity_id": case_id,
                        "label": str(row["case_title"] or case_id),
                    },
                )
                add_edge(case_node_id, card_node_id, "CONTAINS")

        card_node_ids = {card_id: f"card:{card_id}" for card_id in card_ids}
        for row in relation_rows:
            add_edge(
                card_node_ids[int(row["card_id"])],
                card_node_ids[int(row["related_card_id"])],
                str(row["relation_type"]),
                confidence=float(row["confidence"]),
                reason=str(row["reason"]),
                explicit=True,
            )

        nodes.extend(source_nodes.values())
        nodes.extend(object_nodes.values())
        nodes.extend(case_nodes.values())
        kind_counts = {
            kind: sum(1 for node in nodes if node["kind"] == kind)
            for kind in ("card", "case", "object", "source")
        }
        return {
            "nodes": nodes,
            "edges": edges,
            "meta": {
                "status_filter": normalized_status or "ALL",
                "card_limit": min(max(int(limit), 1), 500),
                "node_count": len(nodes),
                "edge_count": len(edges),
                "explicit_relation_count": len(relation_rows),
                "nodes_by_kind": kind_counts,
            },
        }

    def list_audit(self, card_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE card_id = ? ORDER BY id DESC", (card_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _validate_structured_card_evidence(
        connection: sqlite3.Connection,
        current: sqlite3.Row,
    ) -> None:
        lineage = connection.execute(
            "SELECT extraction_strategy, unit_role, source_pointers, unit_metadata "
            "FROM card_lineage WHERE card_id = ?",
            (int(current["id"]),),
        ).fetchone()
        if lineage is None:
            report_row = connection.execute(
                "SELECT strategy FROM extraction_reports WHERE document_id = ?",
                (int(current["source_document_id"]),),
            ).fetchone()
            if report_row is not None and str(report_row["strategy"]) == "change_order_shape_v2":
                raise StoreError("结构化知识尚未完成 lineage 与证据矩阵写入")
            return
        if str(lineage["extraction_strategy"]) != "change_order_shape_v2":
            return

        metadata = json.loads(str(lineage["unit_metadata"] or "{}"))
        quality_policy = metadata.get("quality_policy_version")
        if quality_policy not in {
            "change_order_role_v2",
            "change_order_semantic_v1",
        }:
            # Existing cards created before the structured evidence matrix remain
            # reviewable under the legacy exact-quote gate.
            return
        if metadata.get("evidence_mode") != "STRUCTURED_JSON_POINTERS":
            raise StoreError("结构化知识缺少受信任的 JSON Pointer 证据模式")
        coverage_status = (
            metadata.get("structural_source_coverage_status")
            if quality_policy == "change_order_semantic_v1"
            else metadata.get("content_coverage_status")
        )
        if coverage_status != "COMPLETE":
            raise StoreError("结构化知识的逐源记录覆盖不完整，不能批准")
        if quality_policy == "change_order_semantic_v1":
            qa = metadata.get("qa") or {}
            blockers = [
                name
                for name in (
                    "has_raw_json",
                    "has_html_residue",
                    "has_empty_required_section",
                    "parent_child_retrieval_collision",
                )
                if qa.get(name) is True
            ]
            if blockers:
                raise StoreError(
                    "语义知识正文 QA 未通过，不能批准：" + "、".join(blockers)
                )

        references = metadata.get("source_evidence_refs")
        if not isinstance(references, list) or not references:
            raise StoreError("结构化知识缺少逐源记录证据")
        try:
            expected = int(metadata.get("expected_source_items"))
        except (TypeError, ValueError) as exc:
            raise StoreError("结构化知识的预期源记录数无效") from exc
        if expected != len(references):
            raise StoreError("结构化知识的证据清单与预期源记录数不一致")

        rows = connection.execute(
            "SELECT output_field, output_index, source_index, source_pointer, "
            "source_hash, char_start, char_end FROM card_source_items "
            "WHERE card_id = ? ORDER BY output_index",
            (int(current["id"]),),
        ).fetchall()
        if len(rows) != expected:
            raise StoreError("结构化知识的逐源记录证据数量不完整")

        role = str(lineage["unit_role"])
        expected_field = (
            "__evidence__"
            if quality_policy == "change_order_semantic_v1"
            else {
                "TASKS_CANONICAL": "procedure_steps",
                "PRECHECK_STEPS": "procedure_steps",
                "IMPLEMENTATION_STEPS": "procedure_steps",
                "VALIDATION_STEPS": "validation_steps",
                "ROLLBACK_STEPS": "rollback_steps",
            }.get(role, "__evidence__")
        )
        source_pointers = json.loads(str(lineage["source_pointers"] or "[]"))
        if source_pointers != [str(item.get("pointer")) for item in references]:
            raise StoreError("结构化知识的 lineage 与证据 Pointer 不一致")

        document = connection.execute(
            "SELECT content FROM documents WHERE id = ?",
            (int(current["source_document_id"]),),
        ).fetchone()
        if document is None:
            raise StoreError("结构化知识的来源文档不存在")
        content = str(document["content"])

        for output_index, (row, reference) in enumerate(zip(rows, references)):
            try:
                char_start = int(reference["char_start"])
                char_end = int(reference["char_end"])
                source_index = int(reference["source_index"])
                source_pointer = str(reference["pointer"])
                expected_hash = str(reference["content_sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StoreError("结构化知识包含无效的逐源证据记录") from exc
            if not 0 <= char_start < char_end <= len(content):
                raise StoreError("结构化知识的证据字符范围越界")
            actual_hash = hashlib.sha256(
                content[char_start:char_end].encode("utf-8")
            ).hexdigest()
            if actual_hash != expected_hash:
                raise StoreError("结构化知识的来源内容哈希已漂移，必须重新抽取")
            if (
                str(row["output_field"]) != expected_field
                or int(row["output_index"]) != output_index
                or int(row["source_index"]) != source_index
                or str(row["source_pointer"]) != source_pointer
                or str(row["source_hash"]) != expected_hash
                or int(row["char_start"]) != char_start
                or int(row["char_end"]) != char_end
            ):
                raise StoreError("结构化知识的输出项与逐源证据映射不一致")

    @classmethod
    def _validate_card_approval(
        cls,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
    ) -> None:
        quality_issues = json.loads(current["quality_issues"] or "[]")
        evidence_issues = [issue for issue in quality_issues if "证据" in str(issue)]
        if not current["evidence_quote"] or evidence_issues:
            raise StoreError(
                "该知识缺少可定位的原文证据，不能批准发布；请补充来源后重新抽取。"
            )
        blocking_issues = [
            str(issue)
            for issue in quality_issues
            if str(issue).startswith("阻断：")
        ]
        if blocking_issues:
            raise StoreError(
                "该知识所属变更单的结构覆盖或双视图对账未通过，不能批准发布："
                + "；".join(blocking_issues)
            )
        cls._validate_structured_card_evidence(connection, current)
        report_row = connection.execute(
            "SELECT report FROM extraction_reports WHERE document_id = ?",
            (int(current["source_document_id"]),),
        ).fetchone()
        if report_row is None:
            return
        report = json.loads(str(report_row["report"]) or "{}")
        change_report = report.get("change_order") if isinstance(report, dict) else None
        if (
            isinstance(change_report, dict)
            and change_report.get("matched") is True
            and not change_report.get(
                "safe_for_internal_index",
                change_report.get("safe_to_publish", False),
            )
        ):
            blockers = change_report.get("blockers") or ["结构完整性检查未通过"]
            raise StoreError(
                "该知识所属变更单的结构覆盖或双视图对账未通过，不能批准发布："
                + "；".join(str(item) for item in blockers)
            )

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
                "SELECT id, status, evidence_quote, quality_issues, source_document_id "
                "FROM cards WHERE id = ?",
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
                self._validate_card_approval(connection, current)

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
                    "UPDATE cards SET status = ?, review_status = ?, updated_at = ? WHERE id = ?",
                    (
                        CardStatus.SUPERSEDED.value,
                        CardStatus.SUPERSEDED.value,
                        now,
                        supersedes_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE cards
                    SET status = ?, review_status = ?, supersedes_id = ?, reviewer = ?, review_comment = ?,
                        published_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        CardStatus.APPROVED.value,
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
                    SET status = ?, review_status = ?, reviewer = ?, review_comment = ?,
                        published_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        new_status,
                        reviewer,
                        comment,
                        published_at,
                        now,
                        card_id,
                    ),
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
            connection.execute(
                "UPDATE change_case_bundles SET updated_at = ? "
                "WHERE case_id = (SELECT case_id FROM card_lineage WHERE card_id = ?)",
                (now, card_id),
            )
        card = self.get_card(card_id)
        if card is None:
            raise StoreError("审核后无法读取知识卡片")
        return card

    def review_case_bundle(
        self,
        case_id: str,
        *,
        action: str,
        reviewer: str,
        comment: str = "",
    ) -> dict[str, Any]:
        action = action.strip().upper()
        reviewer = reviewer.strip()
        if not reviewer:
            raise StoreError("审核人不能为空")
        if action not in {"APPROVE", "REJECT"}:
            raise StoreError("案例包审核动作必须是 APPROVE 或 REJECT")

        now = utc_now()
        with self.connect() as connection:
            bundle = connection.execute(
                "SELECT case_id FROM change_case_bundles WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if bundle is None:
                raise StoreError(f"案例包不存在: {case_id}")
            cards = connection.execute(
                """
                SELECT cards.id, cards.status, cards.evidence_quote,
                       cards.quality_issues, cards.source_document_id
                FROM card_lineage AS lineage
                JOIN cards ON cards.id = lineage.card_id
                WHERE lineage.case_id = ?
                ORDER BY lineage.source_order, cards.id
                """,
                (case_id,),
            ).fetchall()
            if not cards:
                raise StoreError("案例包没有可审核的知识卡片")
            reviewable_statuses = {
                CardStatus.DRAFT.value,
                CardStatus.PENDING_REVIEW.value,
            }
            target_status = (
                CardStatus.APPROVED.value
                if action == "APPROVE"
                else CardStatus.REJECTED.value
            )
            conflicting = [
                f"K{int(card['id'])}={card['status']}"
                for card in cards
                if card["status"] not in reviewable_statuses
                and card["status"] != target_status
            ]
            if conflicting:
                raise StoreError(
                    "案例包含有与目标状态冲突的已审核子卡，不能整包变更；"
                    + "、".join(conflicting)
                )
            if action == "APPROVE":
                # Validate every child before the first write. A single failed
                # evidence/coverage gate therefore rolls back the whole bundle.
                for card in cards:
                    self._validate_card_approval(connection, card)

            new_status = target_status
            published_at = now if action == "APPROVE" else None
            for card in cards:
                if card["status"] not in reviewable_statuses:
                    continue
                card_id = int(card["id"])
                connection.execute(
                    """
                    UPDATE cards
                    SET status = ?, review_status = ?, reviewer = ?, review_comment = ?,
                        published_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_status,
                        new_status,
                        reviewer,
                        comment,
                        published_at,
                        now,
                        card_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO audit_log
                        (card_id, action, actor, detail, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        card_id,
                        f"CASE_BUNDLE_{action}",
                        reviewer,
                        json.dumps(
                            {"case_id": case_id, "comment": comment},
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE change_case_bundles SET updated_at = ? WHERE case_id = ?",
                (now, case_id),
            )

        reviewed = self.get_case_bundle(case_id)
        if reviewed is None:
            raise StoreError("审核后无法读取案例包")
        return reviewed

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
            bundle_count = connection.execute(
                "SELECT COUNT(*) AS count FROM change_case_bundles"
            ).fetchone()["count"]
            average_quality = connection.execute(
                "SELECT COALESCE(AVG(quality_score), 0) AS value FROM cards"
            ).fetchone()["value"]
        statuses = {status.value: 0 for status in CardStatus}
        statuses.update({row["status"]: row["count"] for row in status_rows})
        return {
            "documents": document_count,
            "cards": sum(statuses.values()),
            "case_bundles": bundle_count,
            "relations": relation_count,
            "average_quality": round(float(average_quality), 1),
            "statuses": statuses,
        }

    def get_memory_sync_state(
        self, card_id: int, backend: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_sync_state WHERE card_id = ? AND backend = ?",
                (card_id, backend),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["detail"] = json.loads(result["detail"] or "{}")
        return result

    def claim_memory_sync(
        self,
        card_id: int,
        *,
        backend: str,
        content_hash: str,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        lease_text = (now + timedelta(seconds=max(lease_seconds, 1))).isoformat(
            timespec="seconds"
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            card = connection.execute(
                "SELECT status FROM cards WHERE id = ?", (card_id,)
            ).fetchone()
            if card is None:
                raise StoreError(f"知识卡片不存在: {card_id}")
            if str(card["status"]) != CardStatus.APPROVED.value:
                return {"state": "NOT_APPROVED"}
            row = connection.execute(
                "SELECT * FROM memory_sync_state WHERE card_id = ? AND backend = ?",
                (card_id, backend),
            ).fetchone()
            if row is not None:
                current = dict(row)
                if (
                    str(current["status"]) == "SUCCEEDED"
                    and str(current["content_hash"]) == content_hash
                    and int(current["memory_count"]) > 0
                ):
                    return {
                        "state": "ALREADY_SYNCED",
                        "memory_count": int(current["memory_count"]),
                    }
                lease_raw = str(current.get("lease_expires_at") or "")
                try:
                    lease_expires = datetime.fromisoformat(lease_raw)
                except ValueError:
                    lease_expires = now - timedelta(seconds=1)
                if str(current["status"]) == "SYNCING" and lease_expires > now:
                    return {
                        "state": "SYNC_IN_PROGRESS",
                        "lease_expires_at": lease_raw,
                        "attempt": int(current.get("attempt") or 0),
                    }
                attempt = int(current.get("attempt") or 0) + 1
            else:
                attempt = 1
            connection.execute(
                """
                INSERT INTO memory_sync_state
                    (card_id, backend, content_hash, status, memory_count, detail,
                     owner_token, lease_expires_at, attempt, updated_at)
                VALUES (?, ?, ?, 'SYNCING', 0, '{}', ?, ?, ?, ?)
                ON CONFLICT(card_id, backend) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    status = 'SYNCING',
                    memory_count = 0,
                    detail = '{}',
                    owner_token = excluded.owner_token,
                    lease_expires_at = excluded.lease_expires_at,
                    attempt = excluded.attempt,
                    updated_at = excluded.updated_at
                """,
                (
                    card_id,
                    backend,
                    content_hash,
                    owner_token,
                    lease_text,
                    attempt,
                    now_text,
                ),
            )
            return {
                "state": "CLAIMED",
                "attempt": attempt,
                "lease_expires_at": lease_text,
            }

    def record_memory_sync_success(
        self,
        card_id: int,
        *,
        backend: str,
        content_hash: str,
        memory_ids: list[str],
        detail: dict[str, Any],
        owner_token: str,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(item.strip() for item in memory_ids if item.strip()))
        if not unique_ids:
            raise StoreError("长期记忆同步结果缺少 memory_id")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT id FROM cards WHERE id = ?", (card_id,)
            ).fetchone() is None:
                raise StoreError(f"知识卡片不存在: {card_id}")
            claim = connection.execute(
                """
                SELECT status, owner_token, content_hash FROM memory_sync_state
                WHERE card_id = ? AND backend = ?
                """,
                (card_id, backend),
            ).fetchone()
            if (
                claim is None
                or str(claim["status"]) != "SYNCING"
                or str(claim["owner_token"]) != owner_token
                or str(claim["content_hash"]) != content_hash
            ):
                return {"applied": False, "retired_memory_ids": []}
            lineage = connection.execute(
                "SELECT case_id FROM card_lineage WHERE card_id = ?",
                (card_id,),
            ).fetchone()
            retirement_case_id = (
                str(lineage["case_id"]) if lineage is not None else ""
            )
            previous_ids = {
                str(row["memory_id"])
                for row in connection.execute(
                    "SELECT memory_id FROM memory_links WHERE card_id = ? AND backend = ?",
                    (card_id, backend),
                ).fetchall()
            }
            retired_ids = sorted(previous_ids - set(unique_ids))
            for memory_id in retired_ids:
                connection.execute(
                    """
                    INSERT INTO memory_retirements
                        (backend, memory_id, card_id, case_id, status, attempts,
                         last_error, updated_at)
                    VALUES (?, ?, ?, ?, 'PENDING', 0, '', ?)
                    ON CONFLICT(backend, memory_id) DO UPDATE SET
                        card_id = excluded.card_id,
                        case_id = excluded.case_id,
                        status = 'PENDING',
                        last_error = '',
                        updated_at = excluded.updated_at
                    """,
                    (backend, memory_id, card_id, retirement_case_id, now),
                )
            for memory_id in unique_ids:
                connection.execute(
                    "DELETE FROM memory_retirements WHERE backend = ? AND memory_id = ?",
                    (backend, memory_id),
                )
            connection.execute(
                "DELETE FROM memory_links WHERE card_id = ? AND backend = ?",
                (card_id, backend),
            )
            connection.executemany(
                """
                INSERT INTO memory_links
                    (backend, memory_id, card_id, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(backend, memory_id) DO UPDATE SET
                    card_id = excluded.card_id,
                    content_hash = excluded.content_hash,
                    created_at = excluded.created_at
                """,
                [
                    (backend, memory_id, card_id, content_hash, now)
                    for memory_id in unique_ids
                ],
            )
            connection.execute(
                """
                INSERT INTO memory_sync_state
                    (card_id, backend, content_hash, status, memory_count, detail,
                     owner_token, lease_expires_at, attempt, updated_at)
                VALUES (?, ?, ?, 'SUCCEEDED', ?, ?, '', '', 1, ?)
                ON CONFLICT(card_id, backend) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    memory_count = excluded.memory_count,
                    detail = excluded.detail,
                    owner_token = '',
                    lease_expires_at = '',
                    updated_at = excluded.updated_at
                """,
                (
                    card_id,
                    backend,
                    content_hash,
                    len(unique_ids),
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
        return {"applied": True, "retired_memory_ids": retired_ids}

    def record_memory_sync_failure(
        self,
        card_id: int,
        *,
        backend: str,
        content_hash: str,
        error: str,
        owner_token: str,
    ) -> bool:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE memory_sync_state
                SET status = 'FAILED', memory_count = 0, detail = ?,
                    owner_token = '', lease_expires_at = '', updated_at = ?
                WHERE card_id = ? AND backend = ? AND status = 'SYNCING'
                  AND owner_token = ? AND content_hash = ?
                """,
                (
                    json.dumps({"error": error[:2000]}, ensure_ascii=False),
                    now,
                    card_id,
                    backend,
                    owner_token,
                    content_hash,
                ),
            )
        return cursor.rowcount == 1

    def retire_unapproved_memory_links(self, *, backend: str) -> int:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT links.memory_id, links.card_id,
                       COALESCE(lineage.case_id, '') AS case_id
                FROM memory_links AS links
                JOIN cards ON cards.id = links.card_id
                LEFT JOIN card_lineage AS lineage ON lineage.card_id = links.card_id
                WHERE links.backend = ? AND cards.status != ?
                """,
                (backend, CardStatus.APPROVED.value),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO memory_retirements
                        (backend, memory_id, card_id, case_id, status, attempts,
                         last_error, updated_at)
                    VALUES (?, ?, ?, ?, 'PENDING', 0, '', ?)
                    ON CONFLICT(backend, memory_id) DO UPDATE SET
                        card_id = excluded.card_id,
                        case_id = excluded.case_id,
                        status = 'PENDING', updated_at = excluded.updated_at
                    """,
                    (
                        backend,
                        str(row["memory_id"]),
                        int(row["card_id"]),
                        str(row["case_id"]),
                        now,
                    ),
                )
            if rows:
                connection.executemany(
                    "DELETE FROM memory_links WHERE backend = ? AND memory_id = ?",
                    [(backend, str(row["memory_id"])) for row in rows],
                )
                connection.executemany(
                    """
                    UPDATE memory_sync_state SET status = 'RETIRED', memory_count = 0,
                        owner_token = '', lease_expires_at = '', updated_at = ?
                    WHERE card_id = ? AND backend = ?
                    """,
                    [(now, int(row["card_id"]), backend) for row in rows],
                )
        return len(rows)

    def list_memory_retirements(
        self,
        *,
        backend: str,
        limit: int = 100,
        case_id: str | None = None,
    ) -> list[dict[str, Any]]:
        parameters: list[Any] = [backend]
        case_filter = ""
        if case_id is not None:
            case_filter = " AND case_id = ?"
            parameters.append(case_id)
        parameters.append(max(1, min(limit, 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_retirements
                WHERE backend = ? AND status IN ('PENDING', 'FAILED')
                {case_filter}
                ORDER BY updated_at ASC, memory_id ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_memory_retirements(
        self, *, backend: str, case_id: str | None = None
    ) -> int:
        parameters: list[Any] = [backend]
        case_filter = ""
        if case_id is not None:
            case_filter = " AND case_id = ?"
            parameters.append(case_id)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM memory_retirements
                WHERE backend = ? AND status IN ('PENDING', 'FAILED')
                {case_filter}
                """,
                parameters,
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def record_memory_retirement(
        self, *, backend: str, memory_id: str, error: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not error:
                connection.execute(
                    "DELETE FROM memory_retirements WHERE backend = ? AND memory_id = ?",
                    (backend, memory_id),
                )
                return
            connection.execute(
                """
                UPDATE memory_retirements
                SET status = 'FAILED', attempts = attempts + 1,
                    last_error = ?, updated_at = ?
                WHERE backend = ? AND memory_id = ?
                """,
                (error[:2000], utc_now(), backend, memory_id),
            )

    def card_ids_for_memory_ids(
        self, memory_ids: list[str], *, backend: str
    ) -> dict[str, int]:
        unique_ids = list(dict.fromkeys(item for item in memory_ids if item))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT links.memory_id, links.card_id FROM memory_links AS links
                JOIN memory_sync_state AS state
                  ON state.card_id = links.card_id AND state.backend = links.backend
                WHERE links.backend = ? AND links.memory_id IN ({placeholders})
                  AND state.status = 'SUCCEEDED'
                  AND state.content_hash = links.content_hash
                """,
                [backend, *unique_ids],
            ).fetchall()
        return {str(row["memory_id"]): int(row["card_id"]) for row in rows}

    def memory_sync_stats(self, backend: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM memory_sync_state WHERE backend = ? GROUP BY status
                """,
                (backend,),
            ).fetchall()
            link_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_links WHERE backend = ?",
                    (backend,),
                ).fetchone()["count"]
            )
            latest = connection.execute(
                "SELECT MAX(updated_at) AS value FROM memory_sync_state WHERE backend = ?",
                (backend,),
            ).fetchone()["value"]
            retirement_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM memory_retirements WHERE backend = ?",
                    (backend,),
                ).fetchone()["count"]
            )
        statuses = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "cards": sum(statuses.values()),
            "memory_links": link_count,
            "pending_retirements": retirement_count,
            "statuses": statuses,
            "last_updated_at": latest,
        }
