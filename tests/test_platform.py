from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen as http_urlopen
from urllib.error import HTTPError

from harness.api_client import APIError, DeepSeekClient
from harness.config import ConfigurationError, Settings
from knowledge_platform.documents import (
    DocumentChunk,
    _extract_ocr_lines,
    chunk_text,
    ground_evidence_quote,
)
from knowledge_platform.retrieval import HybridRetriever
from knowledge_platform.schema import CardStatus, KnowledgeCardDraft
from knowledge_platform.service import KnowledgeService
from knowledge_platform.service import KnowledgeServiceError
from knowledge_platform.store import KnowledgeStore, StoreError
from knowledge_platform.web import create_server


SOURCE_TEXT = """适用对象：NE-A 网元。适用版本：V3.1 升级到 V3.1-P2。
执行前必须确认主备状态正常、当前无严重告警，并完成配置备份。
操作步骤：先升级备用节点，确认正常后执行主备切换，再升级原主节点。
主要风险是切换期间可能出现短时业务抖动。
回退步骤：卸载补丁并恢复升级前配置。
验证方法：检查双节点版本并连续观察十五分钟无新增严重告警。"""


def make_settings(root: Path, *, configured: bool = True) -> Settings:
    source_dir = root / "knowledge_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        project_root=root,
        api_key="test-key" if configured else "YOUR_DEEPSEEK_API_KEY_HERE",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking_mode="disabled",
        timeout_seconds=10,
        api_max_retries=2,
        api_retry_initial_seconds=0.01,
        api_retry_max_seconds=0.02,
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
    )


class FakeDeepSeekClient:
    def __init__(self):
        self.json_calls = []
        self.answer_calls = []
        self.agent_calls = []
        self.agent_responses = []
        self.comparison_decision = "NEW"
        self.related_card_id = None
        self.answer_payload = {
            "claims": [
                {
                    "category": "适用条件",
                    "text": "确认主备正常、无严重告警并完成备份。",
                    "card_ids": [1],
                },
                {
                    "category": "回退",
                    "text": "卸载补丁并恢复配置。",
                    "card_ids": [1],
                },
            ]
        }

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.json_calls.append((system_prompt, user_prompt, kwargs))
        if '"claims"' in system_prompt:
            self.answer_calls.append((system_prompt, user_prompt, kwargs))
            return (self.answer_payload, {"total_tokens": 80})
        if "知识治理审核助手" in system_prompt:
            return (
                {
                    "decision": self.comparison_decision,
                    "related_card_id": self.related_card_id,
                    "confidence": 0.96,
                    "reason": "适用对象、版本和步骤高度一致",
                },
                {"total_tokens": 30},
            )
        return (
            {
                "knowledge_cards": [
                    {
                        "title": "NE-A V3.1-P2 补丁升级",
                        "summary": "在满足主备正常、无严重告警和已备份条件时执行补丁升级。",
                        "knowledge_type": "procedure",
                        "scenario": "NE-A 补丁升级",
                        "object_type": "网元",
                        "object_name": "NE-A",
                        "applicable_versions": ["V3.1", "V3.1-P2"],
                        "prerequisites": ["主备状态正常", "无严重告警", "已完成配置备份"],
                        "procedure_steps": ["先升级备用节点", "执行主备切换", "升级原主节点"],
                        "risks": ["可能出现短时业务抖动"],
                        "rollback_steps": ["卸载补丁", "恢复升级前配置"],
                        "validation_steps": ["检查双节点版本", "观察十五分钟无新增严重告警"],
                        "keywords": ["NE-A", "V3.1-P2", "补丁升级"],
                        "evidence_quote": "执行前必须确认主备状态正常、当前无严重告警，并完成配置备份。",
                    }
                ]
            },
            {"total_tokens": 100},
        )

    def chat(self, messages, *, tools=None, **kwargs):
        self.agent_calls.append((messages, tools, kwargs))
        if self.agent_responses:
            return self.agent_responses.pop(0), {"total_tokens": 40}
        return {"content": '{"done":true}', "tool_calls": []}, {"total_tokens": 20}

