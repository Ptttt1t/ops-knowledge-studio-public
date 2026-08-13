from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from knowledge_platform.service import KnowledgeService

from tests.test_change_order_extraction import make_change_order, source_json
from tests.test_platform import make_settings


class MultiCardStructuralClient:
    def __init__(self) -> None:
        self.json_calls: list[tuple[str, str, dict[str, object]]] = []

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.json_calls.append((system_prompt, user_prompt, kwargs))
        if "role=ROLLBACK_STEPS" in user_prompt:
            grounded_quote = '"step00": "step-30-0",'
            base = {
                "knowledge_type": "rollback",
                "scenario": "structured change rollback",
                "object_type": "change_order",
                "object_name": "CHG-REAL-SHAPE-001",
                "risks": ["rollback risk"],
                "validation_steps": ["validate rollback state"],
            }
            return (
                {
                    "knowledge_cards": [
                        {
                            **base,
                            "title": "merged rollback unit",
                            "summary": "first non-empty scalar wins",
                            "applicable_versions": ["v1"],
                            "prerequisites": ["snapshot exists"],
                            "procedure_steps": ["prepare rollback"],
                            "rollback_steps": ["rollback step 1", "shared step"],
                            "keywords": ["rollback", "first"],
                            "evidence_quote": "this quote is not in the source",
                        },
                        {
                            **base,
                            "title": "ignored later title",
                            "summary": "ignored later summary",
                            "applicable_versions": ["v2", "v1"],
                            "prerequisites": ["approval exists"],
                            "procedure_steps": ["prepare rollback", "freeze writes"],
                            "rollback_steps": ["rollback step 2", "shared step"],
                            "risks": ["rollback risk", "secondary risk"],
                            "validation_steps": ["validate rollback state", "validate traffic"],
                            "keywords": ["second", "rollback"],
                            "evidence_quote": grounded_quote,
                        },
                        {
                            **base,
                            "rollback_steps": ["rollback step 3"],
                            "keywords": ["third"],
                            "evidence_quote": '"step00": "step-31-0",',
                        },
                    ]
                },
                {"total_tokens": 30},
            )
        if "role=" in user_prompt:
            return {"knowledge_cards": []}, {"total_tokens": 1}
        return (
            {
                "decision": "NEW",
                "related_card_id": None,
                "confidence": 1.0,
                "reason": "no existing cards",
            },
            {"total_tokens": 1},
        )


class ChangeOrderStructuralMergeTests(unittest.TestCase):
    def test_multiple_model_cards_merge_into_one_without_losing_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                make_settings(root),
                max_document_chunks=100,
                max_change_order_chunks=100,
                max_model_calls_per_ingest=120,
            )
            service = KnowledgeService(settings, client=MultiCardStructuralClient())

            result = service.ingest_text(
                source_name="multi-card-change-order.json",
                source_type="json",
                content=source_json(make_change_order()),
            )

            self.assertEqual(result["extracted_cards"], 7)
            self.assertEqual(result["cards_by_role"]["ROLLBACK_STEPS"], 1)
            card = next(
                service.card_detail(card_id)
                for card_id in result["card_ids"]
                if service.card_detail(card_id)["lineage"]["unit_role"]
                == "ROLLBACK_STEPS"
            )
            self.assertEqual(card["title"], "merged rollback unit")
            self.assertEqual(card["summary"], "first non-empty scalar wins")
            self.assertEqual(card["applicable_versions"], ["v1", "v2"])
            self.assertEqual(
                card["prerequisites"], ["snapshot exists", "approval exists"]
            )
            self.assertEqual(
                card["procedure_steps"], ["prepare rollback", "freeze writes"]
            )
            self.assertEqual(
                card["risks"], ["rollback risk", "secondary risk"]
            )
            self.assertEqual(len(card["rollback_steps"]), 3)
            self.assertTrue(
                all(step.startswith("/data/sop_change_step/change_rollback/") for step in card["rollback_steps"])
            )
            self.assertTrue(all("step00" in step for step in card["rollback_steps"]))
            self.assertEqual(
                card["validation_steps"],
                ["validate rollback state", "validate traffic"],
            )
            self.assertEqual(
                card["keywords"], ["rollback", "first", "second", "third"]
            )
            self.assertIn('"step00": "step-30-0"', card["evidence_quote"])
            self.assertIn("match=structured", card["evidence_locator"])
            self.assertEqual(len(card["source_items"]), 3)


if __name__ == "__main__":
    unittest.main()
