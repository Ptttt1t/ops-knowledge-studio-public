from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from knowledge_platform.retrieval import HybridRetriever
from knowledge_platform.schema import (
    CardStatus,
    ComparisonDecision,
    ComparisonResult,
    KnowledgeCardDraft,
)
from knowledge_platform.store import KnowledgeStore, utc_now

from .cases import (
    DEFAULT_CASE_ID,
    ChangeCase,
    get_change_case,
    seed_case_catalog_knowledge,
)
from .schema import (
    ChangeStatus,
    ChangeTicket,
    FeedbackRecord,
    PlanStep,
    ValidationResult,
)
from .simulator import CloudNetworkSimulator, SimulationError
from .store import ChangeStore


class DemoChangeError(RuntimeError):
    """Raised when the synthetic change workflow cannot safely continue."""


class DemoChangeService:
    TICKET_ID = "CHG-DEMO-ROUTE-001"
    DESTINATION = "172.20.32.0/20"
    TOOL_NAME = "cloud_network.apply_plan"

    def __init__(
        self,
        workspace: Path,
        *,
        model_client: Any | None = None,
        case_id: str = DEFAULT_CASE_ID,
    ):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.model_client = model_client
        self.case: ChangeCase = get_change_case(case_id)
        # Preserve the public attributes used by the runtime and existing clients,
        # while making them session-specific.
        self.TICKET_ID = self.case.ticket_id
        self.DESTINATION = self.case.destination
        self.knowledge_store = KnowledgeStore(self.workspace / "knowledge.db")
        self.knowledge_store.initialize()
        self.change_store = ChangeStore(self.workspace / "changes.db")
        self.change_store.initialize()
        self.simulator = CloudNetworkSimulator(
            self.workspace / "cloud_network.db", change_case=self.case
        )
        self.simulator.initialize()

    @classmethod
    def create_workspace(cls, project_root: Path) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return project_root / "artifacts" / "change_demos" / f"{timestamp}-{uuid4().hex[:8]}"

    def seed_demo_knowledge(self) -> list[int]:
        seeded = seed_case_catalog_knowledge(self.knowledge_store)
        return [int(item["knowledge_card_id"]) for item in seeded]

        # Legacy single-case fixtures are intentionally unreachable. They are
        # retained below only to keep older demo workspaces readable.
        existing = self.knowledge_store.list_cards(status=CardStatus.APPROVED.value, limit=100)
        if existing:
            return [int(card["id"]) for card in existing]

        fixtures = [
            {
                "name": "生产VPC专线路由主备切换SOP（合成）",
                "summary": "生产VPC路由切换必须先保存快照，按单AZ灰度并逐级验证。",
                "evidence": "执行生产VPC专线路由切换时，必须先保存当前路由快照，仅灰度修改一个可用区；验证通过后才能继续第二个可用区。",
                "procedure": [
                    "保存路由表、下一跳状态和有效路由快照",
                    "先修改AZ-A路由并完成连通性验证",
                    "验证通过后修改AZ-B路由",
                ],
                "risks": ["下一跳不可用会导致跨专线业务中断", "同时修改双AZ会扩大故障面"],
                "rollback": ["按AZ-B、AZ-A逆序恢复原下一跳"],
                "validation": ["核对有效下一跳", "检查TCP连通率、丢包和P95时延"],
                "type": "procedure",
            },
            {
                "name": "备用专线启用前检查清单（合成）",
                "summary": "备用专线必须处于UP、已通告目标前缀且容量利用率低于60%。",
                "evidence": "备用专线启用前必须确认链路状态为UP，已通告目标业务前缀，且容量利用率低于60%。",
                "procedure": ["检查链路状态", "核对BGP通告前缀", "检查容量利用率"],
                "risks": ["备用链路容量不足会在切换后形成拥塞"],
                "rollback": ["停止切换并保留原主链路路由"],
                "validation": ["备用链路状态UP", "容量利用率低于60%"],
                "type": "constraint",
            },
            {
                "name": "路由切换失败回退规范（合成）",
                "summary": "验证连续两个周期失败时立即逆序回退并确认状态哈希。",
                "evidence": "若TCP成功率低于99.5%、丢包高于1%或P95时延高于30毫秒并持续两个采样周期，应立即按操作逆序回退。",
                "procedure": ["判断连续两个采样周期", "冻结后续步骤", "逆序回退"],
                "risks": ["延迟回退会延长业务影响"],
                "rollback": ["恢复每张路由表的原始下一跳", "核对回退后状态哈希"],
                "validation": ["TCP成功率不低于99.5%", "丢包不高于1%", "P95不高于30毫秒"],
                "type": "rollback",
            },
            {
                "name": "历史成功案例：双AZ专线切换（合成）",
                "summary": "历史演练采用AZ-A灰度、五分钟观察、AZ-B扩展的顺序完成。",
                "evidence": "历史演练先完成AZ-A灰度切换和观测，再扩展到AZ-B，执行中未跳过任何前置检查。",
                "procedure": ["AZ-A灰度", "观察健康指标", "AZ-B扩展"],
                "risks": ["跳过灰度观察会隐藏单可用区故障"],
                "rollback": ["按执行逆序恢复"],
                "validation": ["核对两个可用区有效下一跳"],
                "type": "case",
            },
        ]
        card_ids: list[int] = []
        for index, fixture in enumerate(fixtures, start=1):
            content = (
                f"# {fixture['name']}\n\n{fixture['summary']}\n\n"
                f"证据：{fixture['evidence']}\n"
            )
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            document_id, _ = self.knowledge_store.add_document(
                str(fixture["name"]),
                "synthetic-demo",
                f"synthetic://change-demo/knowledge/{index}",
                checksum,
                content,
            )
            chunk_id = self.knowledge_store.add_chunk(
                document_id, 0, 0, len(content), content
            )
            draft = KnowledgeCardDraft(
                title=str(fixture["name"]),
                summary=str(fixture["summary"]),
                knowledge_type=str(fixture["type"]),
                scenario="生产VPC跨专线路由主备切换",
                object_type="cloud_network_route",
                object_name="vpc-prod-core / 172.20.32.0/20",
                applicable_versions=["synthetic-v1"],
                prerequisites=["数据为合成演示数据，不连接真实云"],
                procedure_steps=list(fixture["procedure"]),
                risks=list(fixture["risks"]),
                rollback_steps=list(fixture["rollback"]),
                validation_steps=list(fixture["validation"]),
                keywords=["VPC", "Direct Connect", "主备切换", "路由表", "合成数据"],
                evidence_quote=str(fixture["evidence"]),
            )
            card_id = self.knowledge_store.add_card(
                draft,
                document_id=document_id,
                chunk_id=chunk_id,
                evidence_locator=f"synthetic://change-demo/knowledge/{index}#evidence",
                status=CardStatus.PENDING_REVIEW,
                quality_score=98.0,
                quality_issues=[],
                comparison=ComparisonResult(
                    decision=ComparisonDecision.NEW,
                    confidence=1.0,
                    reason="合成演示基线知识",
                ),
            )
            self.knowledge_store.review_card(
                card_id,
                action="APPROVE",
                reviewer="demo-fixture-reviewer",
                comment="合成演示夹具，代表历史已审核知识",
            )
            card_ids.append(card_id)
        return card_ids

    def generate_ticket(
        self, *, requested_by: str = "demo-operator", use_model: bool = False
    ) -> dict[str, Any]:
        existing = self.change_store.get_ticket(self.TICKET_ID)
        if existing is not None:
            return self.ticket_package(self.TICKET_ID)

        snapshot = self.simulator.seed()
        self.seed_demo_knowledge()
        hits = HybridRetriever(self.knowledge_store).search(
            self.case.search_query,
            statuses=[CardStatus.APPROVED],
            top_k=4,
        )
        references = [
            {
                "card_id": int(hit.card["id"]),
                "title": str(hit.card["title"]),
                "status": str(hit.card["status"]),
                "score": round(hit.score, 4),
                "evidence_locator": str(hit.card["evidence_locator"]),
            }
            for hit in hits
        ]

        title = self.case.title
        summary = self.case.summary
        generator_mode = "deterministic-offline"
        notes = ["结构、资源、阈值和回退逻辑由确定性规则生成"]
        if use_model:
            title, summary, generator_mode, model_note = self._model_narrative(
                title, summary, snapshot, references
            )
            notes.append(model_note)

        now = datetime.now(timezone.utc)
        window_start = now + timedelta(minutes=15)
        window_end = window_start + timedelta(minutes=self.case.window_minutes)
        thresholds = {
            "min_tcp_success_rate": 99.5,
            "max_packet_loss_percent": 1.0,
            "max_p95_latency_ms": 30.0,
        }
        steps = [
            PlanStep(
                step_id=self.case.step_id(index),
                phase=self.case.step_phase(index),
                action="mod-route-next-hop",
                route_table_id=table["id"],
                availability_zone=table["az"],
                destination=self.DESTINATION,
                from_next_hop=self.case.from_next_hop,
                to_next_hop=self.case.to_next_hop,
                validation_thresholds=thresholds,
            )
            for index, table in enumerate(self.case.route_tables)
        ]
        ticket = ChangeTicket(
            ticket_id=self.TICKET_ID,
            revision=1,
            status=ChangeStatus.DRAFT,
            synthetic=True,
            change_type="计划生产变更",
            risk_level=self.case.risk_level,
            title=title,
            summary=summary,
            requested_by=requested_by.strip() or "demo-operator",
            region=self.case.region,
            environment="production-demo",
            vpc_id=self.case.vpc_id,
            affected_services=list(self.case.affected_services),
            change_window={
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
                "duration_minutes": str(self.case.window_minutes),
            },
            environment_snapshot_version=int(snapshot["version"]),
            environment_snapshot_hash=str(snapshot["state_hash"]),
            knowledge_references=references,
            plan_steps=steps,
            rollback_triggers=list(self.case.rollback_triggers)
            + [
                "TCP 443/5432 成功率连续两个采样周期低于 99.5%",
                "丢包连续两个采样周期高于 1%",
                "P95 时延连续两个采样周期高于 30 ms",
            ],
            rollback_steps=[
                "冻结尚未执行的路由步骤",
                f"按操作日志逆序恢复已执行的路由步骤至 {self.case.from_next_hop}",
                "重新核对有效下一跳、TCP连通性和回退状态哈希",
            ],
            communication_plan=list(self.case.communication_plan),
            risk_score=self.case.risk_score,
            generator_mode=generator_mode,
            generation_notes=notes,
        ).seal()
        self.change_store.save_ticket(ticket)
        validations = self.validate_ticket(ticket, snapshot=snapshot)
        for validation in validations:
            self.change_store.record_validation(ticket.ticket_id, validation)
        hard_failures = [
            item for item in validations if item.hard_gate and item.status != "PASS"
        ]
        self.change_store.update_status(
            ticket.ticket_id,
            ChangeStatus.BLOCKED if hard_failures else ChangeStatus.READY_FOR_APPROVAL,
            actor="change-generator",
            detail={"hard_failures": [item.validator for item in hard_failures]},
        )
        self.write_generation_reports(ticket.ticket_id)
        return self.ticket_package(ticket.ticket_id)

    def _model_narrative(
        self,
        title: str,
        summary: str,
        snapshot: dict[str, Any],
        references: list[dict[str, Any]],
    ) -> tuple[str, str, str, str]:
        if self.model_client is None:
            return title, summary, "deterministic-fallback", "未配置模型客户端，已回退确定性文案"
        system_prompt = (
            "你只负责润色合成云网络变更单的标题和摘要。返回JSON对象，且只能包含title、summary。"
            "不得添加或修改资源ID、CIDR、下一跳、阈值、执行步骤或回退策略。"
        )
        user_prompt = json.dumps(
            {
                "synthetic": True,
                "title": title,
                "summary": summary,
                "fixed_resources": {
                    "vpc": self.case.vpc_id,
                    "route_tables": [item["id"] for item in self.case.route_tables],
                    "destination": self.DESTINATION,
                    "from": self.case.from_next_hop,
                    "to": self.case.to_next_hop,
                },
                "snapshot_version": snapshot["version"],
                "knowledge_card_ids": [item["card_id"] for item in references],
            },
            ensure_ascii=False,
        )
        try:
            payload, _usage = self.model_client.chat_json(
                system_prompt, user_prompt, thinking="disabled", temperature=0.1
            )
            if not isinstance(payload, dict):
                raise ValueError("模型返回不是JSON对象")
            candidate_title = str(payload.get("title") or "").strip()
            candidate_summary = str(payload.get("summary") or "").strip()
            forbidden = [
                self.case.vpc_id,
                self.case.route_tables[0]["id"],
                self.case.route_tables[1]["id"],
                self.DESTINATION,
                self.case.from_next_hop,
                self.case.to_next_hop,
            ]
            combined = f"{candidate_title} {candidate_summary}"
            if not candidate_title or not candidate_summary:
                raise ValueError("模型标题或摘要为空")
            if any(token not in combined for token in forbidden[:1] + forbidden[3:]):
                raise ValueError("模型文案遗漏关键资源或路由约束")
            return (
                candidate_title[:160],
                candidate_summary[:1000],
                "deepseek-narrative",
                "DeepSeek仅润色标题和摘要；机器动作仍由确定性规则生成",
            )
        except Exception as exc:
            return (
                title,
                summary,
                "deterministic-fallback",
                f"模型润色失败，已回退确定性文案: {exc}",
            )

    def validate_ticket(
        self, ticket: ChangeTicket, *, snapshot: dict[str, Any] | None = None
    ) -> list[ValidationResult]:
        current = snapshot or self.simulator.snapshot()
        state = current["state"]
        results: list[ValidationResult] = []

        def add(
            validator: str,
            passed: bool,
            message: str,
            evidence: dict[str, Any],
            *,
            hard_gate: bool = True,
        ) -> None:
            results.append(
                ValidationResult(
                    validator=validator,
                    status="PASS" if passed else "FAIL",
                    message=message,
                    hard_gate=hard_gate,
                    evidence=evidence,
                )
            )

        add(
            "synthetic_boundary",
            ticket.synthetic and bool(state.get("synthetic")),
            "数据源和执行目标均明确标记为合成演示",
            {"ticket_synthetic": ticket.synthetic, "environment_synthetic": state.get("synthetic")},
        )
        add(
            "vpc_exists",
            state.get("vpc", {}).get("id") == ticket.vpc_id,
            "目标VPC存在且与变更单一致",
            {"expected": ticket.vpc_id, "actual": state.get("vpc", {}).get("id")},
        )
        try:
            network = ipaddress.ip_network(self.DESTINATION, strict=False)
            cidr_valid = str(network) == self.DESTINATION
        except ValueError:
            cidr_valid = False
        add(
            "destination_cidr",
            cidr_valid,
            "目标网段是规范IPv4 CIDR",
            {"destination": self.DESTINATION},
        )
        route_tables = state.get("route_tables", {})
        for step in ticket.plan_steps:
            table = route_tables.get(step.route_table_id)
            add(
                f"resource_exists:{step.route_table_id}",
                isinstance(table, dict),
                "目标路由表存在",
                {"route_table_id": step.route_table_id},
            )
            routes = table.get("routes", []) if isinstance(table, dict) else []
            exact = [route for route in routes if route.get("destination") == step.destination]
            add(
                f"single_exact_route:{step.route_table_id}",
                len(exact) == 1,
                "目标CIDR在路由表中必须且只能存在一条精确路由",
                {"matches": len(exact), "destination": step.destination},
            )
            actual_next_hop = exact[0].get("next_hop") if len(exact) == 1 else None
            add(
                f"baseline_next_hop:{step.route_table_id}",
                actual_next_hop == step.from_next_hop,
                "当前下一跳与变更基线一致",
                {"expected": step.from_next_hop, "actual": actual_next_hop},
            )
        standby = state.get("next_hops", {}).get(self.case.to_next_hop, {})
        add(
            "standby_link_health",
            standby.get("status") == "UP",
            "目标下一跳状态必须为UP",
            {"next_hop": self.case.to_next_hop, "status": standby.get("status")},
        )
        utilization = float(standby.get("capacity_utilization_percent", 101))
        add(
            "standby_capacity",
            utilization < 60.0,
            "目标下一跳容量利用率必须低于60%",
            {"capacity_utilization_percent": utilization, "threshold": 60.0},
        )
        prefixes = [str(item) for item in standby.get("advertised_prefixes", [])]
        add(
            "standby_prefix_advertisement",
            self.DESTINATION in prefixes,
            "目标下一跳必须通告目标业务前缀",
            {"required": self.DESTINATION, "advertised": prefixes},
        )
        statuses = [item.get("status") for item in ticket.knowledge_references]
        add(
            "approved_knowledge_only",
            bool(statuses) and all(status == CardStatus.APPROVED.value for status in statuses),
            "生成仅引用APPROVED知识",
            {"statuses": statuses},
        )
        start = datetime.fromisoformat(ticket.change_window["start"])
        end = datetime.fromisoformat(ticket.change_window["end"])
        add(
            "change_window",
            end > start and end - start == timedelta(minutes=self.case.window_minutes),
            f"变更窗口为有效的{self.case.window_minutes}分钟计划窗口",
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "duration_minutes": self.case.window_minutes,
            },
        )
        add(
            "snapshot_consistency",
            int(current["version"]) == ticket.environment_snapshot_version
            and str(current["state_hash"]) == ticket.environment_snapshot_hash,
            "生成时环境版本和状态哈希一致",
            {
                "expected_version": ticket.environment_snapshot_version,
                "actual_version": current["version"],
                "expected_hash": ticket.environment_snapshot_hash,
                "actual_hash": current["state_hash"],
            },
        )
        return results

    def mark_waiting_approval(self, ticket_id: str, *, run_id: str) -> dict[str, Any]:
        ticket = self.change_store.require_ticket(ticket_id)
        if ticket["status"] in {
            ChangeStatus.WAITING_APPROVAL.value,
            ChangeStatus.APPROVED.value,
            ChangeStatus.EXECUTING.value,
            ChangeStatus.VERIFYING.value,
        }:
            return ticket
        if ticket["status"] != ChangeStatus.READY_FOR_APPROVAL.value:
            raise DemoChangeError(f"变更单当前不可提交审批: {ticket['status']}")
        return self.change_store.update_status(
            ticket_id,
            ChangeStatus.WAITING_APPROVAL,
            actor="harness-runtime",
            detail={"run_id": run_id},
        )

    def reject_ticket(self, ticket_id: str, *, actor: str, comment: str) -> dict[str, Any]:
        return self.change_store.update_status(
            ticket_id,
            ChangeStatus.REJECTED,
            actor=actor,
            detail={"comment": comment},
        )

    def apply_plan(self, arguments: dict[str, Any], *, run_id: str) -> dict[str, Any]:
        ticket_id = str(arguments["ticket_id"])
        payload = self.change_store.require_ticket(ticket_id)
        ticket = ChangeTicket.from_dict(payload)
        if int(arguments["revision"]) != ticket.revision:
            raise DemoChangeError("审批请求中的 revision 与变更单不一致")
        if str(arguments["plan_hash"]) != ticket.plan_hash:
            raise DemoChangeError("审批请求中的 plan_hash 与变更单不一致")
        if int(arguments["snapshot_version"]) != ticket.environment_snapshot_version:
            raise DemoChangeError("审批请求中的环境版本与变更单不一致")
        if ticket.plan_hash != ticket.compute_plan_hash():
            raise DemoChangeError("变更单在审批后发生修改")
        if ticket.status not in {
            ChangeStatus.WAITING_APPROVAL,
            ChangeStatus.APPROVED,
            ChangeStatus.EXECUTING,
            ChangeStatus.VERIFYING,
        }:
            raise DemoChangeError(f"变更单不处于等待审批状态: {ticket.status.value}")

        if ticket.status is ChangeStatus.WAITING_APPROVAL:
            self.change_store.update_status(
                ticket_id,
                ChangeStatus.APPROVED,
                actor=str(arguments["actor"]),
                detail={"run_id": run_id, "plan_hash": ticket.plan_hash},
            )
        snapshot = self.simulator.snapshot()
        operations = self.simulator.operation_rows(ticket_id)
        if not operations and (
            snapshot["version"] != ticket.environment_snapshot_version
            or snapshot["state_hash"] != ticket.environment_snapshot_hash
        ):
            self.change_store.update_status(
                ticket_id,
                ChangeStatus.BLOCKED,
                actor="cloud-network-simulator",
                detail={"reason": "environment_drift"},
            )
            raise DemoChangeError("审批后环境已漂移，执行被阻断")
        current_status = self.change_store.require_ticket(ticket_id)["status"]
        if current_status == ChangeStatus.APPROVED.value:
            self.change_store.update_status(
                ticket_id,
                ChangeStatus.EXECUTING,
                actor=str(arguments["actor"]),
                detail={"run_id": run_id},
            )
        try:
            record = self.simulator.execute_plan(
                ticket,
                run_id=run_id,
                inject_failure=str(arguments.get("inject_failure") or ""),
            )
        except SimulationError as exc:
            self.change_store.update_status(
                ticket_id,
                ChangeStatus.FAILED,
                actor="cloud-network-simulator",
                detail={"error": str(exc)},
            )
            raise DemoChangeError(str(exc)) from exc

        if self.change_store.require_ticket(ticket_id)["status"] == ChangeStatus.EXECUTING.value:
            self.change_store.update_status(
                ticket_id,
                ChangeStatus.VERIFYING,
                actor="cloud-network-simulator",
                detail={"validation_count": len(record.validations)},
            )
        for item in record.validations:
            self.change_store.record_validation(
                ticket_id,
                ValidationResult(
                    validator=f"execution:{item['step_id']}",
                    status=str(item["status"]),
                    message="模拟有效下一跳与业务健康指标验证",
                    hard_gate=True,
                    evidence=dict(item),
                    phase="EXECUTION",
                ),
            )
        self.change_store.record_execution(record)
        if record.outcome == ChangeStatus.ROLLED_BACK.value and not bool(
            record.detail.get("rollback_state_matches_before")
        ):
            final_status = ChangeStatus.FAILED
        else:
            final_status = ChangeStatus(record.outcome)
        self.change_store.update_status(
            ticket_id,
            final_status,
            actor="cloud-network-simulator",
            detail={"outcome": record.outcome},
        )
        self.write_execution_report(ticket_id)
        return record.to_dict()

    def finalize_recorded_execution(self, ticket_id: str) -> dict[str, Any] | None:
        """Finish a run that crashed after persisting its execution record."""

        ticket = self.change_store.require_ticket(ticket_id)
        execution = self.change_store.latest_execution(ticket_id)
        if execution is None:
            return None
        status = ticket["status"]
        if status == ChangeStatus.VERIFYING.value:
            outcome = str(execution["outcome"])
            final_status = ChangeStatus(outcome)
            if outcome == ChangeStatus.ROLLED_BACK.value and not bool(
                execution.get("detail", {}).get("rollback_state_matches_before")
            ):
                final_status = ChangeStatus.FAILED
            self.change_store.update_status(
                ticket_id,
                final_status,
                actor="runtime-recovery",
                detail={"reason": "recorded_execution_recovered"},
            )
            self.write_execution_report(ticket_id)
        return execution

    def create_feedback(self, ticket_id: str) -> dict[str, Any]:
        existing = self.change_store.latest_feedback(ticket_id)
        if existing is not None:
            return existing
        ticket = self.change_store.require_ticket(ticket_id)
        execution = self.change_store.latest_execution(ticket_id)
        if execution is None:
            raise DemoChangeError("尚无执行记录，不能生成反馈")
        outcome = str(execution["outcome"])
        evidence = (
            f"合成变更 {ticket_id} 执行结果为 {outcome}；"
            f"已应用 {len(execution['applied_steps'])} 个步骤，"
            f"回退 {len(execution['rollback_steps'])} 个步骤。"
        )
        content = (
            f"# {ticket_id} 执行反馈（合成）\n\n{evidence}\n\n"
            f"前态哈希：{execution['before_state_hash']}\n"
            f"后态哈希：{execution['after_state_hash']}\n"
        )
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id, _ = self.knowledge_store.add_document(
            f"{ticket_id} 执行反馈（合成）",
            "synthetic-execution-feedback",
            f"synthetic://change-demo/executions/{ticket_id}",
            checksum,
            content,
        )
        chunk_id = self.knowledge_store.add_chunk(document_id, 0, 0, len(content), content)
        card_id = self.knowledge_store.add_card(
            KnowledgeCardDraft(
                title=f"{ticket_id} {self.case.label}执行反馈候选",
                summary=evidence,
                knowledge_type="case",
                scenario=self.case.label,
                object_type="cloud_network_change",
                object_name=ticket_id,
                applicable_versions=["synthetic-v1"],
                prerequisites=["仅适用于合成演示环境"],
                procedure_steps=[str(item) for item in execution["applied_steps"]],
                risks=["执行结果仍需人工复核后才能转为正式知识"],
                rollback_steps=[str(item) for item in execution["rollback_steps"]]
                or ["本次未触发回退"],
                validation_steps=[
                    f"检查执行验证记录，共 {len(execution['validations'])} 项"
                ],
                keywords=["执行反馈", self.case.label, "PENDING_REVIEW", "合成数据"],
                evidence_quote=evidence,
            ),
            document_id=document_id,
            chunk_id=chunk_id,
            evidence_locator=f"synthetic://change-demo/executions/{ticket_id}#summary",
            status=CardStatus.PENDING_REVIEW,
            quality_score=96.0,
            quality_issues=[],
            comparison=ComparisonResult(
                decision=ComparisonDecision.NEW,
                confidence=1.0,
                reason="由结构化执行证据生成，等待人工审核",
            ),
        )
        feedback = FeedbackRecord(
            ticket_id=ticket_id,
            outcome=outcome,
            planned_steps=len(ticket["plan_steps"]),
            applied_steps=len(execution["applied_steps"]),
            rollback_steps=len(execution["rollback_steps"]),
            deviations=(
                ["验证失败后按计划自动回退"]
                if outcome == ChangeStatus.ROLLED_BACK.value
                else []
            ),
            lessons=[
                "继续保留单AZ灰度和连续两个采样周期的验证门槛",
                "执行经验只能生成待审核知识候选，不能自动发布",
            ],
            knowledge_candidate_id=card_id,
            created_at=utc_now(),
        )
        payload = self.change_store.record_feedback(feedback)
        self.change_store.add_audit(
            ticket_id,
            "FEEDBACK_CAPTURED",
            "feedback-pipeline",
            {"knowledge_candidate_id": card_id, "knowledge_status": "PENDING_REVIEW"},
        )
        self.write_feedback_report(ticket_id)
        return payload

    def publish_feedback_to(
        self,
        ticket_id: str,
        target_store: KnowledgeStore,
        *,
        actor: str,
    ) -> dict[str, Any]:
        """Copy an execution-backed candidate into the governed knowledge queue.

        The demo owns an isolated knowledge database. This explicit bridge is the
        only operation that writes to the platform knowledge database, and it
        always creates a PENDING_REVIEW card.
        """

        for audit in self.change_store.list_audit(ticket_id):
            if audit["action"] == "FEEDBACK_PUBLISHED_TO_MAIN":
                card_id = int(audit["detail"]["knowledge_card_id"])
                existing = target_store.get_card(card_id)
                if existing is not None:
                    return {
                        "knowledge_card_id": card_id,
                        "status": str(existing["status"]),
                        "created": False,
                    }

        ticket = self.change_store.require_ticket(ticket_id)
        execution = self.change_store.latest_execution(ticket_id)
        feedback = self.change_store.latest_feedback(ticket_id)
        if execution is None or feedback is None:
            raise DemoChangeError("执行反馈尚未生成，不能沉淀到知识审核队列")
        if ticket["status"] not in {
            ChangeStatus.SUCCEEDED.value,
            ChangeStatus.ROLLED_BACK.value,
        }:
            raise DemoChangeError(f"当前变更状态不能沉淀经验: {ticket['status']}")

        outcome = str(execution["outcome"])
        validation_summary = "；".join(
            f"{item.get('step_id', 'unknown')}={item.get('status', 'UNKNOWN')}"
            for item in execution["validations"]
        ) or "无执行期验证记录"
        evidence = (
            f"合成变更 {ticket_id} 执行结果为 {outcome}；"
            f"应用步骤 {len(execution['applied_steps'])} 个，"
            f"回退步骤 {len(execution['rollback_steps'])} 个；"
            f"验证结果：{validation_summary}。"
        )
        content = (
            f"# {ticket_id} 云网络变更执行反馈（合成）\n\n"
            f"> 本文档及全部资源标识均为合成演示数据。\n\n"
            f"{evidence}\n\n"
            f"- 计划哈希：{ticket['plan_hash']}\n"
            f"- 前态哈希：{execution['before_state_hash']}\n"
            f"- 后态哈希：{execution['after_state_hash']}\n"
            f"- 执行 Run：{execution['run_id']}\n"
        )
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id, document_created = target_store.add_document(
            f"{ticket_id} 云网络变更执行反馈（合成）",
            "synthetic-execution-feedback",
            f"synthetic://change-demo/{self.workspace.name}/{ticket_id}",
            checksum,
            content,
        )
        existing_cards = target_store.card_ids_for_document(document_id)
        if not document_created and existing_cards:
            card_id = existing_cards[0]
            card = target_store.get_card(card_id)
            status = str(card["status"]) if card is not None else "PENDING_REVIEW"
            created = False
        else:
            chunk_id = target_store.add_chunk(
                document_id, 0, 0, len(content), content
            )
            card_id = target_store.add_card(
                KnowledgeCardDraft(
                    title=f"{ticket_id} {self.case.label}执行经验候选",
                    summary=evidence,
                    knowledge_type="case",
                    scenario=self.case.label,
                    object_type="cloud_network_change",
                    object_name=ticket_id,
                    applicable_versions=["synthetic-v1"],
                    prerequisites=[
                        "仅适用于合成演示环境",
                        "进入可信知识前必须由知识责任人审核",
                    ],
                    procedure_steps=[str(item) for item in execution["applied_steps"]],
                    risks=["模拟执行结果不能直接作为真实生产变更依据"],
                    rollback_steps=[str(item) for item in execution["rollback_steps"]]
                    or ["本次未触发回退"],
                    validation_steps=[validation_summary],
                    keywords=["云网络", "VPC", self.case.label, "执行反馈", "合成数据"],
                    evidence_quote=evidence,
                ),
                document_id=document_id,
                chunk_id=chunk_id,
                evidence_locator=(
                    f"synthetic://change-demo/{self.workspace.name}/{ticket_id}#execution"
                ),
                status=CardStatus.PENDING_REVIEW,
                quality_score=96.0,
                quality_issues=[],
                comparison=ComparisonResult(
                    decision=ComparisonDecision.NEW,
                    confidence=1.0,
                    reason="由隔离模拟器的结构化执行日志生成，等待人工审核",
                ),
            )
            status = CardStatus.PENDING_REVIEW.value
            created = True

        self.change_store.add_audit(
            ticket_id,
            "FEEDBACK_PUBLISHED_TO_MAIN",
            actor.strip() or "demo-operator",
            {
                "knowledge_card_id": card_id,
                "knowledge_status": status,
                "target_database": str(target_store.database_path),
            },
        )
        return {
            "knowledge_card_id": card_id,
            "status": status,
            "created": created,
        }

    def ticket_package(self, ticket_id: str) -> dict[str, Any]:
        return {
            "ticket": self.change_store.require_ticket(ticket_id),
            "validations": self.change_store.list_validations(ticket_id),
            "execution": self.change_store.latest_execution(ticket_id),
            "feedback": self.change_store.latest_feedback(ticket_id),
            "audit": self.change_store.list_audit(ticket_id),
            "workspace": str(self.workspace),
        }

    def write_generation_reports(self, ticket_id: str) -> None:
        package = self.ticket_package(ticket_id)
        self._write_json("change_package.json", package)
        self._write_json("validation_report.json", package["validations"])
        ticket = package["ticket"]
        validations = package["validations"]
        lines = [
            "# 云网络变更单（合成演示数据）",
            "",
            "> 本文档不包含真实云资产，不得作为生产执行授权。",
            "",
            f"- 变更单：`{ticket['ticket_id']}` / 修订 {ticket['revision']}",
            f"- 状态：`{ticket['status']}`",
            f"- 类型/风险：{ticket['change_type']} / {ticket['risk_level']}（{ticket['risk_score']}）",
            f"- 请求人：{ticket['requested_by']}",
            f"- 区域/VPC：{ticket['region']} / `{ticket['vpc_id']}`",
            f"- 标题：{ticket['title']}",
            f"- 摘要：{ticket['summary']}",
            f"- 计划哈希：`{ticket['plan_hash']}`",
            f"- 环境快照：v{ticket['environment_snapshot_version']} / `{ticket['environment_snapshot_hash']}`",
            "",
            "## 已批准知识证据",
            "",
        ]
        lines.extend(
            f"- K{item['card_id']} `{item['status']}`：{item['title']}"
            for item in ticket["knowledge_references"]
        )
        lines.extend(["", "## 执行步骤", ""])
        lines.extend(
            f"{index}. `{step['phase']}` `{step['route_table_id']}`："
            f"{step['destination']} 从 `{step['from_next_hop']}` 切换至 `{step['to_next_hop']}`"
            for index, step in enumerate(ticket["plan_steps"], start=1)
        )
        lines.extend(["", "## 前置校验", ""])
        lines.extend(
            f"- [{item['status']}] {item['validator']}：{item['message']}"
            for item in validations
        )
        lines.extend(["", "## 回退触发", ""])
        lines.extend(f"- {item}" for item in ticket["rollback_triggers"])
        lines.extend(["", "## 回退步骤", ""])
        lines.extend(f"{index}. {item}" for index, item in enumerate(ticket["rollback_steps"], start=1))
        lines.extend(["", "## 通信计划", ""])
        lines.extend(f"- {item}" for item in ticket["communication_plan"])
        self._write_text("change_order.md", "\n".join(lines) + "\n")

    def write_execution_report(self, ticket_id: str) -> None:
        self.write_generation_reports(ticket_id)
        package = self.ticket_package(ticket_id)
        self._write_json("validation_report.json", package["validations"])
        self._write_json(
            "execution_report.json",
            {
                "ticket": package["ticket"],
                "execution": package["execution"],
                "validations": package["validations"],
                "audit": package["audit"],
            },
        )
        self._write_json("change_package.json", package)

    def write_feedback_report(self, ticket_id: str) -> None:
        package = self.ticket_package(ticket_id)
        feedback = package["feedback"]
        lines = [
            "# 变更执行反馈（合成演示数据）",
            "",
            f"- 变更单：`{ticket_id}`",
            f"- 结果：`{feedback['outcome']}`",
            f"- 计划/执行/回退步骤：{feedback['planned_steps']} / {feedback['applied_steps']} / {feedback['rollback_steps']}",
            f"- 知识候选：K{feedback['knowledge_candidate_id']}，状态 `PENDING_REVIEW`",
            "",
            "## 偏差",
            "",
        ]
        lines.extend(f"- {item}" for item in feedback["deviations"] or ["无计划外偏差"])
        lines.extend(["", "## 经验", ""])
        lines.extend(f"- {item}" for item in feedback["lessons"])
        self._write_text("feedback.md", "\n".join(lines) + "\n")
        self._write_json("change_package.json", package)

    def write_runtime_events(self, events: list[dict[str, Any]]) -> Path:
        return self._write_json("runtime_events.json", events)

    def _write_json(self, name: str, payload: Any) -> Path:
        path = self.workspace / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_text(self, name: str, content: str) -> Path:
        path = self.workspace / name
        path.write_text(content, encoding="utf-8")
        return path