class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PlatformTests(unittest.TestCase):
    def test_dense_chunk_is_split_after_invalid_json_output(self):
        class SplitRetryClient:
            def __init__(self):
                self.calls = []

            def chat_json(self, system_prompt, user_prompt, **kwargs):
                self.calls.append((user_prompt, kwargs))
                source = user_prompt.split("\n\n", 1)[-1]
                if len(source) >= 2000:
                    raise APIError("模拟过长 JSON")
                return {"knowledge_cards": []}, {"total_tokens": 10}

        with tempfile.TemporaryDirectory() as directory:
            client = SplitRetryClient()
            service = KnowledgeService(make_settings(Path(directory)), client=client)
            chunk = DocumentChunk(
                index=3,
                char_start=100,
                char_end=6100,
                content="A" * 6000,
            )
            results = service._extract_chunk("dense.txt", chunk)

        self.assertEqual(len(results), 4)
        self.assertTrue(all(item[3] == 2 for item in results))
        self.assertEqual(results[0][0].char_start, 100)
        self.assertEqual(results[-1][0].char_end, 6100)
        self.assertGreater(len(client.calls), len(results))

    def test_ocr_result_filters_low_confidence_lines(self):
        lines = _extract_ocr_lines(
            [{"rec_texts": ["升级步骤", "噪声"], "rec_scores": [0.98, 0.2]}]
        )
        self.assertEqual(lines, ["升级步骤"])

    def test_placeholder_api_is_rejected_only_when_required(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory), configured=False)
            self.assertFalse(settings.api_configured)
            with self.assertRaises(ConfigurationError):
                settings.require_api()

    def test_deepseek_json_output_payload_and_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory))
            captured = {}

            def fake_urlopen(request, timeout):
                captured["url"] = request.full_url
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                captured["authorization"] = request.get_header("Authorization")
                captured["timeout"] = timeout
                return FakeHTTPResponse(
                    {
                        "choices": [{"message": {"role": "assistant", "content": '{"ok":true}'}}],
                        "usage": {"total_tokens": 8},
                    }
                )

            with patch("harness.api_client.urlopen", fake_urlopen):
                result, usage = DeepSeekClient(settings).chat_json(
                    "请输出 json 对象", "json 输入"
                )

            self.assertEqual(result, {"ok": True})
            self.assertEqual(usage["total_tokens"], 8)
            self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
            self.assertEqual(captured["payload"]["model"], "deepseek-v4-flash")
            self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})
            self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
            self.assertEqual(captured["authorization"], "Bearer test-key")

    def test_deepseek_retries_transient_http_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(Path(directory))
            calls = 0

            def flaky_urlopen(request, timeout):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise HTTPError(
                        request.full_url,
                        503,
                        "temporary",
                        hdrs=None,
                        fp=BytesIO(b'{"error":"temporary"}'),
                    )
                return FakeHTTPResponse(
                    {
                        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"total_tokens": 4},
                    }
                )

            with patch("harness.api_client.urlopen", flaky_urlopen):
                text, usage = DeepSeekClient(settings).chat_text("system", "user")

            self.assertEqual(text, "ok")
            self.assertEqual(usage["total_tokens"], 4)
            self.assertEqual(calls, 2)

    def test_chunking_preserves_overlap_and_boundaries(self):
        text = "第一段。\n" + "A" * 80 + "\n最后一段。"
        chunks = chunk_text(text, chunk_size=50, overlap=10)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0].char_start, 0)
        self.assertLess(chunks[1].char_start, chunks[0].char_end)
        self.assertEqual(chunks[-1].char_end, len(text))

    def test_evidence_quote_is_grounded_to_exact_source_span(self):
        source = (
            "Rolling updates allow Deployments{{< glossary_tooltip text=\"Deployment\" "
            "term_id=\"deployment\" >}}' update to take place with zero downtime."
        )
        proposed = (
            "Rolling updates allow Deployments' update to take place with zero downtime."
        )
        span = ground_evidence_quote(source, proposed)
        self.assertIsNotNone(span)
        self.assertEqual(span.quote, source[span.start : span.end])
        self.assertIn(span.match_method, {"normalized", "anchored"})

    def test_quality_rules_depend_on_knowledge_type(self):
        source = "The product allows remote code execution."
        common = {
            "title": "Remote code execution",
            "summary": "An attacker can execute code.",
            "scenario": "Product is exposed to the network",
            "object_name": "Example Product",
            "evidence_quote": source,
        }
        risk = KnowledgeCardDraft(
            **common,
            knowledge_type="risk",
            risks=["Remote code execution"],
        )
        procedure = KnowledgeCardDraft(**common, knowledge_type="procedure")
        constraint = KnowledgeCardDraft(**common, knowledge_type="constraint")
        self.assertEqual(risk.quality(source)[0], 100.0)
        self.assertEqual(constraint.quality(source)[0], 100.0)
        self.assertLess(procedure.quality(source)[0], 65.0)

    def test_stopword_only_overlap_is_not_retrieved(self):
        card = {
            "id": 4,
            "title": "Deployment 配置环境变量注入节点名称和 Pod 名称",
            "summary": "通过 Downward API 注入元数据。",
            "scenario": "Pod 配置",
            "object_name": "Deployment",
            "applicable_versions": [],
            "keywords": ["Pod", "配置"],
            "prerequisites": [],
            "procedure_steps": [],
            "risks": [],
            "rollback_steps": [],
            "validation_steps": [],
        }
        retriever = HybridRetriever(store=None)  # cards are supplied directly
        hits = retriever.search(
            "HTTP 503 和 Retry-After 表示什么？",
            cards=[card],
            min_score=10.0,
            min_query_coverage=0.15,
        )
        self.assertEqual(hits, [])

    def test_end_to_end_knowledge_lifecycle_and_trusted_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = make_settings(root)
            fake = FakeDeepSeekClient()
            service = KnowledgeService(settings, client=fake)

            result = service.ingest_text(
                source_name="NE-A 升级记录",
                source_ref="ticket://CHG-001",
                content=SOURCE_TEXT,
            )
            self.assertFalse(result["duplicate_document"])
            self.assertEqual(result["extracted_cards"], 1)
            card_id = result["card_ids"][0]
            card = service.card_detail(card_id)
            self.assertEqual(card["status"], CardStatus.PENDING_REVIEW.value)
            self.assertEqual(card["quality_score"], 100.0)
            self.assertIn(card["evidence_quote"], SOURCE_TEXT)

            duplicate = service.ingest_text(
                source_name="重复来源",
                source_ref="ticket://CHG-002",
                content=SOURCE_TEXT,
            )
            self.assertTrue(duplicate["duplicate_document"])
            self.assertEqual(len(fake.json_calls), 1)

            no_approved = service.query("NE-A 如何升级？")
            self.assertEqual(no_approved["sources"], [])
            self.assertEqual(no_approved["refusal_reason"], "no_relevant_approved_knowledge")
            self.assertEqual(len(fake.answer_calls), 0)

            approved = service.review(
                card_id,
                action="approve",
                reviewer="tester",
                comment="证据已核对",
            )
            self.assertEqual(approved["status"], CardStatus.APPROVED.value)

            hits = service.search("NE-A V3.1-P2 回退")
            self.assertEqual(hits[0]["card"]["id"], card_id)
            answer = service.query("NE-A 升级前检查什么，如何回退？")
            self.assertIn("[K1]", answer["answer"])
            self.assertEqual(len(answer["claims"]), 2)
            self.assertEqual(answer["sources"][0]["card_id"], card_id)
            self.assertIn("APPROVED", fake.answer_calls[0][0])

            fake.answer_payload = {
                "claims": [
                    {
                        "category": "结论",
                        "text": "这是一个没有知识证据编号的回答。",
                        "card_ids": [],
                    }
                ]
            }
            with self.assertRaises(KnowledgeServiceError):
                service.query("NE-A V3.1-P2 再次生成方案")

            fake.answer_payload = {
                "claims": [
                    {
                        "category": "结论",
                        "text": "引用了未检索的知识卡片。",
                        "card_ids": [999],
                    }
                ]
            }
            with self.assertRaises(KnowledgeServiceError):
                service.query("NE-A V3.1-P2 检查非法引用")

            fake.answer_payload = {
                "claims": [
                    {
                        "category": "结论",
                        "text": "正文自行声称来自 K1。",
                        "card_ids": [1],
                    }
                ]
            }
            with self.assertRaises(KnowledgeServiceError):
                service.query("NE-A V3.1-P2 检查裸引用")

    def test_read_only_agent_loop_searches_and_selects_approved_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeDeepSeekClient()
            service = KnowledgeService(make_settings(root), client=fake)
            card_id = service.ingest_text(
                source_name="NE-A 升级记录",
                source_ref="ticket://AGENT-001",
                content=SOURCE_TEXT,
            )["card_ids"][0]
            service.review(card_id, action="approve", reviewer="tester")
            fake.agent_responses = [
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-search",
                            "type": "function",
                            "function": {
                                "name": "search_approved_knowledge",
                                "arguments": json.dumps(
                                    {"query": "NE-A V3.1-P2 回退", "top_k": 3},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-card",
                            "type": "function",
                            "function": {
                                "name": "get_approved_card",
                                "arguments": json.dumps({"card_id": card_id}),
                            },
                        }
                    ],
                },
                {"content": '{"done":true}', "tool_calls": []},
            ]

            result = service.agent_query("NE-A 升级失败后如何回退？")

            self.assertTrue(result["agent"]["read_only"])
            self.assertEqual(result["agent"]["steps"], 3)
            self.assertEqual(result["agent"]["selected_card_ids"], [card_id])
            self.assertEqual(len(result["agent"]["tool_calls"]), 2)
            self.assertEqual(result["sources"][0]["card_id"], card_id)
            self.assertIn("[K1]", result["answer"])

    def test_agent_rejects_unapproved_tool_name(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeDeepSeekClient()
            service = KnowledgeService(make_settings(Path(directory)), client=fake)
            fake.agent_responses = [
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-shell",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": json.dumps({"command": "whoami"}),
                            },
                        }
                    ],
                },
                {"content": '{"done":true}', "tool_calls": []},
            ]

            result = service.agent_query("执行系统命令")

            self.assertEqual(result["refusal_reason"], "no_relevant_approved_knowledge")
            self.assertFalse(result["agent"]["tool_calls"][0]["success"])
            self.assertEqual(result["agent"]["tool_calls"][0]["tool"], "bash")
            self.assertEqual(result["sources"], [])

    def test_duplicate_relation_and_supersede_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeDeepSeekClient()
            service = KnowledgeService(make_settings(root), client=fake)
            first = service.ingest_text(
                source_name="旧版本", source_ref="doc://old", content=SOURCE_TEXT
            )["card_ids"][0]
            service.review(first, action="approve", reviewer="tester")

            fake.comparison_decision = "NEW_VERSION"
            fake.related_card_id = first
            second_text = SOURCE_TEXT.replace("十五分钟", "二十分钟")
            second = service.ingest_text(
                source_name="新版本", source_ref="doc://new", content=second_text
            )["card_ids"][0]
            second_card = service.card_detail(second)
            self.assertEqual(second_card["comparison_label"], "NEW_VERSION")
            self.assertEqual(second_card["relations"][0]["related_card_id"], first)

            service.review(
                second,
                action="supersede",
                reviewer="tester",
                comment="新文档替代旧版本",
                supersedes_id=first,
            )
            self.assertEqual(service.card_detail(first)["status"], CardStatus.SUPERSEDED.value)
            self.assertEqual(service.card_detail(second)["status"], CardStatus.APPROVED.value)
            approved_ids = [card["id"] for card in service.store.list_cards(CardStatus.APPROVED)]
            self.assertEqual(approved_ids, [second])

    def test_web_health_and_dashboard_without_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = make_settings(root, configured=False)
            service = KnowledgeService(settings, client=FakeDeepSeekClient())
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                with http_urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                with http_urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    html = response.read().decode("utf-8")
                self.assertEqual(health["status"], "ok")
                self.assertFalse(health["config"]["api_configured"])
                self.assertTrue(health["document_processing"]["paddleocr"])
                self.assertTrue(health["document_processing"]["pdf_text_extraction"])
                self.assertIn("Ops Knowledge", html)
                self.assertIn("知识审核队列", html)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_web_file_upload_ingests_and_preserves_original_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = KnowledgeService(make_settings(root), client=FakeDeepSeekClient())
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                boundary = "----OpsKnowledgeUploadBoundary"
                body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; filename="actual_document.txt"\r\n'
                    "Content-Type: text/plain; charset=utf-8\r\n\r\n"
                ).encode("utf-8") + SOURCE_TEXT.encode("utf-8") + (
                    f"\r\n--{boundary}--\r\n"
                ).encode("ascii")
                request = Request(
                    f"http://127.0.0.1:{server.server_address[1]}/api/ingest-file",
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with http_urlopen(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result["upload"]["original_name"], "actual_document.txt")
                self.assertEqual(result["extracted_cards"], 1)
                saved_path = Path(result["upload"]["stored_path"])
                self.assertTrue(saved_path.is_file())
                self.assertEqual(service.card_detail(result["card_ids"][0])["source_name"], "actual_document.txt")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_sqlite_schema_is_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.db"
            store = KnowledgeStore(path)
            store.initialize()
            self.assertTrue(path.exists())
            self.assertEqual(store.stats()["cards"], 0)

    def test_card_without_source_evidence_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeDeepSeekClient()
            original_chat_json = fake.chat_json

            def no_evidence(system_prompt, user_prompt, **kwargs):
                payload, usage = original_chat_json(system_prompt, user_prompt, **kwargs)
                if "knowledge_cards" in payload:
                    payload["knowledge_cards"][0]["evidence_quote"] = ""
                return payload, usage

            fake.chat_json = no_evidence
            service = KnowledgeService(make_settings(root), client=fake)
            card_id = service.ingest_text(
                source_name="无证据抽取", source_ref="doc://no-evidence", content=SOURCE_TEXT
            )["card_ids"][0]
            with self.assertRaises(StoreError):
                service.review(card_id, action="approve", reviewer="tester")


if __name__ == "__main__":
    unittest.main()
