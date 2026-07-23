from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
import threading
import time
from typing import Any, Callable, Iterator

from .model import ModelClient
from .run_store import RunStore, RunStoreError, TERMINAL_RUN_STATUSES
from .tools import ToolRegistry, ToolResult


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class HarnessRuntimeError(RuntimeError):
    """Base runtime error with a stable machine-readable code."""

    code = "RUNTIME_ERROR"


class RunCancelled(HarnessRuntimeError):
    code = "RUN_CANCELLED"


class RunTimedOut(HarnessRuntimeError):
    code = "RUN_TIMEOUT"


class RunAwaitingApproval(HarnessRuntimeError):
    code = "TOOL_APPROVAL_REQUIRED"


class UnknownTask(HarnessRuntimeError):
    code = "UNKNOWN_TASK"


class RunQueueFull(HarnessRuntimeError):
    code = "RUN_QUEUE_FULL"


@dataclass(frozen=True)
class RunBudget:
    max_steps: int = 12
    timeout_seconds: int = 900
    max_tool_calls: int = 20
    max_total_tokens: int = 50_000

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RunBudget":
        values = payload or {}
        defaults = cls()
        normalized: dict[str, int] = {}
        for field_name, default in asdict(defaults).items():
            raw = values.get(field_name, default)
            if isinstance(raw, bool):
                raise HarnessRuntimeError(f"预算字段 {field_name} 必须是正整数")
            try:
                number = int(raw)
            except (TypeError, ValueError) as exc:
                raise HarnessRuntimeError(f"预算字段 {field_name} 必须是正整数") from exc
            if number <= 0:
                raise HarnessRuntimeError(f"预算字段 {field_name} 必须大于 0")
            normalized[field_name] = number
        return cls(**normalized)

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


TaskHandler = Callable[["RunContext"], dict[str, Any]]


