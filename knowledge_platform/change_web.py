from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
import shutil
import threading
import time
from typing import Any

from change_management.cases import (
    DEFAULT_CASE_ID,
    list_change_cases,
    seed_case_catalog_knowledge,
)
from change_management.runtime_tasks import create_change_runtime
from change_management.service import DemoChangeError, DemoChangeService
from change_management.simulator import SimulationError
from harness.runtime import HarnessRuntime

from .service import KnowledgeService


@dataclass
class ChangeDemoSession:
    session_id: str
    service: DemoChangeService
    runtime: HarnessRuntime
    generate_run_id: str
    execute_run_id: str = ""
    created_at: float = 0.0
    last_accessed_at: float = 0.0
    terminal_at: float = 0.0
    runtime_stopped: bool = False


class ChangeSessionLimitError(DemoChangeError):
    status = int(HTTPStatus.TOO_MANY_REQUESTS)
    code = "change_session_limit_exceeded"


class ChangeDemoWebManager:
    """Own isolated synthetic demo sessions used by the integrated workbench."""

    def __init__(self, knowledge_service: KnowledgeService):
        self.knowledge_service = knowledge_service
        self._case_knowledge: list[dict[str, Any]] = []
        self._sessions: dict[str, ChangeDemoSession] = {}
        self._latest_session_id = ""
        self._lock = threading.RLock()
        self._creating = 0
        self._cleanup_stale_workspaces()

    def create(
        self,
        *,
        requested_by: str,
        use_model: bool = False,
        case_id: str = DEFAULT_CASE_ID,
    ) -> dict[str, Any]:
        self._ensure_case_knowledge()
        self.cleanup()
        evicted = self._reserve_creation_slot()
        self._dispose_sessions(evicted, delete_workspace=True)
        runtime: HarnessRuntime | None = None
        workspace: Path | None = None
        try:
            workspace = DemoChangeService.create_workspace(
                self.knowledge_service.settings.project_root
            )
            model_client = (
                self.knowledge_service.client
                if use_model and self.knowledge_service.settings.api_configured
                else None
            )
            service = DemoChangeService(
                workspace, model_client=model_client, case_id=case_id
            )
            runtime = create_change_runtime(service, worker_count=1, max_queued_runs=10)
            run, _created = runtime.submit(
                "change.generate_demo",
                {
                    "requested_by": requested_by.strip() or "demo-operator",
                    "use_model": bool(use_model),
                },
            )
            now = time.time()
            session = ChangeDemoSession(
                session_id=workspace.name,
                service=service,
                runtime=runtime,
                generate_run_id=str(run["id"]),
                created_at=now,
                last_accessed_at=now,
            )
        except Exception:
            if runtime is not None:
                runtime.stop()
            if workspace is not None:
                self._delete_workspace(workspace)
            raise
        finally:
            with self._lock:
                self._creating = max(0, self._creating - 1)
        with self._lock:
            self._sessions[session.session_id] = session
            self._latest_session_id = session.session_id
        return self.describe(session.session_id)

    def cases(self) -> list[dict[str, Any]]:
        self._ensure_case_knowledge()
        knowledge_by_case = {
            item["case_id"]: item for item in self._case_knowledge
        }
        return [
            {**case, **knowledge_by_case.get(str(case["case_id"]), {})}
            for case in list_change_cases()
        ]

    def _ensure_case_knowledge(self) -> None:
        if self._case_knowledge:
            return
        with self._lock:
            if not self._case_knowledge:
                self._case_knowledge = seed_case_catalog_knowledge(
                    self.knowledge_service.store
                )

    def latest(self) -> dict[str, Any] | None:
        self.cleanup()
        with self._lock:
            session_id = self._latest_session_id
        return self.describe(session_id) if session_id else None

    def describe(self, session_id: str) -> dict[str, Any]:
        self.cleanup()
        session = self._require(session_id)
        session.last_accessed_at = time.time()
        package = None
        if session.service.change_store.get_ticket(session.service.TICKET_ID) is not None:
            package = session.service.ticket_package(session.service.TICKET_ID)
        try:
            network = session.service.simulator.snapshot()
        except SimulationError:
            network = None
        published = None
        if package is not None:
            for audit in package["audit"]:
                if audit["action"] == "FEEDBACK_PUBLISHED_TO_MAIN":
                    published = {
                        "knowledge_card_id": int(audit["detail"]["knowledge_card_id"]),
                        "status": str(audit["detail"]["knowledge_status"]),
                    }
        payload = {
            "session_id": session.session_id,
            "synthetic": True,
            "case": session.service.case.public_dict(),
            "package": package,
            "network": network,
            "operations": (
                session.service.simulator.operation_rows(session.service.TICKET_ID)
                if network is not None
                else []
            ),
            "runs": {
                "generate": self._run_detail(session, session.generate_run_id),
                "execute": self._run_detail(session, session.execute_run_id),
            },
            "published_feedback": published,
        }
        if package is not None and self._is_terminal_status(
            str(package["ticket"]["status"])
        ):
            if not session.terminal_at:
                session.terminal_at = time.time()
            self._stop_session_runtime(session)
        return payload

    def start_execution(
        self,
        session_id: str,
        *,
        actor: str,
        inject_failure: str = "",
    ) -> dict[str, Any]:
        self.cleanup()
        session = self._require(session_id)
        if session.execute_run_id:
            return self.describe(session_id)
        package = session.service.ticket_package(session.service.TICKET_ID)
        if package["ticket"]["status"] != "READY_FOR_APPROVAL":
            raise DemoChangeError(
                f"当前变更状态不能提交审批: {package['ticket']['status']}"
            )
        allowed_failures = {"", *session.service.case.execution_step_ids}
        if inject_failure not in allowed_failures:
            raise ValueError("不支持的故障注入点")
        run, _created = session.runtime.submit(
            "change.execute_demo",
            {
                "ticket_id": session.service.TICKET_ID,
                "actor": actor.strip() or "demo-operator",
                "inject_failure": inject_failure,
            },
        )
        session.execute_run_id = str(run["id"])
        return self.describe(session_id)

    def decide(
        self,
        session_id: str,
        *,
        decision: str,
        actor: str,
        comment: str = "",
        confirmation: str = "",
    ) -> dict[str, Any]:
        self.cleanup()
        session = self._require(session_id)
        if not session.execute_run_id:
            raise DemoChangeError("尚未提交执行审批")
        normalized = decision.strip().upper()
        if normalized == "APPROVED":
            expected = f"APPROVE {session.service.TICKET_ID}"
            if confirmation.strip() != expected:
                raise ValueError(f"确认串必须精确输入：{expected}")
        elif normalized != "REJECTED":
            raise ValueError("审批决定只能是 APPROVED 或 REJECTED")
        reviewer = actor.strip()
        if not reviewer:
            raise ValueError("审批人不能为空")
        run = session.runtime.decide_tool_approval(
            session.execute_run_id,
            session.service.TOOL_NAME,
            decision=normalized,
            actor=reviewer,
            comment=comment,
        )
        if run is None:
            raise DemoChangeError("执行 Run 不存在")
        if normalized == "REJECTED":
            session.service.reject_ticket(
                session.service.TICKET_ID,
                actor=reviewer,
                comment=comment,
            )
        return self.describe(session_id)

    def publish_feedback(self, session_id: str, *, actor: str) -> dict[str, Any]:
        self.cleanup()
        session = self._require(session_id)
        publication = session.service.publish_feedback_to(
            session.service.TICKET_ID,
            self.knowledge_service.store,
            actor=actor,
        )
        payload = self.describe(session_id)
        payload["publication"] = publication
        return payload

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.runtime.stop()

    def cleanup(self) -> None:
        now = time.time()
        expired: list[ChangeDemoSession] = []
        with self._lock:
            for session_id, session in list(self._sessions.items()):
                status = self._ticket_status(session)
                terminal = self._is_terminal_status(status)
                if terminal and not session.terminal_at:
                    session.terminal_at = now
                active_expired = (
                    not terminal
                    and now - session.created_at
                    > self.knowledge_service.settings.change_active_ttl_seconds
                )
                terminal_expired = (
                    terminal
                    and now - (session.terminal_at or session.created_at)
                    > self.knowledge_service.settings.change_terminal_ttl_seconds
                )
                if active_expired or terminal_expired:
                    expired.append(self._sessions.pop(session_id))
            self._repair_latest_locked()
        self._dispose_sessions(expired, delete_workspace=True)

    def _reserve_creation_slot(self) -> list[ChangeDemoSession]:
        evicted: list[ChangeDemoSession] = []
        settings = self.knowledge_service.settings
        with self._lock:
            active = sum(
                1
                for session in self._sessions.values()
                if not self._is_terminal_status(self._ticket_status(session))
            )
            if active + self._creating >= settings.change_max_active_sessions:
                raise ChangeSessionLimitError("活动变更演示会话已达到上限")
            terminal = sorted(
                (
                    session
                    for session in self._sessions.values()
                    if self._is_terminal_status(self._ticket_status(session))
                ),
                key=lambda item: item.terminal_at or item.created_at,
            )
            while (
                len(self._sessions) + self._creating
                >= settings.change_max_retained_sessions
                and terminal
            ):
                victim = terminal.pop(0)
                self._sessions.pop(victim.session_id, None)
                evicted.append(victim)
            if len(self._sessions) + self._creating >= settings.change_max_retained_sessions:
                raise ChangeSessionLimitError("变更演示会话保留数量已达到上限")
            self._creating += 1
            self._repair_latest_locked()
        return evicted

    @staticmethod
    def _is_terminal_status(status: str) -> bool:
        return status in {"BLOCKED", "SUCCEEDED", "ROLLED_BACK", "FAILED", "REJECTED"}

    @staticmethod
    def _ticket_status(session: ChangeDemoSession) -> str:
        ticket = session.service.change_store.get_ticket(session.service.TICKET_ID)
        return str(ticket["status"]) if ticket is not None else ""

    def _repair_latest_locked(self) -> None:
        if self._latest_session_id in self._sessions:
            return
        latest = max(
            self._sessions.values(),
            key=lambda item: item.created_at,
            default=None,
        )
        self._latest_session_id = latest.session_id if latest is not None else ""

    @staticmethod
    def _stop_session_runtime(session: ChangeDemoSession) -> None:
        if not session.runtime_stopped:
            session.runtime.stop()
            session.runtime_stopped = True

    def _dispose_sessions(
        self, sessions: list[ChangeDemoSession], *, delete_workspace: bool
    ) -> None:
        for session in sessions:
            self._stop_session_runtime(session)
            if delete_workspace:
                self._delete_workspace(session.service.workspace)

    def _workspace_root(self) -> Path:
        return (
            self.knowledge_service.settings.project_root / "artifacts" / "change_demos"
        ).resolve()

    def _delete_workspace(self, workspace: Path) -> None:
        root = self._workspace_root()
        target = workspace.resolve()
        if target == root or root not in target.parents:
            raise DemoChangeError("拒绝清理变更演示目录之外的路径")
        if target.is_dir():
            shutil.rmtree(target)

    def _cleanup_stale_workspaces(self) -> None:
        root = self._workspace_root()
        if not root.is_dir():
            return
        cutoff = time.time() - self.knowledge_service.settings.change_terminal_ttl_seconds
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    self._delete_workspace(child)
            except OSError:
                continue

    def _require(self, session_id: str) -> ChangeDemoSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("变更演示会话不存在或服务已重启")
        return session

    @staticmethod
    def _run_detail(
        session: ChangeDemoSession,
        run_id: str,
    ) -> dict[str, Any] | None:
        if not run_id:
            return None
        run = session.runtime.store.get_run(run_id)
        if run is None:
            return None
        run["steps"] = session.runtime.store.list_steps(run_id)
        run["events"] = session.runtime.store.list_events(run_id, limit=500)
        run["latest_checkpoint"] = session.runtime.store.latest_checkpoint(run_id)
        run["tool_approvals"] = session.runtime.store.list_tool_approvals(run_id)
        return run
