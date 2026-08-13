from __future__ import annotations

"""Evaluate the five schema-faithful synthetic ChangeOrder documents."""

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness.config import Settings
from knowledge_platform.change_order_adapter import build_change_order_extraction_plan
from knowledge_platform.service import KnowledgeService


DEFAULT_INPUT = PROJECT_ROOT / "sample_data" / "realistic_change_orders"
EXPECTED_ROLES = [
    "PRECHECK_STEPS",
    "IMPLEMENTATION_STEPS",
    "VALIDATION_STEPS",
    "ROLLBACK_STEPS",
]


class DeterministicDemoClient:
    """A no-network client that validates the full storage/lineage pipeline."""

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _source(user_prompt: str) -> str:
        return user_prompt.split("\n\n", 1)[-1]

    @staticmethod
    def _quote(source: str) -> str:
        candidates = [
            line.strip().rstrip(",")
            for line in source.splitlines()
            if len(line.strip().rstrip(",")) >= 20
            and line.strip() not in {"{", "}", "[", "]"}
        ]
        return candidates[0] if candidates else source[:120]

    def chat_json(
        self, system_prompt: str, user_prompt: str, **_: Any
    ) -> tuple[dict[str, Any], dict[str, int]]:
        self.calls += 1
        if "知识治理审核助手" in system_prompt:
            return (
                {
                    "decision": "NEW",
                    "related_card_id": None,
                    "confidence": 0.99,
                    "reason": "离线闭环固定返回 NEW",
                },
                {"total_tokens": 1},
            )

        source = self._source(user_prompt)
        quote = self._quote(source)
        role_match = re.search(r"role=([A-Z_]+)", user_prompt)
        role = role_match.group(1) if role_match else "CONTEXT"
        pointer_match = re.search(r"JSON Pointer=([^；]+)", user_prompt)
        pointer = pointer_match.group(1) if pointer_match else "/"
        range_match = re.search(
            r"step_start_index=([^；]+)；step_end_index=([^；]+)", user_prompt
        )
        range_label = (
            f"-{range_match.group(1)}-{range_match.group(2)}"
            if range_match
            else ""
        )
        fingerprint = hashlib.sha256(source.encode()).hexdigest()[:8]
        title = f"{role}{range_label}-{fingerprint}"
        knowledge_type = (
            "rollback"
            if role == "ROLLBACK_STEPS"
            else "case"
            if role == "EXECUTION_RESULT"
            else "procedure"
        )
        card = {
            "title": title,
            "summary": f"离线闭环按完整结构单元保存 {role}，来源 {pointer}。",
            "knowledge_type": knowledge_type,
            "scenario": "合成 ChangeOrder 结构与知识流水线验证",
            "object_type": "ChangeOrderUnit",
            "object_name": f"{role}:{pointer}:{fingerprint}",
            "applicable_versions": ["change_order_shape_v2"],
            "prerequisites": ["结构映射和 TaskRecord 对账通过"],
            "procedure_steps": ["保持源数组顺序处理完整结构单元"],
            "risks": ["合成数据只用于内部演示，不能作为生产指令"],
            "rollback_steps": ["停止演示并丢弃隔离数据库"],
            "validation_steps": ["核对 lineage、角色和结构覆盖报告"],
            "keywords": [role, "synthetic", "change-order-v2"],
            "evidence_quote": quote,
        }
        return {"knowledge_cards": [card]}, {"total_tokens": 1}


def _json_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.glob("*.json") if path.is_file())


