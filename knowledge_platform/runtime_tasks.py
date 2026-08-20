from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.run_store import RunStore
from harness.runtime import HarnessRuntime, HarnessRuntimeError, RunContext

from .service import KnowledgeService


def _text(payload: dict[str, Any], field: str, *, required: bool = True) -> str:
    value = str(payload.get(field) or "").strip()
    if required and not value:
        raise HarnessRuntimeError(f"任务输入缺少 {field}")
    return value


def _safe_uploaded_path(service: KnowledgeService, raw_path: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    upload_root = (service.settings.source_dir / "uploads").resolve()
    try:
        path.relative_to(upload_root)
    except ValueError as exc:
        raise HarnessRuntimeError("文件导入任务只允许处理已保存的上传文件") from exc
    if not path.is_file():
        raise HarnessRuntimeError("上传文件不存在")
    return path


def register_knowledge_tasks(runtime: HarnessRuntime, service: KnowledgeService) -> None:
    """Register existing knowledge operations as durable Harness tasks."""

    def ingest_text(context: RunContext) -> dict[str, Any]:
        context.check_cancelled()
        with context.step("knowledge", "ingest_text", payload={"source_name": context.input.get("source_name", "")}):
            result = service.ingest_text(
                source_name=_text(context.input, "source_name"),
                source_ref=_text(context.input, "source_ref", required=False) or "manual://web-input",
                content=_text(context.input, "content"),
                source_type=_text(context.input, "source_type", required=False) or "text",
            )
        context.save_checkpoint(phase="knowledge_imported", document_id=result.get("document_id"))
        return result

    def ingest_file(context: RunContext) -> dict[str, Any]:
        path = _safe_uploaded_path(service, _text(context.input, "path"))
        with context.step("document", "read_uploaded_file", payload={"name": path.name}):
            context.check_cancelled()
        with context.step("knowledge", "ingest_file", payload={"name": path.name}):
            result = service.ingest_file(
                path,
                source_name=_text(context.input, "source_name", required=False) or path.name,
            )
        context.save_checkpoint(phase="knowledge_imported", document_id=result.get("document_id"))
        return result

    def query(context: RunContext) -> dict[str, Any]:
        with context.step("knowledge", "trusted_query"):
            result = service.query(_text(context.input, "question"))
        context.save_checkpoint(phase="answer_generated", source_count=len(result.get("sources", [])))
        return result

    def agent_query(context: RunContext) -> dict[str, Any]:
        with context.step("agent", "trusted_agent_query"):
            result = service.agent_query(_text(context.input, "question"))
        context.save_checkpoint(phase="agent_answer_generated", source_count=len(result.get("sources", [])))
        return result

    def regrade(context: RunContext) -> dict[str, Any]:
        with context.step("knowledge", "regrade_existing_cards"):
            result = service.regrade_existing_cards()
        context.save_checkpoint(phase="regraded", processed=result.get("processed", 0))
        return result

    def generate_real_change_draft(context: RunContext) -> dict[str, Any]:
        draft_id = _text(context.input, "draft_id")
        actor = _text(context.input, "actor", required=False) or "shared-operator"
        with context.step("change", "generate_real_change_draft", payload={"draft_id": draft_id}):
            result = service.change_drafts.generate(draft_id, actor=actor)
        context.save_checkpoint(
            phase="real_change_draft_generated",
            draft_id=draft_id,
            status=result.get("status"),
            revision=result.get("current_revision"),
        )
        return result

    def evaluate_real_change_draft(context: RunContext) -> dict[str, Any]:
        evaluation_id = _text(context.input, "evaluation_id")
        actor = _text(context.input, "actor", required=False) or "shared-operator"
        with context.step(
            "evaluation",
            "leave_one_case_out",
            payload={"evaluation_id": evaluation_id},
        ):
            result = service.change_drafts.run_evaluation(evaluation_id, actor=actor)
        context.save_checkpoint(
            phase="real_change_evaluation_completed",
            evaluation_id=evaluation_id,
            status=result.get("status"),
        )
        return result

    runtime.register_task("knowledge.ingest_text", ingest_text)
    runtime.register_task("knowledge.ingest_file", ingest_file)
    runtime.register_task("knowledge.query", query)
    runtime.register_task("knowledge.agent_query", agent_query)
    runtime.register_task("knowledge.regrade", regrade)
    runtime.register_task("change.generate_real_draft", generate_real_change_draft)
    runtime.register_task("change.evaluate_leave_one_out", evaluate_real_change_draft)


def create_knowledge_runtime(
    service: KnowledgeService,
    *,
    worker_count: int = 2,
    max_queued_runs: int = 100,
) -> HarnessRuntime:
    runtime_path = service.settings.runtime_database_path or (
        service.settings.project_root / "data" / "runtime.db"
    )
    runtime = HarnessRuntime(
        RunStore(runtime_path),
        worker_count=worker_count,
        max_queued_runs=max_queued_runs,
        model_client=service.client,
    )
    register_knowledge_tasks(runtime, service)
    return runtime
