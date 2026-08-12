from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from harness.api_client import APIError, DeepSeekClient
from harness.config import ConfigurationError, Settings
from harness.trace import TraceLogger
from knowledge_platform.change_web import ChangeDemoWebManager, ChangeSessionLimitError
from knowledge_platform.documents import DocumentError, DocumentLimits, read_document
from knowledge_platform.security import (
    SlidingWindowLimiter,
    WebSecurity,
    WebSecurityError,
    generate_access_token,
)
from knowledge_platform.safe_documents import read_document_safely
from knowledge_platform.service import (
    KnowledgeRequestError,
    KnowledgeService,
)
from knowledge_platform.web import create_server


SOURCE_TEXT = """适用对象：测试路由表。执行前必须保存配置快照并确认备用链路健康。
操作步骤：先切换测试可用区，验证成功后切换第二可用区。
主要风险：错误下一跳可能导致访问中断。
回退步骤：按相反顺序恢复原下一跳。
验证方法：确认有效下一跳正确且丢包率不高于百分之一。"""


def make_settings(root: Path, **overrides) -> Settings:
    source_dir = root / "knowledge_sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(
        project_root=root,
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking_mode="disabled",
        timeout_seconds=10,
        api_max_retries=0,
        api_retry_initial_seconds=0.0,
        api_retry_max_seconds=0.0,
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
    payload.update(overrides)
    return Settings(**payload)


class CardClient:
    def __init__(self):
        self.calls = 0

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        return (
            {
                "knowledge_cards": [
                    {
                        "title": "测试路由切换",
                        "summary": "双可用区路由切换必须保留快照和回退路径。",
                        "knowledge_type": "procedure",
                        "scenario": "测试路由切换",
                        "object_type": "route_table",
                        "object_name": "rtb-test",
                        "applicable_versions": ["synthetic-v1"],
                        "prerequisites": ["备用链路健康"],
                        "procedure_steps": ["切换测试可用区", "切换第二可用区"],
                        "risks": ["错误下一跳导致中断"],
                        "rollback_steps": ["按相反顺序恢复原下一跳"],
                        "validation_steps": ["确认有效下一跳正确"],
                        "keywords": ["route", "failover"],
                        "evidence_quote": "执行前必须保存配置快照并确认备用链路健康。",
                    }
                ]
            },
            {"total_tokens": 10},
        )


class EmptyClient:
    def __init__(self, started: threading.Event | None = None, release: threading.Event | None = None):
        self.calls = 0
        self.started = started
        self.release = release

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(5)
        return {"knowledge_cards": []}, {"total_tokens": 1}


