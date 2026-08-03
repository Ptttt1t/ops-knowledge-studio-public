from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from change_management.cases import (
    CHANGE_CASES,
    seed_case_catalog_knowledge,
)
from change_management.runtime_tasks import create_change_runtime
from change_management.schema import ChangeStatus, ChangeTicket
from change_management.service import DemoChangeService
from harness.run_store import RunStore
from harness.tools import approval_digest
from knowledge_platform.schema import CardStatus
from knowledge_platform.cli import main as cli_main
from knowledge_platform.store import KnowledgeStore


class _FailingModel:
    def chat_json(self, *_args, **_kwargs):
        raise RuntimeError("offline provider")


class ChangeDemoTests(unittest.TestCase):
    def make_service(self, root: Path, *, model_client=None) -> DemoChangeService:
        return DemoChangeService(root / "demo", model_client=model_client)

    def generate_with_runtime(self, service: DemoChangeService):
        runtime = create_change_runtime(service)
        submitted, _ = runtime.submit(
            "change.generate_demo", {"requested_by": "tester", "use_model": False}
        )
        completed = runtime.wait(submitted["id"], timeout_seconds=5)
        self.assertIsNotNone(completed)
        self.assertEqual(completed["status"], "SUCCEEDED")
        return runtime, completed["result"]

    def submit_execution(
        self,
        runtime,
        service: DemoChangeService,
        *,
        inject_failure: str = "",
    ):
        submitted, _ = runtime.submit(
            "change.execute_demo",
            {
                "ticket_id": service.TICKET_ID,
                "actor": "tester",
                "inject_failure": inject_failure,
            },
        )
        waiting = runtime.wait(submitted["id"], timeout_seconds=5)
        self.assertEqual(waiting["status"], "WAITING_APPROVAL")
        return submitted

    def approve_and_wait(self, runtime, service: DemoChangeService, run_id: str):
        runtime.decide_tool_approval(
            run_id,
            service.TOOL_NAME,
            decision="APPROVED",
            actor="tester",
            comment="approved test fixture",
        )
        completed = runtime.wait(run_id, timeout_seconds=5)
        self.assertIsNotNone(completed)
        return completed

    def test_case_catalog_is_approved_idempotent_and_drives_each_plan(self) -> None:
        self.assertGreaterEqual(len(CHANGE_CASES), 5)
        self.assertEqual(len({item.case_id for item in CHANGE_CASES}), len(CHANGE_CASES))
        self.assertEqual(len({item.ticket_id for item in CHANGE_CASES}), len(CHANGE_CASES))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = KnowledgeStore(root / "main.db")
            store.initialize()
            first = seed_case_catalog_knowledge(store)
            second = seed_case_catalog_knowledge(store)
            self.assertEqual(first, second)
            self.assertEqual(store.stats()["cards"], len(CHANGE_CASES))
            self.assertTrue(
                all(item["knowledge_status"] == CardStatus.APPROVED.value for item in first)
            )

            for case in CHANGE_CASES:
                service = DemoChangeService(root / case.case_id, case_id=case.case_id)
                package = service.generate_ticket(requested_by="catalog-tester")
                ticket = package["ticket"]
                self.assertEqual(ticket["ticket_id"], case.ticket_id)
                self.assertEqual(ticket["vpc_id"], case.vpc_id)
                self.assertEqual(ticket["plan_steps"][0]["route_table_id"], case.route_tables[0]["id"])
                self.assertEqual(ticket["plan_steps"][0]["to_next_hop"], case.to_next_hop)
                self.assertEqual(ticket["status"], ChangeStatus.READY_FOR_APPROVAL.value)
                self.assertTrue(
                    all(ref["status"] == CardStatus.APPROVED.value for ref in ticket["knowledge_references"])
                )

    def test_non_default_nat_case_executes_to_selected_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = DemoChangeService(
                Path(temporary) / "nat-demo", case_id="nat-egress-bluegreen"
            )
            runtime, package = self.generate_with_runtime(service)
            try:
                self.assertEqual(package["ticket"]["ticket_id"], "CHG-DEMO-NAT-002")
                execution = self.submit_execution(runtime, service)
                completed = self.approve_and_wait(runtime, service, execution["id"])
                self.assertEqual(completed["status"], "SUCCEEDED")
                snapshot = service.simulator.snapshot()["state"]
                for table in service.case.route_tables:
                    route = next(
                        item
                        for item in snapshot["route_tables"][table["id"]]["routes"]
                        if item["destination"] == service.case.destination
                    )
                    self.assertEqual(route["next_hop"], "nat-green")
            finally:
                runtime.stop()

    def test_offline_happy_path_waits_for_approval_then_closes_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            runtime, package = self.generate_with_runtime(service)
            try:
                self.assertEqual(
                    package["ticket"]["status"], ChangeStatus.READY_FOR_APPROVAL.value
                )
                self.assertTrue(package["validations"])
                self.assertTrue(all(item["status"] == "PASS" for item in package["validations"]))
                self.assertTrue(
                    all(
                        item["status"] == CardStatus.APPROVED.value
                        for item in package["ticket"]["knowledge_references"]
                    )
                )
                before = service.simulator.snapshot()
                execution = self.submit_execution(runtime, service)
                self.assertEqual(service.simulator.snapshot()["state_hash"], before["state_hash"])
                approval = runtime.store.list_tool_approvals(execution["id"])[0]
                self.assertEqual(len(approval["request_digest"]), 64)

                completed = self.approve_and_wait(runtime, service, execution["id"])
                self.assertEqual(completed["status"], "SUCCEEDED")
                final = service.ticket_package(service.TICKET_ID)
                self.assertEqual(final["ticket"]["status"], ChangeStatus.SUCCEEDED.value)
                self.assertEqual(final["execution"]["applied_steps"], [
                    "route-switch-az-a",
                    "route-switch-az-b",
                ])
                candidate = service.knowledge_store.get_card(
                    final["feedback"]["knowledge_candidate_id"]
                )
                self.assertEqual(candidate["status"], CardStatus.PENDING_REVIEW.value)
                for name in (
                    "change_order.md",
                    "change_package.json",
                    "validation_report.json",
                    "execution_report.json",
                    "feedback.md",
                ):
                    self.assertTrue((service.workspace / name).is_file(), name)
            finally:
                runtime.stop()

    def test_rejection_preserves_simulated_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            runtime, _package = self.generate_with_runtime(service)
            try:
                before = service.simulator.snapshot()
                execution = self.submit_execution(runtime, service)
                rejected_run = runtime.decide_tool_approval(
                    execution["id"],
                    service.TOOL_NAME,
                    decision="REJECTED",
                    actor="tester",
                    comment="reject test",
                )
                service.reject_ticket(service.TICKET_ID, actor="tester", comment="reject test")
                self.assertEqual(rejected_run["status"], "FAILED")
                self.assertEqual(
                    service.change_store.require_ticket(service.TICKET_ID)["status"],
                    ChangeStatus.REJECTED.value,
                )
                self.assertEqual(service.simulator.snapshot()["state_hash"], before["state_hash"])
                self.assertEqual(service.simulator.operation_rows(service.TICKET_ID), [])
            finally:
                runtime.stop()

    def test_unhealthy_standby_blocks_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            service.simulator.seed()
            service.simulator.force_next_hop_properties("dc-standby", status="DOWN")
            package = service.generate_ticket(requested_by="tester")
            self.assertEqual(package["ticket"]["status"], ChangeStatus.BLOCKED.value)
            failed = [item["validator"] for item in package["validations"] if item["status"] == "FAIL"]
            self.assertIn("standby_link_health", failed)
            self.assertFalse((service.workspace / "runtime.db").exists())

    def test_environment_drift_after_approval_request_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            runtime, _package = self.generate_with_runtime(service)
            try:
                execution = self.submit_execution(runtime, service)
                service.simulator.force_route_next_hop(
                    "rtb-prod-app-a", service.DESTINATION, "dc-out-of-band"
                )
                completed = self.approve_and_wait(runtime, service, execution["id"])
                self.assertEqual(completed["status"], "FAILED")
                self.assertEqual(
                    service.change_store.require_ticket(service.TICKET_ID)["status"],
                    ChangeStatus.BLOCKED.value,
                )
                self.assertEqual(service.simulator.operation_rows(service.TICKET_ID), [])
            finally:
                runtime.stop()

    def test_approval_digest_cannot_authorize_changed_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = RunStore(Path(temporary) / "runtime.db")
            store.initialize()
            run, _ = store.create_run(
                task_type="digest-test", payload={}, budget={}, idempotency_key=None
            )
            store.claim_next()
            digest_a = approval_digest("write", {"value": "A"})
            digest_b = approval_digest("write", {"value": "B"})
            store.request_tool_approval(
                run["id"], "write", reason="test", request_digest=digest_a
            )
            store.decide_tool_approval(
                run["id"],
                "write",
                decision="APPROVED",
                actor="tester",
                comment="digest A only",
            )
            self.assertTrue(
                store.is_tool_approved(run["id"], "write", request_digest=digest_a)
            )
            self.assertFalse(
                store.is_tool_approved(run["id"], "write", request_digest=digest_b)
            )

    def test_injected_failure_rolls_back_and_run_still_closes_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            runtime, _package = self.generate_with_runtime(service)
            try:
                before = service.simulator.snapshot()
                execution = self.submit_execution(
                    runtime, service, inject_failure="route-switch-az-b"
                )
                completed = self.approve_and_wait(runtime, service, execution["id"])
                self.assertEqual(completed["status"], "SUCCEEDED")
                package = service.ticket_package(service.TICKET_ID)
                self.assertEqual(package["ticket"]["status"], ChangeStatus.ROLLED_BACK.value)
                self.assertEqual(package["execution"]["before_state_hash"], before["state_hash"])
                self.assertEqual(package["execution"]["after_state_hash"], before["state_hash"])
                self.assertEqual(
                    package["execution"]["rollback_steps"],
                    ["route-switch-az-b", "route-switch-az-a"],
                )
                self.assertTrue(
                    all(
                        row["status"] == "ROLLED_BACK"
                        for row in service.simulator.operation_rows(service.TICKET_ID)
                    )
                )
            finally:
                runtime.stop()

    def test_simulator_operation_journal_makes_reexecution_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            package = service.generate_ticket(requested_by="tester")
            ticket = ChangeTicket.from_dict(package["ticket"])
            first = service.simulator.execute_plan(ticket, run_id="run-one")
            second = service.simulator.execute_plan(ticket, run_id="run-two")
            self.assertEqual(first.outcome, "SUCCEEDED")
            self.assertEqual(second.outcome, "SUCCEEDED")
            self.assertEqual(
                second.skipped_steps, ["route-switch-az-a", "route-switch-az-b"]
            )
            self.assertEqual(len(service.simulator.operation_rows(service.TICKET_ID)), 2)

    def test_interrupted_execution_resumes_after_first_az_without_reapplying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            package = service.generate_ticket(requested_by="tester")
            ticket = ChangeTicket.from_dict(package["ticket"])
            runtime = create_change_runtime(service)
            run, _ = runtime.store.create_run(
                task_type="change.execute_demo",
                payload={
                    "ticket_id": service.TICKET_ID,
                    "actor": "tester",
                    "inject_failure": "",
                },
                budget={},
                idempotency_key=None,
            )
            claimed = runtime.store.claim_next()
            self.assertEqual(claimed["status"], "RUNNING")
            service.mark_waiting_approval(service.TICKET_ID, run_id=run["id"])
            arguments = {
                "ticket_id": service.TICKET_ID,
                "revision": ticket.revision,
                "plan_hash": ticket.plan_hash,
                "snapshot_version": ticket.environment_snapshot_version,
                "actor": "tester",
                "inject_failure": "",
            }
            runtime.store.request_tool_approval(
                run["id"],
                service.TOOL_NAME,
                reason="recovery test",
                request_digest=approval_digest(service.TOOL_NAME, arguments),
            )
            runtime.store.decide_tool_approval(
                run["id"],
                service.TOOL_NAME,
                decision="APPROVED",
                actor="tester",
                comment="approved before simulated crash",
            )
            service.change_store.update_status(
                service.TICKET_ID, ChangeStatus.APPROVED, actor="tester"
            )
            service.change_store.update_status(
                service.TICKET_ID, ChangeStatus.EXECUTING, actor="tester"
            )
            service.simulator._apply_step(
                service.TICKET_ID, run["id"], ticket.plan_steps[0]
            )
            runtime.store.claim_next()
            self.assertEqual(runtime.store.recover_interrupted(), 1)
            try:
                resumed = runtime.resume(run["id"])
                self.assertEqual(resumed["status"], "QUEUED")
                completed = runtime.wait(run["id"], timeout_seconds=5)
                self.assertEqual(completed["status"], "SUCCEEDED")
                final = service.ticket_package(service.TICKET_ID)
                self.assertEqual(final["ticket"]["status"], ChangeStatus.SUCCEEDED.value)
                self.assertEqual(
                    final["execution"]["skipped_steps"], ["route-switch-az-a"]
                )
                self.assertEqual(len(service.simulator.operation_rows(service.TICKET_ID)), 2)
            finally:
                runtime.stop()

    def test_runtime_store_migrates_legacy_approval_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "runtime.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE tool_approvals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        comment TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()
            store = RunStore(database)
            store.initialize()
            with store.connect() as migrated:
                columns = {
                    str(row["name"])
                    for row in migrated.execute("PRAGMA table_info(tool_approvals)")
                }
                versions = {
                    int(row["version"])
                    for row in migrated.execute(
                        "SELECT version FROM runtime_schema_migrations"
                    )
                }
            self.assertIn("request_digest", columns)
            self.assertIn(2, versions)

    def test_model_failure_falls_back_without_changing_machine_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary), model_client=_FailingModel())
            package = service.generate_ticket(requested_by="tester", use_model=True)
            ticket = package["ticket"]
            self.assertEqual(ticket["generator_mode"], "deterministic-fallback")
            self.assertEqual(ticket["plan_steps"][0]["to_next_hop"], "dc-standby")
            self.assertIn("模型润色失败", ticket["generation_notes"][-1])

    def test_demo_change_cli_is_one_command_and_does_not_touch_main_knowledge_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text(
                "DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY_HERE\n",
                encoding="utf-8",
            )
            output = StringIO()
            with patch("builtins.input", return_value="APPROVE CHG-DEMO-ROUTE-001"):
                with redirect_stdout(output):
                    exit_code = cli_main(["--env", str(env_file), "demo-change"])
            self.assertEqual(exit_code, 0, output.getvalue())
            self.assertIn("变更结果: SUCCEEDED", output.getvalue())
            self.assertFalse((root / "data" / "knowledge.db").exists())
            workspaces = list((root / "artifacts" / "change_demos").iterdir())
            self.assertEqual(len(workspaces), 1)
            self.assertTrue((workspaces[0] / "runtime_events.json").is_file())


if __name__ == "__main__":
    unittest.main()
