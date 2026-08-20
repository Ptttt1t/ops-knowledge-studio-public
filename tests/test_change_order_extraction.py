from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from knowledge_platform.change_order_adapter import (
    build_change_order_extraction_plan,
)
from knowledge_platform.documents import DocumentError, read_document
from knowledge_platform.schema import CardStatus
from knowledge_platform.service import KnowledgeRequestError, KnowledgeService
from knowledge_platform.store import StoreError

from tests.test_platform import FakeDeepSeekClient, SOURCE_TEXT, make_settings
from tests.test_change_order_card_builder import make_payload as make_semantic_payload


def _record(prefix: str, index: int, fields: int) -> dict[str, object]:
    return {
        f"{prefix}{field:02d}": (
            None if field == fields - 1 and index % 2 else f"{prefix}-{index}-{field}"
        )
        for field in range(fields)
    }


def make_change_order(
    *,
    task_count: int = 4,
    grouped_mutation: bool = False,
) -> dict[str, object]:
    tasks = [_record("task", index, 13) for index in range(task_count)]
    grouped_tasks = [dict(task) for task in tasks]
    if grouped_mutation:
        grouped_tasks[-1]["task00"] = "different-view-value"

    procedure_groups: list[list[dict[str, object]]] = []
    for group_index, count in enumerate((2, 3, 2, 3)):
        procedure_groups.append(
            [_record("step", group_index * 10 + index, 20) for index in range(count)]
        )

    return {
        "code": 0,
        "provider_code": "OK",
        "msg": "success",
        "data": {
            "ticket_id": "CHG-REAL-SHAPE-001",
            "title": "脱敏结构回归变更单",
            "original_system": "internal-change-platform",
            "create_time": "2026-08-12T12:00:00+08:00",
            "cloud_service": "VPC",
            "affected_service": "order-service",
            "change_scene": "route cutover",
            "risk_level": "high",
            "region": "internal-region-1",
            "approval_status": "approved",
            **{f"meta{index:02d}": f"metadata-{index}" for index in range(20)},
            "action_list": tasks,
            "change_tool_relate_action": {
                "unknown_group_1": grouped_tasks[:1],
                "unknown_group_2": grouped_tasks[1:3],
                "unknown_group_3": grouped_tasks[3:],
            },
            "sop_change_step": {
                "check_before_change": procedure_groups[0],
                "change_implement": procedure_groups[1],
                "change_verified": procedure_groups[2],
                "change_rollback": procedure_groups[3],
            },
            "change_plan": [
                {
                    "result": {
                        **{
                            f"result{index:02d}": f"value-{index}"
                            for index in range(14)
                        },
                        "result_items": ["success"],
                    }
                }
            ],
        },
    }


def source_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


class StructuralFakeClient(FakeDeepSeekClient):
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if "运维变更知识工程师" in system_prompt:
            self.json_calls.append((system_prompt, user_prompt, kwargs))
            source = user_prompt.split("\n\n", 1)[-1]
            quote = next(
                (
                    line.strip()
                    for line in source.splitlines()
                    if line.strip() and len(line.strip()) >= 20
                ),
                source[:100],
            )
            if "role=ROLLBACK_STEPS" in user_prompt:
                card = {
                    "title": "变更回退步骤",
                    "summary": "按源顺序保留回退步骤。",
                    "knowledge_type": "rollback",
                    "scenario": "结构化变更单回退",
                    "object_type": "变更单",
                    "object_name": "匿名变更对象",
                    "applicable_versions": [],
                    "prerequisites": [],
                    "procedure_steps": [],
                    "risks": ["需人工核对真实字段语义"],
                    "rollback_steps": ["保持源数组顺序执行"],
                    "validation_steps": ["确认回退结果"],
                    "keywords": ["rollback"],
                    "evidence_quote": quote,
                }
                return {"knowledge_cards": [card]}, {"total_tokens": 20}
            return {"knowledge_cards": []}, {"total_tokens": 5}
        return super().chat_json(system_prompt, user_prompt, **kwargs)


class OverproducingStructuralClient(StructuralFakeClient):
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        payload, usage = super().chat_json(system_prompt, user_prompt, **kwargs)
        if (
            "运维变更知识工程师" in system_prompt
            and payload.get("knowledge_cards")
        ):
            payload["knowledge_cards"] = payload["knowledge_cards"] * 3
        return payload, usage


