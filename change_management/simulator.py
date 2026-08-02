from __future__ import annotations

from contextlib import contextmanager
import hashlib
import ipaddress
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from knowledge_platform.store import utc_now

from .cases import ChangeCase, get_change_case
from .schema import ChangeTicket, ExecutionRecord, PlanStep


class SimulationError(RuntimeError):
    """Raised when a synthetic cloud mutation is unsafe or inconsistent."""


class CloudNetworkSimulator:
    """Persistent, deterministic VPC route-table simulator.

    The simulator deliberately models only the operations needed by this demo.
    It never loads cloud credentials and has no network client dependency.
    """

    ENVIRONMENT_ID = "demo-production-network"

    def __init__(self, database_path: Path, *, change_case: ChangeCase | None = None):
        self.database_path = database_path
        self.change_case = change_case or get_change_case()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _state_hash(cls, state: dict[str, Any]) -> str:
        return hashlib.sha256(cls._json(state).encode("utf-8")).hexdigest()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS simulator_environment (
            id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS simulator_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            status TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticket_id, step_id)
        );
        """
        with self.connect() as connection:
            connection.executescript(schema)

    def seed(self) -> dict[str, Any]:
        case = self.change_case
        local_route = {
            "destination": case.vpc_cidr,
            "type": "local",
            "next_hop": "local",
        }
        state = {
            "synthetic": True,
            "case_id": case.case_id,
            "ticket_id": case.ticket_id,
            "region": case.region,
            "environment": "production-demo",
            "vpc": {"id": case.vpc_id, "cidr": case.vpc_cidr},
            "route_tables": {
                table["id"]: {
                    "availability_zone": table["az"],
                    "subnets": [table["subnet"]],
                    "routes": [
                        dict(local_route),
                        {
                            "destination": case.destination,
                            "type": case.route_type,
                            "next_hop": case.from_next_hop,
                        },
                    ],
                }
                for table in case.route_tables
            },
            "next_hops": {
                case.from_next_hop: {
                    "type": case.from_next_hop_type,
                    "status": case.from_status,
                    "capacity_utilization_percent": case.from_capacity_percent,
                    "advertised_prefixes": [case.destination],
                },
                case.to_next_hop: {
                    "type": case.to_next_hop_type,
                    "status": case.to_status,
                    "capacity_utilization_percent": case.to_capacity_percent,
                    "advertised_prefixes": [case.destination],
                },
            },
            "services": {
                service: {"ports": list(case.service_ports), "criticality": "P1"}
                for service in case.affected_services
            },
        }
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM simulator_environment WHERE id = ?", (self.ENVIRONMENT_ID,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO simulator_environment (id, version, state_json, updated_at)
                    VALUES (?, 1, ?, ?)
                    """,
                    (self.ENVIRONMENT_ID, self._json(state), utc_now()),
                )
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT version, state_json, updated_at FROM simulator_environment WHERE id = ?",
                (self.ENVIRONMENT_ID,),
            ).fetchone()
        if row is None:
            raise SimulationError("模拟云网络尚未初始化")
        state = json.loads(str(row["state_json"]))
        return {
            "version": int(row["version"]),
            "state_hash": self._state_hash(state),
            "state": state,
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def effective_next_hop(
        state: dict[str, Any], route_table_id: str, destination_ip: str
    ) -> str | None:
        table = state.get("route_tables", {}).get(route_table_id)
        if not isinstance(table, dict):
            return None
        address = ipaddress.ip_address(destination_ip)
        candidates: list[tuple[int, str]] = []
        for route in table.get("routes", []):
            network = ipaddress.ip_network(str(route["destination"]), strict=False)
            if address in network:
                candidates.append((network.prefixlen, str(route["next_hop"])))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def operation_rows(self, ticket_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM simulator_operations WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["before"] = json.loads(str(item.pop("before_json")))
            item["after"] = json.loads(str(item.pop("after_json")))
            result.append(item)
        return result

    def execute_plan(
        self,
        ticket: ChangeTicket,
        *,
        run_id: str,
        inject_failure: str = "",
    ) -> ExecutionRecord:
        started_at = utc_now()
        if ticket.plan_hash != ticket.compute_plan_hash():
            raise SimulationError("变更单内容与 plan_hash 不一致")

        initial_snapshot = self.snapshot()
        existing = self.operation_rows(ticket.ticket_id)
        if existing and any(item["status"] == "ROLLED_BACK" for item in existing):
            if not self._state_matches_operation_journal(initial_snapshot["state"], existing):
                raise SimulationError("恢复回退时环境与操作日志不一致")
            remaining = [
                str(item["step_id"]) for item in existing if item["status"] == "APPLIED"
            ]
            newly_rolled_back = self._rollback(ticket.ticket_id, remaining)
            final_snapshot = self.snapshot()
            all_rollback_steps = [
                str(item["step_id"])
                for item in reversed(self.operation_rows(ticket.ticket_id))
            ]
            return ExecutionRecord(
                ticket_id=ticket.ticket_id,
                run_id=run_id,
                outcome="ROLLED_BACK",
                started_at=started_at,
                finished_at=utc_now(),
                before_state_hash=str(existing[0]["before"].get("state_hash") or ""),
                after_state_hash=str(final_snapshot["state_hash"]),
                applied_steps=[str(item["step_id"]) for item in existing],
                skipped_steps=[str(item["step_id"]) for item in existing],
                rollback_steps=all_rollback_steps,
                validations=[],
                detail={
                    "synthetic": True,
                    "resumed_from_rollback_journal": True,
                    "newly_rolled_back": newly_rolled_back,
                    "rollback_state_matches_before": (
                        final_snapshot["state_hash"]
                        == str(existing[0]["before"].get("state_hash") or "")
                    ),
                },
            )
        if not existing:
            if initial_snapshot["version"] != ticket.environment_snapshot_version:
                raise SimulationError("环境版本已漂移，必须重新生成变更单")
            if initial_snapshot["state_hash"] != ticket.environment_snapshot_hash:
                raise SimulationError("环境状态已漂移，必须重新生成变更单")
        else:
            expected_hash = str(existing[-1]["after"].get("state_hash") or "")
            if initial_snapshot["state_hash"] != expected_hash:
                raise SimulationError("恢复执行时环境与最近操作日志不一致")

        before_hash = (
            str(existing[0]["before"].get("state_hash"))
            if existing
            else str(initial_snapshot["state_hash"])
        )
        applied = [item["step_id"] for item in existing if item["status"] == "APPLIED"]
        skipped: list[str] = []
        validations: list[dict[str, Any]] = []

        for step in ticket.plan_steps:
            if step.step_id in applied:
                skipped.append(step.step_id)
                metrics = self._verification_metrics(step, inject_failure=inject_failure)
                passed = self._metrics_pass(step, metrics)
                validations.append(
                    {
                        "phase": step.phase,
                        "step_id": step.step_id,
                        "status": "PASS" if passed else "FAIL",
                        "effective_next_hop": metrics["effective_next_hop"],
                        "metrics": metrics,
                        "revalidated_after_resume": True,
                    }
                )
                if not passed:
                    rollback_steps = self._rollback(ticket.ticket_id, applied)
                    final_snapshot = self.snapshot()
                    return ExecutionRecord(
                        ticket_id=ticket.ticket_id,
                        run_id=run_id,
                        outcome="ROLLED_BACK",
                        started_at=started_at,
                        finished_at=utc_now(),
                        before_state_hash=before_hash,
                        after_state_hash=str(final_snapshot["state_hash"]),
                        applied_steps=applied,
                        skipped_steps=skipped,
                        rollback_steps=rollback_steps,
                        validations=validations,
                        detail={
                            "synthetic": True,
                            "failure_injected_at": inject_failure,
                            "rollback_state_matches_before": (
                                final_snapshot["state_hash"] == before_hash
                            ),
                        },
                    )
                continue
            self._apply_step(ticket.ticket_id, run_id, step)
            applied.append(step.step_id)
            metrics = self._verification_metrics(step, inject_failure=inject_failure)
            passed = self._metrics_pass(step, metrics)
            validations.append(
                {
                    "phase": step.phase,
                    "step_id": step.step_id,
                    "status": "PASS" if passed else "FAIL",
                    "effective_next_hop": metrics["effective_next_hop"],
                    "metrics": metrics,
                }
            )
            if not passed:
                rollback_steps = self._rollback(ticket.ticket_id, applied)
                final_snapshot = self.snapshot()
                return ExecutionRecord(
                    ticket_id=ticket.ticket_id,
                    run_id=run_id,
                    outcome="ROLLED_BACK",
                    started_at=started_at,
                    finished_at=utc_now(),
                    before_state_hash=before_hash,
                    after_state_hash=str(final_snapshot["state_hash"]),
                    applied_steps=applied,
                    skipped_steps=skipped,
                    rollback_steps=rollback_steps,
                    validations=validations,
                    detail={
                        "synthetic": True,
                        "failure_injected_at": inject_failure,
                        "rollback_state_matches_before": final_snapshot["state_hash"] == before_hash,
                    },
                )

        final_snapshot = self.snapshot()
        return ExecutionRecord(
            ticket_id=ticket.ticket_id,
            run_id=run_id,
            outcome="SUCCEEDED",
            started_at=started_at,
            finished_at=utc_now(),
            before_state_hash=before_hash,
            after_state_hash=str(final_snapshot["state_hash"]),
            applied_steps=applied,
            skipped_steps=skipped,
            rollback_steps=[],
            validations=validations,
            detail={"synthetic": True, "environment_version": final_snapshot["version"]},
        )

    def _apply_step(self, ticket_id: str, run_id: str, step: PlanStep) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, state_json FROM simulator_environment WHERE id = ?",
                (self.ENVIRONMENT_ID,),
            ).fetchone()
            if row is None:
                raise SimulationError("模拟云网络不存在")
            state = json.loads(str(row["state_json"]))
            table = state.get("route_tables", {}).get(step.route_table_id)
            if not isinstance(table, dict):
                raise SimulationError(f"路由表不存在: {step.route_table_id}")
            matching = [
                route
                for route in table.get("routes", [])
                if str(route.get("destination")) == step.destination
            ]
            if len(matching) != 1:
                raise SimulationError(
                    f"路由 {step.route_table_id}/{step.destination} 必须且只能存在一条"
                )
            route = matching[0]
            if str(route.get("next_hop")) != step.from_next_hop:
                raise SimulationError(
                    f"当前下一跳不是计划基线: {step.route_table_id} "
                    f"expected={step.from_next_hop} actual={route.get('next_hop')}"
                )
            before = {
                "version": int(row["version"]),
                "state_hash": self._state_hash(state),
                "route_table_id": step.route_table_id,
                "route": dict(route),
            }
            route["next_hop"] = step.to_next_hop
            new_version = int(row["version"]) + 1
            after = {
                "version": new_version,
                "state_hash": self._state_hash(state),
                "route_table_id": step.route_table_id,
                "route": dict(route),
            }
            now = utc_now()
            connection.execute(
                """
                UPDATE simulator_environment SET version = ?, state_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_version, self._json(state), now, self.ENVIRONMENT_ID),
            )
            connection.execute(
                """
                INSERT INTO simulator_operations
                    (ticket_id, run_id, step_id, status, before_json, after_json,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'APPLIED', ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    run_id,
                    step.step_id,
                    self._json(before),
                    self._json(after),
                    now,
                    now,
                ),
            )

    def _verification_metrics(
        self, step: PlanStep, *, inject_failure: str
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        destination_ip = str(ipaddress.ip_network(step.destination, strict=False).network_address + 10)
        effective = self.effective_next_hop(
            snapshot["state"], step.route_table_id, destination_ip
        )
        if inject_failure == step.step_id:
            return {
                "effective_next_hop": effective,
                "tcp_443_success_rate": 98.1,
                "tcp_5432_success_rate": 97.8,
                "packet_loss_percent": 2.6,
                "p95_latency_ms": 46.0,
                "samples": 2,
            }
        return {
            "effective_next_hop": effective,
            "tcp_443_success_rate": 99.9,
            "tcp_5432_success_rate": 99.8,
            "packet_loss_percent": 0.2,
            "p95_latency_ms": 18.0,
            "samples": 2,
        }

    @staticmethod
    def _metrics_pass(step: PlanStep, metrics: dict[str, Any]) -> bool:
        thresholds = step.validation_thresholds
        return (
            metrics["effective_next_hop"] == step.to_next_hop
            and float(metrics["tcp_443_success_rate"])
            >= float(thresholds["min_tcp_success_rate"])
            and float(metrics["tcp_5432_success_rate"])
            >= float(thresholds["min_tcp_success_rate"])
            and float(metrics["packet_loss_percent"])
            <= float(thresholds["max_packet_loss_percent"])
            and float(metrics["p95_latency_ms"])
            <= float(thresholds["max_p95_latency_ms"])
        )

    def _rollback(self, ticket_id: str, applied_steps: list[str]) -> list[str]:
        rolled_back: list[str] = []
        for step_id in reversed(applied_steps):
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                operation = connection.execute(
                    """
                    SELECT * FROM simulator_operations
                    WHERE ticket_id = ? AND step_id = ?
                    """,
                    (ticket_id, step_id),
                ).fetchone()
                if operation is None or str(operation["status"]) == "ROLLED_BACK":
                    continue
                before = json.loads(str(operation["before_json"]))
                after = json.loads(str(operation["after_json"]))
                environment = connection.execute(
                    "SELECT version, state_json FROM simulator_environment WHERE id = ?",
                    (self.ENVIRONMENT_ID,),
                ).fetchone()
                if environment is None:
                    raise SimulationError("回退时模拟云网络不存在")
                state = json.loads(str(environment["state_json"]))
                route_after = dict(after["route"])
                route_before = dict(before["route"])
                table_id = str(after["route_table_id"])
                route = self._find_route(state, table_id, str(route_after["destination"]))
                if str(route.get("next_hop")) != str(route_after["next_hop"]):
                    raise SimulationError(f"回退前路由状态漂移: {step_id}")
                route.clear()
                route.update(route_before)
                new_version = int(environment["version"]) + 1
                now = utc_now()
                connection.execute(
                    """
                    UPDATE simulator_environment SET version = ?, state_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_version, self._json(state), now, self.ENVIRONMENT_ID),
                )
                connection.execute(
                    """
                    UPDATE simulator_operations SET status = 'ROLLED_BACK', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, int(operation["id"])),
                )
                rolled_back.append(step_id)
        return rolled_back

    def _state_matches_operation_journal(
        self, state: dict[str, Any], rows: list[dict[str, Any]]
    ) -> bool:
        for row in rows:
            before = dict(row["before"])
            after = dict(row["after"])
            route_table_id = str(after["route_table_id"])
            destination = str(after["route"]["destination"])
            try:
                route = self._find_route(state, route_table_id, destination)
            except (KeyError, SimulationError):
                return False
            expected = (
                str(before["route"]["next_hop"])
                if row["status"] == "ROLLED_BACK"
                else str(after["route"]["next_hop"])
            )
            if str(route.get("next_hop")) != expected:
                return False
        return True

    @staticmethod
    def _find_route(
        state: dict[str, Any], route_table_id: str, destination: str
    ) -> dict[str, Any]:
        table = state["route_tables"][route_table_id]
        for route in table["routes"]:
            if str(route.get("destination")) == destination:
                return route
        raise SimulationError(f"无法定位路由: {route_table_id}/{destination}")

    def force_route_next_hop(
        self, route_table_id: str, destination: str, next_hop: str
    ) -> dict[str, Any]:
        """Test helper used to model out-of-band environment drift."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, state_json FROM simulator_environment WHERE id = ?",
                (self.ENVIRONMENT_ID,),
            ).fetchone()
            if row is None:
                raise SimulationError("模拟云网络不存在")
            state = json.loads(str(row["state_json"]))
            route = self._find_route(state, route_table_id, destination)
            route["next_hop"] = next_hop
            connection.execute(
                """
                UPDATE simulator_environment SET version = ?, state_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(row["version"]) + 1, self._json(state), utc_now(), self.ENVIRONMENT_ID),
            )
        return self.snapshot()

    def force_next_hop_properties(
        self, next_hop: str, **properties: Any
    ) -> dict[str, Any]:
        """Test helper used to model an unhealthy standby link."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, state_json FROM simulator_environment WHERE id = ?",
                (self.ENVIRONMENT_ID,),
            ).fetchone()
            if row is None:
                raise SimulationError("模拟云网络不存在")
            state = json.loads(str(row["state_json"]))
            target = state.get("next_hops", {}).get(next_hop)
            if not isinstance(target, dict):
                raise SimulationError(f"下一跳不存在: {next_hop}")
            target.update(properties)
            connection.execute(
                """
                UPDATE simulator_environment SET version = ?, state_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(row["version"]) + 1, self._json(state), utc_now(), self.ENVIRONMENT_ID),
            )
        return self.snapshot()