class SecurityHardeningTests(unittest.TestCase):
    def test_change_snapshot_retries_ready_ticket_seen_with_waiting_run(self):
        class FakeChangeStore:
            @staticmethod
            def get_ticket(_ticket_id):
                return {"ticket_id": "CHG-TEST"}

        class FakeService:
            TICKET_ID = "CHG-TEST"
            change_store = FakeChangeStore()

            def __init__(self):
                self.reads = 0

            def ticket_package(self, _ticket_id):
                self.reads += 1
                status = "READY_FOR_APPROVAL" if self.reads == 1 else "WAITING_APPROVAL"
                return {"ticket": {"status": status}}

        class FakeSession:
            service = FakeService()
            generate_run_id = "generate"
            execute_run_id = "execute"

        manager = object.__new__(ChangeDemoWebManager)
        manager._run_detail = lambda _session, run_id: {
            "status": "SUCCEEDED" if run_id == "generate" else "WAITING_APPROVAL"
        }
        package, _generate, execute = manager._consistent_snapshot(FakeSession())
        self.assertEqual(execute["status"], "WAITING_APPROVAL")
        self.assertEqual(package["ticket"]["status"], "WAITING_APPROVAL")
        self.assertEqual(FakeSession.service.reads, 2)

    def test_trace_redacts_queries_rotates_and_hash_chains_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                stale = root / f"session-20000101-00000{index}-stale.jsonl"
                stale.write_text(
                    json.dumps(
                        {
                            "event": "legacy",
                            "question": "旧日志中的明文业务查询",
                        },
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                stale.touch()
                time.sleep(0.01)
            logger = TraceLogger(
                root, retention_days=1, max_files=2, hmac_key="trace-test-key"
            )
            logger.log(
                "search",
                question="包含敏感业务名称的完整问题",
                authorization="Bearer top-secret-value",
                diagnostics={"query": "另一个明文查询"},
            )
            logger.log("search.completed", result_count=1)

            raw = logger.path.read_text(encoding="utf-8")
            self.assertNotIn("敏感业务名称", raw)
            self.assertNotIn("top-secret-value", raw)
            self.assertNotIn("另一个明文查询", raw)
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(records[0]["question"]["redacted"], True)
            self.assertEqual(records[1]["previous_hash"], records[0]["record_hash"])
            first = dict(records[0])
            expected_hash = first.pop("record_hash")
            canonical = json.dumps(
                first,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                expected_hash,
                hmac.new(b"trace-test-key", canonical, hashlib.sha256).hexdigest(),
            )
            self.assertLessEqual(len(list(root.glob("session-*.jsonl"))), 2)
            self.assertTrue(
                all(
                    "旧日志中的明文业务查询"
                    not in path.read_text(encoding="utf-8")
                    for path in root.glob("session-*.jsonl")
                )
            )

    def test_token_host_origin_content_type_and_actor_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token, hashed = generate_access_token()
            settings = make_settings(
                root,
                auth_mode="token",
                access_token_hash=hashed,
                allowed_hosts=("127.0.0.1", "localhost"),
                allowed_origins=("http://trusted.local",),
            )
            service = KnowledgeService(settings, client=CardClient())
            card_id = service.ingest_text(
                source_name="security.md", content=SOURCE_TEXT
            )["card_ids"][0]
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            auth = {"Authorization": f"Bearer {token}"}
            try:
                with urlopen(f"{base}/api/health/live", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as missing_auth:
                    urlopen(f"{base}/api/health", timeout=5)
                self.assertEqual(missing_auth.exception.code, 401)

                connection = HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=5
                )
                connection.request(
                    "GET",
                    "/api/health",
                    headers={**auth, "Host": "attacker.invalid"},
                )
                rejected_host = connection.getresponse()
                self.assertEqual(rejected_host.status, 421)
                rejected_host.read()
                connection.close()

                health_request = Request(f"{base}/api/health", headers=auth)
                with urlopen(health_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")

                bad_origin = Request(
                    f"{base}/api/search",
                    data=b"{}",
                    headers={
                        **auth,
                        "Origin": "https://attacker.invalid",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as rejected_origin:
                    urlopen(bad_origin, timeout=5)
                self.assertEqual(rejected_origin.exception.code, 403)

                plain_text = Request(
                    f"{base}/api/search",
                    data=b"{}",
                    headers={
                        **auth,
                        "Origin": "http://trusted.local",
                        "Content-Type": "text/plain",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as rejected_type:
                    urlopen(plain_text, timeout=5)
                self.assertEqual(rejected_type.exception.code, 415)

                review = Request(
                    f"{base}/api/cards/{card_id}/review",
                    data=json.dumps(
                        {"action": "approve", "reviewer": "forged-user"}
                    ).encode("utf-8"),
                    headers={
                        **auth,
                        "Origin": "http://trusted.local",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(review, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(service.store.get_card(card_id)["reviewer"], "shared-operator")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_text_and_chunk_limits_reject_before_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = EmptyClient()
            service = KnowledgeService(
                make_settings(root, max_text_chars=20), client=client
            )
            with self.assertRaises(KnowledgeRequestError) as too_large:
                service.ingest_text(source_name="large", content="x" * 21)
            self.assertEqual(too_large.exception.code, "document_text_too_large")
            self.assertEqual(client.calls, 0)

            chunk_client = EmptyClient()
            chunk_service = KnowledgeService(
                replace(
                    make_settings(root / "chunks"),
                    chunk_size=20,
                    chunk_overlap=5,
                    max_text_chars=100,
                    max_document_chunks=1,
                ),
                client=chunk_client,
            )
            with self.assertRaises(KnowledgeRequestError) as too_many_chunks:
                chunk_service.ingest_text(source_name="chunks", content="x" * 60)
            self.assertEqual(
                too_many_chunks.exception.code, "document_chunk_limit_exceeded"
            )
            self.assertEqual(chunk_client.calls, 0)

    def test_concurrent_duplicate_reservation_prevents_double_model_spend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = make_settings(root)
            started = threading.Event()
            release = threading.Event()
            first_client = EmptyClient(started, release)
            second_client = EmptyClient()
            first = KnowledgeService(settings, client=first_client)
            second = KnowledgeService(settings, client=second_client)
            result: dict[str, object] = {}
            failure: list[Exception] = []

            def run_first():
                try:
                    result.update(
                        first.ingest_text(source_name="same", content=SOURCE_TEXT)
                    )
                except Exception as exc:
                    failure.append(exc)

            worker = threading.Thread(target=run_first)
            worker.start()
            self.assertTrue(started.wait(3))
            with self.assertRaises(KnowledgeRequestError) as concurrent:
                second.ingest_text(source_name="same", content=SOURCE_TEXT)
            self.assertEqual(concurrent.exception.code, "ingestion_in_progress")
            self.assertEqual(second_client.calls, 0)
            release.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failure, [])
            self.assertFalse(result["duplicate_document"])
            duplicate = second.ingest_text(source_name="same", content=SOURCE_TEXT)
            self.assertTrue(duplicate["duplicate_document"])
            self.assertEqual(second_client.calls, 0)

    def test_docx_dtd_compression_ratio_and_pdf_page_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dtd_path = root / "dtd.docx"
            with zipfile.ZipFile(dtd_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "word/document.xml",
                    '<!DOCTYPE x [<!ENTITY a "boom">]><x>&a;</x>',
                )
            with self.assertRaises(DocumentError):
                read_document(dtd_path)
            with self.assertRaises(DocumentError):
                read_document_safely(
                    dtd_path, limits=DocumentLimits(), timeout_seconds=10
                )

            bomb_path = root / "ratio.docx"
            with zipfile.ZipFile(bomb_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", "A" * 100_000)
            with self.assertRaises(DocumentError):
                read_document(
                    bomb_path,
                    limits=replace(
                        DocumentLimits(), max_archive_compression_ratio=2
                    ),
                )

            try:
                from pypdf import PdfWriter
            except ImportError:
                self.skipTest("pypdf unavailable")
            pdf_path = root / "pages.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_blank_page(width=100, height=100)
            with pdf_path.open("wb") as output:
                writer.write(output)
            with self.assertRaises(DocumentError):
                read_document(
                    pdf_path, limits=replace(DocumentLimits(), max_pdf_pages=1)
                )

    def test_model_response_limit_and_insecure_remote_base_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text(
                "DEMO_MODE=false\n"
                "DEEPSEEK_BASE_URL=http://api.example.invalid\n",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError):
                Settings.load(env_file)

            settings = make_settings(root, model_max_response_bytes=32)

            class OversizedResponse:
                headers = {}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self, size=-1):
                    return b"x" * (33 if size < 0 else min(size, 33))

            with patch("harness.api_client.urlopen", return_value=OversizedResponse()):
                with self.assertRaises(APIError):
                    DeepSeekClient(settings).chat_text("system", "user")

            invalid_hash = make_settings(
                root / "invalid-hash",
                auth_mode="token",
                access_token_hash="sha256:" + "z" * 64,
            )
            with self.assertRaises(ConfigurationError):
                invalid_hash.validate_web_security()

    def test_model_call_budget_and_rate_limiter_stop_excess_work(self):
        class FailingClient:
            def __init__(self):
                self.calls = 0

            def chat_json(self, system_prompt, user_prompt, **kwargs):
                self.calls += 1
                raise APIError("synthetic upstream failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FailingClient()
            service = KnowledgeService(
                make_settings(root, max_model_calls_per_ingest=1), client=client
            )
            with self.assertRaises(KnowledgeRequestError) as exhausted:
                service.ingest_text(source_name="budget", content="x" * 3000)
            self.assertEqual(exhausted.exception.code, "model_call_budget_exceeded")
            self.assertEqual(client.calls, 1)

        limiter = SlidingWindowLimiter()
        limiter.consume("client", 1, now=10.0)
        with self.assertRaises(WebSecurityError) as limited:
            limiter.consume("client", 1, now=11.0)
        self.assertEqual(limited.exception.status, 429)

        with tempfile.TemporaryDirectory() as directory:
            rate_settings = make_settings(
                Path(directory),
                auth_mode="token",
                access_token_hash="sha256:" + "0" * 64,
                request_rate_per_minute=1,
            )
            security = WebSecurity(rate_settings)
            security.authorize(
                host="127.0.0.1",
                origin="",
                authorization="Bearer caller-controlled-one",
                path="/api/health/live",
                method="GET",
                client_key="127.0.0.1",
            )
            with self.assertRaises(WebSecurityError):
                security.authorize(
                    host="127.0.0.1",
                    origin="",
                    authorization="Bearer caller-controlled-two",
                    path="/api/health/live",
                    method="GET",
                    client_key="127.0.0.1",
                )

    def test_change_session_limit_and_active_ttl_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = make_settings(
                root,
                api_key="YOUR_DEEPSEEK_API_KEY_HERE",
                change_max_active_sessions=1,
                change_max_retained_sessions=2,
                change_active_ttl_seconds=1,
            )
            service = KnowledgeService(settings, client=EmptyClient())
            manager = ChangeDemoWebManager(service)
            try:
                created = manager.create(requested_by="shared-operator")
                session_id = str(created["session_id"])
                with self.assertRaises(ChangeSessionLimitError):
                    manager.create(requested_by="shared-operator")
                session = manager._sessions[session_id]
                workspace = session.service.workspace
                session.created_at = time.time() - 5
                manager.cleanup()
                self.assertFalse(workspace.exists())
                self.assertIsNone(manager.latest())
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
