from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from harness.api_client import APIError, DeepSeekClient
from harness.config import ConfigurationError, Settings
from harness.run_store import RunStoreError
from harness.runtime import HarnessRuntimeError

from change_management.cases import DEFAULT_CASE_ID, list_change_cases
from change_management.runtime_tasks import create_change_runtime
from change_management.service import DemoChangeError, DemoChangeService
from change_management.store import ChangeStoreError

from .documents import DocumentError
from .schema import CardStatus
from .runtime_tasks import create_knowledge_runtime
from .security import generate_access_token
from .service import KnowledgeService, KnowledgeServiceError
from .store import StoreError
from .web import create_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parse_json_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ops Knowledge Studio：面向 DeepSeek 的运维知识工程平台"
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="配置文件路径",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="初始化 SQLite 知识库")
    subparsers.add_parser(
        "generate-access-token",
        help="生成一次性显示的 Web 访问令牌及其 SHA-256 配置值",
    )

    serve_parser = subparsers.add_parser("serve", help="启动本地网页平台")
    serve_parser.add_argument("--host", help="监听地址，默认读取 .env")
    serve_parser.add_argument("--port", type=int, help="监听端口，默认读取 .env")

    ingest_parser = subparsers.add_parser("ingest", help="导入文档并抽取知识卡片")
    ingest_parser.add_argument("--file", type=Path, required=True, help="文档路径")

    list_parser = subparsers.add_parser("list", help="列出知识卡片")
    list_parser.add_argument(
        "--status",
        choices=[status.value for status in CardStatus],
        help="按生命周期状态过滤",
    )
    list_parser.add_argument("--limit", type=int, default=100)

    show_parser = subparsers.add_parser("show", help="查看知识卡片详情")
    show_parser.add_argument("--id", type=int, required=True)

    review_parser = subparsers.add_parser("review", help="审核、驳回或替代知识")
    review_parser.add_argument("--id", type=int, required=True)
    review_parser.add_argument(
        "--action", choices=["approve", "reject", "supersede"], required=True
    )
    review_parser.add_argument("--reviewer", required=True)
    review_parser.add_argument("--comment", default="")
    review_parser.add_argument("--supersedes-id", type=int)

    bundles_parser = subparsers.add_parser(
        "case-bundles", help="列出结构化变更案例包"
    )
    bundles_parser.add_argument(
        "--status",
        choices=[status.value for status in CardStatus] + ["PARTIAL", "EMPTY"],
    )
    bundles_parser.add_argument("--limit", type=int, default=100)

    bundle_parser = subparsers.add_parser(
        "case-bundle", help="查看一个结构化变更案例包及其有序原子卡"
    )
    bundle_parser.add_argument("--case-id", required=True)

    bundle_review_parser = subparsers.add_parser(
        "review-case-bundle", help="原子地批准或驳回整个变更案例包"
    )
    bundle_review_parser.add_argument("--case-id", required=True)
    bundle_review_parser.add_argument(
        "--action", choices=["approve", "reject"], required=True
    )
    bundle_review_parser.add_argument("--reviewer", required=True)
    bundle_review_parser.add_argument("--comment", default="")

    search_parser = subparsers.add_parser(
        "search",
        help="治理检索；默认使用本地索引，无命中时可选用 MindMemOS 语义后备",
    )
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument(
        "--status",
        choices=[status.value for status in CardStatus],
        default=CardStatus.APPROVED.value,
    )
    search_parser.add_argument("--top-k", type=int)

    query_parser = subparsers.add_parser("query", help="基于已审核知识生成可信方案")
    query_parser.add_argument("--question", required=True)

    agent_query_parser = subparsers.add_parser(
        "agent-query", help="使用有步骤上限的只读知识 Agent 检索并生成可信方案"
    )
    agent_query_parser.add_argument("--question", required=True)

    subparsers.add_parser("stats", help="查看知识库统计")
    memory_status = subparsers.add_parser(
        "memory-status", help="查看 MindMemOS 长期记忆连接与同步状态"
    )
    memory_status.add_argument(
        "--probe", action="store_true", help="实际探测 MindMemOS 健康接口"
    )
    subparsers.add_parser(
        "memory-sync", help="将全部 APPROVED 知识幂等同步到 MindMemOS"
    )
    subparsers.add_parser(
        "regrade",
        help="不调用 API，按当前证据回定位和分类质量规则重新评分已有卡片",
    )
    demo_change = subparsers.add_parser(
        "demo-change",
        help="运行离线云网络变更单生成、审批、模拟执行和反馈闭环",
    )
    demo_change.add_argument("--actor", default="demo-operator", help="演示请求人与审批人")
    demo_change.add_argument(
        "--case-id",
        choices=[item["case_id"] for item in list_change_cases()],
        default=DEFAULT_CASE_ID,
        help="选择要生成和执行的合成云网络案例",
    )
    demo_change.add_argument(
        "--use-model",
        action="store_true",
        help="尝试使用DeepSeek润色标题和摘要；失败时自动回退离线模板",
    )
    demo_change.add_argument(
        "--inject-failure",
        metavar="STEP_ID",
        help="可选：使用当前案例的步骤ID注入验证失败并演示自动回退",
    )
    run_submit = subparsers.add_parser(
        "run-submit", help="Submit a durable Harness task and wait for its result"
    )
    run_submit.add_argument("--task-type", required=True)
    run_submit.add_argument("--input-json", required=True)
    run_submit.add_argument("--budget-json", default="{}")
    run_submit.add_argument("--idempotency-key")
    run_submit.add_argument("--wait-seconds", type=int)

    run_list = subparsers.add_parser("run-list", help="List durable Harness runs")
    run_list.add_argument("--status")
    run_list.add_argument("--limit", type=int, default=100)

    run_show = subparsers.add_parser("run-show", help="Show a durable Harness run")
    run_show.add_argument("--id", required=True)
    run_show.add_argument("--events", action="store_true")

    run_cancel = subparsers.add_parser(
        "run-cancel", help="Request cancellation of a Harness run"
    )
    run_cancel.add_argument("--id", required=True)

    run_resume = subparsers.add_parser(
        "run-resume", help="Resume a failed or interrupted Harness run"
    )
    run_resume.add_argument("--id", required=True)
    run_resume.add_argument("--wait-seconds", type=int)

    run_approve_tool = subparsers.add_parser(
        "run-approve-tool", help="Approve or reject the pending tool request for a Run"
    )
    run_approve_tool.add_argument("--id", required=True)
    run_approve_tool.add_argument("--tool-name", required=True)
    run_approve_tool.add_argument(
        "--decision", choices=["APPROVED", "REJECTED"], required=True
    )
    run_approve_tool.add_argument("--actor", required=True)
    run_approve_tool.add_argument("--comment", default="")
    run_approve_tool.add_argument("--wait-seconds", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "serve"

    try:
        settings = Settings.load(args.env)
        service = None if command == "demo-change" else KnowledgeService(settings)

        if command == "generate-access-token":
            token, hashed = generate_access_token()
            _print_json(
                {
                    "access_token": token,
                    "env": f"PLATFORM_ACCESS_TOKEN_HASH={hashed}",
                    "warning": "访问令牌只显示本次；请通过受保护渠道分发。",
                }
            )
        elif command == "init":
            _print_json(
                {
                    "initialized": True,
                    "database_path": str(settings.database_path),
                    "api_configured": settings.api_configured,
                }
            )
        elif command == "serve":
            host = getattr(args, "host", None) or settings.host
            port = getattr(args, "port", None) or settings.port
            server = create_server(service, host=host, port=port)
            print(f"Ops Knowledge Studio 已启动：http://{host}:{port}")
            for message in settings.startup_security_messages():
                print(message)
            print("按 Ctrl+C 停止。")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n正在停止平台……")
            finally:
                server.server_close()
        elif command == "ingest":
            _print_json(service.ingest_file(args.file))
        elif command == "list":
            _print_json(
                {
                    "cards": service.store.list_cards(
                        status=args.status, limit=args.limit
                    )
                }
            )
        elif command == "show":
            card = service.card_detail(args.id)
            if card is None:
                raise KnowledgeServiceError(f"知识卡片不存在: {args.id}")
            _print_json(card)
        elif command == "review":
            _print_json(
                service.review(
                    args.id,
                    action=args.action,
                    reviewer=args.reviewer,
                    comment=args.comment,
                    supersedes_id=args.supersedes_id,
                )
            )
        elif command == "case-bundles":
            _print_json(
                {
                    "case_bundles": service.list_case_bundles(
                        status=args.status,
                        limit=args.limit,
                    )
                }
            )
        elif command == "case-bundle":
            bundle = service.case_bundle_detail(args.case_id)
            if bundle is None:
                raise KnowledgeServiceError(f"案例包不存在: {args.case_id}")
            _print_json(bundle)
        elif command == "review-case-bundle":
            _print_json(
                service.review_case_bundle(
                    args.case_id,
                    action=args.action,
                    reviewer=args.reviewer,
                    comment=args.comment,
                )
            )
        elif command == "search":
            _print_json(
                service.search_with_diagnostics(
                    args.query,
                    status=args.status,
                    top_k=args.top_k,
                )
            )
        elif command == "query":
            _print_json(service.query(args.question))
        elif command == "agent-query":
            _print_json(service.agent_query(args.question))
        elif command == "stats":
            _print_json(service.stats())
        elif command == "memory-status":
            _print_json(service.long_term_memory_status(probe=args.probe))
        elif command == "memory-sync":
            _print_json(service.sync_long_term_memory())
        elif command == "regrade":
            _print_json(service.regrade_existing_cards())
        elif command == "demo-change":
            workspace = DemoChangeService.create_workspace(settings.project_root)
            demo_service = DemoChangeService(
                workspace,
                model_client=DeepSeekClient(settings) if args.use_model else None,
                case_id=args.case_id,
            )
            if (
                args.inject_failure
                and args.inject_failure not in demo_service.case.execution_step_ids
            ):
                raise ValueError(
                    f"故障注入点 {args.inject_failure} 不属于案例 {args.case_id}"
                )
            runtime = create_change_runtime(demo_service)
            generation_run_id = ""
            execution_run_id = ""
            try:
                submitted, _ = runtime.submit(
                    "change.generate_demo",
                    {"requested_by": args.actor, "use_model": args.use_model},
                    idempotency_key=f"{workspace.name}:generate",
                )
                generation_run_id = submitted["id"]
                generated = runtime.wait(
                    generation_run_id,
                    timeout_seconds=settings.runtime_sync_wait_seconds,
                )
                if generated is None or generated["status"] != "SUCCEEDED":
                    raise DemoChangeError(f"变更单生成失败: {generated}")
                package = generated["result"]
                ticket = package["ticket"]
                print("\n=== 合成云网络变更单：不连接任何真实云 ===")
                print(f"变更单: {ticket['ticket_id']}  状态: {ticket['status']}")
                print(f"标题: {ticket['title']}")
                print(f"计划哈希: {ticket['plan_hash']}")
                print(f"环境快照: v{ticket['environment_snapshot_version']} {ticket['environment_snapshot_hash']}")
                print(f"知识证据: {', '.join('K' + str(item['card_id']) for item in ticket['knowledge_references'])}")
                step_tables = [
                    str(item["route_table_id"]) for item in ticket["plan_steps"]
                ]
                if len(step_tables) > 5:
                    sequence = " -> ".join(step_tables[:4] + ["…", step_tables[-1]])
                else:
                    sequence = " -> ".join(step_tables)
                print(f"执行顺序: {len(step_tables)} 步 · {sequence}（逐步验证）")
                failed_gates = [
                    item for item in package["validations"]
                    if item["hard_gate"] and item["status"] != "PASS"
                ]
                print(
                    f"前置校验: {len(package['validations']) - len(failed_gates)} PASS / "
                    f"{len(failed_gates)} FAIL"
                )
                print(f"工件目录: {workspace}")
                if ticket["status"] == "BLOCKED":
                    demo_service.write_runtime_events(
                        [
                            {
                                "run_id": generation_run_id,
                                "events": runtime.store.list_events(generation_run_id),
                            }
                        ]
                    )
                    _print_json({"blocked": True, "package": package})
                    return 2

                execution, _ = runtime.submit(
                    "change.execute_demo",
                    {
                        "ticket_id": ticket["ticket_id"],
                        "actor": args.actor,
                        "inject_failure": args.inject_failure or "",
                    },
                    idempotency_key=f"{workspace.name}:execute",
                )
                execution_run_id = execution["id"]
                waiting = runtime.wait(
                    execution_run_id,
                    timeout_seconds=settings.runtime_sync_wait_seconds,
                )
                if waiting is None or waiting["status"] != "WAITING_APPROVAL":
                    raise DemoChangeError(f"执行任务未进入审批门禁: {waiting}")

                confirmation = f"APPROVE {ticket['ticket_id']}"
                print("\n审批后将只修改本次演示目录内的模拟SQLite网络状态。")
                print(f"如要批准，请完整输入：{confirmation}")
                try:
                    decision = input("审批确认> ").strip()
                except EOFError:
                    decision = ""
                if decision != confirmation:
                    runtime.decide_tool_approval(
                        execution_run_id,
                        demo_service.TOOL_NAME,
                        decision="REJECTED",
                        actor=args.actor,
                        comment="未输入精确批准串，演示执行已拒绝",
                    )
                    rejected = demo_service.reject_ticket(
                        ticket["ticket_id"],
                        actor=args.actor,
                        comment="未输入精确批准串",
                    )
                    demo_service.write_generation_reports(ticket["ticket_id"])
                    demo_service.write_runtime_events(
                        [
                            {
                                "run_id": generation_run_id,
                                "events": runtime.store.list_events(generation_run_id),
                            },
                            {
                                "run_id": execution_run_id,
                                "events": runtime.store.list_events(execution_run_id),
                            },
                        ]
                    )
                    _print_json(
                        {
                            "approved": False,
                            "ticket": rejected,
                            "workspace": str(workspace),
                        }
                    )
                    return 3

                runtime.decide_tool_approval(
                    execution_run_id,
                    demo_service.TOOL_NAME,
                    decision="APPROVED",
                    actor=args.actor,
                    comment="已核对合成变更单、计划哈希和环境快照",
                )
                completed = runtime.wait(
                    execution_run_id,
                    timeout_seconds=settings.runtime_sync_wait_seconds,
                )
                events = [
                    {
                        "run_id": generation_run_id,
                        "events": runtime.store.list_events(generation_run_id),
                    },
                    {
                        "run_id": execution_run_id,
                        "events": runtime.store.list_events(execution_run_id),
                    },
                ]
                demo_service.write_runtime_events(events)
                if completed is None or completed["status"] != "SUCCEEDED":
                    raise DemoChangeError(f"变更执行Run失败: {completed}")
                final_package = demo_service.ticket_package(ticket["ticket_id"])
                print("\n=== 闭环完成 ===")
                print(f"变更结果: {final_package['ticket']['status']}")
                print(
                    "知识候选: "
                    f"K{final_package['feedback']['knowledge_candidate_id']} "
                    "PENDING_REVIEW"
                )
                print(f"完整工件: {workspace}")
                _print_json(
                    {
                        "approved": True,
                        "run_id": execution_run_id,
                        "run_status": completed["status"],
                        "ticket_id": ticket["ticket_id"],
                        "ticket_status": final_package["ticket"]["status"],
                        "knowledge_candidate": {
                            "card_id": final_package["feedback"]["knowledge_candidate_id"],
                            "status": "PENDING_REVIEW",
                        },
                        "artifacts": {
                            name: str(workspace / name)
                            for name in [
                                "change_order.md",
                                "change_package.json",
                                "validation_report.json",
                                "execution_report.json",
                                "feedback.md",
                                "runtime_events.json",
                            ]
                        },
                    }
                )
            finally:
                runtime.stop()
        elif command == "run-submit":
            runtime = create_knowledge_runtime(
                service,
                worker_count=settings.runtime_workers,
                max_queued_runs=settings.runtime_max_queued_runs,
            )
            try:
                submitted, created = runtime.submit(
                    args.task_type,
                    _parse_json_object(args.input_json, label="--input-json"),
                    budget=_parse_json_object(args.budget_json, label="--budget-json"),
                    idempotency_key=args.idempotency_key,
                )
                timeout_seconds = args.wait_seconds or settings.runtime_sync_wait_seconds
                if timeout_seconds <= 0:
                    raise ValueError("--wait-seconds must be greater than 0")
                run = runtime.wait(submitted["id"], timeout_seconds=timeout_seconds)
                _print_json({"created": created, "run": run})
            finally:
                runtime.stop()
        elif command == "run-list":
            runtime = create_knowledge_runtime(
                service,
                worker_count=settings.runtime_workers,
                max_queued_runs=settings.runtime_max_queued_runs,
            )
            try:
                _print_json(
                    {"runs": runtime.store.list_runs(status=args.status, limit=args.limit)}
                )
            finally:
                runtime.stop()
        elif command == "run-show":
            runtime = create_knowledge_runtime(
                service,
                worker_count=settings.runtime_workers,
                max_queued_runs=settings.runtime_max_queued_runs,
            )
            try:
                run = runtime.store.get_run(args.id)
                if run is None:
                    raise KnowledgeServiceError(f"Run not found: {args.id}")
                run["steps"] = runtime.store.list_steps(args.id)
                run["latest_checkpoint"] = runtime.store.latest_checkpoint(args.id)
                if args.events:
                    run["events"] = runtime.store.list_events(args.id)
                _print_json(run)
            finally:
                runtime.stop()
        elif command == "run-cancel":
            runtime = create_knowledge_runtime(
                service,
                worker_count=settings.runtime_workers,
                max_queued_runs=settings.runtime_max_queued_runs,
            )
            try:
                run = runtime.cancel(args.id)
                if run is None:
                    raise KnowledgeServiceError(f"Run not found: {args.id}")
                _print_json(run)
            finally:
                runtime.stop()
        elif command == "run-resume":
            runtime = create_knowledge_runtime(
                service,
                worker_count=settings.runtime_workers,
                max_queued_runs=settings.runtime_max_queued_runs,
            )
            try:
                resumed = runtime.resume(args.id)
                if resumed is None:
                    raise KnowledgeServiceError(f"Run not found: {args.id}")
                timeout_seconds = args.wait_seconds or settings.runtime_sync_wait_seconds
                if timeout_seconds <= 0:
                    raise ValueError("--wait-seconds must be greater than 0")
                _print_json(runtime.wait(args.id, timeout_seconds=timeout_seconds))
            finally:
                runtime.stop()
        elif command == "run-approve-tool":
            runtime = create_knowledge_runtime(
                service,
                worker_count=settings.runtime_workers,
                max_queued_runs=settings.runtime_max_queued_runs,
            )
            try:
                run = runtime.decide_tool_approval(
                    args.id,
                    args.tool_name,
                    decision=args.decision,
                    actor=args.actor,
                    comment=args.comment,
                )
                if run is None:
                    raise KnowledgeServiceError(f"Run not found: {args.id}")
                if run["status"] == "QUEUED" and args.wait_seconds:
                    if args.wait_seconds <= 0:
                        raise ValueError("--wait-seconds must be greater than 0")
                    run = runtime.wait(args.id, timeout_seconds=args.wait_seconds)
                _print_json(run)
            finally:
                runtime.stop()
        else:
            raise KnowledgeServiceError(f"未知命令: {command}")
        return 0
    except (
        APIError,
        ConfigurationError,
        DocumentError,
        KnowledgeServiceError,
        StoreError,
        RunStoreError,
        HarnessRuntimeError,
        DemoChangeError,
        ChangeStoreError,
        OSError,
        ValueError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
