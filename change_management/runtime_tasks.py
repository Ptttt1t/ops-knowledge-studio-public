from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.run_store import RunStore
from harness.runtime import HarnessRuntime, HarnessRuntimeError, RunContext
from harness.tools import RiskLevel, ToolSpec

from .service import DemoChangeService


def apply_plan_in_isolated_worker(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    static = context.get("static")
    if not isinstance(static, dict):
        raise HarnessRuntimeError("隔离工具缺少静态上下文")
    workspace = Path(str(static.get("workspace") or "")).resolve()
    case_id = str(static.get("case_id") or "").strip()
    if not workspace.name or not case_id:
        raise HarnessRuntimeError("隔离工具缺少工作区或案例标识")
    service = DemoChangeService(workspace, case_id=case_id)
    return service.apply_plan(
        arguments, run_id=str(context.get("run_id") or "unknown-run")
    )


def register_change_tasks(runtime: HarnessRuntime, service: DemoChangeService) -> None:
    runtime.tools.register(
        ToolSpec(
            name=service.TOOL_NAME,
            description=(
                "Apply one immutable synthetic cloud-network route change plan. "
                "This tool can only write to the per-demo SQLite simulator."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "plan_hash": {"type": "string"},
                    "snapshot_version": {"type": "integer", "minimum": 1},
                    "actor": {"type": "string"},
                    "inject_failure": {"type": "string"},
                },
                "required": [
                    "ticket_id",
                    "revision",
                    "plan_hash",
                    "snapshot_version",
                    "actor",
                    "inject_failure",
                ],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.LOCAL_WRITE,
            timeout_seconds=120,
            isolated_entrypoint=(
                "change_management.runtime_tasks:apply_plan_in_isolated_worker"
            ),
            isolated_context={
                "workspace": str(service.workspace),
                "case_id": service.case.case_id,
            },
        )
    )

    def generate(context: RunContext) -> dict[str, Any]:
        with context.step("environment", "seed_synthetic_environment"):
            context.check_cancelled()
            service.simulator.seed()
        with context.step("knowledge", "reuse_approved_demo_knowledge"):
            service.seed_demo_knowledge()
        with context.step("decision", "generate_and_validate_change"):
            package = service.generate_ticket(
                requested_by=str(context.input.get("requested_by") or "demo-operator"),
                use_model=bool(context.input.get("use_model", False)),
            )
        context.save_checkpoint(
            phase="change_generated",
            ticket_id=package["ticket"]["ticket_id"],
            status=package["ticket"]["status"],
            plan_hash=package["ticket"]["plan_hash"],
        )
        return package

    def execute(context: RunContext) -> dict[str, Any]:
        ticket_id = str(context.input.get("ticket_id") or "").strip()
        if not ticket_id:
            raise HarnessRuntimeError("变更执行任务缺少 ticket_id")
        package = service.ticket_package(ticket_id)
        ticket = package["ticket"]
        if ticket["status"] in {
            "SUCCEEDED",
            "ROLLED_BACK",
        } and package["execution"] is not None:
            with context.step("recovery", "reuse_completed_execution"):
                feedback = service.create_feedback(ticket_id)
            return {
                "ticket": ticket,
                "execution": package["execution"],
                "feedback": feedback,
                "workspace": str(service.workspace),
            }
        if ticket["status"] == "VERIFYING" and package["execution"] is not None:
            with context.step("recovery", "finalize_recorded_execution"):
                recorded = service.finalize_recorded_execution(ticket_id)
                feedback = service.create_feedback(ticket_id)
            return {
                "ticket": service.change_store.require_ticket(ticket_id),
                "execution": recorded,
                "feedback": feedback,
                "workspace": str(service.workspace),
            }
        arguments = {
            "ticket_id": ticket_id,
            "revision": int(ticket["revision"]),
            "plan_hash": str(ticket["plan_hash"]),
            "snapshot_version": int(ticket["environment_snapshot_version"]),
            "actor": str(context.input.get("actor") or "demo-operator"),
            "inject_failure": str(context.input.get("inject_failure") or ""),
        }
        with context.step(
            "approval",
            "await_change_approval",
            payload={
                "ticket_id": ticket_id,
                "plan_hash": ticket["plan_hash"],
                "snapshot_version": ticket["environment_snapshot_version"],
            },
        ):
            service.mark_waiting_approval(ticket_id, run_id=context.run_id)
            tool_result = context.call_tool(service.TOOL_NAME, arguments)
            if not tool_result.ok:
                raise HarnessRuntimeError(
                    tool_result.error_message or "模拟云网络工具执行失败"
                )
        with context.step("feedback", "capture_execution_feedback"):
            feedback = service.create_feedback(ticket_id)
        context.save_checkpoint(
            phase="change_closed_loop_completed",
            ticket_id=ticket_id,
            outcome=tool_result.output.get("outcome") if isinstance(tool_result.output, dict) else None,
            knowledge_candidate_id=feedback.get("knowledge_candidate_id"),
        )
        return {
            "ticket": service.change_store.require_ticket(ticket_id),
            "execution": tool_result.output,
            "feedback": feedback,
            "workspace": str(service.workspace),
        }

    runtime.register_task("change.generate_demo", generate)
    runtime.register_task("change.execute_demo", execute)


def create_change_runtime(
    service: DemoChangeService,
    *,
    worker_count: int = 1,
    max_queued_runs: int = 10,
) -> HarnessRuntime:
    runtime = HarnessRuntime(
        RunStore(service.workspace / "runtime.db"),
        worker_count=worker_count,
        max_queued_runs=max_queued_runs,
        model_client=service.model_client,
    )
    register_change_tasks(runtime, service)
    return runtime
