from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from change_management.cases import seed_case_catalog_knowledge
from harness.config import Settings
from knowledge_platform.schema import CardStatus
from knowledge_platform.service import KnowledgeService


DEFAULT_QUERIES = [
    {
        "query": "内部批处理和分析工作负载要从旧私有入口迁到新入口，出问题如何撤销？",
        "expected_title": "私网终端节点服务蓝绿切换",
    },
    {
        "query": "把多组容器网络出口逐批挪走时，灰度波次应该怎么安排？",
        "expected_title": "Kubernetes 多集群 NAT 出口池迁移",
    },
    {
        "query": "合作伙伴的私网接入要换通道，怎样保证双隧道可回退？",
        "expected_title": "合作方 VPN 双隧道迁移",
    },
]

DIFFICULT_NEGATIVE_QUERIES = [
    "数据库主从切换失败后如何回退？",
    "Kubernetes Ingress 的 TLS 证书轮换需要哪些步骤？",
    "员工 IAM 账号权限开通失败后如何撤销授权？",
    "请推荐一份周末晚餐菜单并说明明天的天气。",
]


def _hit_summary(hits: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "card_id": int(hit.card["id"]),
            "title": str(hit.card["title"]),
            "score": round(float(hit.score), 4),
            "channel": (
                "mindmemos_semantic"
                if "mindmemos:semantic" in hit.matched_terms
                else "local_lexical"
            ),
        }
        for hit in hits
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed approved change cases and compare local vs MindMemOS recall."
    )
    parser.add_argument("--env", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    settings = Settings.load(args.env)
    service = KnowledgeService(settings)
    seeded = seed_case_catalog_knowledge(service.store)
    cards = service.store.list_cards(
        CardStatus.APPROVED, limit=settings.mindmemos_max_sync_cards
    )
    print(
        f"[experiment] seeded={len(seeded)} approved={len(cards)} "
        f"memory_enabled={service.memory.enabled}",
        flush=True,
    )

    sync_results: list[dict[str, Any]] = []
    sync_started = time.monotonic()
    for index, card in enumerate(cards, start=1):
        try:
            result = service.memory.sync_card(card)
        except Exception as exc:
            result = {
                "status": "FAILED",
                "card_id": int(card["id"]),
                "error": str(exc),
            }
        sync_results.append(result)
        print(
            f"[experiment] sync {index}/{len(cards)} "
            f"K{card['id']} {result['status']}",
            flush=True,
        )

    comparisons: list[dict[str, Any]] = []
    for case in DEFAULT_QUERIES:
        query = case["query"]
        local_hits = service.retriever.search(
            query,
            statuses=[CardStatus.APPROVED],
            top_k=settings.retrieval_top_k,
            min_score=settings.retrieval_min_score,
            min_query_coverage=settings.retrieval_min_coverage,
        )
        enhanced_hits, diagnostics = service.trusted_search_hits(
            query, top_k=settings.retrieval_top_k
        )
        comparisons.append(
            {
                "query": query,
                "expected_title": case["expected_title"],
                "local": _hit_summary(local_hits),
                "enhanced": _hit_summary(enhanced_hits),
                "local_top_correct": bool(
                    local_hits
                    and case["expected_title"] in str(local_hits[0].card["title"])
                ),
                "enhanced_top_correct": bool(
                    enhanced_hits
                    and case["expected_title"] in str(enhanced_hits[0].card["title"])
                ),
                "memory_retrieval": diagnostics,
            }
        )
        print(
            f"[experiment] query local={len(local_hits)} "
            f"enhanced={len(enhanced_hits)} semantic_added="
            f"{len(diagnostics['semantic_added_card_ids'])}",
            flush=True,
        )

    negative_results: list[dict[str, Any]] = []
    for query in DIFFICULT_NEGATIVE_QUERIES:
        enhanced_hits, diagnostics = service.trusted_search_hits(
            query, top_k=settings.retrieval_top_k
        )
        negative_results.append(
            {
                "query": query,
                "accepted": bool(enhanced_hits),
                "enhanced": _hit_summary(enhanced_hits),
                "memory_retrieval": diagnostics,
            }
        )
        print(
            f"[experiment] negative accepted={bool(enhanced_hits)} "
            f"semantic_rejected={len(diagnostics.get('semantic_rejected', []))}",
            flush=True,
        )

    false_accepts = sum(1 for item in negative_results if item["accepted"])

    report = {
        "synthetic": True,
        "database_path": str(settings.database_path),
        "mindmemos_base_url": settings.mindmemos_base_url,
        "trust_policy": "local_approved_only",
        "seeded_cases": len(seeded),
        "approved_cards": len(cards),
        "sync_elapsed_seconds": round(time.monotonic() - sync_started, 2),
        "sync_results": sync_results,
        "memory_status": service.long_term_memory_status(probe=True),
        "comparisons": comparisons,
        "difficult_negatives": negative_results,
        "false_acceptance_rate": (
            false_accepts / len(negative_results) if negative_results else 0.0
        ),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[experiment] report={args.report}", flush=True)
    sync_ok = all(item["status"] != "FAILED" for item in sync_results)
    positives_ok = all(item["enhanced_top_correct"] for item in comparisons)
    return 0 if sync_ok and positives_ok and false_accepts == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
