from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from knowledge_platform.change_order_adapter import (
    build_change_order_extraction_plan,
)
from knowledge_platform.schema import CardStatus
from knowledge_platform.service import KnowledgeService
from knowledge_platform.store import StoreError

from tests.test_change_order_extraction import (
    StructuralFakeClient,
    make_change_order,
    source_json,
)
from tests.test_platform import make_settings
from tests.test_change_order_card_builder import make_payload as make_semantic_payload


LIST_FIELD_BY_ROLE = {
    "TASKS_CANONICAL": "procedure_steps",
    "PRECHECK_STEPS": "procedure_steps",
    "IMPLEMENTATION_STEPS": "procedure_steps",
    "VALIDATION_STEPS": "validation_steps",
    "ROLLBACK_STEPS": "rollback_steps",
}


def make_service(root: Path) -> KnowledgeService:
    settings = replace(
        make_settings(root),
        max_document_chunks=100,
        max_change_order_chunks=100,
        max_model_calls_per_ingest=120,
    )
    return KnowledgeService(settings, client=StructuralFakeClient())


class ChangeOrderStructuredEvidenceTests(unittest.TestCase):
    def test_legacy_database_initialization_adds_source_matrix_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(Path(directory))
            with service.store.connect() as connection:
                connection.execute("DROP TABLE card_source_items")

            service.store.initialize()
            service.store.initialize()

            with service.store.connect() as connection:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(card_source_items)"
                    ).fetchall()
                }
            self.assertEqual(
                columns,
                {
                    "card_id",
                    "output_field",
                    "output_index",
                    "source_index",
                    "source_pointer",
                    "source_hash",
                    "char_start",
                    "char_end",
                    "created_at",
                },
            )

    def test_adapter_evidence_spans_are_exact_and_hash_reproducible(self) -> None:
        text = source_json(make_change_order())
        plan, report = build_change_order_extraction_plan(text, chunk_size=6000)

        self.assertTrue(report["matched"])
        self.assertIsNotNone(plan)
        assert plan is not None
        for unit in plan.units:
            with self.subTest(role=unit.role, pointer=unit.pointer):
                self.assertEqual(len(unit.source_evidence_refs), unit.item_count)
                self.assertEqual(
                    [item.pointer for item in unit.source_evidence_refs],
                    list(unit.source_pointers),
                )
                for reference in unit.source_evidence_refs:
                    fragment = text[reference.char_start : reference.char_end]
                    self.assertTrue(fragment)
                    self.assertEqual(
                        hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
                        reference.content_sha256,
                    )

    def test_service_persists_complete_source_matrix_and_regrades_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(Path(directory))
            result = service.ingest_text(
                source_name="evidence-matrix.json",
                source_type="json",
                content=source_json(make_semantic_payload()),
            )

            coverage = result["extraction_report"]["structural_source_coverage"]
            self.assertEqual(coverage["status"], "COMPLETE")
            self.assertEqual(
                coverage["expected_source_items"], coverage["accounted_source_items"]
            )

            for card_id in result["card_ids"]:
                card = service.card_detail(card_id)
                self.assertIsNotNone(card)
                assert card is not None
                lineage = card["lineage"]
                self.assertEqual(
                    lineage["quality_policy_version"], "change_order_semantic_v1"
                )
                self.assertEqual(
                    lineage["evidence_mode"], "STRUCTURED_JSON_POINTERS"
                )
                self.assertEqual(
                    lineage["structural_source_coverage_status"], "COMPLETE"
                )
                self.assertEqual(
                    len(card["source_items"]), lineage["expected_source_items"]
                )
                self.assertIn("match=structured", card["evidence_locator"])
                self.assertTrue(card["semantic_fingerprint"])

            regraded = service.regrade_existing_cards()
            self.assertEqual(regraded["processed"], len(result["card_ids"]))
            self.assertEqual(regraded["grounded"], len(result["card_ids"]))
            for card_id in result["card_ids"]:
                self.assertIn(
                    "match=structured",
                    service.card_detail(card_id)["evidence_locator"],
                )
                self.assertEqual(
                    service.card_detail(card_id)["quality_score"],
                    service.card_detail(card_id)["content_quality"],
                )

    def test_approval_rejects_incomplete_or_tampered_source_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(Path(directory))
            result = service.ingest_text(
                source_name="approval-matrix.json",
                source_type="json",
                content=source_json(make_semantic_payload()),
            )
            cards = [service.card_detail(card_id) for card_id in result["card_ids"]]
            task = next(
                card for card in cards if card["lineage"]["unit_role"] == "CASE_CONTEXT"
            )
            rollback = next(
                card
                for card in cards
                if card["lineage"]["unit_role"] == "PROCEDURE_STEP"
                and card["lineage"]["procedure_group"] == "ROLLBACK"
            )
            execution = next(
                card for card in cards if card["lineage"]["unit_role"] == "EXECUTION_OUTCOME"
            )
            precheck = next(
                card
                for card in cards
                if card["lineage"]["unit_role"] == "PROCEDURE_STEP"
                and card["lineage"]["procedure_group"] == "PRECHECK"
            )
            self.assertEqual(task["status"], CardStatus.PENDING_REVIEW.value)
            self.assertEqual(rollback["status"], CardStatus.PENDING_REVIEW.value)

            with service.store.connect() as connection:
                row = connection.execute(
                    "SELECT unit_metadata FROM card_lineage WHERE card_id = ?",
                    (task["id"],),
                ).fetchone()
                metadata = json.loads(row["unit_metadata"])
                metadata["structural_source_coverage_status"] = "INCOMPLETE"
                connection.execute(
                    "UPDATE card_lineage SET unit_metadata = ? WHERE card_id = ?",
                    (json.dumps(metadata, ensure_ascii=False), task["id"]),
                )
            with self.assertRaisesRegex(StoreError, "覆盖不完整"):
                service.review(
                    task["id"],
                    action="approve",
                    reviewer="evidence-reviewer",
                )

            with service.store.connect() as connection:
                connection.execute(
                    "UPDATE card_source_items SET source_hash = ? "
                    "WHERE card_id = ? AND output_index = 0",
                    ("0" * 64, rollback["id"]),
                )
            with self.assertRaisesRegex(StoreError, "映射不一致"):
                service.review(
                    rollback["id"],
                    action="approve",
                    reviewer="evidence-reviewer",
                )

            with service.store.connect() as connection:
                connection.execute(
                    "DELETE FROM card_lineage WHERE card_id = ?", (precheck["id"],)
                )
            with self.assertRaisesRegex(StoreError, "尚未完成 lineage"):
                service.review(
                    precheck["id"],
                    action="approve",
                    reviewer="evidence-reviewer",
                )

            first = execution["source_items"][0]
            with service.store.connect() as connection:
                row = connection.execute(
                    "SELECT content FROM documents WHERE id = ?",
                    (execution["source_document_id"],),
                ).fetchone()
                content = row["content"]
                position = int(first["char_start"])
                replacement = "X" if content[position] != "X" else "Y"
                drifted = content[:position] + replacement + content[position + 1 :]
                connection.execute(
                    "UPDATE documents SET content = ? WHERE id = ?",
                    (drifted, execution["source_document_id"]),
                )
            with self.assertRaisesRegex(StoreError, "来源内容哈希已漂移"):
                service.review(
                    execution["id"],
                    action="approve",
                    reviewer="evidence-reviewer",
                )

    def test_deleting_card_cascades_source_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(Path(directory))
            result = service.ingest_text(
                source_name="delete-matrix.json",
                source_type="json",
                content=source_json(make_semantic_payload()),
            )
            card_id = result["card_ids"][0]
            self.assertTrue(service.store.list_card_source_items(card_id))

            service.delete_card(card_id, actor="demo-operator")

            self.assertEqual(service.store.list_card_source_items(card_id), [])


if __name__ == "__main__":
    unittest.main()
