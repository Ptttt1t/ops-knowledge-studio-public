from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest

from harness.run_store import RunStore
from harness.runtime import HarnessRuntime, RunStatus
from harness.tools import RiskLevel, ToolRegistry, ToolSpec
from knowledge_platform.cli import main as cli_main


class _ToolContext:
    def check_cancelled(self) -> None:
        return None


class _FakeModelClient:
    def chat(self, messages, **_kwargs):
        return {"content": messages[-1]["content"]}, {"total_tokens": 3}

    def chat_json(self, _system_prompt, _user_prompt, **_kwargs):
        return {"answer": "model-ok"}, {"total_tokens": 3}


class HarnessRuntimeTests(unittest.TestCase):
    def make_runtime(
        self, root: Path, *, workers: int = 1, model_client=None
    ) -> HarnessRuntime:
        return HarnessRuntime(
            RunStore(root / "data" / "runtime.db"),
            worker_count=workers,
            max_queued_runs=10,
            poll_interval_seconds=0.01,
            model_client=model_client,
        )

    def test_run_persists_steps_events_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary))
            runtime.register_task(
                "echo",
                lambda context: {
                    "value": context.input["value"],
                    "checkpoint": context.checkpoint,
                },
            )
            try:
                submitted, created = runtime.submit("echo", {"value": "ok"})
                completed = runtime.wait(submitted["id"], timeout_seconds=3)
            finally:
                runtime.stop()

            self.assertTrue(created)
            self.assertIsNotNone(completed)
            self.assertEqual(completed["status"], RunStatus.SUCCEEDED.value)
            self.assertEqual(completed["result"]["value"], "ok")
            self.assertEqual(runtime.store.latest_checkpoint(submitted["id"])["state"]["phase"], "completed")
            event_types = [event["event_type"] for event in runtime.store.list_events(submitted["id"])]
            self.assertIn("run.queued", event_types)
            self.assertIn("run.started", event_types)
            self.assertIn("step.succeeded", event_types)
            self.assertIn("run.succeeded", event_types)

    def test_same_idempotency_key_returns_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary))
            runtime.register_task("echo", lambda context: {"value": context.input["value"]})
            try:
                first, first_created = runtime.submit(
                    "echo", {"value": "first"}, idempotency_key="client-request-1"
                )
                second, second_created = runtime.submit(
                    "echo", {"value": "second"}, idempotency_key="client-request-1"
                )
                runtime.wait(first["id"], timeout_seconds=3)
            finally:
                runtime.stop()

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first["id"], second["id"])

    def test_queued_run_can_be_cancelled_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary))
            started = threading.Event()
            release = threading.Event()

            def blocking_handler(context):
                started.set()
                while not release.wait(0.01):
                    context.check_cancelled()
                return {"released": True}

            runtime.register_task("blocking", blocking_handler)
            try:
                active, _ = runtime.submit("blocking", {})
                self.assertTrue(started.wait(timeout=2))
                queued, _ = runtime.submit("blocking", {})
                cancelled = runtime.cancel(queued["id"])
                release.set()
                runtime.wait(active["id"], timeout_seconds=3)
            finally:
                release.set()
                runtime.stop()

            self.assertEqual(cancelled["status"], RunStatus.CANCELLED.value)
            self.assertEqual(runtime.store.get_run(queued["id"])["status"], RunStatus.CANCELLED.value)

    def test_failed_run_can_resume_with_incremented_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary))
            calls: list[int] = []

            def flaky_handler(_context):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("first attempt fails")
                return {"attempt": len(calls)}

            runtime.register_task("flaky", flaky_handler)
            try:
                submitted, _ = runtime.submit("flaky", {})
                failed = runtime.wait(submitted["id"], timeout_seconds=3)
                resumed = runtime.resume(submitted["id"])
                completed = runtime.wait(submitted["id"], timeout_seconds=3)
            finally:
                runtime.stop()

            self.assertEqual(failed["status"], RunStatus.FAILED.value)
            self.assertEqual(resumed["status"], RunStatus.QUEUED.value)
            self.assertEqual(completed["status"], RunStatus.SUCCEEDED.value)
            self.assertEqual(completed["attempt"], 2)
            self.assertEqual(completed["result"]["attempt"], 2)

    def test_interrupted_run_is_recovered_on_runtime_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "data" / "runtime.db"
            store = RunStore(database_path)
            store.initialize()
            created, _ = store.create_run(
                task_type="echo",
                payload={},
                budget={},
                idempotency_key=None,
            )
            self.assertEqual(store.claim_next()["status"], RunStatus.RUNNING.value)
            store.start_step(
                created["id"],
                step_index=1,
                kind="task",
                name="unfinished",
                payload={},
            )

            runtime = HarnessRuntime(store, worker_count=1)
            try:
                recovered = runtime.wait(created["id"], timeout_seconds=0.2)
            finally:
                runtime.stop()

            self.assertEqual(recovered["status"], RunStatus.INTERRUPTED.value)
            self.assertEqual(runtime.store.list_steps(created["id"])[0]["status"], "INTERRUPTED")

    def test_cancel_request_wins_over_late_success_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore(Path(temporary) / "data" / "runtime.db")
            store.initialize()
            submitted, _ = store.create_run(
                task_type="echo", payload={}, budget={}, idempotency_key=None
            )
            self.assertEqual(store.claim_next()["status"], RunStatus.RUNNING.value)
            self.assertEqual(
                store.request_cancel(submitted["id"])["status"],
                RunStatus.CANCEL_REQUESTED.value,
            )

            completed = store.finish_run(
                submitted["id"], status=RunStatus.SUCCEEDED.value, result={"unsafe": False}
            )

            self.assertEqual(completed["status"], RunStatus.CANCELLED.value)
            self.assertIsNone(completed["result"])
            self.assertEqual(completed["error_code"], "RUN_CANCELLED")
            event_types = [event["event_type"] for event in store.list_events(submitted["id"])]
            self.assertIn("run.cancelled", event_types)
            self.assertNotIn("run.succeeded", event_types)

    def test_context_accounts_for_model_and_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary), model_client=_FakeModelClient())
            runtime.tools.register(
                ToolSpec(
                    name="lookup",
                    description="Look up a value.",
                    input_schema={
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                    },
                    handler=lambda arguments, _context: {"value": arguments["key"]},
                )
            )

            def handler(context):
                model_payload, usage = context.chat_json("system", "user")
                tool_result = context.call_tool("lookup", {"key": "safe"})
                return {
                    "model": model_payload,
                    "tokens": usage["total_tokens"],
                    "tool": tool_result.to_dict(),
                }

            runtime.register_task("accounted", handler)
            try:
                submitted, _ = runtime.submit(
                    "accounted", {}, budget={"max_total_tokens": 10, "max_tool_calls": 1}
                )
                completed = runtime.wait(submitted["id"], timeout_seconds=3)
            finally:
                runtime.stop()

            self.assertEqual(completed["status"], RunStatus.SUCCEEDED.value)
            self.assertEqual(completed["result"]["model"], {"answer": "model-ok"})
            self.assertTrue(completed["result"]["tool"]["ok"])
            event_types = [event["event_type"] for event in runtime.store.list_events(submitted["id"])]
            self.assertIn("tool.completed", event_types)

    def test_non_read_only_tool_waits_for_persistent_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary))
            executions: list[str] = []
            runtime.tools.register(
                ToolSpec(
                    name="controlled_write",
                    description="A controlled local mutation.",
                    input_schema={"type": "object", "properties": {}},
                    risk_level=RiskLevel.LOCAL_WRITE,
                    handler=lambda _arguments, _context: executions.append("executed")
                    or {"written": True},
                )
            )

            def handler(context):
                result = context.call_tool("controlled_write", {})
                return {"tool": result.to_dict()}

            runtime.register_task("approval-gated", handler)
            try:
                submitted, _ = runtime.submit("approval-gated", {})
                waiting = runtime.wait(submitted["id"], timeout_seconds=3)
                approvals = runtime.store.list_tool_approvals(submitted["id"])
                approved = runtime.decide_tool_approval(
                    submitted["id"],
                    "controlled_write",
                    decision="APPROVED",
                    actor="reviewer",
                    comment="safe to continue",
                )
                completed = runtime.wait(submitted["id"], timeout_seconds=3)
            finally:
                runtime.stop()

            self.assertEqual(waiting["status"], RunStatus.WAITING_APPROVAL.value)
            self.assertEqual(executions, ["executed"])
            self.assertEqual(approvals[0]["decision"], "REQUESTED")
            self.assertEqual(approved["status"], RunStatus.QUEUED.value)
            self.assertEqual(completed["status"], RunStatus.SUCCEEDED.value)
            self.assertEqual(completed["attempt"], 2)
            self.assertEqual(completed["result"]["tool"]["output"], {"written": True})