class ExecutionAwareStructuralClient(StructuralFakeClient):
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if (
            "运维变更知识工程师" in system_prompt
            and "role=EXECUTION_RESULT" in user_prompt
        ):
            self.json_calls.append((system_prompt, user_prompt, kwargs))
            source = user_prompt.split("\n\n", 1)[-1]
            quote = next(
                line.strip()
                for line in source.splitlines()
                if line.strip() and len(line.strip()) >= 20
            )
            return (
                {
                    "knowledge_cards": [
                        {
                            "title": "execution result success evidence",
                            "summary": "execution result success is post execution evidence",
                            "knowledge_type": "case",
                            "scenario": "execution result analysis",
                            "object_type": "change_order",
                            "object_name": "execution-result-success",
                            "applicable_versions": [],
                            "prerequisites": [],
                            "procedure_steps": [],
                            "risks": [],
                            "rollback_steps": [],
                            "validation_steps": ["execution result success"],
                            "keywords": ["execution", "result", "success"],
                            "evidence_quote": quote,
                        }
                    ]
                },
                {"total_tokens": 20},
            )
        return super().chat_json(system_prompt, user_prompt, **kwargs)


class ChangeOrderExtractionTests(unittest.TestCase):
    def test_grouped_task_projection_accepts_dynamic_group_counts(self):
        for group_count in (4, 5, 8, 9):
            with self.subTest(group_count=group_count):
                payload = make_change_order(task_count=group_count)
                data = payload["data"]
                assert isinstance(data, dict)
                tasks = data["action_list"]
                assert isinstance(tasks, list)
                data["change_tool_relate_action"] = {
                    f"dynamic_group_{index + 1}": [dict(tasks[index])]
                    for index in range(group_count)
                }

                plan, report = build_change_order_extraction_plan(
                    source_json(payload),
                    chunk_size=6000,
                )

                self.assertIsNotNone(plan)
                self.assertTrue(report["matched"])
                self.assertEqual(report["semantic_mapping_status"], "CONFIRMED")
                self.assertTrue(report["safe_for_internal_index"])
                self.assertEqual(report["blockers"], [])
                self.assertEqual(
                    report["task_record"]["group_sizes"],
                    [1] * group_count,
                )
                self.assertEqual(
                    report["task_record"]["exact_record_matches"],
                    group_count,
                )
                self.assertTrue(report["task_record"]["reconciled"])

    def test_adapter_reconciles_task_views_and_preserves_procedure_roles(self):
        text = source_json(make_change_order())
        plan, report = build_change_order_extraction_plan(text, chunk_size=6000)

        self.assertIsNotNone(plan)
        self.assertTrue(report["matched"])
        self.assertTrue(report["safe_for_internal_index"])
        self.assertFalse(report["safe_for_external_publish"])
        self.assertEqual(report["publish_scope"], "INTERNAL_ONLY")
        self.assertEqual(report["semantic_mapping_status"], "CONFIRMED")
        self.assertEqual(report["coverage"]["structural_coverage_ratio"], 1.0)
        self.assertEqual(
            report["coverage"]["structural_node_coverage_ratio"], 1.0
        )
        self.assertEqual(report["coverage"]["uncovered"], 0)
        self.assertEqual(report["coverage"]["nodes_uncovered"], 0)
        self.assertEqual(report["coverage"]["excluded_api_envelope"], 3)
        self.assertEqual(report["coverage"]["nodes_excluded_api_envelope"], 3)
        self.assertGreater(report["coverage"]["observed_presence"]["NULL"], 0)
        self.assertIsNone(report["coverage"]["observed_presence"]["MISSING"])
        self.assertEqual(report["task_record"]["flat_count"], 4)
        self.assertEqual(report["task_record"]["grouped_count"], 4)
        self.assertEqual(report["task_record"]["exact_record_matches"], 4)
        self.assertEqual(report["task_record"]["group_sizes"], [1, 2, 1])
        self.assertEqual(
            [group["role"] for group in report["procedure"]["groups"]],
            [
                "PRECHECK_STEPS",
                "IMPLEMENTATION_STEPS",
                "VALIDATION_STEPS",
                "ROLLBACK_STEPS",
            ],
        )
        self.assertEqual(
            [group["step_count"] for group in report["procedure"]["groups"]],
            [2, 3, 2, 3],
        )

        units = list(plan.units)
        self.assertFalse(any("TASKS_GROUPED" in unit.role for unit in units))
        rollback = [unit for unit in units if unit.role == "ROLLBACK_STEPS"]
        validation = [unit for unit in units if unit.role == "VALIDATION_STEPS"]
        self.assertTrue(rollback)
        self.assertTrue(validation)
        self.assertLess(
            min(unit.chunk.char_start for unit in validation),
            min(unit.chunk.char_start for unit in rollback),
        )
        precheck = next(unit for unit in units if unit.role == "PRECHECK_STEPS")
        self.assertEqual(precheck.procedure_group, "PRECHECK")
        self.assertEqual(precheck.step_start_index, 0)
        self.assertEqual(precheck.step_end_index, 0)
        self.assertEqual(precheck.total_steps_in_group, 2)
        self.assertEqual(
            [unit.step_start_index for unit in units if unit.role == "PRECHECK_STEPS"],
            [0, 1],
        )
        execution = next(unit for unit in units if unit.role == "EXECUTION_RESULT")
        self.assertEqual(execution.lifecycle_stage, "post_execution")
        self.assertFalse(execution.include_in_generation)
        self.assertEqual(report["api_envelope"]["role"], "API_ENVELOPE")
        self.assertFalse(report["api_envelope"]["include_in_rag"])
        context_roles = {
            item["path"]: item["role"] for item in report["context_classification"]
        }
        self.assertEqual(context_roles["/data/ticket_id"], "IDENTITY")
        self.assertEqual(context_roles["/data/cloud_service"], "SERVICE_SCOPE")
        self.assertEqual(context_roles["/data/change_scene"], "CHANGE_CONTEXT")
        self.assertEqual(context_roles["/data/risk_level"], "RISK_IMPACT")
        self.assertEqual(context_roles["/data/region"], "EXECUTION_CONTEXT")
        self.assertEqual(context_roles["/data/approval_status"], "GOVERNANCE_CONTEXT")

    def test_adapter_flags_task_projection_mismatch_and_keeps_both_views(self):
        text = source_json(make_change_order(grouped_mutation=True))
        plan, report = build_change_order_extraction_plan(text, chunk_size=6000)

        self.assertIsNotNone(plan)
        self.assertTrue(report["matched"])
        self.assertFalse(report["safe_for_internal_index"])
        self.assertEqual(report["task_record"]["exact_record_matches"], 3)
        self.assertEqual(report["task_record"]["flat_unmatched"], 1)
        self.assertIn(
            "TaskRecord 扁平视图与分组视图未能逐项对齐",
            report["blockers"],
        )
        self.assertTrue(
            any(unit.role == "TASKS_GROUPED_UNRECONCILED" for unit in plan.units)
        )
        self.assertEqual(report["coverage"]["structural_coverage_ratio"], 1.0)

    def test_non_matching_json_falls_back_without_guessing(self):
        text = source_json({"title": "普通 JSON", "items": [{"a": 1}]})
        plan, report = build_change_order_extraction_plan(text, chunk_size=6000)

        self.assertIsNone(plan)
        self.assertFalse(report["matched"])
        self.assertTrue(report["blockers"])

    def test_unrelated_json_keeps_generic_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = KnowledgeService(
                make_settings(root), client=FakeDeepSeekClient()
            )
            result = service.ingest_text(
                source_name="generic.json",
                source_type="json",
                content=source_json({"content": SOURCE_TEXT}),
            )

            self.assertEqual(result["extraction_strategy"], "generic_text_v1")
            card_id = result["card_ids"][0]
            self.assertEqual(
                service.card_detail(card_id)["status"],
                CardStatus.PENDING_REVIEW.value,
            )
            approved = service.review(
                card_id,
                action="approve",
                reviewer="tester",
                comment="普通 JSON 仍按原流程审核",
            )
            self.assertEqual(approved["status"], CardStatus.APPROVED.value)

    def test_near_match_schema_drift_is_blocked_before_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = StructuralFakeClient()
            service = KnowledgeService(make_settings(root), client=client)
            payload = make_change_order()
            del payload["data"]["change_plan"]

            with self.assertRaises(KnowledgeRequestError) as captured:
                service.ingest_text(
                    source_name="drifted.json",
                    source_type="json",
                    content=source_json(payload),
                )

            self.assertEqual(captured.exception.code, "change_order_schema_ambiguous")
            self.assertEqual(client.json_calls, [])

    def test_duplicate_json_keys_are_not_structurally_adapted(self):
        plan, report = build_change_order_extraction_plan(
            '{"same": 1, "same": 2}', chunk_size=6000
        )

        self.assertIsNone(plan)
        self.assertIn("重复 Key", report["reason"])

    def test_json_file_preserves_source_and_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.json"
            original = '{\n  "a": 1,\n  "b": null\n}'
            valid.write_bytes(original.encode("utf-8"))
            self.assertEqual(read_document(valid).content, original)

            duplicate = root / "duplicate.json"
            duplicate.write_bytes(b'{"same": 1, "same": 2}')
            with self.assertRaises(DocumentError):
                read_document(duplicate)

    def test_service_uses_structural_strategy_and_persists_coverage_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = StructuralFakeClient()
            settings = replace(
                make_settings(root),
                max_document_chunks=100,
                max_model_calls_per_ingest=120,
            )
            service = KnowledgeService(settings, client=client)

            result = service.ingest_text(
                source_name="真实变更单脱敏结构.json",
                source_ref="ticket://sanitized-change-order",
                source_type="json",
                content=source_json(make_semantic_payload()),
            )

            self.assertEqual(result["extraction_strategy"], "change_order_shape_v2")
            report = result["extraction_report"]["change_order"]
            self.assertTrue(report["safe_for_internal_index"])
            self.assertFalse(report["safe_for_external_publish"])
            self.assertEqual(
                report["coverage"]["structural_coverage_ratio"], 1.0
            )
            self.assertEqual(result["extracted_cards"], 6)
            self.assertEqual(
                set(result["cards_by_role"]),
                {
                    "CASE_CONTEXT",
                    "PROCEDURE_STEP",
                    "EXECUTION_OUTCOME",
                },
            )
            self.assertEqual(
                result["extraction_report"]["structural_source_coverage"]["status"],
                "COMPLETE",
            )
            card = next(
                service.card_detail(card_id)
                for card_id in result["card_ids"]
                if service.card_detail(card_id)["lineage"]["unit_role"]
                == "PROCEDURE_STEP"
                and service.card_detail(card_id)["lineage"]["procedure_group"]
                == "ROLLBACK"
            )
            self.assertEqual(card["knowledge_type"], "procedure_step")
            self.assertEqual(card["card_type"], "PROCEDURE_STEP")
            self.assertEqual(card["status"], CardStatus.PENDING_REVIEW.value)
            self.assertEqual(card["lineage"]["unit_role"], "PROCEDURE_STEP")
            self.assertEqual(
                card["lineage"]["case_id"], result["extraction_report"]["case_id"]
            )
            self.assertTrue(
                card["extraction_report"]["change_order"][
                    "safe_for_internal_index"
                ]
            )
            self.assertEqual(card["lineage"]["procedure_group"], "ROLLBACK")
            self.assertEqual(card["lineage"]["semantic_mapping_status"], "CONFIRMED")
            self.assertEqual(
                card["lineage"]["structural_source_coverage_status"], "COMPLETE"
            )
            self.assertEqual(card["lineage"]["evidence_mode"], "STRUCTURED_JSON_POINTERS")
            self.assertEqual(len(card["source_items"]), 1)
            self.assertTrue(all(item["source_hash"] for item in card["source_items"]))

            persisted = service.store.get_extraction_report(result["document_id"])
            self.assertEqual(persisted["strategy"], "change_order_shape_v2")
            self.assertTrue(persisted["change_order"]["safe_for_internal_index"])
            self.assertEqual(client.json_calls, [])
            approved = service.review(
                card["id"],
                action="approve",
                reviewer="internal-demo-reviewer",
                comment="内部索引验证通过；不代表允许外部发布",
            )
            self.assertEqual(approved["status"], CardStatus.APPROVED.value)

    def test_large_json_requires_known_structure_and_uses_separate_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = StructuralFakeClient()
            settings = replace(
                make_settings(root),
                max_text_chars=1000,
                max_change_order_json_chars=200_000,
                max_change_order_chunks=100,
                change_order_chunk_size=12_000,
                max_model_calls_per_ingest=120,
            )
            service = KnowledgeService(settings, client=client)
            structured = source_json(make_change_order(task_count=10))
            self.assertGreater(len(structured), settings.max_text_chars)
            result = service.ingest_text(
                source_name="large.json",
                source_type="json",
                source_ref="ticket://large-known",
                content=structured,
            )
            self.assertEqual(result["extraction_strategy"], "change_order_shape_v2")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = StructuralFakeClient()
            settings = replace(
                make_settings(root),
                max_text_chars=100,
                max_change_order_json_chars=20_000,
            )
            service = KnowledgeService(settings, client=client)
            with self.assertRaises(KnowledgeRequestError) as captured:
                service.ingest_text(
                    source_name="large-unknown.json",
                    source_type="json",
                    content=source_json({"unknown": ["x" * 1000]}),
                )
            self.assertEqual(captured.exception.code, "large_json_schema_not_recognized")
            self.assertEqual(client.json_calls, [])

    def test_structural_unit_hard_limits_model_to_one_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                make_settings(root),
                max_document_chunks=100,
                max_change_order_chunks=100,
                max_model_calls_per_ingest=120,
            )
            service = KnowledgeService(
                settings, client=OverproducingStructuralClient()
            )
            result = service.ingest_text(
                source_name="overproducing.json",
                source_type="json",
                content=source_json(make_semantic_payload()),
            )
            self.assertEqual(result["extracted_cards"], 6)
            self.assertEqual(result["cards_by_role"]["PROCEDURE_STEP"], 4)
            self.assertEqual(result["model_calls"], 0)

    def test_unreconciled_structure_blocks_card_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                make_settings(root),
                max_document_chunks=100,
                max_model_calls_per_ingest=120,
            )
            service = KnowledgeService(settings, client=StructuralFakeClient())
            result = service.ingest_text(
                source_name="对账失败变更单.json",
                source_ref="ticket://unreconciled",
                source_type="json",
                content=source_json(make_change_order(grouped_mutation=True)),
            )

            self.assertFalse(
                result["extraction_report"]["change_order"][
                    "safe_for_internal_index"
                ]
            )
            self.assertEqual(result["pending_review"], 0)
            card = service.card_detail(result["card_ids"][0])
            self.assertEqual(card["status"], CardStatus.DRAFT.value)
            self.assertTrue(
                any(str(issue).startswith("阻断：") for issue in card["quality_issues"])
            )
            with service.store.connect() as connection:
                connection.execute(
                    "UPDATE cards SET quality_issues = '[]', quality_score = 100 WHERE id = ?",
                    (card["id"],),
                )
            with self.assertRaises(StoreError):
                service.review(
                    card["id"],
                    action="approve",
                    reviewer="tester",
                    comment="不应越过结构阻断",
                )

    def test_post_execution_is_searchable_but_excluded_from_plan_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                make_settings(root),
                max_document_chunks=100,
                max_change_order_chunks=100,
                max_model_calls_per_ingest=120,
            )
            service = KnowledgeService(
                settings, client=ExecutionAwareStructuralClient()
            )
            result = service.ingest_text(
                source_name="post-execution.json",
                source_type="json",
                content=source_json(make_semantic_payload()),
            )
            execution_id = next(
                card_id
                for card_id in result["card_ids"]
                if service.card_detail(card_id)["lineage"]["unit_role"]
                == "EXECUTION_OUTCOME"
            )
            service.review(
                execution_id,
                action="approve",
                reviewer="internal-demo-reviewer",
                comment="历史结果可用于经验检索",
            )

            searchable, _ = service.trusted_search_hits(
                "SUCCESS 虚构服务验证通过", top_k=10, for_generation=False
            )
            self.assertIn(execution_id, [int(hit.card["id"]) for hit in searchable])
            planning, diagnostics = service.trusted_search_hits(
                "SUCCESS 虚构服务验证通过", top_k=10, for_generation=True
            )
            self.assertNotIn(execution_id, [int(hit.card["id"]) for hit in planning])
            self.assertTrue(
                any(
                    item.get("card_id") == execution_id
                    and item.get("reason")
                    == "POST_EXECUTION_EXCLUDED_FROM_GENERATION"
                    for item in diagnostics.get("lexical_rejected", [])
                )
            )


if __name__ == "__main__":
    unittest.main()
