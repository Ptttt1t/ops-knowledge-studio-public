from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness.config import Settings
from knowledge_platform.security import generate_access_token
from knowledge_platform.service import KnowledgeService
from knowledge_platform.web import create_server

from tests.test_platform import FakeDeepSeekClient, SOURCE_TEXT


class DemoModeAndCardDeleteTests(unittest.TestCase):
    def _start(self, settings: Settings):
        service = KnowledgeService(settings, client=FakeDeepSeekClient())
        server = create_server(service, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return service, server, thread

    def test_demo_mode_allows_internal_http_and_unauthenticated_web_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "DEMO_MODE=true",
                        "DEEPSEEK_API_KEY=internal-demo-key",
                        "DEEPSEEK_BASE_URL=http://10.20.30.40:8080/v1",
                        "DEEPSEEK_MODEL=internal-model",
                        "PLATFORM_AUTH_MODE=token",
                        "PLATFORM_HOST=0.0.0.0",
                    )
                ),
                encoding="utf-8",
            )
            settings = Settings.load(env_file)
            self.assertTrue(settings.demo_mode)
            self.assertTrue(settings.allow_insecure_model_http)
            self.assertFalse(settings.startup_token_required)
            self.assertFalse(settings.effective_access_token_required)
            self.assertFalse(settings.request_boundary_checks_enabled)
            self.assertTrue(settings.csp_allow_inline)
            self.assertEqual(
                settings.startup_security_messages(),
                [
                    "[DEMO MODE] Authentication disabled",
                    "[DEMO MODE] Startup token disabled",
                    "[DEMO MODE] Access token disabled",
                ],
            )

            service, server, thread = self._start(settings)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base}/api/health", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    csp = response.headers["Content-Security-Policy"]
                    self.assertIn("'unsafe-inline'", csp)
                    self.assertIn("'unsafe-hashes'", csp)
                    health = json.load(response)
                    self.assertFalse(health["config"]["access_token_required"])

                with urlopen(f"{base}/", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("data-page=\"library\">知识库", response.read().decode("utf-8"))
                with urlopen(f"{base}/app.js", timeout=5) as response:
                    app_script = response.read().decode("utf-8")
                self.assertIn("window.deleteCard", app_script)
                self.assertIn('method: "DELETE"', app_script)
                self.assertIn("确认永久删除知识卡片", app_script)

                request = Request(
                    f"{base}/api/ingest-text",
                    data=json.dumps(
                        {
                            "source_name": "demo.md",
                            "source_ref": "demo://no-auth",
                            "content": SOURCE_TEXT,
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "http://internal-demo.invalid",
                    },
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    result = json.load(response)
                self.assertEqual(result["extracted_cards"], 1)
                card_id = int(result["card_ids"][0])

                service.store.save_card_lineage(
                    card_id,
                    case_id="delete-demo",
                    extraction_strategy="test",
                    unit_role="TASKS_CANONICAL",
                    unit_pointer="/data/action_list",
                    source_pointers=["/data/action_list/0"],
                    source_order=0,
                    unit_metadata={"include_in_generation": True},
                )
                with service.store.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO memory_sync_state
                            (card_id, backend, content_hash, status, memory_count,
                             detail, owner_token, lease_expires_at, attempt, updated_at)
                        VALUES (?, 'mindmemos', 'content-hash', 'SUCCEEDED', 1,
                                '{}', '', '', 1, '2026-08-12T00:00:00+00:00')
                        """,
                        (card_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO memory_links
                            (backend, memory_id, card_id, content_hash, created_at)
                        VALUES ('mindmemos', 'memory-delete-demo', ?, 'content-hash',
                                '2026-08-12T00:00:00+00:00')
                        """,
                        (card_id,),
                    )

                delete = Request(
                    f"{base}/api/cards/{card_id}",
                    method="DELETE",
                )
                with urlopen(delete, timeout=5) as response:
                    deleted = json.load(response)
                self.assertTrue(deleted["deleted"])
                self.assertEqual(deleted["deleted_card_id"], card_id)
                self.assertEqual(deleted["queued_memory_retirements"], 1)
                self.assertIsNone(service.store.get_card(card_id))
                with self.assertRaises(HTTPError) as missing:
                    urlopen(f"{base}/api/cards/{card_id}", timeout=5)
                self.assertEqual(missing.exception.code, 404)
                with service.store.connect() as connection:
                    audit = connection.execute(
                        "SELECT action, actor, detail FROM audit_log "
                        "WHERE action = 'CARD_DELETED' ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    lineage_count = connection.execute(
                        "SELECT COUNT(*) FROM card_lineage WHERE card_id = ?",
                        (card_id,),
                    ).fetchone()[0]
                    link_count = connection.execute(
                        "SELECT COUNT(*) FROM memory_links WHERE card_id = ?",
                        (card_id,),
                    ).fetchone()[0]
                    sync_count = connection.execute(
                        "SELECT COUNT(*) FROM memory_sync_state WHERE card_id = ?",
                        (card_id,),
                    ).fetchone()[0]
                    retirement = connection.execute(
                        "SELECT status, card_id FROM memory_retirements "
                        "WHERE backend = 'mindmemos' AND memory_id = 'memory-delete-demo'"
                    ).fetchone()
                self.assertIsNotNone(audit)
                self.assertEqual(audit["actor"], "shared-operator")
                self.assertEqual(json.loads(audit["detail"])["deleted_card_id"], card_id)
                self.assertEqual(lineage_count, 0)
                self.assertEqual(link_count, 0)
                self.assertEqual(sync_count, 0)
                self.assertEqual(retirement["status"], "PENDING")
                self.assertEqual(retirement["card_id"], card_id)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_production_switches_restore_token_boundary_and_strict_csp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token, token_digest = generate_access_token()
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "DEMO_MODE=false",
                        "DEEPSEEK_BASE_URL=https://internal-model.example/v1",
                        "STARTUP_TOKEN_REQUIRED=true",
                        "ACCESS_TOKEN_REQUIRED=true",
                        f"PLATFORM_ACCESS_TOKEN_HASH={token_digest}",
                        "PLATFORM_REQUEST_BOUNDARY_CHECKS_ENABLED=true",
                        "PLATFORM_CSP_ALLOW_INLINE=false",
                        "PLATFORM_ALLOWED_HOSTS=127.0.0.1,localhost",
                        "PLATFORM_ALLOWED_ORIGINS=http://trusted.local",
                    )
                ),
                encoding="utf-8",
            )
            settings = Settings.load(env_file)
            self.assertFalse(settings.demo_mode)
            self.assertTrue(settings.startup_token_required)
            self.assertTrue(settings.effective_access_token_required)
            self.assertFalse(settings.csp_allow_inline)

            _service, server, thread = self._start(settings)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with self.assertRaises(HTTPError) as missing:
                    urlopen(f"{base}/api/health", timeout=5)
                self.assertEqual(missing.exception.code, 401)
                request = Request(
                    f"{base}/api/health",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urlopen(request, timeout=5) as response:
                    csp = response.headers["Content-Security-Policy"]
                    self.assertNotIn("'unsafe-inline'", csp)
                    self.assertNotIn("'unsafe-hashes'", csp)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