class ToolRegistryTests(unittest.TestCase):
    def test_validates_inputs_and_requires_approval_for_write_tools(self) -> None:
        tools = ToolRegistry()
        tools.register(
            ToolSpec(
                name="read_value",
                description="Read a value.",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
                handler=lambda arguments, _context: {"name": arguments["name"]},
            )
        )
        tools.register(
            ToolSpec(
                name="write_value",
                description="Write a value.",
                input_schema={"type": "object", "properties": {}},
                risk_level=RiskLevel.LOCAL_WRITE,
                handler=lambda _arguments, _context: {"written": True},
            )
        )

        context = _ToolContext()
        success = tools.execute("read_value", {"name": "safe"}, context)
        malformed = tools.execute("read_value", {"unexpected": "x"}, context)
        needs_approval = tools.execute("write_value", {}, context)
        approved = tools.execute("write_value", {}, context, approved=True)

        self.assertTrue(success.ok)
        self.assertEqual(success.output, {"name": "safe"})
        self.assertFalse(malformed.ok)
        self.assertEqual(malformed.error_code, "TOOL_VALIDATION_ERROR")
        self.assertFalse(needs_approval.ok)
        self.assertEqual(needs_approval.error_code, "APPROVAL_REQUIRED")
        self.assertTrue(approved.ok)


class RuntimeCliTests(unittest.TestCase):
    def test_submit_and_show_runtime_task_without_api_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY_HERE\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "--env",
                        str(env_file),
                        "run-submit",
                        "--task-type",
                        "knowledge.regrade",
                        "--input-json",
                        "{}",
                        "--wait-seconds",
                        "3",
                    ]
                )
            submitted = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(submitted["run"]["status"], RunStatus.SUCCEEDED.value)
            self.assertEqual(submitted["run"]["result"]["processed"], 0)

            output = StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    ["--env", str(env_file), "run-show", "--id", submitted["run"]["id"], "--events"]
                )
            shown = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(shown["status"], RunStatus.SUCCEEDED.value)
            self.assertGreaterEqual(len(shown["steps"]), 2)
            self.assertIn("run.succeeded", [event["event_type"] for event in shown["events"]])


if __name__ == "__main__":
    unittest.main()
