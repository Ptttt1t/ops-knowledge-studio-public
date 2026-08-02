from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from knowledge_platform.store import utc_now

from .schema import (
    ChangeStatus,
    ChangeTicket,
    ExecutionRecord,
    FeedbackRecord,
    ValidationResult,
)


class ChangeStoreError(RuntimeError):
    """Raised when a change lifecycle invariant is violated."""


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ChangeStatus.DRAFT.value: {
        ChangeStatus.BLOCKED.value,
        ChangeStatus.READY_FOR_APPROVAL.value,
    },
    ChangeStatus.READY_FOR_APPROVAL.value: {ChangeStatus.WAITING_APPROVAL.value},
    ChangeStatus.WAITING_APPROVAL.value: {
        ChangeStatus.APPROVED.value,
        ChangeStatus.BLOCKED.value,
        ChangeStatus.REJECTED.value,
    },
    ChangeStatus.APPROVED.value: {
        ChangeStatus.EXECUTING.value,
        ChangeStatus.BLOCKED.value,
    },
    ChangeStatus.EXECUTING.value: {
        ChangeStatus.BLOCKED.value,
        ChangeStatus.VERIFYING.value,
        ChangeStatus.FAILED.value,
    },
    ChangeStatus.VERIFYING.value: {
        ChangeStatus.SUCCEEDED.value,
        ChangeStatus.ROLLED_BACK.value,
        ChangeStatus.FAILED.value,
    },
}


class ChangeStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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
        CREATE TABLE IF NOT EXISTS change_tickets (
            ticket_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            plan_hash TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS change_validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL REFERENCES change_tickets(ticket_id) ON DELETE CASCADE,
            phase TEXT NOT NULL,
            validator TEXT NOT NULL,
            status TEXT NOT NULL,
            hard_gate INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS change_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL REFERENCES change_tickets(ticket_id) ON DELETE CASCADE,
            run_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(ticket_id, run_id)
        );

        CREATE TABLE IF NOT EXISTS change_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL REFERENCES change_tickets(ticket_id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS change_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL REFERENCES change_tickets(ticket_id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save_ticket(self, ticket: ChangeTicket) -> dict[str, Any]:
        if not ticket.plan_hash or ticket.plan_hash != ticket.compute_plan_hash():
            raise ChangeStoreError("变更单必须以当前内容计算并封存 plan_hash")
        now = utc_now()
        payload = ticket.to_dict()
        with self.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO change_tickets
                        (ticket_id, revision, status, plan_hash, snapshot_version,
                         payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket.ticket_id,
                        ticket.revision,
                        ticket.status.value,
                        ticket.plan_hash,
                        ticket.environment_snapshot_version,
                        self._json(payload),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChangeStoreError(f"变更单已存在: {ticket.ticket_id}") from exc
            self._insert_audit(
                connection,
                ticket.ticket_id,
                "TICKET_CREATED",
                ticket.requested_by,
                {"status": ticket.status.value, "plan_hash": ticket.plan_hash},
            )
        result = self.get_ticket(ticket.ticket_id)
        if result is None:
            raise ChangeStoreError("变更单保存后无法读取")
        return result

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM change_tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        payload["status"] = str(row["status"])
        payload["created_at"] = str(row["created_at"])
        payload["updated_at"] = str(row["updated_at"])
        return payload

    def require_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ChangeStoreError(f"变更单不存在: {ticket_id}")
        return ticket

    def update_status(
        self,
        ticket_id: str,
        status: ChangeStatus | str,
        *,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = status.value if isinstance(status, ChangeStatus) else str(status).upper()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM change_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise ChangeStoreError(f"变更单不存在: {ticket_id}")
            current = str(row["status"])
            if current == target:
                payload = json.loads(str(row["payload_json"]))
                payload["status"] = current
                payload["created_at"] = str(row["created_at"])
                payload["updated_at"] = str(row["updated_at"])
                return payload
            if target not in ALLOWED_TRANSITIONS.get(current, set()):
                raise ChangeStoreError(f"非法变更单状态迁移: {current} -> {target}")
            payload = json.loads(str(row["payload_json"]))
            payload["status"] = target
            now = utc_now()
            connection.execute(
                """
                UPDATE change_tickets
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (target, self._json(payload), now, ticket_id),
            )
            self._insert_audit(
                connection,
                ticket_id,
                "STATUS_CHANGED",
                actor.strip() or "system",
                {"from": current, "to": target, **(detail or {})},
                created_at=now,
            )
        return self.require_ticket(ticket_id)

    def record_validation(self, ticket_id: str, result: ValidationResult) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO change_validations
                    (ticket_id, phase, validator, status, hard_gate, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    result.phase,
                    result.validator,
                    result.status,
                    1 if result.hard_gate else 0,
                    self._json(result.to_dict()),
                    utc_now(),
                ),
            )

    def list_validations(self, ticket_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM change_validations WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def record_execution(self, record: ExecutionRecord) -> dict[str, Any]:
        payload = record.to_dict()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO change_executions
                    (ticket_id, run_id, outcome, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.ticket_id,
                    record.run_id,
                    record.outcome,
                    self._json(payload),
                    utc_now(),
                ),
            )
        return payload

    def latest_execution(self, ticket_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM change_executions
                WHERE ticket_id = ? ORDER BY id DESC LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()
        return json.loads(str(row["payload_json"])) if row is not None else None

    def record_feedback(self, feedback: FeedbackRecord) -> dict[str, Any]:
        payload = feedback.to_dict()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO change_feedback (ticket_id, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (feedback.ticket_id, self._json(payload), utc_now()),
            )
        return payload

    def latest_feedback(self, ticket_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM change_feedback
                WHERE ticket_id = ? ORDER BY id DESC LIMIT 1
                """,
                (ticket_id,),
            ).fetchone()
        return json.loads(str(row["payload_json"])) if row is not None else None

    def list_audit(self, ticket_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM change_audit WHERE ticket_id = ? ORDER BY id", (ticket_id,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(str(item.pop("detail_json")))
            result.append(item)
        return result

    def add_audit(
        self,
        ticket_id: str,
        action: str,
        actor: str,
        detail: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            self._insert_audit(connection, ticket_id, action, actor, detail)

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        ticket_id: str,
        action: str,
        actor: str,
        detail: dict[str, Any],
        *,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO change_audit (ticket_id, action, actor, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                action,
                actor.strip() or "system",
                self._json(detail),
                created_at or utc_now(),
            ),
        )
