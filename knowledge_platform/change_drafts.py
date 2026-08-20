from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from .change_order_adapter import build_change_order_extraction_plan
from .schema import CardStatus

if TYPE_CHECKING:
    from .service import KnowledgeService


GENERATION_SYSTEM_PROMPT = """你是公司内网的真实 ChangeOrder 草案生成器。
你只能使用用户提供的结构化需求和 APPROVED 案例包知识，不得使用常识补全具体设备ID、IP、账号、阈值、时间或命令参数。
你只生成规范化草案 JSON，不得生成 Markdown，也不得直接生成 action_list 的分组副本或 ExecutionResult。
每个任务、步骤和风险必须带 source_refs 或 input_refs。source_refs 只能使用提示中列出的 card_id、字段和索引；input_refs 只能指向结构化需求字段。
任务 record 必须包含 SchemaProfile 声明的全部 TaskRecord 字段；四类 Procedure record 必须包含全部 ProcedureStep 字段。缺失值写入 missing_fields，禁止猜测。

输出格式：
{
  "title": "草案标题",
  "summary": "草案摘要",
  "tasks": [
    {
      "group": "动态业务分组名",
      "record": {"严格按 profile 的字段输出": "值"},
      "source_refs": [{"card_id": 1, "field": "procedure_steps", "index": 0}],
      "input_refs": ["parameters.example"]
    }
  ],
  "procedure": {
    "check_before_change": [{"record": {}, "source_refs": [], "input_refs": []}],
    "change_implement": [{"record": {}, "source_refs": [], "input_refs": []}],
    "change_verified": [{"record": {}, "source_refs": [], "input_refs": []}],
    "change_rollback": [{"record": {}, "source_refs": [], "input_refs": []}]
  },
  "risks": [{"text": "风险", "source_refs": [], "input_refs": []}],
  "missing_fields": []
}
"""

PROCEDURE_KEYS = (
    "check_before_change",
    "change_implement",
    "change_verified",
    "change_rollback",
)
ROLE_TO_PROCEDURE_KEY = {
    "PRECHECK_STEPS": "check_before_change",
    "IMPLEMENTATION_STEPS": "change_implement",
    "VALIDATION_STEPS": "change_verified",
    "ROLLBACK_STEPS": "change_rollback",
}
GENERATION_ROLES = {
    "IDENTITY_METADATA_CONTEXT",
    "TASKS_CANONICAL",
    "CASE_CONTEXT",
    "PROCEDURE_STEP",
    *ROLE_TO_PROCEDURE_KEY,
}
ALLOWED_PROFILE_POLICIES = {
    "generated",
    "input_optional",
    "empty",
    "not_executed",
    "fixed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def empty_value(type_name: str, *, not_executed: bool = False, field_name: str = "") -> Any:
    if type_name == "array":
        return []
    if type_name == "object":
        return {}
    if type_name == "boolean":
        return False
    if type_name in {"integer", "number"}:
        return 0
    if type_name == "null":
        return None
    if not_executed and any(token in field_name.lower() for token in ("status", "state", "result")):
        return "NOT_EXECUTED"
    return ""


def value_matches_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def dotted_get(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def compact_tokens(value: Any) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {
        token
        for token in re.findall(r"[0-9a-z\u4e00-\u9fff_-]+", normalized)
        if len(token) > 1
    }


class ChangeDraftError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = int(HTTPStatus.BAD_REQUEST),
        code: str = "change_draft_error",
    ):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class RealChangeRequest:
    goal: str
    scenario: str
    region: str
    services: list[str]
    objects: list[str]
    current_state: str
    target_state: str
    window: dict[str, str]
    impact_scope: str = ""
    constraints: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)
    validation_requirements: list[str] = field(default_factory=list)
    requester: str = "shared-operator"

    @classmethod
    def from_payload(cls, payload: Any) -> "RealChangeRequest":
        if not isinstance(payload, dict):
            raise ChangeDraftError("真实变更需求必须是 JSON 对象")

        def text(name: str, *, required: bool = False) -> str:
            value = str(payload.get(name) or "").strip()
            if required and not value:
                raise ChangeDraftError(f"真实变更需求缺少 {name}")
            return value

        def strings(name: str) -> list[str]:
            value = payload.get(name) or []
            if isinstance(value, str):
                value = [item.strip() for item in re.split(r"[,，\n]", value)]
            if not isinstance(value, list):
                raise ChangeDraftError(f"{name} 必须是字符串数组")
            return [str(item).strip() for item in value if str(item).strip()]

        window = payload.get("window") or {}
        parameters = payload.get("parameters") or {}
        if not isinstance(window, dict) or not isinstance(parameters, dict):
            raise ChangeDraftError("window 和 parameters 必须是 JSON 对象")
        return cls(
            goal=text("goal", required=True),
            scenario=text("scenario", required=True),
            region=text("region"),
            services=strings("services"),
            objects=strings("objects"),
            current_state=text("current_state", required=True),
            target_state=text("target_state", required=True),
            window={str(key): str(value) for key, value in window.items()},
            impact_scope=text("impact_scope"),
            constraints=strings("constraints"),
            parameters=dict(parameters),
            validation_requirements=strings("validation_requirements"),
            requester=text("requester") or "shared-operator",
        )

    def query_text(self) -> str:
        return " ".join(
            item
            for item in (
                self.goal,
                self.scenario,
                self.region,
                *self.services,
                *self.objects,
                self.current_state,
                self.target_state,
                self.impact_scope,
                *self.constraints,
                *self.validation_requirements,
                *[f"{key} {value}" for key, value in self.parameters.items()],
            )
            if item
        )


class ChangeDraftStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_profiles (
                    profile_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    source_checksum TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS change_drafts (
                    draft_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    selected_case_ids TEXT NOT NULL,
                    held_out_case_id TEXT NOT NULL DEFAULT '',
                    profile_id TEXT NOT NULL,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    model_error TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS change_draft_revisions (
                    draft_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    normalized_json TEXT NOT NULL,
                    change_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    model_usage_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reviewer TEXT NOT NULL DEFAULT '',
                    review_comment TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (draft_id, revision),
                    FOREIGN KEY (draft_id) REFERENCES change_drafts(draft_id)
                );
                CREATE TABLE IF NOT EXISTS change_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    draft_id TEXT NOT NULL DEFAULT '',
                    held_out_case_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row | None, json_fields: tuple[str, ...]) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for field_name in json_fields:
            value[field_name.removesuffix("_json")] = json.loads(str(value.pop(field_name) or "{}"))
        return value

    def activate_profile(self, profile: dict[str, Any], *, actor: str) -> dict[str, Any]:
        now = utc_now()
        profile_id = str(profile["profile_id"])
        with self.connect() as connection:
            connection.execute("UPDATE schema_profiles SET active = 0")
            connection.execute(
                """
                INSERT INTO schema_profiles
                    (profile_id, version, case_id, source_checksum, profile_json,
                     active, actor, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    profile_id,
                    int(profile["version"]),
                    str(profile["source_case_id"]),
                    str(profile["source_checksum"]),
                    stable_json(profile),
                    actor,
                    now,
                ),
            )
        return self.active_profile() or {}

    def active_profile(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM schema_profiles WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        decoded = self._decode(row, ("profile_json",))
        if decoded is None:
            return None
        profile = decoded.pop("profile")
        profile["active"] = True
        profile["activated_by"] = decoded["actor"]
        profile["activated_at"] = decoded["created_at"]
        return profile

    def create_draft(
        self,
        *,
        request: dict[str, Any],
        selected_case_ids: list[str],
        profile_id: str,
        actor: str,
        mode: str = "normal",
        held_out_case_id: str = "",
    ) -> dict[str, Any]:
        draft_id = f"RCHG-{datetime.now().strftime('%Y%m%d')}-{uuid4().hex[:10].upper()}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO change_drafts
                    (draft_id, mode, status, request_json, selected_case_ids,
                     held_out_case_id, profile_id, created_by, created_at, updated_at)
                VALUES (?, ?, 'GENERATING', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    mode,
                    stable_json(request),
                    stable_json(selected_case_ids),
                    held_out_case_id,
                    profile_id,
                    actor,
                    now,
                    now,
                ),
            )
        return self.get_draft(draft_id) or {}

    def set_draft_run(self, draft_id: str, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE change_drafts SET run_id = ?, updated_at = ? WHERE draft_id = ?",
                (run_id, utc_now(), draft_id),
            )

    def set_draft_failure(self, draft_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE change_drafts SET status = 'GENERATION_FAILED', model_error = ?, updated_at = ? WHERE draft_id = ?",
                (error[:4000], utc_now(), draft_id),
            )

    def save_revision(
        self,
        draft_id: str,
        *,
        normalized: dict[str, Any],
        change_json: dict[str, Any],
        provenance: dict[str, Any],
        validation: dict[str, Any],
        usage: dict[str, Any] | None,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        status = "DRAFT" if validation.get("passed") else "BLOCKED"
        with self.connect() as connection:
            row = connection.execute(
                "SELECT current_revision FROM change_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise ChangeDraftError("真实变更草案不存在", status=404)
            revision = int(row["current_revision"]) + 1
            digest = content_hash(change_json)
            connection.execute(
                """
                INSERT INTO change_draft_revisions
                    (draft_id, revision, normalized_json, change_json,
                     provenance_json, validation_json, model_usage_json,
                     content_hash, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    revision,
                    stable_json(normalized),
                    stable_json(change_json),
                    stable_json(provenance),
                    stable_json(validation),
                    stable_json(usage or {}),
                    digest,
                    actor,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE change_drafts
                SET current_revision = ?, status = ?, model_error = '', updated_at = ?
                WHERE draft_id = ?
                """,
                (revision, status, now, draft_id),
            )
        if status == "DRAFT":
            with self.connect() as connection:
                connection.execute(
                    "UPDATE change_drafts SET status = 'READY_FOR_REVIEW', updated_at = ? "
                    "WHERE draft_id = ? AND current_revision = ? AND status = 'DRAFT'",
                    (utc_now(), draft_id, revision),
                )
        return self.get_draft(draft_id) or {}

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM change_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                return None
            revision = connection.execute(
                """
                SELECT * FROM change_draft_revisions
                WHERE draft_id = ? AND revision = ?
                """,
                (draft_id, int(row["current_revision"])),
            ).fetchone()
        result = self._decode(row, ("request_json", "selected_case_ids")) or {}
        if revision is not None:
            result["revision"] = self._decode(
                revision,
                (
                    "normalized_json",
                    "change_json",
                    "provenance_json",
                    "validation_json",
                    "model_usage_json",
                ),
            )
        else:
            result["revision"] = None
        return result

    def list_drafts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM change_drafts ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._decode(row, ("request_json", "selected_case_ids")) or {} for row in rows]

    def invalidate_case_references(self, case_id: str) -> int:
        """Invalidate temporary drafts/evaluations derived from a rebuilt case."""

        now = utc_now()
        affected: list[str] = []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT draft_id, selected_case_ids, held_out_case_id "
                "FROM change_drafts"
            ).fetchall()
            for row in rows:
                try:
                    selected = json.loads(str(row["selected_case_ids"] or "[]"))
                except json.JSONDecodeError:
                    selected = []
                if case_id in selected or str(row["held_out_case_id"] or "") == case_id:
                    affected.append(str(row["draft_id"]))
            if affected:
                connection.executemany(
                    """
                    UPDATE change_drafts
                    SET status = 'BLOCKED',
                        model_error = 'SOURCE_CASE_REBUILT', updated_at = ?
                    WHERE draft_id = ?
                    """,
                    [(now, draft_id) for draft_id in affected],
                )
                connection.executemany(
                    """
                    UPDATE change_draft_revisions
                    SET reviewer = '', review_comment = 'SOURCE_CASE_REBUILT',
                        reviewed_at = ''
                    WHERE draft_id = ?
                    """,
                    [(draft_id,) for draft_id in affected],
                )
            connection.execute(
                """
                UPDATE change_evaluations
                SET status = 'FAILED', error = 'SOURCE_CASE_REBUILT', updated_at = ?
                WHERE held_out_case_id = ?
                """,
                (now, case_id),
            )
        return len(affected)

    def review(self, draft_id: str, *, decision: str, reviewer: str, comment: str) -> dict[str, Any]:
        normalized = decision.strip().upper()
        if normalized not in {"APPROVED", "REJECTED"}:
            raise ChangeDraftError("草案审核决定只能是 APPROVED 或 REJECTED")
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status, current_revision FROM change_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise ChangeDraftError("真实变更草案不存在", status=404)
            if normalized == "APPROVED" and str(row["status"]) != "READY_FOR_REVIEW":
                raise ChangeDraftError("只有通过全部硬校验的草案可以批准")
            if normalized == "REJECTED" and str(row["status"]) in {"REVIEW_APPROVED", "REJECTED"}:
                raise ChangeDraftError("当前草案已完成审核")
            status = "REVIEW_APPROVED" if normalized == "APPROVED" else "REJECTED"
            connection.execute(
                "UPDATE change_drafts SET status = ?, updated_at = ? WHERE draft_id = ?",
                (status, now, draft_id),
            )
            connection.execute(
                """
                UPDATE change_draft_revisions
                SET reviewer = ?, review_comment = ?, reviewed_at = ?
                WHERE draft_id = ? AND revision = ?
                """,
                (reviewer, comment, now, draft_id, int(row["current_revision"])),
            )
        return self.get_draft(draft_id) or {}

    def create_evaluation(self, *, held_out_case_id: str, actor: str) -> dict[str, Any]:
        evaluation_id = f"EVAL-{uuid4().hex[:12].upper()}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO change_evaluations
                    (evaluation_id, held_out_case_id, status, created_by, created_at, updated_at)
                VALUES (?, ?, 'QUEUED', ?, ?, ?)
                """,
                (evaluation_id, held_out_case_id, actor, now, now),
            )
        return self.get_evaluation(evaluation_id) or {}

    def set_evaluation_run(self, evaluation_id: str, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE change_evaluations SET run_id = ?, status = 'RUNNING', updated_at = ? WHERE evaluation_id = ?",
                (run_id, utc_now(), evaluation_id),
            )

    def finish_evaluation(
        self,
        evaluation_id: str,
        *,
        draft_id: str = "",
        report: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        status = "FAILED" if error else "COMPLETED"
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE change_evaluations
                SET status = ?, draft_id = ?, report_json = ?, error = ?, updated_at = ?
                WHERE evaluation_id = ?
                """,
                (status, draft_id, stable_json(report or {}), error[:4000], utc_now(), evaluation_id),
            )
        return self.get_evaluation(evaluation_id) or {}

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM change_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
        return self._decode(row, ("report_json",))

    def list_evaluations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM change_evaluations ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self._decode(row, ("report_json",)) or {} for row in rows]


class RealChangeDraftService:
    def __init__(self, knowledge_service: "KnowledgeService"):
        self.knowledge = knowledge_service
        path = knowledge_service.settings.change_draft_database_path or (
            knowledge_service.settings.project_root / "data" / "change_drafts.db"
        )
        self.store = ChangeDraftStore(path)

    @property
    def enabled(self) -> bool:
        return bool(self.knowledge.settings.real_change_generation_enabled)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ChangeDraftError(
                "真实变更生成功能未启用",
                status=int(HTTPStatus.NOT_FOUND),
                code="real_change_generation_disabled",
            )

    @staticmethod
    def _eligible_bundle(bundle: dict[str, Any] | None) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if bundle is None:
            return False, ["CASE_NOT_FOUND"]
        if bundle.get("status") != CardStatus.APPROVED.value:
            reasons.append("CASE_NOT_APPROVED")
        report = bundle.get("extraction_report") or {}
        coverage = (
            report.get("structural_source_coverage")
            or report.get("content_coverage")
            or {}
        )
        change_order = report.get("change_order") or report
        if coverage.get("status") != "COMPLETE":
            reasons.append("CONTENT_COVERAGE_INCOMPLETE")
        if change_order.get("semantic_mapping_status") != "CONFIRMED":
            reasons.append("SEMANTIC_MAPPING_NOT_CONFIRMED")
        if not change_order.get("safe_for_internal_index", False):
            reasons.append("NOT_SAFE_FOR_INTERNAL_INDEX")
        return not reasons, reasons

    @staticmethod
    def _source_json_for_bundle(bundle: dict[str, Any]) -> tuple[dict[str, Any], Path]:
        raw = str(bundle.get("source_ref") or "").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ChangeDraftError("案例包来源不是可读取的本地 JSON 文件，无法建立 SchemaProfile")
        path = path.resolve()
        if not path.is_file() or path.suffix.lower() != ".json":
            raise ChangeDraftError("SchemaProfile 来源 JSON 已不存在或格式不受支持")
        data = path.read_bytes()
        try:
            decoded_text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ChangeDraftError("SchemaProfile 来源不是合法 UTF-8 JSON") from exc
        expected = str(bundle.get("source_checksum") or "")
        if expected and hashlib.sha256(decoded_text.encode("utf-8")).hexdigest() != expected:
            raise ChangeDraftError("SchemaProfile 来源 JSON 与已审核案例校验和不一致")
        try:
            payload = json.loads(decoded_text)
        except json.JSONDecodeError as exc:
            raise ChangeDraftError("SchemaProfile 来源不是合法 UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ChangeDraftError("SchemaProfile 来源 JSON 根节点不是 object")
        return payload, path

    @staticmethod
    def _field_specs(record: dict[str, Any], *, policy: str) -> dict[str, dict[str, Any]]:
        return {
            str(name): {"type": json_type(value), "policy": policy, "sample": value}
            for name, value in record.items()
        }

    def inspect_schema_profile(self, case_id: str) -> dict[str, Any]:
        bundle = self.knowledge.store.get_case_bundle(case_id, include_cards=True)
        eligible, reasons = self._eligible_bundle(bundle)
        if not eligible or bundle is None:
            raise ChangeDraftError("案例包不能建立 SchemaProfile：" + "、".join(reasons))
        payload, path = self._source_json_for_bundle(bundle)
        plan, report = build_change_order_extraction_plan(
            json.dumps(payload, ensure_ascii=False),
            chunk_size=self.knowledge.settings.change_order_chunk_size,
        )
        if plan is None or report.get("semantic_mapping_status") != "CONFIRMED":
            raise ChangeDraftError("来源 JSON 未通过已确认 ChangeOrder 结构校验")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ChangeDraftError("来源 JSON 缺少 data object")
        tasks = data.get("action_list")
        procedures = data.get("sop_change_step")
        change_plan = data.get("change_plan")
        if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], dict):
            raise ChangeDraftError("来源缺少可用 TaskRecord 样例")
        if not isinstance(procedures, dict):
            raise ChangeDraftError("来源缺少 ProcedureStep 样例")
        procedure_sample: dict[str, Any] | None = None
        for key in PROCEDURE_KEYS:
            values = procedures.get(key)
            if isinstance(values, list) and values and isinstance(values[0], dict):
                procedure_sample = values[0]
                break
        if procedure_sample is None:
            raise ChangeDraftError("来源四类 ProcedureStep 均为空，不能建立字段 Profile")
        try:
            execution = change_plan[0]["result"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ChangeDraftError("来源缺少 ExecutionResult 样例") from exc
        if not isinstance(execution, dict):
            raise ChangeDraftError("ExecutionResult 不是 object")
        context = {
            str(name): {"type": json_type(value), "policy": "input_optional", "sample": value}
            for name, value in data.items()
            if name not in {"action_list", "change_tool_relate_action", "sop_change_step", "change_plan"}
        }
        profile = {
            "profile_id": f"profile-{uuid4().hex[:12]}",
            "version": 1,
            "source_case_id": case_id,
            "source_checksum": str(bundle["source_checksum"]),
            "source_name": str(bundle["source_name"]),
            "source_path": str(path),
            "paths": {
                "tasks": "/data/action_list",
                "grouped_tasks": "/data/change_tool_relate_action",
                "procedure": "/data/sop_change_step",
                "execution_result": "/data/change_plan/0/result",
            },
            "task_fields": self._field_specs(tasks[0], policy="generated"),
            "procedure_fields": self._field_specs(procedure_sample, policy="generated"),
            "execution_fields": self._field_specs(execution, policy="not_executed"),
            "context_fields": context,
            "envelope_fields": {
                str(name): {"type": json_type(value), "policy": "empty", "sample": value}
                for name, value in payload.items()
                if name != "data"
            },
            "inspection": {
                "task_field_count": len(tasks[0]),
                "procedure_field_count": len(procedure_sample),
                "execution_field_count": len(execution),
                "adapter_report": {
                    "semantic_mapping_status": report.get("semantic_mapping_status"),
                    "task_reconciled": (report.get("task_record") or {}).get("reconciled"),
                },
            },
        }
        return profile

    @staticmethod
    def _validate_profile(profile: Any) -> dict[str, Any]:
        if not isinstance(profile, dict):
            raise ChangeDraftError("SchemaProfile 必须是 JSON 对象")
        counts = {"task_fields": 13, "procedure_fields": 20, "execution_fields": 15}
        for section, expected in counts.items():
            fields = profile.get(section)
            if not isinstance(fields, dict) or len(fields) != expected:
                raise ChangeDraftError(f"{section} 必须包含 {expected} 个字段")
            for name, spec in fields.items():
                if not isinstance(spec, dict):
                    raise ChangeDraftError(f"{section}.{name} 配置无效")
                if str(spec.get("type") or "") not in {
                    "null",
                    "boolean",
                    "string",
                    "integer",
                    "number",
                    "array",
                    "object",
                }:
                    raise ChangeDraftError(f"{section}.{name} type 无效")
                policy = str(spec.get("policy") or "")
                if not (policy in ALLOWED_PROFILE_POLICIES or policy.startswith("input:")):
                    raise ChangeDraftError(f"{section}.{name} policy 无效: {policy}")
                if policy == "fixed" and "value" not in spec:
                    raise ChangeDraftError(f"{section}.{name} fixed policy 缺少 value")
                if section == "execution_fields" and policy not in {
                    "empty",
                    "not_executed",
                    "fixed",
                }:
                    raise ChangeDraftError(
                        f"execution_fields.{name} 只允许 empty、not_executed 或 fixed"
                    )
                if section == "execution_fields" and policy == "fixed":
                    fixed = spec.get("value")
                    if fixed not in (None, "", "NOT_EXECUTED", False, 0, [], {}):
                        raise ChangeDraftError(
                            f"execution_fields.{name} fixed value 必须为空或 NOT_EXECUTED"
                        )
        for section in ("context_fields", "envelope_fields"):
            fields = profile.get(section)
            if not isinstance(fields, dict):
                raise ChangeDraftError(f"{section} 必须是字段配置对象")
            for name, spec in fields.items():
                if not isinstance(spec, dict):
                    raise ChangeDraftError(f"{section}.{name} 配置无效")
                if str(spec.get("type") or "") not in {
                    "null",
                    "boolean",
                    "string",
                    "integer",
                    "number",
                    "array",
                    "object",
                }:
                    raise ChangeDraftError(f"{section}.{name} type 无效")
                policy = str(spec.get("policy") or "")
                if not (
                    policy in ALLOWED_PROFILE_POLICIES
                    or policy.startswith("input:")
                ):
                    raise ChangeDraftError(
                        f"{section}.{name} policy 无效: {policy}"
                    )
                if policy == "fixed" and "value" not in spec:
                    raise ChangeDraftError(
                        f"{section}.{name} fixed policy 缺少 value"
                    )
        if not str(profile.get("profile_id") or "").strip():
            raise ChangeDraftError("SchemaProfile 缺少 profile_id")
        return profile

    def activate_schema_profile(self, profile: Any, *, actor: str) -> dict[str, Any]:
        reviewer = actor.strip()
        if not reviewer:
            raise ChangeDraftError("SchemaProfile 激活人不能为空")
        validated = self._validate_profile(profile)
        source_case_id = str(validated.get("source_case_id") or "")
        bundle = self.knowledge.store.get_case_bundle(source_case_id, include_cards=True)
        eligible, reasons = self._eligible_bundle(bundle)
        if not eligible:
            raise ChangeDraftError("SchemaProfile 来源案例已不再可用：" + "、".join(reasons))
        if str(bundle.get("source_checksum")) != str(validated.get("source_checksum")):
            raise ChangeDraftError("SchemaProfile 来源案例校验和已漂移")
        if not self._profile_compatible(bundle, validated):
            raise ChangeDraftError(
                "SchemaProfile 字段集合或 JSON 类型与来源案例不一致"
            )
        return self.store.activate_profile(validated, actor=reviewer)

    def profile_status(self) -> dict[str, Any]:
        return {
            "feature_enabled": self.enabled,
            "active_profile": self.store.active_profile(),
            "warning": (
                "当前是未鉴权能力试验；审核人仅为自报身份，不能作为正式生产审计。"
                if self.knowledge.settings.demo_mode
                else ""
            ),
        }

    @staticmethod
    def _bundle_text(bundle: dict[str, Any]) -> str:
        values: list[str] = [str(bundle.get("title") or "")]
        for card in bundle.get("cards") or []:
            values.extend(
                str(value or "")
                for value in (
                    card.get("title"),
                    card.get("summary"),
                    card.get("scenario"),
                    card.get("object_name"),
                    " ".join(card.get("keywords") or []),
                )
            )
        return " ".join(values)

    @classmethod
    def _near_duplicate(cls, left: dict[str, Any], right: dict[str, Any]) -> bool:
        if left.get("source_checksum") == right.get("source_checksum"):
            return True
        left_tokens = compact_tokens(cls._bundle_text(left))
        right_tokens = compact_tokens(cls._bundle_text(right))
        if not left_tokens or not right_tokens:
            return False
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.9

    @staticmethod
    def _parse_source_record(value: str) -> dict[str, Any] | None:
        raw = str(value or "")
        candidate = raw.split(": ", 1)[1] if ": " in raw else raw
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None

    @classmethod
    def _bundle_field_sets(cls, bundle: dict[str, Any]) -> dict[str, list[str]]:
        task_fields: list[str] = []
        procedure_fields: list[str] = []
        for card in bundle.get("cards") or []:
            role = str((card.get("lineage") or {}).get("unit_role") or "")
            field_name = (
                "procedure_steps"
                if role in {"TASKS_CANONICAL", "PRECHECK_STEPS", "IMPLEMENTATION_STEPS"}
                else "validation_steps"
                if role == "VALIDATION_STEPS"
                else "rollback_steps"
                if role == "ROLLBACK_STEPS"
                else ""
            )
            if not field_name:
                continue
            for value in card.get(field_name) or []:
                record = cls._parse_source_record(value)
                if record is None:
                    continue
                names = sorted(record)
                if role == "TASKS_CANONICAL" and not task_fields:
                    task_fields = names
                elif role != "TASKS_CANONICAL" and not procedure_fields:
                    procedure_fields = names
        return {"task_fields": task_fields, "procedure_fields": procedure_fields}

    @staticmethod
    def _record_signature(record: dict[str, Any]) -> dict[str, str]:
        return {str(name): json_type(value) for name, value in record.items()}

    def _profile_compatible(self, bundle: dict[str, Any], profile: dict[str, Any] | None) -> bool:
        if profile is None:
            return False
        observed_cards = self._bundle_field_sets(bundle)
        semantic_model = any(
            str(card.get("card_model_version") or "").startswith(
                "change_order_card_model_v"
            )
            for card in bundle.get("cards") or []
        )
        if not semantic_model and not (
            observed_cards["task_fields"] == sorted(profile["task_fields"])
            and observed_cards["procedure_fields"] == sorted(profile["procedure_fields"])
        ):
            return False
        try:
            payload, _path = self._source_json_for_bundle(bundle)
            data = payload["data"]
            tasks = data["action_list"]
            procedures = data["sop_change_step"]
            execution = data["change_plan"][0]["result"]
            procedure_sample = next(
                item
                for key in PROCEDURE_KEYS
                for item in procedures[key]
                if isinstance(item, dict)
            )
        except (ChangeDraftError, KeyError, IndexError, StopIteration, TypeError):
            return False
        if not (
            isinstance(data, dict)
            and isinstance(tasks, list)
            and tasks
            and isinstance(tasks[0], dict)
            and isinstance(execution, dict)
        ):
            return False
        context = {
            str(name): json_type(value)
            for name, value in data.items()
            if name
            not in {
                "action_list",
                "change_tool_relate_action",
                "sop_change_step",
                "change_plan",
            }
        }
        envelope = {
            str(name): json_type(value)
            for name, value in payload.items()
            if name != "data"
        }
        profile_signature = lambda section: {
            str(name): str(spec.get("type") or "")
            for name, spec in (profile.get(section) or {}).items()
        }
        return all(
            (
                self._record_signature(tasks[0]) == profile_signature("task_fields"),
                self._record_signature(procedure_sample)
                == profile_signature("procedure_fields"),
                self._record_signature(execution)
                == profile_signature("execution_fields"),
                context == profile_signature("context_fields"),
                envelope == profile_signature("envelope_fields"),
            )
        )

    def recommend(
        self,
        request_payload: Any,
        *,
        held_out_case_id: str = "",
        limit: int | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        request = RealChangeRequest.from_payload(request_payload)
        profile = self.store.active_profile()
        hits, diagnostics = self.knowledge.trusted_search_hits(
            request.query_text(), top_k=50, for_generation=True
        )
        scores: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(hits):
            card_id = int(hit.card["id"])
            lineage = self.knowledge.store.get_card_lineage(card_id) or {}
            case_id = str(lineage.get("case_id") or "")
            if not case_id:
                continue
            item = scores.setdefault(case_id, {"score": 0.0, "card_ids": [], "matched_roles": []})
            item["score"] += float(hit.score) + max(0.0, 1.0 - rank * 0.01)
            item["card_ids"].append(card_id)
            role = str(lineage.get("unit_role") or "")
            if role and role not in item["matched_roles"]:
                item["matched_roles"].append(role)

        held_out = (
            self.knowledge.store.get_case_bundle(held_out_case_id, include_cards=True)
            if held_out_case_id
            else None
        )
        rejected: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for case_id, score in scores.items():
            bundle = self.knowledge.store.get_case_bundle(case_id, include_cards=True)
            eligible, reasons = self._eligible_bundle(bundle)
            if not eligible or bundle is None:
                rejected.append({"case_id": case_id, "reasons": reasons})
                continue
            if held_out and (case_id == held_out_case_id or self._near_duplicate(bundle, held_out)):
                rejected.append({"case_id": case_id, "reasons": ["BLIND_TARGET_OR_NEAR_DUPLICATE"]})
                continue
            compatible = self._profile_compatible(bundle, profile)
            if not compatible:
                rejected.append({"case_id": case_id, "reasons": ["SCHEMA_PROFILE_MISMATCH"]})
                continue
            candidates.append(
                {
                    "case_id": case_id,
                    "title": bundle["title"],
                    "card_count": bundle["card_count"],
                    "score": round(float(score["score"]), 4),
                    "matched_card_ids": score["card_ids"],
                    "matched_roles": score["matched_roles"],
                    "profile_compatible": compatible,
                    "reason": "命中已审核完整案例包，并通过当前 SchemaProfile 字段一致性检查。",
                }
            )
        candidates.sort(key=lambda item: (-float(item["score"]), str(item["case_id"])))
        cap = min(
            max(1, int(limit or self.knowledge.settings.change_generation_max_case_bundles)),
            self.knowledge.settings.change_generation_max_case_bundles,
            3,
        )
        return {
            "request": asdict(request),
            "candidates": candidates[:cap],
            "rejected": rejected,
            "profile": profile,
            "retrieval": diagnostics,
            "held_out_case_id": held_out_case_id or None,
        }

    def create_draft(
        self,
        request_payload: Any,
        *,
        selected_case_ids: list[str],
        actor: str,
        mode: str = "normal",
        held_out_case_id: str = "",
    ) -> dict[str, Any]:
        self._require_enabled()
        self.knowledge.settings.require_api()
        request = RealChangeRequest.from_payload(request_payload)
        profile = self.store.active_profile()
        if profile is None:
            raise ChangeDraftError("没有已激活的 SchemaProfile")
        selected = list(dict.fromkeys(str(item).strip() for item in selected_case_ids if str(item).strip()))
        if not 1 <= len(selected) <= self.knowledge.settings.change_generation_max_case_bundles:
            raise ChangeDraftError("必须确认 1 到 3 个推荐案例包")
        recommendations = self.recommend(asdict(request), held_out_case_id=held_out_case_id, limit=3)
        allowed = {str(item["case_id"]) for item in recommendations["candidates"]}
        if not set(selected) <= allowed:
            raise ChangeDraftError("所选案例包不在当前推荐结果中，或已不再满足硬条件")
        return self.store.create_draft(
            request=asdict(request),
            selected_case_ids=selected,
            profile_id=str(profile["profile_id"]),
            actor=actor.strip() or request.requester,
            mode=mode,
            held_out_case_id=held_out_case_id,
        )

    def _selected_context(self, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
        bundles: list[dict[str, Any]] = []
        cards: dict[int, dict[str, Any]] = {}
        for case_id in draft["selected_case_ids"]:
            bundle = self.knowledge.store.get_case_bundle(str(case_id), include_cards=True)
            eligible, reasons = self._eligible_bundle(bundle)
            if not eligible or bundle is None:
                raise ChangeDraftError(f"案例包 {case_id} 已失效：{'、'.join(reasons)}")
            bundles.append(bundle)
            for card in bundle.get("cards") or []:
                role = str((card.get("lineage") or {}).get("unit_role") or "")
                if role == "EXECUTION_RESULT" or (card.get("lineage") or {}).get("include_in_generation") is False:
                    continue
                if role in GENERATION_ROLES:
                    cards[int(card["id"])] = card
        if len(cards) > self.knowledge.settings.change_generation_max_context_cards:
            raise ChangeDraftError(
                "已选完整案例包超过生成上下文预算；请减少案例包，系统不会静默截断。",
                code="change_generation_context_too_large",
            )
        return bundles, cards

    @staticmethod
    def _prompt_card(card: dict[str, Any]) -> dict[str, Any]:
        lineage = card.get("lineage") or {}
        return {
            "card_id": int(card["id"]),
            "case_id": lineage.get("case_id"),
            "unit_role": lineage.get("unit_role"),
            "source_order": lineage.get("source_order"),
            "title": card.get("title"),
            "summary": card.get("summary"),
            "scenario": card.get("scenario"),
            "object_type": card.get("object_type"),
            "object_name": card.get("object_name"),
            "prerequisites": card.get("prerequisites") or [],
            "procedure_steps": card.get("procedure_steps") or [],
            "risks": card.get("risks") or [],
            "rollback_steps": card.get("rollback_steps") or [],
            "validation_steps": card.get("validation_steps") or [],
            "card_type": card.get("card_type"),
            "generalized_operation": card.get("generalized_operation"),
            "impact_analysis": card.get("impact_analysis"),
            "instance_parameters": card.get("instance_parameters") or {},
            "applicable_phases": card.get("applicable_phases") or [],
            "actions": card.get("actions") or [],
            "context": card.get("context") or {},
        }

    @staticmethod
    def _card_snapshot(card: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "id": int(card["id"]),
            "status": card.get("status"),
            "source_ref": card.get("source_ref"),
            "evidence_locator": card.get("evidence_locator"),
            "evidence_quote": card.get("evidence_quote"),
            "summary": card.get("summary"),
            "prerequisites": card.get("prerequisites") or [],
            "procedure_steps": card.get("procedure_steps") or [],
            "risks": card.get("risks") or [],
            "rollback_steps": card.get("rollback_steps") or [],
            "validation_steps": card.get("validation_steps") or [],
            "actions": card.get("actions") or [],
            "operation": card.get("operation"),
            "generalized_operation": card.get("generalized_operation"),
            "validation": card.get("validation"),
            "rollback": card.get("rollback"),
            "impact_analysis": card.get("impact_analysis"),
            "context": card.get("context") or {},
        }
        return {"card_id": int(card["id"]), "snapshot_hash": content_hash(fields), **fields}

    @staticmethod
    def _validate_source_ref(
        raw: Any,
        cards: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ChangeDraftError("source_ref 必须是对象")
        try:
            card_id = int(raw.get("card_id"))
        except (TypeError, ValueError) as exc:
            raise ChangeDraftError("source_ref.card_id 无效") from exc
        card = cards.get(card_id)
        if card is None or card.get("status") != CardStatus.APPROVED.value:
            raise ChangeDraftError(f"source_ref 引用了未选中或未批准卡片 K{card_id}")
        field_name = str(raw.get("field") or "")
        if field_name not in {
            "summary",
            "scenario",
            "object_name",
            "prerequisites",
            "procedure_steps",
            "risks",
            "rollback_steps",
            "validation_steps",
            "actions",
            "operation",
            "generalized_operation",
            "validation",
            "rollback",
            "impact_analysis",
        }:
            raise ChangeDraftError(f"source_ref 字段不可引用: {field_name}")
        value = card.get(field_name)
        index: int | None = None
        if isinstance(value, list):
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError) as exc:
                raise ChangeDraftError(f"K{card_id}.{field_name} 缺少有效 index") from exc
            if not 0 <= index < len(value):
                raise ChangeDraftError(f"K{card_id}.{field_name}[{index}] 越界")
        elif raw.get("index") not in (None, "", 0, "0"):
            raise ChangeDraftError(f"K{card_id}.{field_name} 是标量字段，不允许 index")
        return {"card_id": card_id, "field": field_name, "index": index}

    @staticmethod
    def _validate_input_ref(raw: Any, request: dict[str, Any]) -> str:
        reference = str(raw or "").strip()
        if not reference or dotted_get(request, reference) in (None, "", [], {}):
            raise ChangeDraftError(f"input_ref 不存在或为空: {reference!r}")
        return reference

    @classmethod
    def _validated_item_refs(
        cls,
        item: dict[str, Any],
        *,
        cards: dict[int, dict[str, Any]],
        request: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        source_refs = [cls._validate_source_ref(raw, cards) for raw in item.get("source_refs") or []]
        input_refs = [cls._validate_input_ref(raw, request) for raw in item.get("input_refs") or []]
        if not source_refs and not input_refs:
            raise ChangeDraftError("每个任务、步骤和风险必须包含 source_refs 或 input_refs")
        return source_refs, input_refs

    @staticmethod
    def _specific_parameter_violations(
        record: dict[str, Any],
        *,
        input_refs: list[str],
        request: dict[str, Any],
    ) -> list[str]:
        parameter_values = [
            dotted_get(request, reference)
            for reference in input_refs
            if reference.startswith("parameters.")
        ]
        allowed = stable_json(parameter_values)
        identifier_field = re.compile(
            r"(?:device|resource|object|host|node|element|instance)[_-]?id|"
            r"(?:设备|资源|对象|主机|网元|实例).*(?:id|标识)",
            re.IGNORECASE,
        )
        numeric_field = re.compile(
            r"threshold|timeout|limit|port|ratio|percent|duration|interval|retry|"
            r"阈值|超时|上限|端口|比例|百分比|时长|间隔|重试",
            re.IGNORECASE,
        )
        ipv4 = re.compile(r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(?:\."
                          r"(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])")
        violations: list[str] = []

        def visit(value: Any, path: str, field_name: str) -> None:
            if isinstance(value, dict):
                for child_name, child in value.items():
                    visit(child, f"{path}.{child_name}", str(child_name))
                return
            if isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}[{index}]", field_name)
                return
            if value in (None, "", [], {}):
                return
            serialized = stable_json(value)
            unbound = (
                bool(identifier_field.search(field_name))
                and serialized not in allowed
            )
            if bool(numeric_field.search(field_name)):
                numeric_tokens = re.findall(r"-?[0-9]+(?:\.[0-9]+)?", str(value))
                unbound = unbound or any(
                    stable_json(float(token) if "." in token else int(token))
                    not in allowed
                    for token in numeric_tokens
                )
            if isinstance(value, str):
                unbound = unbound or any(
                    stable_json(token) not in allowed
                    for token in ipv4.findall(value)
                )
            if unbound:
                violations.append(path)

        visit(record, "record", "")
        return violations

    @staticmethod
    def _apply_field_policy(
        raw_record: dict[str, Any],
        specs: dict[str, dict[str, Any]],
        request: dict[str, Any],
        *,
        location: str,
        missing: list[str],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        extra = sorted(set(raw_record) - set(specs))
        if extra:
            raise ChangeDraftError(f"{location} 包含 Profile 外字段: {extra}")
        for name, spec in specs.items():
            policy = str(spec.get("policy") or "generated")
            expected_type = str(spec.get("type") or "string")
            if policy.startswith("input:"):
                value = dotted_get(request, policy.split(":", 1)[1])
                if value in (None, "", [], {}):
                    missing.append(f"{location}.{name}")
                    value = empty_value(expected_type, field_name=name)
            elif policy == "fixed":
                value = spec.get("value")
            elif policy == "empty":
                value = empty_value(expected_type, field_name=name)
            elif policy == "not_executed":
                value = empty_value(expected_type, not_executed=True, field_name=name)
            else:
                if name not in raw_record or raw_record[name] in (None, ""):
                    missing.append(f"{location}.{name}")
                    value = empty_value(expected_type, field_name=name)
                else:
                    value = raw_record[name]
            if not value_matches_type(value, expected_type):
                raise ChangeDraftError(
                    f"{location}.{name} 类型错误，期望 {expected_type}，实际 {json_type(value)}"
                )
            result[name] = value
        return result

    @staticmethod
    def _profile_owned_value(
        name: str,
        spec: dict[str, Any],
        request: dict[str, Any],
        *,
        location: str,
        missing: list[str],
        suggested: Any = None,
    ) -> Any:
        policy = str(spec.get("policy") or "input_optional")
        expected_type = str(spec.get("type") or "string")
        if policy.startswith("input:"):
            value = dotted_get(request, policy.split(":", 1)[1])
            if value in (None, "", [], {}):
                missing.append(f"{location}.{name}")
                value = empty_value(expected_type, field_name=name)
        elif policy == "fixed":
            value = spec.get("value")
        elif policy == "empty":
            value = empty_value(expected_type, field_name=name)
        elif policy == "not_executed":
            value = empty_value(
                expected_type, not_executed=True, field_name=name
            )
        elif policy == "input_optional":
            value = suggested
            if value in (None, "", [], {}):
                value = (request.get("parameters") or {}).get(name)
            if value in (None, "", [], {}):
                value = empty_value(expected_type, field_name=name)
        else:
            missing.append(f"{location}.{name}")
            value = empty_value(expected_type, field_name=name)
        if expected_type == "string" and isinstance(value, list):
            value = "、".join(str(item) for item in value)
        if (
            expected_type in {"integer", "number"}
            and isinstance(value, str)
            and "time" in name.casefold()
        ):
            try:
                normalized_time = re.sub(
                    r"\s+([+-]\d{2}:\d{2})$", r"\1", value.strip()
                )
                parsed_time = datetime.fromisoformat(
                    normalized_time.replace("Z", "+00:00")
                )
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=timezone.utc)
                epoch = parsed_time.timestamp()
                sample = spec.get("sample")
                if isinstance(sample, (int, float)) and abs(float(sample)) >= 100_000_000_000:
                    epoch *= 1000
                value = int(epoch) if expected_type == "integer" else float(epoch)
            except (OverflowError, ValueError):
                pass
        if not value_matches_type(value, expected_type):
            raise ChangeDraftError(
                f"{location}.{name} 类型错误，期望 {expected_type}，实际 {json_type(value)}"
            )
        return value

    @classmethod
    def _context_value(
        cls,
        name: str,
        spec: dict[str, Any],
        request: dict[str, Any],
        *,
        missing: list[str],
    ) -> Any:
        mapping: dict[str, Any] = {
            "ticket_id": "DRAFT",
            "title": request.get("goal"),
            "change_scene": request.get("scenario"),
            "change_notes": f"当前状态：{request.get('current_state', '')}；目标状态：{request.get('target_state', '')}",
            "region": request.get("region"),
            "affected_service": request.get("services"),
            "affected_scope": request.get("impact_scope"),
            "impact_scope": request.get("impact_scope"),
            "expected_start_time": (request.get("window") or {}).get("start"),
            "expected_end_time": (request.get("window") or {}).get("end"),
        }
        return cls._profile_owned_value(
            name,
            spec,
            request,
            location="context",
            missing=missing,
            suggested=mapping.get(name),
        )

    def _assemble(
        self,
        normalized: Any,
        *,
        draft: dict[str, Any],
        profile: dict[str, Any],
        cards: dict[int, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not isinstance(normalized, dict):
            raise ChangeDraftError("模型草案不是 JSON 对象")
        request = draft["request"]
        tasks_raw = normalized.get("tasks")
        procedure_raw = normalized.get("procedure")
        risks_raw = normalized.get("risks") or []
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise ChangeDraftError("模型草案缺少 tasks")
        if not isinstance(procedure_raw, dict):
            raise ChangeDraftError("模型草案缺少 procedure")
        if not isinstance(risks_raw, list) or not risks_raw:
            raise ChangeDraftError("模型草案缺少至少一项带引用的风险")
        missing = [str(item) for item in normalized.get("missing_fields") or [] if str(item).strip()]
        provenance_items: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        normalized_tasks: list[dict[str, Any]] = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for index, raw_item in enumerate(tasks_raw):
            if not isinstance(raw_item, dict) or not isinstance(raw_item.get("record"), dict):
                raise ChangeDraftError(f"tasks[{index}] 不是有效任务对象")
            source_refs, input_refs = self._validated_item_refs(raw_item, cards=cards, request=request)
            record = self._apply_field_policy(
                raw_item["record"],
                profile["task_fields"],
                request,
                location=f"tasks[{index}]",
                missing=missing,
            )
            violations = self._specific_parameter_violations(
                record, input_refs=input_refs, request=request
            )
            if violations:
                raise ChangeDraftError(
                    f"tasks[{index}] 含未由 parameters input_ref 绑定的设备/IP/阈值字段: {violations}"
                )
            group = str(raw_item.get("group") or "default").strip() or "default"
            tasks.append(record)
            normalized_tasks.append(
                {
                    "group": group,
                    "record": record,
                    "source_refs": source_refs,
                    "input_refs": input_refs,
                }
            )
            groups.setdefault(group, []).append(dict(record))
            provenance_items.append(
                {"output": f"/data/action_list/{index}", "source_refs": source_refs, "input_refs": input_refs}
            )

        procedures: dict[str, list[dict[str, Any]]] = {}
        normalized_procedures: dict[str, list[dict[str, Any]]] = {}
        for key in PROCEDURE_KEYS:
            values = procedure_raw.get(key)
            if not isinstance(values, list) or not values:
                raise ChangeDraftError(f"模型草案缺少非空 procedure.{key}")
            records: list[dict[str, Any]] = []
            normalized_records: list[dict[str, Any]] = []
            for index, raw_item in enumerate(values):
                if not isinstance(raw_item, dict) or not isinstance(raw_item.get("record"), dict):
                    raise ChangeDraftError(f"procedure.{key}[{index}] 无效")
                source_refs, input_refs = self._validated_item_refs(raw_item, cards=cards, request=request)
                record = self._apply_field_policy(
                    raw_item["record"],
                    profile["procedure_fields"],
                    request,
                    location=f"procedure.{key}[{index}]",
                    missing=missing,
                )
                violations = self._specific_parameter_violations(
                    record, input_refs=input_refs, request=request
                )
                if violations:
                    raise ChangeDraftError(
                        f"procedure.{key}[{index}] 含未由 parameters input_ref 绑定的设备/IP/阈值字段: {violations}"
                    )
                records.append(record)
                normalized_records.append(
                    {
                        "record": record,
                        "source_refs": source_refs,
                        "input_refs": input_refs,
                    }
                )
                provenance_items.append(
                    {
                        "output": f"/data/sop_change_step/{key}/{index}",
                        "source_refs": source_refs,
                        "input_refs": input_refs,
                    }
                )
            procedures[key] = records
            normalized_procedures[key] = normalized_records

        validated_risks: list[dict[str, Any]] = []
        for index, risk in enumerate(risks_raw):
            if not isinstance(risk, dict) or not str(risk.get("text") or "").strip():
                raise ChangeDraftError(f"risks[{index}] 无效")
            source_refs, input_refs = self._validated_item_refs(risk, cards=cards, request=request)
            violations = self._specific_parameter_violations(
                {"risk_text": str(risk["text"]).strip()},
                input_refs=input_refs,
                request=request,
            )
            if violations:
                raise ChangeDraftError(
                    f"risks[{index}] 含未由 parameters input_ref 绑定的具体值: {violations}"
                )
            validated_risks.append(
                {"text": str(risk["text"]).strip(), "source_refs": source_refs, "input_refs": input_refs}
            )

        execution = self._apply_field_policy(
            {},
            profile["execution_fields"],
            request,
            location="execution_result",
            missing=missing,
        )
        data: dict[str, Any] = {}
        for name, spec in profile.get("context_fields", {}).items():
            data[name] = self._context_value(
                name, spec, request, missing=missing
            )
        data.update(
            action_list=tasks,
            change_tool_relate_action=groups,
            sop_change_step=procedures,
            change_plan=[{"result": execution}],
        )
        change_json: dict[str, Any] = {}
        for name, spec in profile.get("envelope_fields", {}).items():
            change_json[name] = self._profile_owned_value(
                name,
                spec,
                request,
                location="envelope",
                missing=missing,
            )
        change_json["data"] = data
        missing = list(dict.fromkeys(item for item in missing if item))
        normalized_result = {
            "title": str(normalized.get("title") or draft["request"]["goal"]),
            "summary": str(normalized.get("summary") or ""),
            "tasks": normalized_tasks,
            "procedure": normalized_procedures,
            "risks": validated_risks,
            "missing_fields": missing,
            "parameter_placeholders": [
                {"path": item, "placeholder": f"<MISSING:{item}>"}
                for item in missing
            ],
        }
        provenance = {
            "draft_id": draft["draft_id"],
            "profile_id": profile["profile_id"],
            "selected_case_ids": list(draft["selected_case_ids"]),
            "held_out_case_id": draft.get("held_out_case_id") or None,
            "items": provenance_items,
            "card_snapshots": [self._card_snapshot(card) for card in cards.values()],
            "request_hash": content_hash(request),
            "generated_at": utc_now(),
        }
        return normalized_result, change_json, provenance

    def _validate_assembled(
        self,
        change_json: dict[str, Any],
        *,
        normalized: dict[str, Any],
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        _plan, adapter_report = build_change_order_extraction_plan(
            json.dumps(change_json, ensure_ascii=False),
            chunk_size=self.knowledge.settings.change_order_chunk_size,
        )
        hard_failures: list[str] = []
        if adapter_report.get("semantic_mapping_status") != "CONFIRMED":
            hard_failures.append("SCHEMA_PROFILE_OR_ADAPTER_CONFLICT")
        if not (adapter_report.get("task_record") or {}).get("reconciled", False):
            hard_failures.append("TASK_GROUP_RECONCILIATION_FAILED")
        if normalized.get("missing_fields"):
            hard_failures.append("REQUIRED_FIELDS_MISSING")
        if not provenance.get("items"):
            hard_failures.append("PROVENANCE_MISSING")
        if len(provenance.get("items") or []) != (
            len(normalized.get("tasks") or [])
            + sum(len((normalized.get("procedure") or {}).get(key) or []) for key in PROCEDURE_KEYS)
        ):
            hard_failures.append("PROVENANCE_COVERAGE_INCOMPLETE")
        procedure = ((change_json.get("data") or {}).get("sop_change_step") or {})
        four_phase_complete = all(
            isinstance(procedure.get(key), list) and bool(procedure.get(key))
            for key in PROCEDURE_KEYS
        )
        if not four_phase_complete:
            hard_failures.append("FOUR_PHASE_CONTENT_INCOMPLETE")
        try:
            execution = change_json["data"]["change_plan"][0]["result"]
        except (KeyError, IndexError, TypeError):
            execution = None
        safe_execution_values = (None, "", "NOT_EXECUTED", False, 0, [], {})
        execution_sanitized = isinstance(execution, dict) and all(
            value in safe_execution_values for value in execution.values()
        )
        if not execution_sanitized:
            hard_failures.append("EXECUTION_RESULT_NOT_SANITIZED")
        expected_provenance = (
            len(normalized.get("tasks") or [])
            + sum(len((normalized.get("procedure") or {}).get(key) or []) for key in PROCEDURE_KEYS)
        )
        gates = {
            "schema_profile_consistent": (
                adapter_report.get("semantic_mapping_status") == "CONFIRMED"
            ),
            "required_parameters_complete": not bool(normalized.get("missing_fields")),
            "four_phase_complete": four_phase_complete,
            "task_views_reconciled": bool(
                (adapter_report.get("task_record") or {}).get("reconciled", False)
            ),
            "provenance_coverage_complete": (
                bool(provenance.get("items"))
                and len(provenance.get("items") or []) == expected_provenance
            ),
            "approved_sources_unchanged": True,
            "execution_result_sanitized": execution_sanitized,
            "no_unattributed_specific_values": True,
        }
        return {
            "passed": not hard_failures,
            "hard_failures": hard_failures,
            "missing_fields": normalized.get("missing_fields") or [],
            "gates": gates,
            "adapter": {
                "semantic_mapping_status": adapter_report.get("semantic_mapping_status"),
                "blockers": adapter_report.get("blockers") or [],
                "task_record": adapter_report.get("task_record") or {},
                "procedure": adapter_report.get("procedure") or {},
            },
        }

    def generate(self, draft_id: str, *, actor: str = "shared-operator") -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise ChangeDraftError("真实变更草案不存在", status=404)
        profile = self.store.active_profile()
        if profile is None or str(profile["profile_id"]) != str(draft["profile_id"]):
            error = "生成期间 SchemaProfile 已变化"
            self.store.set_draft_failure(draft_id, error)
            raise ChangeDraftError(error)
        try:
            bundles, cards = self._selected_context(draft)
            context = [self._prompt_card(card) for card in cards.values()]
            prompt = stable_json(
                {
                    "request": draft["request"],
                    "schema_profile": {
                        "profile_id": profile["profile_id"],
                        "task_fields": {
                            name: {
                                key: value
                                for key, value in spec.items()
                                if key in {"type", "policy"}
                            }
                            for name, spec in profile["task_fields"].items()
                        },
                        "procedure_fields": {
                            name: {
                                key: value
                                for key, value in spec.items()
                                if key in {"type", "policy"}
                            }
                            for name, spec in profile["procedure_fields"].items()
                        },
                    },
                    "selected_case_ids": [bundle["case_id"] for bundle in bundles],
                    "approved_cards": context,
                }
            )
            validation_error = ""
            usage: dict[str, Any] | None = None
            normalized: dict[str, Any] | None = None
            change_json: dict[str, Any] | None = None
            provenance: dict[str, Any] | None = None
            for attempt in range(2):
                user_prompt = prompt
                if attempt:
                    user_prompt += f"\n\n上一次输出未通过协议：{validation_error}。只修复协议错误，不增加无来源内容。"
                try:
                    payload, usage = self.knowledge.client.chat_json(
                        GENERATION_SYSTEM_PROMPT, user_prompt
                    )
                except Exception as exc:
                    validation_error = f"模型请求失败：{exc}"
                    if attempt:
                        raise
                    continue
                try:
                    normalized, change_json, provenance = self._assemble(
                        payload,
                        draft=draft,
                        profile=profile,
                        cards=cards,
                    )
                    break
                except ChangeDraftError as exc:
                    validation_error = str(exc)
                    if attempt:
                        raise
            assert normalized is not None and change_json is not None and provenance is not None
            validation = self._validate_assembled(
                change_json, normalized=normalized, provenance=provenance
            )
            return self.store.save_revision(
                draft_id,
                normalized=normalized,
                change_json=change_json,
                provenance=provenance,
                validation=validation,
                usage=usage,
                actor=actor,
            )
        except Exception as exc:
            self.store.set_draft_failure(draft_id, str(exc))
            raise

    def update_draft(self, draft_id: str, normalized: Any, *, actor: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise ChangeDraftError("真实变更草案不存在", status=404)
        if not draft.get("revision"):
            raise ChangeDraftError("草案尚未形成可编辑 revision")
        profile = self.store.active_profile()
        if profile is None or str(profile["profile_id"]) != str(draft["profile_id"]):
            raise ChangeDraftError("SchemaProfile 已变化，不能在旧 Profile 上编辑")
        _bundles, cards = self._selected_context(draft)
        normalized_result, change_json, provenance = self._assemble(
            normalized, draft=draft, profile=profile, cards=cards
        )
        validation = self._validate_assembled(
            change_json, normalized=normalized_result, provenance=provenance
        )
        return self.store.save_revision(
            draft_id,
            normalized=normalized_result,
            change_json=change_json,
            provenance=provenance,
            validation=validation,
            usage={"mode": "human_edit"},
            actor=actor,
        )

    def _revalidate_snapshots(self, draft: dict[str, Any]) -> None:
        revision = draft.get("revision") or {}
        provenance = revision.get("provenance") or {}
        for snapshot in provenance.get("card_snapshots") or []:
            card_id = int(snapshot["card_id"])
            card = self.knowledge.store.get_card(card_id)
            if card is None or card.get("status") != CardStatus.APPROVED.value:
                raise ChangeDraftError(f"引用卡片 K{card_id} 已不存在或不再 APPROVED")
            detail = self.knowledge.card_detail(card_id) or card
            current = self._card_snapshot(detail)
            if current["snapshot_hash"] != snapshot["snapshot_hash"]:
                raise ChangeDraftError(f"引用卡片 K{card_id} 已漂移，必须重新生成")

    def review_draft(
        self,
        draft_id: str,
        *,
        decision: str,
        reviewer: str,
        comment: str = "",
    ) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise ChangeDraftError("真实变更草案不存在", status=404)
        if decision.strip().upper() == "APPROVED":
            self._revalidate_snapshots(draft)
            revision = draft.get("revision") or {}
            validation = self._validate_assembled(
                revision.get("change") or {},
                normalized=revision.get("normalized") or {},
                provenance=revision.get("provenance") or {},
            )
            if not validation["passed"]:
                raise ChangeDraftError("草案复核未通过硬门禁")
        return self.store.review(
            draft_id,
            decision=decision,
            reviewer=reviewer.strip() or "shared-operator",
            comment=comment,
        )

    def export_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_draft(draft_id)
        if draft is None:
            raise ChangeDraftError("真实变更草案不存在", status=404)
        if draft["status"] != "REVIEW_APPROVED":
            raise ChangeDraftError("只有人工审核通过的 revision 可以导出", status=409)
        self._revalidate_snapshots(draft)
        revision = draft.get("revision") or {}
        return {
            "draft_id": draft_id,
            "revision": draft["current_revision"],
            "change_order": revision.get("change"),
            "provenance_report": revision.get("provenance"),
            "validation_report": revision.get("validation"),
        }

    def blind_request(self, case_id: str, *, actor: str) -> dict[str, Any]:
        bundle = self.knowledge.store.get_case_bundle(case_id, include_cards=True)
        eligible, reasons = self._eligible_bundle(bundle)
        if not eligible or bundle is None:
            raise ChangeDraftError("盲测目标不可用：" + "、".join(reasons))
        context_cards = [
            card
            for card in bundle.get("cards") or []
            if str((card.get("lineage") or {}).get("unit_role") or "")
            in {"IDENTITY_METADATA_CONTEXT", "CASE_CONTEXT"}
        ]
        context = context_cards[0] if context_cards else {}
        return asdict(
            RealChangeRequest(
                goal=str(bundle.get("title") or "留一案例盲测"),
                scenario=str(context.get("scenario") or "历史变更案例盲测"),
                region="",
                services=[],
                objects=[str(context.get("object_name") or "ChangeOrder")],
                current_state=str(context.get("summary") or "仅提供规划上下文，不暴露任务与步骤"),
                target_state=str(context.get("scenario") or "完成规划目标"),
                window={},
                impact_scope="",
                constraints=list(context.get("prerequisites") or []),
                parameters={},
                validation_requirements=[],
                requester=actor,
            )
        )

    def create_evaluation(self, held_out_case_id: str, *, actor: str) -> dict[str, Any]:
        self._require_enabled()
        self.knowledge.settings.require_api()
        profile = self.store.active_profile()
        if profile is None:
            raise ChangeDraftError("没有已激活的 SchemaProfile")
        self.blind_request(held_out_case_id, actor=actor)
        held_out = self.knowledge.store.get_case_bundle(
            held_out_case_id, include_cards=True
        )
        profile_source = self.knowledge.store.get_case_bundle(
            str(profile.get("source_case_id") or ""), include_cards=True
        )
        if held_out and profile_source and self._near_duplicate(held_out, profile_source):
            raise ChangeDraftError(
                "盲测目标或其近重复案例被当前 SchemaProfile 用作来源样例；请改用独立代表案例激活 Profile"
            )
        return self.store.create_evaluation(
            held_out_case_id=held_out_case_id,
            actor=actor.strip() or "shared-operator",
        )

    @classmethod
    def _ground_truth(cls, bundle: dict[str, Any]) -> dict[str, list[str]]:
        result = {"tasks": [], **{key: [] for key in PROCEDURE_KEYS}}
        for card in bundle.get("cards") or []:
            role = str((card.get("lineage") or {}).get("unit_role") or "")
            semantic = card.get("semantic_payload") or {}
            if role == "CASE_CONTEXT":
                result["tasks"].extend(
                    stable_json(item) for item in semantic.get("actions") or []
                )
                continue
            if role == "PROCEDURE_STEP":
                phase = str(card.get("scenario") or "").upper()
                key = {
                    "PRECHECK": "check_before_change",
                    "IMPLEMENTATION": "change_implement",
                    "VALIDATION": "change_verified",
                    "ROLLBACK": "change_rollback",
                }.get(phase)
                if key:
                    result[key].append(
                        str(
                            semantic.get("operation")
                            or semantic.get("validation")
                            or semantic.get("rollback")
                            or card.get("summary")
                            or ""
                        )
                    )
                continue
            if role == "TASKS_CANONICAL":
                result["tasks"].extend(str(item) for item in card.get("procedure_steps") or [])
            elif role in ROLE_TO_PROCEDURE_KEY:
                key = ROLE_TO_PROCEDURE_KEY[role]
                field_name = (
                    "validation_steps" if role == "VALIDATION_STEPS" else
                    "rollback_steps" if role == "ROLLBACK_STEPS" else
                    "procedure_steps"
                )
                result[key].extend(str(item) for item in card.get(field_name) or [])
        return result

    @staticmethod
    def _record_texts(normalized: dict[str, Any]) -> dict[str, list[str]]:
        return {
            "tasks": [stable_json(item.get("record") or {}) for item in normalized.get("tasks") or []],
            **{
                key: [stable_json(item.get("record") or {}) for item in (normalized.get("procedure") or {}).get(key) or []]
                for key in PROCEDURE_KEYS
            },
        }

    @staticmethod
    def _coverage(generated: list[str], expected: list[str]) -> float:
        if not expected:
            return 1.0 if not generated else 0.0
        generated_tokens = compact_tokens(" ".join(generated))
        expected_tokens = compact_tokens(" ".join(expected))
        if not expected_tokens:
            return 0.0
        return round(len(generated_tokens & expected_tokens) / len(expected_tokens), 4)

    @staticmethod
    def _record_similarity(left: str, right: str) -> float:
        left_tokens = compact_tokens(left)
        right_tokens = compact_tokens(right)
        if not left_tokens and not right_tokens:
            return 1.0
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    @classmethod
    def _match_metrics(
        cls, generated: list[str], expected: list[str]
    ) -> dict[str, Any]:
        if not expected:
            return {
                "matched": 0,
                "expected": 0,
                "generated": len(generated),
                "match_rate": 1.0 if not generated else 0.0,
                "order_similarity": 1.0 if not generated else 0.0,
            }
        available = set(range(len(expected)))
        matched_pairs: list[tuple[int, int, float]] = []
        for generated_index, generated_record in enumerate(generated):
            candidates = [
                (
                    cls._record_similarity(generated_record, expected[expected_index]),
                    expected_index,
                )
                for expected_index in available
            ]
            if not candidates:
                break
            score, expected_index = max(candidates)
            if score >= 0.5:
                matched_pairs.append((generated_index, expected_index, score))
                available.remove(expected_index)
        expected_order = [item[1] for item in matched_pairs]
        longest: list[int] = []
        for value in expected_order:
            position = 0
            while position < len(longest) and longest[position] < value:
                position += 1
            if position == len(longest):
                longest.append(value)
            else:
                longest[position] = value
        matched = len(matched_pairs)
        return {
            "matched": matched,
            "expected": len(expected),
            "generated": len(generated),
            "match_rate": round(matched / len(expected), 4),
            "order_similarity": round(len(longest) / matched, 4) if matched else 0.0,
            "average_pair_similarity": round(
                sum(item[2] for item in matched_pairs) / matched, 4
            ) if matched else 0.0,
        }

    def run_evaluation(self, evaluation_id: str, *, actor: str) -> dict[str, Any]:
        evaluation = self.store.get_evaluation(evaluation_id)
        if evaluation is None:
            raise ChangeDraftError("盲测评测不存在", status=404)
        held_out_case_id = str(evaluation["held_out_case_id"])
        try:
            request = self.blind_request(held_out_case_id, actor=actor)
            recommendations = self.recommend(request, held_out_case_id=held_out_case_id, limit=3)
            selected = [str(item["case_id"]) for item in recommendations["candidates"]]
            if not selected:
                raise ChangeDraftError("排除目标及近重复案例后没有可用参考案例")
            draft = self.create_draft(
                request,
                selected_case_ids=selected,
                actor=actor,
                mode="blind",
                held_out_case_id=held_out_case_id,
            )
            generated = self.generate(str(draft["draft_id"]), actor=actor)
            hidden = self.knowledge.store.get_case_bundle(held_out_case_id, include_cards=True)
            assert hidden is not None
            ground_truth = self._ground_truth(hidden)
            generated_records = self._record_texts((generated.get("revision") or {}).get("normalized") or {})
            category_metrics = {
                key: self._match_metrics(generated_records[key], ground_truth[key])
                for key in generated_records
            }
            all_generated = [
                item for key in generated_records for item in generated_records[key]
            ]
            all_expected = [item for key in ground_truth for item in ground_truth[key]]
            step_keys = [key for key in PROCEDURE_KEYS]
            all_generated_steps = [
                item for key in step_keys for item in generated_records[key]
            ]
            all_expected_steps = [item for key in step_keys for item in ground_truth[key]]
            near_duplicate_exclusions = [
                str(item.get("case_id"))
                for item in recommendations.get("rejected") or []
                if "BLIND_TARGET_OR_NEAR_DUPLICATE" in (item.get("reasons") or [])
            ]
            report = {
                "evaluation_id": evaluation_id,
                "mode": "leave_one_case_out",
                "held_out_case_id": held_out_case_id,
                "selected_case_ids": selected,
                "leakage_check": {
                    "target_excluded": held_out_case_id not in selected,
                    "near_duplicates_excluded": not bool(
                        set(near_duplicate_exclusions) & set(selected)
                    ),
                    "excluded_case_ids": near_duplicate_exclusions,
                    "profile_source_is_independent": True,
                },
                "hard_validation": (generated.get("revision") or {}).get("validation") or {},
                "counts": {
                    key: {"generated": len(generated_records[key]), "expected": len(ground_truth[key])}
                    for key in generated_records
                },
                "coverage": {
                    key: self._coverage(generated_records[key], ground_truth[key])
                    for key in generated_records
                },
                "soft_metrics": {
                    "original_record_similarity": self._coverage(
                        all_generated, all_expected
                    ),
                    "task_matching": category_metrics["tasks"],
                    "step_matching": self._match_metrics(
                        all_generated_steps, all_expected_steps
                    ),
                    "by_stage": category_metrics,
                    "expert_review": {
                        "status": "PENDING",
                        "safety_score": None,
                        "completeness_score": None,
                        "evidence_score": None,
                        "comment": "",
                    },
                },
                "soft_scores_only": True,
            }
            return self.store.finish_evaluation(
                evaluation_id,
                draft_id=str(draft["draft_id"]),
                report=report,
            )
        except Exception as exc:
            self.store.finish_evaluation(evaluation_id, error=str(exc))
            raise
