from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from harness.api_client import APIError
from harness.config import ConfigurationError, Settings
from harness.run_store import RunStoreError
from harness.runtime import HarnessRuntimeError

from .documents import DocumentError
from .schema import CardStatus
from .runtime_tasks import create_knowledge_runtime
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

    search_parser = subparsers.add_parser("search", help="本地检索知识，不调用 API")
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
    subparsers.add_parser(
        "regrade",
        help="不调用 API，按当前证据回定位和分类质量规则重新评分已有卡片",
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
        service = KnowledgeService(settings)

        if command == "init":
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
        elif command == "search":
            _print_json(
                {
                    "hits": service.search(
                        args.query,
                        status=args.status,
                        top_k=args.top_k,
                    )
                }
            )
        elif command == "query":
            _print_json(service.query(args.question))
        elif command == "agent-query":
            _print_json(service.agent_query(args.question))
        elif command == "stats":
            _print_json(service.stats())
        elif command == "regrade":
            _print_json(service.regrade_existing_cards())
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
        OSError,
        ValueError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
