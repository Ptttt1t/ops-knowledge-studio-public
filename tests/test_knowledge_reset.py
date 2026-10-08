from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from knowledge_platform.change_drafts import ChangeDraftError
from knowledge_platform.security import WebSecurityError
from knowledge_platform.service import KnowledgeService
from knowledge_platform.web import create_server
from tests.test_platform import make_settings
from tests.test_sample_cases import NoModelClient


class KnowledgeResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = replace(
            make_settings(self.root), demo_mode=True, demo_full_reset_enabled=True,
            semantic_governance_enabled=False,
            change_draft_database_path=self.root / 'data/change_drafts.db',
        )
        self.service = KnowledgeService(settings, client=NoModelClient())
        self.server = create_server(self.service, host='127.0.0.1', port=0)
        self.addCleanup(self.server.server_close)
        self.fixture = Path(__file__).parent / 'fixtures/structure_samples/capacity-adjustment.json'
        self.imported = self.service.ingest_file(self.fixture)

    def clear(self):
        return self.server.clear_knowledge(actor='synthetic-tester', confirmation='CLEAR_ALL_KNOWLEDGE')

    def seed_dependencies(self):
        case_id = self.imported['case_id']
        with self.service.change_drafts.store.connect() as con:
            con.execute("INSERT INTO schema_profiles VALUES ('p', 1, ?, 'hash', '{}', 1, 'tester', 'now')", (case_id,))
            con.execute("""INSERT INTO change_drafts
                (draft_id, mode, status, request_json, selected_case_ids, profile_id,
                 created_by, created_at, updated_at)
                VALUES ('d', 'GENERATE', 'REVIEW_APPROVED', '{}', ?, 'p', 'tester', 'now', 'now')""",
                (json.dumps([case_id]),))
            con.execute("""INSERT INTO change_draft_revisions
                (draft_id, revision, normalized_json, change_json, provenance_json,
                 validation_json, model_usage_json, content_hash, actor, reviewer,
                 reviewed_at, created_at)
                VALUES ('d', 1, '{}', '{}', '{}', '{}', '{}', 'hash', 'tester',
                        'reviewer', 'now', 'now')""")
            con.execute("""INSERT INTO change_evaluations
                (evaluation_id, held_out_case_id, status, created_by, created_at, updated_at)
                VALUES ('e', ?, 'SUCCEEDED', 'tester', 'now', 'now')""", (case_id,))
        with self.service.store.connect() as con:
            con.execute("INSERT INTO memory_links VALUES ('mindmemos', 'old-memory', ?, 'hash', 'now')",
                        (self.imported['card_ids'][0],))

    def test_clear_all_backup_dependencies_and_reimport(self):
        self.seed_dependencies()
        other = self.service.ingest_file(self.fixture.with_name('configuration-rollout.json'))
        self.service.review_case_bundle(self.imported['case_id'], action='approve', reviewer='tester')
        original = self.service.store.stats()
        source = self.root / 'knowledge_sources/uploads/keep.json'
        source.parent.mkdir(parents=True); source.write_text('synthetic source')
        result = self.clear()
        self.assertEqual(result['cleared']['cards'], original['cards'])
        self.assertEqual(self.service.store.stats()['cards'], 0)
        self.assertEqual(self.service.store.stats()['documents'], 0)
        self.assertEqual(self.service.store.list_case_bundles(), [])
        self.assertEqual(self.service.knowledge_graph()['nodes'], [])
        for table in result['cleared']:
            with self.service.store.connect() as con:
                count = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            self.assertEqual(count, 1 if table == 'audit_log' else 0, table)
        backup = Path(result['backup_directory'])
        for filename in ('knowledge.db', 'change_drafts.db', 'runtime.db'):
            with closing(sqlite3.connect(backup / filename)) as con:
                self.assertEqual(con.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        with closing(sqlite3.connect(backup / 'knowledge.db')) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM cards').fetchone()[0], original['cards'])
        with self.service.change_drafts.store.connect() as con:
            self.assertEqual(con.execute('SELECT active FROM schema_profiles').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT status FROM change_drafts').fetchone()[0], 'BLOCKED')
            self.assertEqual(con.execute('SELECT status FROM change_evaluations').fetchone()[0], 'FAILED')
            revision = con.execute('SELECT reviewer, reviewed_at, review_comment FROM change_draft_revisions').fetchone()
            self.assertEqual(tuple(revision), ('', '', 'KNOWLEDGE_RESET'))
        with self.assertRaises(ChangeDraftError):
            self.service.change_drafts.export_draft('d')
        self.assertEqual(result['pending_memory_retirements'], 1)
        self.assertTrue(source.exists())
        again = self.service.ingest_file(self.fixture)
        self.assertFalse(again.get('duplicate_document', False))
        self.assertGreater(min(again['card_ids']), max(other['card_ids']))
        self.assertEqual(again['model_calls'], 0)
        self.assertTrue(all(self.service.card_detail(i)['status'] == 'PENDING_REVIEW' for i in again['card_ids']))

    def test_transaction_failure_rolls_back_knowledge_and_draft_changes(self):
        self.seed_dependencies()
        with self.service.store.connect() as con:
            con.execute("CREATE TRIGGER fail_clear BEFORE DELETE ON documents BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.clear()
        self.assertEqual(self.service.store.stats()['cards'], 6)
        with self.service.change_drafts.store.connect() as con:
            self.assertEqual(con.execute('SELECT active FROM schema_profiles').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT status FROM change_drafts').fetchone()[0], 'REVIEW_APPROVED')
            self.assertEqual(con.execute('SELECT reviewer FROM change_draft_revisions').fetchone()[0], 'reviewer')
        self.assertFalse(self.server._resetting_knowledge)
        self.assertEqual(self.server._active_work_requests, 0)

    def test_backup_failure_leaves_data_intact(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.clear()
        self.assertEqual(self.service.store.stats()['cards'], 6)
        self.server.begin_work(); self.server.end_work()

    def test_busy_requests_scans_and_models_are_rejected(self):
        self.server.begin_work()
        try:
            with self.assertRaises(WebSecurityError): self.clear()
        finally: self.server.end_work()
        with self.service.semantic_governance._scan_lock:
            with self.assertRaises(WebSecurityError): self.clear()
        with patch.object(self.service, 'model_request_activity', return_value={'active_requests': 1}):
            with self.assertRaises(WebSecurityError): self.clear()
        self.assertEqual(self.service.store.stats()['cards'], 6)

    def test_unfinished_runtime_and_ingestion_are_rejected(self):
        run, _ = self.server.runtime.store.create_run(task_type='synthetic', payload={}, budget={})
        with self.assertRaises(WebSecurityError): self.clear()
        self.server.runtime.store.request_cancel(run['id'])
        self.service.store.claim_ingestion('another-source', 'test-owner', lease_seconds=100)
        with self.assertRaises(WebSecurityError): self.clear()
        self.assertEqual(self.service.store.stats()['cards'], 6)

    def test_reset_excludes_new_posts_and_concurrent_reset(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.store.clear_knowledge
        errors = []
        def delayed(**kwargs):
            entered.set(); release.wait(5)
            return original(**kwargs)
        def worker():
            try: self.clear()
            except Exception as exc: errors.append(exc)
        with patch.object(self.service.store, 'clear_knowledge', side_effect=delayed):
            thread = threading.Thread(target=worker); thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(WebSecurityError): self.server.begin_work()
                with self.assertRaises(WebSecurityError): self.clear()
                self.assertEqual(self.server.shutdown_status()['active_work_requests'], 1)
            finally: release.set(); thread.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())

    def test_inflight_memory_sync_rejected_and_retirement_queue_preserved(self):
        self.service.review_case_bundle(self.imported['case_id'], action='approve', reviewer='tester')
        card_id = self.imported['card_ids'][0]
        self.service.store.claim_memory_sync(card_id, backend='mindmemos', content_hash='hash',
                                             owner_token='test-owner', lease_seconds=100)
        with self.assertRaises(WebSecurityError): self.clear()
        with self.service.store.connect() as con:
            con.execute("UPDATE memory_sync_state SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
            con.execute("INSERT INTO memory_retirements VALUES ('mindmemos', 'already-queued', ?, '', 'FAILED', 2, 'offline', 'now')", (card_id,))
        result = self.clear()
        self.assertEqual(result['pending_memory_retirements'], 1)
        with self.service.store.connect() as con:
            self.assertEqual(con.execute('SELECT attempts FROM memory_retirements').fetchone()[0], 2)

    def test_http_requires_flag_confirmation_and_returns_backup(self):
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        opener = build_opener(ProxyHandler({}))
        url = f'http://127.0.0.1:{self.server.server_port}/api/admin/clear-knowledge'
        def post(body):
            return opener.open(Request(url, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}), timeout=10)
        try:
            with self.assertRaises(HTTPError) as error: post({})
            self.assertEqual(error.exception.code, 400)
            self.service.settings = replace(self.service.settings, demo_full_reset_enabled=False)
            with self.assertRaises(HTTPError) as error: post({'confirmation':'CLEAR_ALL_KNOWLEDGE'})
            self.assertEqual(error.exception.code, 404)
            self.service.settings = replace(self.service.settings, demo_full_reset_enabled=True, demo_mode=False)
            with self.assertRaises(HTTPError) as error: post({'confirmation':'CLEAR_ALL_KNOWLEDGE'})
            self.assertEqual(error.exception.code, 404)
            self.service.settings = replace(self.service.settings, demo_mode=True)
            with post({'confirmation':'CLEAR_ALL_KNOWLEDGE'}) as response: result=json.load(response)
            self.assertTrue(Path(result['backup_directory']).is_dir())
            self.assertEqual(self.service.store.stats()['cards'], 0)
        finally:
            self.server.shutdown(); thread.join(5)


if __name__ == '__main__':
    unittest.main()
