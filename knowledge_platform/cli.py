from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from harness.api_client import APIError
from harness.config import ConfigurationError, Settings

from .documents import DocumentError
from .schema import CardStatus
from .service import KnowledgeService, KnowledgeServiceError
from .store import StoreError
from .web import create_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


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
        else:
            raise KnowledgeServiceError(f"未知命令: {command}")
        return 0
    except (
        APIError,
        ConfigurationError,
        DocumentError,
        KnowledgeServiceError,
        StoreError,
        OSError,
        ValueError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