def _structural_case(path: Path, chunk_size: int) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    plan, report = build_change_order_extraction_plan(text, chunk_size=chunk_size)
    groups = report.get("procedure", {}).get("groups", [])
    task = report.get("task_record", {})
    coverage = report.get("coverage", {})
    result = {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "adapter": report.get("adapter"),
        "matched": report.get("matched"),
        "semantic_mapping_status": report.get("semantic_mapping_status"),
        "safe_for_internal_index": report.get("safe_for_internal_index"),
        "safe_for_external_publish": report.get("safe_for_external_publish"),
        "publish_scope": report.get("publish_scope"),
        "blockers": report.get("blockers", []),
        "tasks": {
            key: task.get(key)
            for key in (
                "flat_count",
                "grouped_count",
                "group_sizes",
                "exact_record_matches",
                "flat_unmatched",
                "grouped_unmatched",
                "reconciled",
            )
        },
        "procedure": [
            {
                "source_key": item.get("source_key"),
                "role": item.get("role"),
                "step_count": item.get("step_count"),
                "semantic_mapping_status": item.get("semantic_mapping_status"),
            }
            for item in groups
        ],
        "post_execution": report.get("post_execution", {}).get(
            "execution_result", {}
        ),
        "api_envelope": report.get("api_envelope", {}),
        "coverage": {
            key: coverage.get(key)
            for key in (
                "structural_coverage_ratio",
                "structural_node_coverage_ratio",
                "assigned_for_extraction",
                "excluded_api_envelope",
                "reconciled_duplicate_projection",
                "uncovered",
                "nodes_uncovered",
            )
        },
        "unit_count": len(plan.units) if plan else 0,
        "units": (
            [
                {
                    "index": unit.chunk.index,
                    "role": unit.role,
                    "item_count": unit.item_count,
                    "procedure_group": unit.procedure_group,
                    "step_start_index": unit.step_start_index,
                    "step_end_index": unit.step_end_index,
                    "total_steps_in_group": unit.total_steps_in_group,
                    "include_in_rag": unit.include_in_rag,
                    "include_in_generation": unit.include_in_generation,
                }
                for unit in plan.units
            ]
            if plan
            else []
        ),
    }
    result["passed"] = bool(
        result["matched"]
        and result["semantic_mapping_status"] == "CONFIRMED"
        and result["safe_for_internal_index"]
        and not result["safe_for_external_publish"]
        and [item["role"] for item in result["procedure"]] == EXPECTED_ROLES
        and result["tasks"]["reconciled"]
        and result["post_execution"].get("include_in_generation") is False
        and result["api_envelope"].get("include_in_rag") is False
        and result["coverage"]["structural_coverage_ratio"] == 1.0
        and result["coverage"]["structural_node_coverage_ratio"] == 1.0
        and result["coverage"]["uncovered"] == 0
        and result["coverage"]["nodes_uncovered"] == 0
    )
    return result


def evaluate_structure(input_dir: Path, chunk_size: int) -> dict[str, Any]:
    cases = [_structural_case(path, chunk_size) for path in _json_files(input_dir)]
    return {
        "mode": "deterministic_adapter",
        "case_count": len(cases),
        "passed_cases": sum(bool(item["passed"]) for item in cases),
        "all_passed": len(cases) == 5 and all(item["passed"] for item in cases),
        "cases": cases,
    }


def _isolated_settings(base: Settings, root: Path) -> Settings:
    source_dir = root / "knowledge_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    return replace(
        base,
        project_root=root,
        database_path=root / "knowledge.db",
        source_dir=source_dir,
        runtime_database_path=root / "runtime.db",
        max_cards_per_document=30,
        max_model_calls_per_ingest=60,
        max_concurrent_ingestions=1,
    )


