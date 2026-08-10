from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from harness.config import Settings
from knowledge_platform.service import KnowledgeService


SOURCE_TEXT = """生产 VPC 路由切换操作。
执行前确认备用链路健康且容量低于 60%。
按 AZ-A、AZ-B 顺序切换下一跳。
风险是切换期间可能出现短时丢包。
回退时按 AZ-B、AZ-A 逆序恢复。
验证 TCP 443/5432 成功率不低于 99.5%，丢包不高于 1%，P95 时延不高于 30 ms。"""


def make_settings(root: Path) -> Settings:
    source_dir = root / "knowledge_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        project_root=root,
        api_key="deepseek-test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking_mode="disabled",
        timeout_seconds=10,
        api_max_retries=0,
        api_retry_initial_seconds=0,
        api_retry_max_seconds=0,
        max_tokens=2048,
        temperature=0.1,
        database_path=root / "data" / "knowledge.db",
        source_dir=source_dir,
        chunk_size=6000,
        chunk_overlap=500,
        retrieval_top_k=6,
        retrieval_min_score=10.0,
        retrieval_min_coverage=0.15,
        agent_max_steps=4,
        host="127.0.0.1",
        port=8765,
        mindmemos_enabled=True,
        mindmemos_api_key="memory-test-key",
    )


class FakeDeepSeekClient:
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if '"claims"' in system_prompt:
            return (
                {
                    "claims": [
                        {
                            "category": "回退",
                            "card_id": 1,
                            "support_field": "rollback_steps",
                            "support_index": 0,
                        }
                    ]
                },
                {"total_tokens": 10},
            )
        if "知识治理审核助手" in system_prompt:
            return (
                {
                    "decision": "NEW",
                    "related_card_id": None,
                    "confidence": 0.9,
                    "reason": "新知识",
                },
                {"total_tokens": 10},
            )
        return (
            {
                "knowledge_cards": [
                    {
                        "title": "生产 VPC 路由切换",
                        "summary": "双 AZ 路由分批切换与逆序回退。",
                        "knowledge_type": "procedure",
                        "scenario": "生产 VPC 专线路由主备切换",
                        "object_type": "route_table",
                        "object_name": "vpc-prod-core",
                        "applicable_versions": ["cn-north-4"],
                        "prerequisites": ["备用链路健康", "容量低于 60%"],
                        "procedure_steps": ["先切 AZ-A", "验证后切 AZ-B"],
                        "risks": ["切换期间可能出现短时丢包"],
                        "rollback_steps": ["按 AZ-B、AZ-A 逆序恢复"],
                        "validation_steps": [
                            "TCP 443/5432 成功率不低于 99.5%",
                            "丢包不高于 1%",
                            "P95 时延不高于 30 ms",
                        ],
                        "keywords": ["VPC", "主备切换", "双 AZ"],
                        "evidence_quote": "执行前确认备用链路健康且容量低于 60%。",
                    }
                ]
            },
            {"total_tokens": 20},
        )


class FakeMindMemOSClient:
    def __init__(self):
        self.add_calls = []
        self.search_calls = []

    def add(self, **kwargs):
        self.add_calls.append(kwargs)
        return {
            "code": "ok",
            "request_id": "add-1",
            "data": {
                "memories": [
                    {"memory_id": "memory-1"},
                    {"memory_id": "memory-2"},
                ]
            },
        }

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {
            "code": "ok",
            "request_id": "search-1",
            "data": {
                "memories": [
                    {"id": "memory-2", "memory": "逆序恢复路由"},
                    {"id": "unmapped-memory", "memory": "未经治理的记忆"},
                ]
            },
        }

    def health(self):
        return {"status": "ok"}


class LongTermMemoryTests(unittest.TestCase):
    def test_approved_card_sync_is_idempotent_and_semantic_recall_is_governed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory_client = FakeMindMemOSClient()
            service = KnowledgeService(
                make_settings(root), client=FakeDeepSeekClient()
            )
            service.memory.client = memory_client
            card_id = service.ingest_text(
                source_name="路由切换 SOP",
                source_ref="ticket://CHG-DEMO-ROUTE-001",
                content=SOURCE_TEXT,
            )["card_ids"][0]

            approved = service.review(
                card_id, action="approve", reviewer="tester"
            )
            self.assertEqual(approved["memory_sync"]["status"], "SUCCEEDED")
            self.assertEqual(len(memory_client.add_calls), 1)
            self.assertNotIn(
                "memory-test-key", str(service.settings.public_config())
            )

            second = service.sync_long_term_memory()
            self.assertEqual(second["results"][0]["status"], "ALREADY_SYNCED")
            self.assertEqual(len(memory_client.add_calls), 1)

            recalled = service.search_with_diagnostics(
                "如果第二阶段有问题，撤销动作应该从哪边开始？"
            )
            self.assertEqual(recalled["hits"][0]["card"]["id"], card_id)
            self.assertEqual(
                recalled["memory_retrieval"]["semantic_added_card_ids"],
                [card_id],
            )
            self.assertEqual(
                recalled["hits"][0]["matched_terms"], ["mindmemos:semantic"]
            )

            # A stale long-term memory link cannot bypass the current local
            # lifecycle state: the bridge re-reads the card on every recall.
            with service.store.connect() as connection:
                connection.execute(
                    "UPDATE cards SET status = 'REJECTED' WHERE id = ?", (card_id,)
                )
            blocked = service.search_with_diagnostics(
                "如果第二阶段有问题，撤销动作应该从哪边开始？"
            )
            self.assertEqual(blocked["hits"], [])
            self.assertEqual(
                blocked["memory_retrieval"]["mapped_approved_cards"], 0
            )

    def test_local_retrieval_avoids_memory_dependency_and_failure_degrades(self):
        class FailedSearchClient(FakeMindMemOSClient):
            def search(self, **kwargs):
                raise TimeoutError("simulated timeout")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = KnowledgeService(
                make_settings(root), client=FakeDeepSeekClient()
            )
            service.memory.client = FakeMindMemOSClient()
            card_id = service.ingest_text(
                source_name="路由切换 SOP",
                source_ref="ticket://CHG-DEMO-ROUTE-002",
                content=SOURCE_TEXT,
            )["card_ids"][0]
            service.review(card_id, action="approve", reviewer="tester")
            service.memory.client = FailedSearchClient()

            result = service.search_with_diagnostics("VPC 主备切换如何回退")

            self.assertEqual(result["hits"][0]["card"]["id"], card_id)
            self.assertEqual(
                result["memory_retrieval"]["status"],
                "SKIPPED_LOCAL_SUFFICIENT",
            )
            self.assertEqual(
                result["memory_retrieval"]["lexical_card_ids"], [card_id]
            )

            degraded = service.search_with_diagnostics(
                "完全无关且本地没有命中的后备语义问题"
            )
            self.assertEqual(degraded["hits"], [])
            self.assertEqual(
                degraded["memory_retrieval"]["status"], "DEGRADED"
            )


if __name__ == "__main__":
    unittest.main()
