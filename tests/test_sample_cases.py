from pathlib import Path
import tempfile
import unittest

from knowledge_platform.service import KnowledgeService
from tests.test_platform import make_settings


class NoModelClient:
    def chat_json(self, *args, **kwargs):
        raise AssertionError("Structured samples must use deterministic extraction")


class SampleCasesTests(unittest.TestCase):
    def test_samples_import_as_distinct_reviewable_bundles_without_result_reuse(self):
        sample_dir = Path(__file__).resolve().parents[1] / "tests/fixtures/structure_samples"
        with tempfile.TemporaryDirectory() as directory:
            service = KnowledgeService(make_settings(Path(directory)), client=NoModelClient())
            case_ids = set()
            for path in sorted(sample_dir.glob("*.json")):
                result = service.ingest_file(path)
                case_ids.add(result["case_id"])
                self.assertEqual(result["cards_by_role"], {
                    "CASE_CONTEXT": 1, "PROCEDURE_STEP": 4, "EXECUTION_OUTCOME": 1,
                })
                self.assertEqual(result["model_calls"], 0)
                cards = [service.card_detail(card_id) for card_id in result["card_ids"]]
                self.assertTrue(all(card["status"] == "PENDING_REVIEW" for card in cards))
                steps = [card for card in cards if card["card_type"] == "PROCEDURE_STEP"]
                self.assertEqual(len(steps), 4)
                for step in steps:
                    self.assertTrue(step["actions"])
                    self.assertNotIn("SYNTH_OK", step["summary"])
                    self.assertNotIn("合成历史核验说明", str(step["procedure_steps"]))
                outcome = next(card for card in cards if card["card_type"] == "EXECUTION_OUTCOME")
                self.assertFalse(outcome["lineage"]["planning_rag_enabled"])
                self.assertFalse(outcome["lineage"]["include_in_generation"])
                repeated = service.ingest_file(path)
                self.assertTrue(repeated["duplicate_document"])
                self.assertEqual(repeated["card_ids"], result["card_ids"])
            self.assertEqual(len(case_ids), 4)


if __name__ == "__main__":
    unittest.main()