def _ingestion_summary(
    service: KnowledgeService, path: Path, result: dict[str, Any], elapsed: float
) -> dict[str, Any]:
    cards = [
        card
        for card_id in result.get("card_ids", [])
        if (card := service.card_detail(card_id)) is not None
    ]
    lineages = [card.get("lineage") or {} for card in cards]
    issue_counts = Counter(
        str(issue) for card in cards for issue in card.get("quality_issues", [])
    )
    expected_source_items = sum(
        int(lineage.get("expected_source_items") or 0) for lineage in lineages
    )
    mapped_source_items = sum(len(card.get("source_items") or []) for card in cards)
    return {
        "file": path.name,
        "elapsed_seconds": round(elapsed, 3),
        "extraction_strategy": result.get("extraction_strategy"),
        "chunks": result.get("chunks"),
        "extracted_cards": result.get("extracted_cards"),
        "cards_by_role": result.get("cards_by_role"),
        "pending_review": result.get("pending_review"),
        "model_calls": result.get("model_calls"),
        "batch_duplicates_skipped": result.get("batch_duplicates_skipped"),
        "card_statuses": {
            status: sum(card.get("status") == status for card in cards)
            for status in ("DRAFT", "PENDING_REVIEW", "APPROVED")
        },
        "quality_score": {
            "minimum": min((card.get("quality_score", 0) for card in cards), default=0),
            "maximum": max((card.get("quality_score", 0) for card in cards), default=0),
            "average": (
                round(
                    sum(card.get("quality_score", 0) for card in cards) / len(cards),
                    2,
                )
                if cards
                else 0
            ),
        },
        "lineage_roles": [lineage.get("unit_role") for lineage in lineages],
        "post_execution_card_ids": [
            int(card["id"])
            for card, lineage in zip(cards, lineages)
            if lineage.get("lifecycle_stage") == "post_execution"
        ],
        "post_execution_excluded_from_generation": all(
            lineage.get("include_in_generation") is False
            for lineage in lineages
            if lineage.get("lifecycle_stage") == "post_execution"
        ),
        "structured_evidence": {
            "cards": sum(
                lineage.get("evidence_mode") == "STRUCTURED_JSON_POINTERS"
                for lineage in lineages
            ),
            "complete_cards": sum(
                lineage.get("content_coverage_status") == "COMPLETE"
                for lineage in lineages
            ),
            "expected_source_items": expected_source_items,
            "mapped_source_items": mapped_source_items,
            "all_complete": bool(cards)
            and all(
                lineage.get("content_coverage_status") == "COMPLETE"
                for lineage in lineages
            )
            and expected_source_items == mapped_source_items,
            "unverified_cards": sum(
                "unverified" in str(card.get("evidence_locator") or "")
                for card in cards
            ),
        },
        "quality_issues": dict(sorted(issue_counts.items())),
    }


