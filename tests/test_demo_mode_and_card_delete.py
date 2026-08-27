from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from harness.config import ConfigurationError, Settings
from knowledge_platform.security import generate_access_token
from knowledge_platform.service import KnowledgeService
from knowledge_platform.web import create_server

from tests.test_platform import FakeDeepSeekClient, SOURCE_TEXT


class DemoModeAndCardDeleteTests(unittest.TestCase):
    def _start(self, settings: Settings, client=None):
        service = KnowledgeService(
            settings, client=client or FakeDeepSeekClient()
        )
        server = create_server(service, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return service, server, thread

    def test_real_change_model_concurrency_cannot_exceed_one(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DEMO_MODE=true\nCHANGE_DRAFT_MODEL_MAX_CONCURRENCY=2\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "固定为 1"):
                Settings.load(env_file)

    def test_change_draft_timeout_defaults_to_240_and_supports_env_override(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("DEMO_MODE=true\n", encoding="utf-8")
            defaults = Settings.load(env_file)
            self.assertEqual(defaults.change_draft_timeout_seconds, 240)
            self.assertEqual(
                defaults.public_config()["real_change_generation"]["timeout_seconds"],
                240,
            )

            env_file.write_text(
                "DEMO_MODE=true\nCHANGE_DRAFT_TIMEOUT_SECONDS=315\n",
                encoding="utf-8",
            )
            overridden = Settings.load(env_file)
            self.assertEqual(overridden.change_draft_timeout_seconds, 315)

    def test_change_draft_fallback_model_configuration_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "DEEPSEEK_MODEL=synthetic-primary-model",
                        "DEEPSEEK_MODEL_MAX_CONCURRENCY=2",
                        "CHANGE_DRAFT_FALLBACK_MODEL=synthetic-qwen-27b",
                        "CHANGE_DRAFT_FALLBACK_MODEL_MAX_CONCURRENCY=6",
                    )
                ),
                encoding="utf-8",
            )

            settings = Settings.load(env_file)

            self.assertEqual(settings.model_max_concurrency, 2)
            self.assertEqual(
                settings.change_draft_fallback_model, "synthetic-qwen-27b"
            )
            self.assertEqual(
                settings.change_draft_fallback_model_max_concurrency, 6
            )
            self.assertTrue(
                settings.public_config()["real_change_generation"][
                    "fallback_model_configured"
                ]
            )

    def test_change_draft_fallback_cannot_repeat_primary_model(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DEEPSEEK_MODEL=same-model\nCHANGE_DRAFT_FALLBACK_MODEL=SAME-MODEL\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "必须与 DEEPSEEK_MODEL 不同"):
                Settings.load(env_file)

    def test_demo_mode_allows_internal_http_and_unauthenticated_web_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "DEMO_MODE=true",
                        "DEEPSEEK_API_KEY=internal-demo-key",
                        # RFC 5737 TEST-NET-1 address; never resolves to an
                        # organization-internal endpoint.
                        "DEEPSEEK_BASE_URL=http://192.0.2.10:8080/v1",
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
            self.assertTrue(settings.demo_rebuild_enabled)
            self.assertTrue(settings.demo_safe_shutdown_enabled)
            self.assertTrue(settings.semantic_governance_enabled)
            self.assertFalse(settings.demo_full_reset_enabled)
            self.assertFalse(settings.real_change_generation_enabled)
            self.assertEqual(settings.model_max_concurrency, 2)
            self.assertEqual(settings.change_draft_model_max_concurrency, 1)
            self.assertEqual(settings.change_draft_fallback_model, "")
            self.assertEqual(
                settings.change_draft_fallback_model_max_concurrency, 2
            )
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
                self.assertIn("safe-shutdown-button", app_script)
                self.assertIn("SAFE_SHUTDOWN", app_script)

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
            self.assertFalse(settings.demo_rebuild_enabled)
            self.assertFalse(settings.demo_safe_shutdown_enabled)
            self.assertFalse(settings.semantic_governance_enabled)
            self.assertFalse(settings.demo_full_reset_enabled)
            self.assertFalse(settings.real_change_generation_enabled)
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
                shutdown = Request(
                    f"{base}/api/admin/safe-shutdown",
                    data=b'{"confirmation":"SAFE_SHUTDOWN"}',
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as disabled:
                    urlopen(shutdown, timeout=5)
                self.assertEqual(disabled.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_demo_safe_shutdown_waits_for_model_drain_and_rejects_new_work(self):
        class DrainTrackingClient(FakeDeepSeekClient):
            def __init__(self):
                super().__init__()
                self.active_requests = 1

            def model_activity(self):
                active = self.active_requests
                return {
                    "scope": "synthetic_model_route",
                    "active_requests": active,
                    "routes": {
                        "synthetic-model": {
                            "active_requests": active,
                            "max_concurrency": 2,
                        }
                    },
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "DEMO_MODE=true",
                        "DEEPSEEK_API_KEY=synthetic-safe-shutdown-key",
                        "DEEPSEEK_MODEL=synthetic-model",
                        "SEMANTIC_GOVERNANCE_ENABLED=false",
                    )
                ),
                encoding="utf-8",
            )
            settings = Settings.load(env_file)
            client = DrainTrackingClient()
            _service, server, thread = self._start(settings, client=client)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                shutdown = Request(
                    f"{base}/api/admin/safe-shutdown",
                    data=b'{"confirmation":"SAFE_SHUTDOWN"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(shutdown, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    accepted = json.load(response)
                self.assertEqual(accepted["state"], "DRAINING")
                self.assertEqual(accepted["active_model_requests"], 1)

                time.sleep(0.15)
                self.assertTrue(thread.is_alive())
                with urlopen(
                    f"{base}/api/admin/safe-shutdown", timeout=5
                ) as response:
                    status = json.load(response)
                self.assertFalse(status["accepting_new_work"])
                self.assertEqual(status["active_model_requests"], 1)

                rejected = Request(
                    f"{base}/api/ingest-text",
                    data=json.dumps(
                        {
                            "source_name": "must-not-run.md",
                            "content": SOURCE_TEXT,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as draining:
                    urlopen(rejected, timeout=5)
                self.assertEqual(draining.exception.code, 503)
                self.assertEqual(
                    json.load(draining.exception)["code"], "platform_draining"
                )

                client.active_requests = 0
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            finally:
                if thread.is_alive():
                    client.active_requests = 0
                    server.shutdown()
                    thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