class RunContext:
    """Per-run execution helper handed to registered task handlers."""

    def __init__(
        self,
        runtime: "HarnessRuntime",
        run: dict[str, Any],
    ):
        self.runtime = runtime
        self.run = run
        self.input = dict(run.get("input") or {})
        self.budget = RunBudget.from_dict(run.get("budget"))
        self._started = time.monotonic()
        self._step_count = self.runtime.store.next_step_index(self.run_id) - 1
        self._tool_call_count = 0
        self._total_tokens = 0

    @property
    def run_id(self) -> str:
        return str(self.run["id"])

    @property
    def checkpoint(self) -> dict[str, Any] | None:
        return self.runtime.store.latest_checkpoint(self.run_id)

    def emit(self, event_type: str, **payload: Any) -> None:
        self.runtime.store.add_event(self.run_id, event_type, payload)

    def save_checkpoint(self, **state: Any) -> dict[str, Any]:
        return self.runtime.store.save_checkpoint(self.run_id, state)

    def check_cancelled(self) -> None:
        status = self.runtime.store.current_status(self.run_id)
        if status == RunStatus.CANCEL_REQUESTED.value:
            raise RunCancelled("运行已请求取消")
        if status == RunStatus.CANCELLED.value:
            raise RunCancelled("运行已取消")
        if time.monotonic() - self._started > self.budget.timeout_seconds:
            raise RunTimedOut(f"运行超过 {self.budget.timeout_seconds} 秒预算")

    def consume_tokens(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        raw = usage.get("total_tokens", 0)
        try:
            tokens = int(raw)
        except (TypeError, ValueError):
            return
        self._total_tokens += max(tokens, 0)
        if self._total_tokens > self.budget.max_total_tokens:
            raise RunTimedOut("运行超过最大 token 预算")

    def register_tool_call(self) -> None:
        self._tool_call_count += 1
        if self._tool_call_count > self.budget.max_tool_calls:
            raise HarnessRuntimeError("运行超过最大工具调用次数")

    def chat(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Call the configured provider while charging usage to this Run budget."""
        if self.runtime.model_client is None:
            raise HarnessRuntimeError("No model client is configured for this runtime")
        self.check_cancelled()
        message, usage = self.runtime.model_client.chat(messages, **kwargs)
        normalized_usage = usage if isinstance(usage, dict) else {}
        self.consume_tokens(normalized_usage)
        self.check_cancelled()
        return message, normalized_usage

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        **kwargs: Any,
    ) -> tuple[Any, dict[str, Any]]:
        """Call a JSON-producing provider while charging usage to this Run budget."""
        if self.runtime.model_client is None:
            raise HarnessRuntimeError("No model client is configured for this runtime")
        self.check_cancelled()
        payload, usage = self.runtime.model_client.chat_json(
            system_prompt, user_prompt, **kwargs
        )
        normalized_usage = usage if isinstance(usage, dict) else {}
        self.consume_tokens(normalized_usage)
        self.check_cancelled()
        return payload, normalized_usage

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """Execute a registered tool with budget accounting and an audit event."""
        self.register_tool_call()
        self.check_cancelled()
        approved = self.runtime.store.is_tool_approved(self.run_id, name)
        result = self.runtime.tools.execute(name, arguments, self, approved=approved)
        self.emit(
            "tool.completed",
            tool=name,
            ok=result.ok,
            error_code=result.error_code,
            truncated=result.truncated,
        )
        if result.error_code == "APPROVAL_REQUIRED":
            self.runtime.store.request_tool_approval(
                self.run_id,
                name,
                reason=result.error_message or "A non-read-only tool requires approval",
            )
            raise RunAwaitingApproval(f"Tool approval required: {name}")
        self.check_cancelled()
        return result

    @contextmanager
    def step(
        self,
        kind: str,
        name: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        self.check_cancelled()
        self._step_count += 1
        if self._step_count > self.budget.max_steps:
            raise HarnessRuntimeError("运行超过最大步骤数")
        step_id = self.runtime.store.start_step(
            self.run_id,
            step_index=self._step_count,
            kind=kind,
            name=name,
            payload=payload or {},
        )
        try:
            yield
        except RunAwaitingApproval as exc:
            self.runtime.store.finish_step(
                self.run_id,
                step_id,
                status="WAITING_APPROVAL",
                error_code=exc.code,
                error_message=str(exc),
            )
            raise
        except HarnessRuntimeError as exc:
            self.runtime.store.finish_step(
                self.run_id,
                step_id,
                status="FAILED",
                error_code=exc.code,
                error_message=str(exc),
            )
            raise
        except Exception as exc:
            self.runtime.store.finish_step(
                self.run_id,
                step_id,
                status="FAILED",
                error_code="STEP_FAILED",
                error_message=str(exc),
            )
            raise
        else:
            self.runtime.store.finish_step(self.run_id, step_id, status="SUCCEEDED")
        finally:
            self.check_cancelled()


class HarnessRuntime:
    """Small persistent local runtime for bounded, observable Agent tasks."""

    def __init__(
        self,
        store: RunStore,
        *,
        worker_count: int = 2,
        max_queued_runs: int = 100,
        poll_interval_seconds: float = 0.05,
        model_client: ModelClient | None = None,
    ):
        if worker_count <= 0 or max_queued_runs <= 0:
            raise HarnessRuntimeError("Worker 数量和队列上限必须大于 0")
        self.store = store
        self.store.initialize()
        self.store.recover_interrupted()
        self.worker_count = worker_count
        self.max_queued_runs = max_queued_runs
        self.poll_interval_seconds = max(poll_interval_seconds, 0.01)
        self.model_client = model_client
        self.tools = ToolRegistry()
        self._handlers: dict[str, TaskHandler] = {}
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start_lock = threading.Lock()

    def register_task(self, task_type: str, handler: TaskHandler) -> None:
        normalized = task_type.strip()
        if not normalized:
            raise HarnessRuntimeError("任务类型不能为空")
        if normalized in self._handlers:
            raise HarnessRuntimeError(f"任务类型已注册: {normalized}")
        self._handlers[normalized] = handler

    def task_types(self) -> list[str]:
        return sorted(self._handlers)

    def start(self) -> None:
        with self._start_lock:
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if self._threads:
                return
            self._stop_event.clear()
            for index in range(self.worker_count):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"harness-worker-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        for thread in list(self._threads):
            thread.join(timeout=max(timeout_seconds, 0.0))
        self._threads = []

    def submit(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        budget: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        normalized = task_type.strip()
        if normalized not in self._handlers:
            raise UnknownTask(f"未注册任务类型: {normalized}")
        if not isinstance(payload, dict):
            raise HarnessRuntimeError("运行输入必须是 JSON 对象")
        existing = self.store.find_idempotent(normalized, idempotency_key)
        if existing is not None:
            return existing, False
        if self.store.queued_count() >= self.max_queued_runs:
            raise RunQueueFull("运行队列已满，请稍后重试")
        run, created = self.store.create_run(
            task_type=normalized,
            payload=payload,
            budget=RunBudget.from_dict(budget).to_dict(),
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
        )
        self.start()
        return run, created

    def wait(self, run_id: str, *, timeout_seconds: float | None = None) -> dict[str, Any] | None:
        started = time.monotonic()
        while True:
            run = self.store.get_run(run_id)
            if run is None or run["status"] in (
                TERMINAL_RUN_STATUSES
                | {RunStatus.INTERRUPTED.value, RunStatus.WAITING_APPROVAL.value}
            ):
                return run
            if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
                return run
            time.sleep(self.poll_interval_seconds)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        return self.store.request_cancel(run_id)

    def resume(self, run_id: str) -> dict[str, Any] | None:
        resumed = self.store.resume(run_id)
        if resumed is not None:
            self.start()
        return resumed

    def decide_tool_approval(
        self,
        run_id: str,
        tool_name: str,
        *,
        decision: str,
        actor: str,
        comment: str = "",
    ) -> dict[str, Any] | None:
        run = self.store.decide_tool_approval(
            run_id,
            tool_name,
            decision=decision,
            actor=actor,
            comment=comment,
        )
        if run is not None and run["status"] == RunStatus.QUEUED.value:
            self.start()
        return run

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            run = self.store.claim_next()
            if run is None:
                self._stop_event.wait(self.poll_interval_seconds)
                continue
            self._execute_claimed_run(run)

    def _execute_claimed_run(self, run: dict[str, Any]) -> None:
        run_id = str(run["id"])
        handler = self._handlers.get(str(run["task_type"]))
        if handler is None:
            self.store.finish_run(
                run_id,
                status=RunStatus.FAILED.value,
                error_code=UnknownTask.code,
                error_message=f"未注册任务类型: {run['task_type']}",
            )
            return
        context = RunContext(self, run)
        try:
            context.check_cancelled()
            with context.step("task", str(run["task_type"]), payload={"attempt": run["attempt"]}):
                result = handler(context)
            context.check_cancelled()
        except RunCancelled as exc:
            self.store.finish_run(
                run_id,
                status=RunStatus.CANCELLED.value,
                error_code=exc.code,
                error_message=str(exc),
            )
        except RunAwaitingApproval as exc:
            self.store.add_event(
                run_id,
                "run.waiting_approval",
                {"error_code": exc.code},
            )
        except HarnessRuntimeError as exc:
            self.store.finish_run(
                run_id,
                status=RunStatus.FAILED.value,
                error_code=exc.code,
                error_message=str(exc),
            )
        except Exception as exc:
            self.store.finish_run(
                run_id,
                status=RunStatus.FAILED.value,
                error_code="TASK_FAILED",
                error_message=str(exc),
            )
        else:
            if not isinstance(result, dict):
                self.store.finish_run(
                    run_id,
                    status=RunStatus.FAILED.value,
                    error_code="INVALID_TASK_RESULT",
                    error_message="任务处理器必须返回 JSON 对象",
                )
                return
            context.save_checkpoint(phase="completed", result_summary={"keys": sorted(result)[:20]})
            self.store.finish_run(run_id, status=RunStatus.SUCCEEDED.value, result=result)

    def __enter__(self) -> "HarnessRuntime":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()
