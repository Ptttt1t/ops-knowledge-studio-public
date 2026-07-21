from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.config import Settings  # noqa: E402
from knowledge_platform.service import KnowledgeService  # noqa: E402


RESULT_PATH = PROJECT_ROOT / "artifacts" / "public_test" / "query_results.json"

SEARCH_CASES = [
    {
        "name": "yaml_ingress_configuration",
        "question": "nginx ingress 如何按 predictorid 配置一致性哈希并验证？",
        "expected_card_id": 3,
        "run_trusted_answer": True,
    },
    {
        "name": "json_github_incident",
        "question": "GitHub Webhook 交付状态没有持久化与哪次部署问题有关？",
        "expected_card_id": 7,
        "run_trusted_answer": False,
    },
    {
        "name": "csv_cisa_vulnerability",
        "question": "CVE-2026-58644 对 Microsoft SharePoint 有什么风险？",
        "expected_card_id": 10,
        "run_trusted_answer": True,
    },
    {
        "name": "docx_response_plan",
        "question": "如何利用呼叫树演练、桌面推演和技术恢复测试来维护网络应急计划？",
        "expected_card_id": 61,
        "run_trusted_answer": True,
    },
    {
        "name": "txt_rfc_negative_control",
        "question": "HTTP 503 和 Retry-After 表示什么？",
        "expected_card_id": None,
        "run_trusted_answer": False,
    },
]


def main() -> int:
    settings = Settings.load(PROJECT_ROOT / ".env")
    settings.require_api()
    service = KnowledgeService(settings)
    report: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.model,
        "cases": [],
    }

    failures = 0
    for case in SEARCH_CASES:
        started = time.perf_counter()
        item = dict(case)
        try:
            hits = service.search(case["question"], top_k=5)
            item["search_hits"] = [
                {
                    "card_id": hit["card"]["id"],
                    "title": hit["card"]["title"],
                    "score": hit["score"],
                    "matched_terms": hit["matched_terms"],
                }
                for hit in hits
            ]
            expected = case["expected_card_id"]
            item["search_passed"] = (
                not hits if expected is None else any(hit["card_id"] == expected for hit in item["search_hits"])
            )
            if case["run_trusted_answer"] and hits:
                item["trusted_answer"] = service.query(case["question"])
                item["citation_passed"] = bool(item["trusted_answer"]["sources"])
            else:
                item["trusted_answer"] = None
                item["citation_passed"] = None
            if not item["search_passed"] or item["citation_passed"] is False:
                failures += 1
        except Exception as exc:
            failures += 1
            item["error"] = str(exc)
        item["duration_seconds"] = round(time.perf_counter() - started, 2)
        report["cases"].append(item)
        print(
            f"{case['name']}: search={item.get('search_passed')} "
            f"citation={item.get('citation_passed')} error={item.get('error', '-')}",
            flush=True,
        )

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["failures"] = failures
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"report: {RESULT_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
