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
        **{f"meta{index:02d}": f"metadata-{index}" for index in range(85)},
        "task_bundle": {
            "flat_view": tasks,
            "grouped_view": {
                "unknown_group_1": grouped_tasks[:1],
                "unknown_group_2": grouped_tasks[1:3],
                "unknown_group_3": grouped_tasks[3:],
            },
        },
        "procedure_container": {
            "unknown_a": procedure_groups[0],
            "rollback": procedure_groups[1],
            "unknown_c": procedure_groups[2],
            "validation": procedure_groups[3],
        },
        "execution_result": {
            **{f"result{index:02d}": f"value-{index}" for index in range(14)},
            "result_items": ["success"],
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


class ChangeOrderExtractionTests(unittest.TestCase):
    def test_adapter_reconciles_task_views_and_preserves_procedure_roles(self):
        text = source_json(make_change_order())
        plan, report = build_change_order_extraction_plan(text, chunk_size=6000)

        self.assertIsNotNone(plan)
        self.assertTrue(report["matched"])
        self.assertTrue(report["safe_to_publish"])
        self.assertEqual(report["coverage"]["coverage_ratio"], 1.0)
        self.assertEqual(report["coverage"]["node_coverage_ratio"], 1.0)
        self.assertEqual(report["coverage"]["uncovered"], 0)
        self.assertEqual(report["coverage"]["nodes_uncovered"], 0)
        self.assertGreater(report["coverage"]["observed_presence"]["NULL"], 0)
        self.assertIsNone(report["coverage"]["observed_presence"]["MISSING"])
        self.assertEqual(report["task_record"]["flat_count"], 4)
        self.assertEqual(report["task_record"]["grouped_count"], 4)
        self.assertEqual(report["task_record"]["exact_record_matches"], 4)
        self.assertEqual(report["task_record"]["group_sizes"], [1, 2, 1])
        self.assertEqual(
            [group["role"] for group in report["procedure"]["groups"]],
            [
                "PROCEDURE_GROUP_A",
                "ROLLBACK_STEPS",
                "PROCEDURE_GROUP_C",
                "VALIDATION_STEPS",
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
            min(unit.chunk.char_start for unit in rollback),
            min(unit.chunk.char_start for unit in validation),
        )

    def test_adapter_flags_task_projection_mismatch_and_keeps_both_views(self):
        text = source_json(make_change_order(grouped_mutation=True))
        plan, report = build_change_order_extraction_plan(text, chunk_size=6000)

        self.assertIsNotNone(plan)
        self.assertTrue(report["matched"])
        self.assertFalse(report["safe_to_publish"])
        self.assertEqual(report["task_record"]["exact_record_matches"], 3)
        self.assertEqual(report["task_record"]["flat_unmatched"], 1)
        self.assertIn(
            "TaskRecord 扁平视图与分组视图未能逐项对齐",
            report["blockers"],
        )
        self.assertTrue(
            any(unit.role == "TASKS_GROUPED_UNRECONCILED" for unit in plan.units)
        )
        self.assertEqual(report["coverage"]["coverage_ratio"], 1.0)

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
            del payload["execution_result"]

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
                content=source_json(make_change_order()),
            )

            self.assertEqual(result["extraction_strategy"], "change_order_shape_v1")
            report = result["extraction_report"]["change_order"]
            self.assertTrue(report["safe_to_publish"])
            self.assertEqual(report["coverage"]["coverage_ratio"], 1.0)
            self.assertEqual(result["extracted_cards"], 1)
            self.assertEqual(result["cards_by_role"], {"ROLLBACK_STEPS": 1})
            card = service.card_detail(result["card_ids"][0])
            self.assertEqual(card["knowledge_type"], "rollback")
            self.assertEqual(card["status"], CardStatus.PENDING_REVIEW.value)
            self.assertEqual(card["lineage"]["unit_role"], "ROLLBACK_STEPS")
            self.assertEqual(
                card["lineage"]["case_id"], result["extraction_report"]["case_id"]
            )
            self.assertTrue(
                card["extraction_report"]["change_order"]["safe_to_publish"]
            )

            persisted = service.store.get_extraction_report(result["document_id"])
            self.assertEqual(persisted["strategy"], "change_order_shape_v1")
            self.assertTrue(persisted["change_order"]["safe_to_publish"])
            self.assertTrue(
                any(
                    "role=ROLLBACK_STEPS" in call[1]
                    for call in client.json_calls
                    if "运维变更知识工程师" in call[0]
                )
            )

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
            self.assertEqual(result["extraction_strategy"], "change_order_shape_v1")

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
                content=source_json(make_change_order()),
            )
            self.assertEqual(result["extracted_cards"], 1)

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
                result["extraction_report"]["change_order"]["safe_to_publish"]
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


if __name__ == "__main__":
    unittest.main()
