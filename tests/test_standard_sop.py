from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from knowledge_platform.service import KnowledgeService
from knowledge_platform.standard_sop import DIMENSIONS, KINDS, normalize_source
from knowledge_platform.web import create_server
from tests.test_platform import make_settings


def source(number=1, *, published=True):
    return {"external_id": f"synthetic-sop-{number}", "published": published,
            "title": f"冷补丁写作合成案例 {number}", "version": "synthetic-v1",
            "sections": [{"kind": kind, "text": f"合成冷补丁 {kind} 指导：确认测试目标和对应版本，异常时停止并由人工核查。"} for kind in KINDS]}


class WritingClient:
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.gaps = []
        self.conflicts = []

    def chat_json(self, prompt, content, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == self.fail_at:
            raise TimeoutError("synthetic model timeout")
        payload = json.loads(content)
        if "source" in payload:
            section = payload["source"]["sections"][0]
            return {"relevant": True, "dimensions": dict.fromkeys(DIMENSIONS, 4),
                    "strengths": [{"section_id": section["section_id"], "quote": section["text"], "reason": "清楚说明目标及停止要求"}],
                    "issues": [], "compatibility_notes": "仅供合成测试，版本由人工确认"}, {"total_tokens": 100}
        selected = payload["sources"][0]
        sections = []
        for kind in KINDS:
            section = next(s for s in selected["sections"] if s["kind"] == kind)
            sections.append({"kind": kind, "items": [{"text": "确认 <测试目标> 和适用版本，异常时停止并由人工核查。",
                                                       "citations": [{"source_id": selected["source_id"], "section_id": section["section_id"], "quote": section["text"]}]}]})
        return {"title": "冷补丁标准 SOP（合成测试）", "sections": sections,
                "parameters": [{"name": "测试目标", "description": "由操作者填写并核对"}],
                "gaps": self.gaps, "conflicts": self.conflicts}, {"total_tokens": 100}


class Context:
    def __init__(self, client):
        self.client = client
        self.saved = []

    def check_cancelled(self): pass
    def step(self, *args, **kwargs): return nullcontext()
    def chat_json(self, *args, **kwargs): return self.client.chat_json(*args, **kwargs)
    def save_checkpoint(self, **kwargs): self.saved.append(kwargs)


class StandardSOPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = WritingClient()
        settings = replace(make_settings(self.root), standard_sop_generation_enabled=True,
                           demo_mode=True, demo_full_reset_enabled=True, semantic_governance_enabled=False,
                           change_draft_database_path=self.root / 'data/change_drafts.db')
        self.service = KnowledgeService(settings, client=self.client)
        self.manager = self.service.standard_sop
        self.batch = self.manager.create_batch({"scenario": "synthetic-v1 冷补丁标准操作", "keywords": ["冷补丁"]}, actor="tester")
        self.batch_id = self.batch["batch_id"]
        self.context = Context(self.client)

    def prepare(self, count=1):
        self.manager.import_sources(self.batch_id, [source(index) for index in range(count)])
        self.manager.analyze(self.batch_id, self.context)
        return [s["source_id"] for s in self.manager.batch(self.batch_id)["sources"]]

    def generate(self):
        ids = self.prepare()
        return self.manager.generate(self.batch_id, ids, self.context, actor="tester")

    def test_121_sources_without_models_dedup_and_atomic_validation(self):
        documents = [source(index) for index in range(121)]
        result = self.manager.import_sources(self.batch_id, documents)
        self.assertEqual(result["inserted"], 121)
        self.assertEqual(self.manager.import_sources(self.batch_id, documents)["duplicates"], 121)
        with self.assertRaises(ValueError):
            self.manager.import_sources(self.batch_id, [source(122), {"title": "unknown", "body": "unknown"}])
        self.assertEqual(len(self.manager.batch(self.batch_id)["sources"]), 121)
        self.assertEqual(self.service.store.stats()["cards"], 0)
        self.assertEqual(self.client.calls, [])

    def test_known_adapter_excludes_historical_results(self):
        raw = json.loads((Path(__file__).parent / 'fixtures/structure_samples/capacity-adjustment.json').read_text(encoding='utf-8'))
        normalized = normalize_source({"content": raw, "published": True})
        text = json.dumps(normalized, ensure_ascii=False)
        self.assertNotIn('SYNTH_COMPLETED', text)
        self.assertNotIn('合成历史核验说明', text)
        self.assertTrue(any('/sop_change_step/' in s['pointer'] for s in normalized['sections']))
        self.assertNotIn('PRECONDITIONS', normalized['structure']['covered_sections'])

    def test_evidence_pointers_resolve_in_wrapped_and_root_documents(self):
        raw = json.loads((Path(__file__).parent / 'fixtures/structure_samples/capacity-adjustment.json').read_text(encoding='utf-8'))
        for content in (raw, raw['data'], source(), {'data': source()}):
            normalized = normalize_source({'content': content, 'published': True})
            for section in normalized['sections']:
                value = content
                for part in section['pointer'].strip('/').split('/'):
                    value = value[int(part)] if isinstance(value, list) else value[part]
                self.assertTrue(value)

    def test_partial_analysis_retained_no_hidden_retry_and_resume(self):
        self.manager.import_sources(self.batch_id, [source(1), source(2), source(3, published=False)])
        self.client.fail_at = 2
        with self.assertRaises(TimeoutError): self.manager.analyze(self.batch_id, self.context)
        self.assertEqual(len(self.client.calls), 2)
        sources = self.manager.batch(self.batch_id)['sources']
        self.assertEqual(sources[0]['analysis']['status'], 'SUCCEEDED')
        self.assertEqual(sources[0]['analysis']['rubric_version'], 'standard_sop_writing_v1')
        self.assertEqual(sources[1]['analysis'], {})
        self.client.fail_at = None
        self.assertEqual(self.manager.analyze(self.batch_id, self.context)['analyzed_this_run'], 1)
        self.assertEqual(len(self.client.calls), 3)
        self.assertTrue(all(c['http_retries'] == 0 and c['retries'] == 0 and c['exclusive'] for c in self.client.calls))

    def test_quality_citations_and_numeric_contract(self):
        normalized = normalize_source(source())
        report, _ = self.client.chat_json('', json.dumps({'source': normalized}))
        invalid = deepcopy(report); invalid['strengths'][0]['quote'] = '不存在于来源的合成引用，必须拒绝'
        with self.assertRaises(ValueError): self.manager.validate_analysis(invalid, normalized)
        invalid = deepcopy(report); invalid['dimensions']['scope'] = True
        with self.assertRaises(ValueError): self.manager.validate_analysis(invalid, normalized)

    def test_disabled_model_and_oversized_context_do_not_call_model(self):
        self.manager.import_sources(self.batch_id, [source()])
        self.service.settings = replace(self.service.settings, standard_sop_generation_enabled=False)
        with self.assertRaises(ValueError): self.manager.analyze(self.batch_id, self.context)
        self.assertEqual(self.client.calls, [])
        self.service.settings = replace(self.service.settings, standard_sop_generation_enabled=True)
        with patch.object(self.manager, 'MAX_ANALYSIS_CHARS', 20):
            with self.assertRaises(ValueError): self.manager.analyze(self.batch_id, self.context)
        self.assertEqual(self.client.calls, [])

    def test_invalid_source_ids_and_overlapping_operation_are_rejected(self):
        ids = self.prepare()
        with self.assertRaises(ValueError): self.manager.selected_sources(self.manager.batch(self.batch_id), [{}])
        with self.manager.operation():
            with self.assertRaises(ValueError): self.manager.analyze(self.batch_id, self.context)
        draft = self.manager.generate(self.batch_id, ids, self.context, actor='tester')
        payload = deepcopy(draft['payload'])
        payload['sections'][0]['items'][0]['citations'][0]['section_id'] = []
        with self.assertRaises(ValueError): self.manager.edit(draft['draft_id'], payload, revision=1, actor='tester')

    def test_generation_review_export_edit_revokes_approval_and_stale_review(self):
        draft = self.generate()
        self.assertEqual(draft['status'], 'PENDING_REVIEW')
        with self.assertRaises(ValueError): self.manager.export(draft['draft_id'])
        self.manager.review(draft['draft_id'], revision=1, decision='APPROVED', actor='reviewer', comment='合成流程及引用已核查')
        exported = self.manager.export(draft['draft_id'])
        self.assertIn('## 操作回退', exported['markdown'])
        self.assertIn('<测试目标>', exported['markdown'])
        edited = self.manager.edit(draft['draft_id'], draft['payload'], revision=1, actor='editor')
        self.assertEqual(edited['revision'], 2)
        self.assertEqual(edited['reviewer'], '')
        with self.assertRaises(ValueError): self.manager.export(draft['draft_id'])
        with self.assertRaises(ValueError):
            self.manager.review(draft['draft_id'], revision=1, decision='APPROVED', actor='stale', comment='旧版本')

    def test_invalid_citation_cannot_be_saved_and_conflicts_cannot_be_approved(self):
        self.client.conflicts = ['合成版本冲突，须人工处理']
        draft = self.generate()
        self.assertEqual(draft['status'], 'BLOCKED')
        with self.assertRaises(ValueError):
            self.manager.review(draft['draft_id'], revision=1, decision='APPROVED', actor='reviewer', comment='不能直接绕过冲突')
        payload = deepcopy(draft['payload']); payload['sections'][0]['items'][0]['citations'][0]['source_id'] = 'unselected'
        with self.assertRaises(ValueError):
            self.manager.edit(draft['draft_id'], payload, revision=1, actor='editor')
        self.assertEqual(self.manager.draft(draft['draft_id'])['revision'], 1)

    def test_clear_knowledge_also_clears_sop_corpus_and_drafts(self):
        draft = self.generate()
        server = create_server(self.service, host='127.0.0.1', port=0)
        try:
            result = server.clear_knowledge(actor='tester', confirmation='CLEAR_ALL_KNOWLEDGE')
            self.assertEqual(result['cleared']['standard_sop_sources'], 1)
            self.assertEqual(result['cleared']['standard_sop_drafts'], 1)
            self.assertEqual(self.manager.batches(), [])
            with self.assertRaises(ValueError): self.manager.draft(draft['draft_id'])
        finally: server.server_close()

    def test_http_connector_and_background_generation(self):
        server = create_server(self.service, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        opener = build_opener(ProxyHandler({}))
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, payload=None):
            data = None if payload is None else json.dumps(payload).encode()
            with opener.open(Request(base+path, data=data, headers={'Content-Type':'application/json'}), timeout=10) as response:
                return json.load(response)
        try:
            response = request(f'/api/standard-sop/batches/{self.batch_id}/sources', {'documents':[source()]})
            self.assertEqual(response['inserted'], 1)
            boundary = 'synthetic-sop-upload'
            upload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="batch.json"\r\n'
                      'Content-Type: application/json\r\n\r\n' + json.dumps([source(2)]) + f'\r\n--{boundary}--\r\n').encode()
            with opener.open(Request(base + f'/api/standard-sop/batches/{self.batch_id}/upload', data=upload,
                                     headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}), timeout=10) as response:
                self.assertEqual(json.load(response)['inserted'], 1)
            response = request('/api/standard-sop/jobs', {'batch_id':self.batch_id, 'operation':'analyze'})
            run = server.runtime.wait(response['run']['id'], timeout_seconds=10)
            self.assertEqual(run['status'], 'SUCCEEDED', run)
            observed = request(f"/api/runs/{run['id']}")
            self.assertEqual(observed['status'], 'SUCCEEDED')
            self.assertEqual(observed['id'], run['id'])
            batch = request(f'/api/standard-sop/batches/{self.batch_id}')
            self.assertNotIn('sections', batch['sources'][0])
            detail = request(f"/api/standard-sop/batches/{self.batch_id}/sources/{batch['sources'][0]['source_id']}")
            self.assertEqual(len(detail['sections']), 7)
            response = request('/api/standard-sop/jobs', {'batch_id':self.batch_id, 'operation':'generate','source_ids':[batch['sources'][0]['source_id']]})
            run = server.runtime.wait(response['run']['id'], timeout_seconds=10)
            self.assertEqual(run['status'], 'SUCCEEDED', run)
            draft = run['result']
            request(f"/api/standard-sop/drafts/{draft['draft_id']}/review", {'revision':1,'decision':'APPROVED','comment':'合成引用和流程已人工核查'})
            self.assertIn('markdown', request(f"/api/standard-sop/drafts/{draft['draft_id']}/export"))
        finally:
            server.shutdown(); thread.join(5); server.server_close()


if __name__ == '__main__': unittest.main()
