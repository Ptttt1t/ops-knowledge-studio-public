from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from knowledge_platform.retrieval import SearchHit
from knowledge_platform.schema import CardStatus
from knowledge_platform.service import KnowledgeService
from knowledge_platform.store import StoreError
from knowledge_platform.web import create_server
from tests.test_change_order_extraction import (
    StructuralFakeClient,
    make_change_order,
    source_json,
)
from tests.test_platform import make_settings


class QueuedAnswerClient:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system_prompt: str, user_prompt: str, **_kwargs):
        self.calls.append((system_prompt, user_prompt))
        return self.payloads.pop(0), {"total_tokens": 10}


def approved_card(*, procedure_steps: list[str]) -> dict:
    return {
        "id": 1,
        "title": "受信任变更步骤",
        "summary": "按已审核流程实施变更。",
        "knowledge_type": "procedure",
        "scenario": "网络变更",
        "object_type": "设备",
        "object_name": "NE-A",
        "applicable_versions": ["V1"],
        "prerequisites": ["变更窗口已批准"],
        "procedure_steps": procedure_steps,
        "risks": ["业务可能短时抖动"],
        "rollback_steps": ["恢复变更前配置"],
        "validation_steps": ["确认业务恢复"],
        "keywords": ["NE-A"],
        "source_ref": "ticket://trusted-case",
        "evidence_locator": "trusted.json#pointer=/steps",
        "evidence_quote": "执行已审核步骤",
        "status": CardStatus.APPROVED.value,
    }