def evaluate_ingestion(
    input_dir: Path,
    output_dir: Path,
    *,
    mode: str,
    env_file: Path | None,
) -> dict[str, Any]:
    if mode == "offline":
        base = replace(
            Settings.load(output_dir / "offline.env"),
            api_key="deterministic-offline-demo-key",
            model="deterministic-offline-client",
        )
        client: Any = DeterministicDemoClient()
    else:
        if env_file is None:
            raise ValueError("model 模式必须通过 --env 提供已有模型配置")
        base = Settings.load(env_file)
        if not base.api_configured:
            raise ValueError("--env 中没有可用模型配置")
        client = None

    settings = _isolated_settings(base, output_dir)
    service = KnowledgeService(settings, client=client)
    cases: list[dict[str, Any]] = []
    for path in _json_files(input_dir):
        started = time.perf_counter()
        try:
            result = service.ingest_file(path)
            case = _ingestion_summary(
                service, path, result, time.perf_counter() - started
            )
            case["passed"] = bool(
                case["extraction_strategy"] == "change_order_shape_v2"
                and case["extracted_cards"] >= 1
                and case["post_execution_excluded_from_generation"]
                and case["structured_evidence"]["all_complete"]
                and case["structured_evidence"]["unverified_cards"] == 0
            )
        except Exception as exc:  # noqa: BLE001 - report every case and continue
            case = {
                "file": path.name,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "passed": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        cases.append(case)
    return {
        "mode": mode,
        "model": settings.model,
        "base_url": settings.base_url,
        "database_path": str(settings.database_path),
        "case_count": len(cases),
        "passed_cases": sum(bool(item["passed"]) for item in cases),
        "all_passed": len(cases) == 5 and all(item["passed"] for item in cases),
        "totals": {
            "cards": sum(int(item.get("extracted_cards", 0)) for item in cases),
            "model_calls": sum(int(item.get("model_calls", 0)) for item in cases),
            "pending_review": sum(int(item.get("pending_review", 0)) for item in cases),
            "expected_source_items": sum(
                int(item.get("structured_evidence", {}).get("expected_source_items", 0))
                for item in cases
            ),
            "mapped_source_items": sum(
                int(item.get("structured_evidence", {}).get("mapped_source_items", 0))
                for item in cases
            ),
            "unverified_cards": sum(
                int(item.get("structured_evidence", {}).get("unverified_cards", 0))
                for item in cases
            ),
        },
        "cases": cases,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 五份拟真 ChangeOrder 验证报告",
        "",
        "> 所有输入均为合成脱敏演示数据，不是生产工单。",
        "",
        f"生成时间：{report['generated_at']}",
        "",
    ]
    structural = report.get("structural")
    if structural:
        lines.extend(
            [
                "## 确定性结构验证",
                "",
                f"结果：{structural['passed_cases']} / {structural['case_count']} 通过。",
                "",
                "| 文件 | Task 对账 | 前检/实施/验证/回退 | 单元数 | 结构覆盖 | 结果 |",
                "| --- | --- | --- | ---: | --- | --- |",
            ]
        )
        for case in structural["cases"]:
            task = case["tasks"]
            counts = "/".join(str(item["step_count"]) for item in case["procedure"])
            coverage = case["coverage"]
            lines.append(
                f"| {case['file']} | {task['exact_record_matches']}/{task['flat_count']} | "
                f"{counts} | {case['unit_count']} | "
                f"{coverage['structural_coverage_ratio']}/"
                f"{coverage['structural_node_coverage_ratio']} | "
                f"{'PASS' if case['passed'] else 'FAIL'} |"
            )
        lines.append("")

    for key, heading in (("offline", "离线知识流水线"), ("model", "真实模型抽取")):
        section = report.get(key)
        if not section:
            continue
        lines.extend(
            [
                f"## {heading}",
                "",
                f"结果：{section['passed_cases']} / {section['case_count']} 通过；"
                f"卡片 {section['totals']['cards']} 张，模型调用 "
                f"{section['totals']['model_calls']} 次，待审核 "
                f"{section['totals']['pending_review']} 张；逐源证据 "
                f"{section['totals']['mapped_source_items']}/"
                f"{section['totals']['expected_source_items']}，未验证证据卡 "
                f"{section['totals']['unverified_cards']} 张。",
                "",
                "| 文件 | 卡片 | 角色分布 | 待审核 | 调用 | 用时 | 结果 |",
                "| --- | ---: | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for case in section["cases"]:
            if "error" in case:
                lines.append(
                    f"| {case['file']} | 0 | — | 0 | 0 | "
                    f"{case['elapsed_seconds']}s | FAIL: {case['error_type']} |"
                )
                continue
            roles = ", ".join(
                f"{role}:{count}"
                for role, count in sorted((case.get("cards_by_role") or {}).items())
            )
            lines.append(
                f"| {case['file']} | {case['extracted_cards']} | {roles} | "
                f"{case['pending_review']} | {case['model_calls']} | "
                f"{case['elapsed_seconds']}s | {'PASS' if case['passed'] else 'FAIL'} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env", type=Path)
    parser.add_argument(
        "--mode", choices=("structure", "offline", "model", "all"), default="all"
    )
    parser.add_argument("--chunk-size", type=int, default=12_000)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_classification": "SYNTHETIC_DEIDENTIFIED_DEMO",
        "input_dir": str(args.input_dir.resolve()),
    }
    if args.mode in {"structure", "all"}:
        report["structural"] = evaluate_structure(args.input_dir, args.chunk_size)
    if args.mode in {"offline", "all"}:
        report["offline"] = evaluate_ingestion(
            args.input_dir,
            args.output_dir / "offline",
            mode="offline",
            env_file=None,
        )
    if args.mode in {"model", "all"}:
        report["model"] = evaluate_ingestion(
            args.input_dir,
            args.output_dir / "model",
            mode="model",
            env_file=args.env,
        )

    (args.output_dir / "evaluation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "evaluation_summary.md").write_text(
        _markdown(report), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sections = [report[key] for key in ("structural", "offline", "model") if key in report]
    return 0 if sections and all(section["all_passed"] for section in sections) else 1


if __name__ == "__main__":
    raise SystemExit(main())
