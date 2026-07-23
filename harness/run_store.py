from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4


TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStoreError(RuntimeError):
    """Raised when a persisted runtime state transition is invalid."""


class RunStore:
    """SQLite persistence for durable Harness runs, steps, events and checkpoints."""

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
        CREATE TABLE IF NOT EXISTS runtime_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL,
            input_json TEXT NOT NULL,
            budget_json TEXT NOT NULL,
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            idempotency_key TEXT,
            attempt INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(task_type, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_runs_status_created ON runs(status, created_at);

        CREATE TABLE IF NOT EXISTS run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            input_json TEXT NOT NULL,
            output_json TEXT,
            error_code TEXT,
            error_message TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(run_id, step_index)
        );

        CREATE TABLE IF NOT EXISTS run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id, id);

        CREATE TABLE IF NOT EXISTS run_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            checkpoint_index INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, checkpoint_index)
        );

        CREATE TABLE IF NOT EXISTS tool_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            tool_name TEXT NOT NULL,
            decision TEXT NOT NULL,
            actor TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT OR IGNORE INTO runtime_schema_migrations (version, applied_at) VALUES (1, ?)",
                (utc_now(),),
            )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

    @staticmethod
    def _decode_json(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback

    @classmethod
    def _decode_run(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["input"] = cls._decode_json(result.pop("input_json"), {})
        result["budget"] = cls._decode_json(result.pop("budget_json"), {})
        result["result"] = cls._decode_json(result.pop("result_json"), None)
        return result

    @classmethod
    def _decode_step(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["input"] = cls._decode_json(result.pop("input_json"), {})
        result["output"] = cls._decode_json(result.pop("output_json"), None)
        return result

    @classmethod
    def _decode_event(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = cls._decode_json(result.pop("payload_json"), {})
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._decode_run(row)

    def find_idempotent(self, task_type: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE task_type = ? AND idempotency_key = ?",
                (task_type, key),
            ).fetchone()
        return self._decode_run(row)

    def create_run(
        self,
        *,
        task_type: str,
        payload: dict[str, Any],
        budget: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        existing = self.find_idempotent(task_type, idempotency_key)
        if existing is not None:
            return existing, False
        now = utc_now()
        run_id = uuid4().hex
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runs (
                        id, task_type, status, input_json, budget_json, idempotency_key,
                        created_at, updated_at
                    ) VALUES (?, ?, 'QUEUED', ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_type,
                        self._json(payload),
                        self._json(budget),
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                self._insert_event(connection, run_id, "run.queued", {"task_type": task_type})
        except sqlite3.IntegrityError:
            existing = self.find_idempotent(task_type, idempotency_key)
            if existing is not None:
                return existing, False
            raise
        created = self.get_run(run_id)
        if created is None:
            raise RunStoreError("创建运行后无法读取运行记录")
        return created, True

    def list_runs(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status.upper())
        params.append(min(max(limit, 1), 1000))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [decoded for row in rows if (decoded := self._decode_run(row)) is not None]

    def queued_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM runs WHERE status = 'QUEUED'").fetchone()
        return int(row["count"]) if row is not None else 0

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO run_events (run_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, event_type, self._json(payload), utc_now()),
        )

    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            self._insert_event(connection, run_id, event_type, payload)

    def list_events(
        self, run_id: str, *, after_id: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (run_id, max(after_id, 0), min(max(limit, 1), 1000)),
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index ASC, id ASC",
                (run_id,),
            ).fetchall()
        return [self._decode_step(row) for row in rows]

    def list_tool_approvals(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_approvals WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_tool_approved(self, run_id: str, tool_name: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT decision FROM tool_approvals
                WHERE run_id = ? AND tool_name = ?
                ORDER BY id DESC LIMIT 1
                """,
                (run_id, tool_name),
            ).fetchone()
        return row is not None and str(row["decision"]) == "APPROVED"

    def request_tool_approval(
        self, run_id: str, tool_name: str, *, reason: str
    ) -> dict[str, Any] | None:
        normalized_name = tool_name.strip()
        if not normalized_name:
            raise RunStoreError("Tool name is required for approval")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if current is None:
                return None
            status = str(current["status"])
            if status == "WAITING_APPROVAL":
                return self._decode_run(current)
            if status != "RUNNING":
                raise RunStoreError(
                    f"Cannot request tool approval from status {status}"
                )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO tool_approvals (run_id, tool_name, decision, actor, comment, created_at)
                VALUES (?, ?, 'REQUESTED', 'runtime', ?, ?)
                """,
                (run_id, normalized_name, reason, now),
            )
            connection.execute(
                "UPDATE runs SET status = 'WAITING_APPROVAL', updated_at = ? WHERE id = ?",
                (now, run_id),
            )
            self._insert_event(
                connection,
                run_id,
                "tool.approval_requested",
                {"tool_name": normalized_name},
            )
        return self.get_run(run_id)

    def decide_tool_approval(
        self,
        run_id: str,
        tool_name: str,
        *,
        decision: str,
        actor: str,
        comment: str = "",
    ) -> dict[str, Any] | None:
        normalized_name = tool_name.strip()
        normalized_decision = decision.strip().upper()
        normalized_actor = actor.strip()
        if not normalized_name or not normalized_actor:
            raise RunStoreError("Tool name and actor are required for approval")
        if normalized_decision not in {"APPROVED", "REJECTED"}:
            raise RunStoreError("Approval decision must be APPROVED or REJECTED")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if current is None:
                return None
            if str(current["status"]) != "WAITING_APPROVAL":
                raise RunStoreError("Run is not waiting for a tool approval")
            requested = connection.execute(
                """
                SELECT tool_name FROM tool_approvals
                WHERE run_id = ? AND decision = 'REQUESTED'
                ORDER BY id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if requested is None or str(requested["tool_name"]) != normalized_name:
                raise RunStoreError("Approval does not match the pending tool request")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO tool_approvals (run_id, tool_name, decision, actor, comment, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    normalized_name,
                    normalized_decision,
                    normalized_actor,
                    comment.strip(),
                    now,
                ),
            )
            if normalized_decision == "APPROVED":
                connection.execute(
                    """
                    UPDATE runs SET status = 'QUEUED', updated_at = ?, finished_at = NULL
                    WHERE id = ?
                    """,
                    (now, run_id),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "tool.approved",
                    {"tool_name": normalized_name, "actor": normalized_actor},
                )
                self._insert_event(connection, run_id, "run.resumed", {"reason": "tool_approved"})
            else:
                connection.execute(
                    """
                    UPDATE runs
                    SET status = 'FAILED', error_code = 'TOOL_APPROVAL_REJECTED',
                        error_message = ?, updated_at = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (f"Tool approval rejected: {normalized_name}", now, now, run_id),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "tool.rejected",
                    {"tool_name": normalized_name, "actor": normalized_actor},
                )
                self._insert_event(
                    connection,
                    run_id,
                    "run.failed",
                    {"error_code": "TOOL_APPROVAL_REJECTED"},
                )
        return self.get_run(run_id)

    def claim_next(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE status = 'QUEUED' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            run_id = str(row["id"])
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = 'RUNNING', attempt = attempt + 1, updated_at = ?, started_at = ?
                WHERE id = ? AND status = 'QUEUED'
                """,
                (now, now, run_id),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_event(connection, run_id, "run.started", {})
            claimed = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._decode_run(claimed)

    def request_cancel(self, run_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                return None
            status = str(current["status"])
            if status in {"QUEUED", "WAITING_APPROVAL"}:
                connection.execute(
                    "UPDATE runs SET status = 'CANCELLED', updated_at = ?, finished_at = ? WHERE id = ?",
                    (now, now, run_id),
                )
                self._insert_event(
                    connection,
                    run_id,
                    "run.cancelled",
                    {"while": status.lower()},
                )
            elif status == "RUNNING":
                connection.execute(
                    "UPDATE runs SET status = 'CANCEL_REQUESTED', updated_at = ? WHERE id = ?",
                    (now, run_id),
                )
                self._insert_event(connection, run_id, "run.cancel_requested", {})
        return self.get_run(run_id)

    def resume(self, run_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                return None
            if current["status"] not in {"FAILED", "INTERRUPTED"}:
                raise RunStoreError("只有 FAILED 或 INTERRUPTED 运行可以恢复")
            connection.execute(
                """
                UPDATE runs
                SET status = 'QUEUED', result_json = NULL, error_code = NULL,
                    error_message = NULL, updated_at = ?, finished_at = NULL
                WHERE id = ?
                """,
                (now, run_id),
            )
            self._insert_event(connection, run_id, "run.resumed", {})
        return self.get_run(run_id)

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in TERMINAL_RUN_STATUSES:
            raise RunStoreError(f"无效终态: {status}")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if current is None:
                return None
            current_status = str(current["status"])
            if current_status in TERMINAL_RUN_STATUSES:
                return self._decode_run(current)
            if current_status not in {"RUNNING", "CANCEL_REQUESTED"}:
                raise RunStoreError(
                    f"Cannot complete run {run_id} from status {current_status}"
                )
            if current_status == "CANCEL_REQUESTED" and status != "CANCELLED":
                status = "CANCELLED"
                result = None
                error_code = "RUN_CANCELLED"
                error_message = "Cancellation was requested before task completion"
            connection.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, error_code = ?, error_message = ?,
                    updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    self._json(result) if result is not None else None,
                    error_code,
                    error_message,
                    now,
                    now,
                    run_id,
                ),
            )
            self._insert_event(
                connection,
                run_id,
                f"run.{status.lower()}",
                {"error_code": error_code} if error_code else {},
            )
        return self.get_run(run_id)

    def current_status(self, run_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        return str(row["status"]) if row is not None else None

    def next_step_index(self, run_id: str) -> int:
        """Return a per-run step index that is safe across retries and resumes."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(step_index), 0) AS max_index FROM run_steps WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["max_index"]) + 1 if row is not None else 1

    def start_step(
        self,
        run_id: str,
        *,
        step_index: int,
        kind: str,
        name: str,
        payload: dict[str, Any],
    ) -> int:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO run_steps (
                    run_id, step_index, kind, name, status, input_json, started_at
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
                """,
                (run_id, step_index, kind, name, self._json(payload), now),
            )
            step_id = int(cursor.lastrowid)
            self._insert_event(connection, run_id, "step.started", {"step_id": step_id, "kind": kind, "name": name})
        return step_id

    def finish_step(
        self,
        run_id: str,
        step_id: int,
        *,
        status: str,
        output: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE run_steps
                SET status = ?, output_json = ?, error_code = ?, error_message = ?, finished_at = ?
                WHERE id = ? AND run_id = ?
                """,
                (
                    status,
                    self._json(output) if output is not None else None,
                    error_code,
                    error_message,
                    now,
                    step_id,
                    run_id,
                ),
            )
            self._insert_event(connection, run_id, f"step.{status.lower()}", {"step_id": step_id, "error_code": error_code})

    def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(checkpoint_index), 0) AS max_index FROM run_checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            checkpoint_index = int(row["max_index"]) + 1 if row is not None else 1
            connection.execute(
                "INSERT INTO run_checkpoints (run_id, checkpoint_index, state_json, created_at) VALUES (?, ?, ?, ?)",
                (run_id, checkpoint_index, self._json(state), utc_now()),
            )
            self._insert_event(connection, run_id, "checkpoint.saved", {"checkpoint_index": checkpoint_index})
        return {"checkpoint_index": checkpoint_index, "state": state}

    def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_checkpoints WHERE run_id = ? ORDER BY checkpoint_index DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "checkpoint_index": int(row["checkpoint_index"]),
            "state": self._decode_json(row["state_json"], {}),
            "created_at": row["created_at"],
        }

    def recover_interrupted(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute("SELECT id FROM runs WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')").fetchall()
            run_ids = [str(row["id"]) for row in rows]
            if run_ids:
                connection.execute(
                    "UPDATE runs SET status = 'INTERRUPTED', updated_at = ?, finished_at = ? WHERE status IN ('RUNNING', 'CANCEL_REQUESTED')",
                    (now, now),
                )
                placeholders = ", ".join("?" for _ in run_ids)
                connection.execute(
                    f"""
                    UPDATE run_steps
                    SET status = 'INTERRUPTED', error_code = 'RUNTIME_RESTARTED',
                        error_message = 'Runtime restarted before this step completed', finished_at = ?
                    WHERE status = 'RUNNING' AND run_id IN ({placeholders})
                    """,
                    [now, *run_ids],
                )
                for run_id in run_ids:
                    self._insert_event(connection, run_id, "run.interrupted", {"reason": "runtime_restarted"})
        return len(run_ids)