class CaseBundleTests(unittest.TestCase):
    def make_service(self, root: Path) -> KnowledgeService:
        return KnowledgeService(
            make_settings(root),
            client=StructuralFakeClient(),
        )

    def test_change_order_is_exposed_as_one_ordered_case_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            result = service.ingest_text(
                source_name="完整变更单.json",
                source_ref="ticket://bundle-001",
                source_type="json",
                content=source_json(make_change_order()),
            )

            summary = result["case_bundle"]
            self.assertIsNotNone(summary)
            self.assertEqual(summary["case_id"], result["case_id"])
            self.assertEqual(
                summary["title"], "脱敏结构回归变更单 · CHG-REAL-SHAPE-001"
            )
            self.assertEqual(summary["card_count"], result["extracted_cards"])
            self.assertEqual(summary["status"], CardStatus.PENDING_REVIEW.value)
            self.assertEqual(service.stats()["case_bundles"], 1)

            listed = service.list_case_bundles()
            self.assertEqual([item["case_id"] for item in listed], [result["case_id"]])
            detail = service.case_bundle_detail(result["case_id"])
            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(
                [card["id"] for card in detail["cards"]], detail["card_ids"]
            )
            self.assertEqual(
                [card["lineage"]["source_order"] for card in detail["cards"]],
                sorted(card["lineage"]["source_order"] for card in detail["cards"]),
            )

            with service.store.connect() as connection:
                connection.execute("DROP TABLE change_case_bundles")
            service.store.initialize()
            migrated = service.case_bundle_detail(result["case_id"])
            self.assertIsNotNone(migrated)
            assert migrated is not None
            self.assertEqual(migrated["card_ids"], result["card_ids"])

    def test_case_bundle_approval_is_atomic_and_uses_existing_evidence_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            result = service.ingest_text(
                source_name="整包审核.json",
                source_ref="ticket://bundle-review",
                source_type="json",
                content=source_json(make_change_order()),
            )
            detail = service.case_bundle_detail(result["case_id"])
            assert detail is not None
            tampered_id = detail["cards"][0]["id"]
            with service.store.connect() as connection:
                connection.execute(
                    "UPDATE card_source_items SET source_hash = ? "
                    "WHERE card_id = ? AND output_index = 0",
                    ("0" * 64, tampered_id),
                )

            with self.assertRaisesRegex(StoreError, "映射不一致"):
                service.review_case_bundle(
                    result["case_id"],
                    action="approve",
                    reviewer="bundle-reviewer",
                )
            unchanged = service.case_bundle_detail(result["case_id"])
            assert unchanged is not None
            self.assertEqual(unchanged["status"], CardStatus.PENDING_REVIEW.value)
            self.assertTrue(
                all(
                    card["status"]
                    in {CardStatus.DRAFT.value, CardStatus.PENDING_REVIEW.value}
                    for card in unchanged["cards"]
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            result = service.ingest_text(
                source_name="整包批准.json",
                source_ref="ticket://bundle-approve",
                source_type="json",
                content=source_json(make_change_order()),
            )
            first_card_id = result["card_ids"][0]
            service.review(
                first_card_id,
                action="approve",
                reviewer="bundle-reviewer",
                comment="先前已核对这一张",
            )
            self.assertEqual(
                service.case_bundle_detail(result["case_id"])["status"], "PARTIAL"
            )
            approved = service.review_case_bundle(
                result["case_id"],
                action="approve",
                reviewer="bundle-reviewer",
                comment="完整案例证据已核对",
            )
            self.assertEqual(approved["status"], CardStatus.APPROVED.value)
            self.assertTrue(
                all(card["status"] == CardStatus.APPROVED.value for card in approved["cards"])
            )

    def test_case_bundle_http_endpoints_return_and_review_the_whole_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            result = service.ingest_text(
                source_name="接口案例包.json",
                source_ref="ticket://bundle-api",
                source_type="json",
                content=source_json(make_change_order()),
            )
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            encoded_case_id = result["case_id"].replace(":", "%3A")
            try:
                with urlopen(f"{base}/api/knowledge-case-bundles", timeout=10) as response:
                    listed = json.loads(response.read().decode("utf-8"))
                self.assertEqual(listed["case_bundles"][0]["card_count"], 7)

                request = Request(
                    f"{base}/api/knowledge-case-bundles/{encoded_case_id}/review",
                    data=json.dumps({"action": "reject", "comment": "测试整包驳回"}).encode(
                        "utf-8"
                    ),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=10) as response:
                    reviewed = json.loads(response.read().decode("utf-8"))
                self.assertEqual(reviewed["status"], CardStatus.REJECTED.value)
                self.assertEqual(reviewed["status_counts"], {"REJECTED": 7})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class AnswerContractTests(unittest.TestCase):
    def test_scalar_zero_is_normalized_to_null_without_weakening_card_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = QueuedAnswerClient(
                [
                    {
                        "claims": [
                            {
                                "category": "结论",
                                "card_id": 1,
                                "support_field": "summary",
                                "support_index": 0,
                            }
                        ]
                    }
                ]
            )
            service = KnowledgeService(make_settings(Path(directory)), client=client)
            card = approved_card(procedure_steps=["执行步骤 1"])
            result = service._answer_from_hits(
                "如何实施？",
                [SearchHit(card=card, score=20.0, matched_terms=["实施"], query_coverage=1.0)],
            )

            self.assertEqual(result["claims"][0]["support"]["index"], None)
            self.assertEqual(result["answer_contract"]["retries"], 0)
            self.assertEqual(len(client.calls), 1)

    def test_more_than_thirty_unique_claims_trigger_one_bounded_correction_retry(self) -> None:
        steps = [f"执行步骤 {index}" for index in range(31)]
        oversized = {
            "claims": [
                {
                    "category": "执行步骤",
                    "card_id": 1,
                    "support_field": "procedure_steps",
                    "support_index": index,
                }
                for index in range(31)
            ]
        }
        corrected = {
            "claims": [
                {
                    "category": "执行步骤",
                    "card_id": 1,
                    "support_field": "procedure_steps",
                    "support_index": 0,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            client = QueuedAnswerClient([oversized, corrected])
            service = KnowledgeService(make_settings(Path(directory)), client=client)
            card = approved_card(procedure_steps=steps)
            result = service._answer_from_hits(
                "给出关键步骤",
                [SearchHit(card=card, score=20.0, matched_terms=["步骤"], query_coverage=1.0)],
            )

            self.assertEqual(len(client.calls), 2)
            self.assertIn("去重后超过 30 条", client.calls[1][1])
            self.assertIn("最多 12 条 claims", client.calls[1][1])
            self.assertEqual(result["answer_contract"]["retries"], 1)
            self.assertEqual(len(result["claims"]), 1)


if __name__ == "__main__":
    unittest.main()
