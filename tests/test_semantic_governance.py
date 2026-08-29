from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from knowledge_platform.schema import CardStatus, ComparisonResult, KnowledgeCardDraft
from knowledge_platform.semantic_governance import (
    SemanticGovernanceError,
    SemanticGovernanceService,
    SemanticGovernanceWorker,
    explainable_anchors,
)
from knowledge_platform.service import KnowledgeService
from knowledge_platform.web import create_server
from tests.test_platform import make_settings


class SemanticGovernanceTests(unittest.TestCase):
    def make_service(self, root: Path) -> KnowledgeService:
        settings = replace(
            make_settings(root),
            semantic_governance_enabled=True,
            semantic_proposal_min_score=0.68,
            semantic_scan_max_cards=100,
            semantic_scan_max_proposals=20,
            mindmemos_enabled=False,
        )
        return KnowledgeService(settings, client=object())

    @staticmethod
    def add_procedure_card(
        service: KnowledgeService,
        *,
        source_name: str,
        title: str,
        operation: str,
        phase: str,
        fingerprint_seed: str,
        case_id: str | None = None,
    ) -> int:
        summary = f"{title}：{operation}"
        content = f"标题：{title}\n摘要：{summary}\n通用化操作：{operation}"
        checksum = hashlib.sha256(
            f"{source_name}\n{content}".encode("utf-8")
        ).hexdigest()
        document_id, created = service.store.add_document(
            source_name,
            "json",
            f"synthetic://{source_name}",
            checksum,
            content,
        )
        assert created
        chunk_id = service.store.add_chunk(
            document_id, 0, 0, len(content), content
        )
        draft = KnowledgeCardDraft(
            title=title,
            summary=summary,
            knowledge_type="procedure",
            scenario="synthetic service maintenance",
            object_type="service",
            object_name="demo-service",
            procedure_steps=[operation],
            validation_steps=[operation],
            keywords=["alarm", "probe", "health"],
            evidence_quote=summary,
        )
        fingerprint = hashlib.sha256(fingerprint_seed.encode("utf-8")).hexdigest()
        card_id = service.store.add_card(
            draft,
            document_id=document_id,
            chunk_id=chunk_id,
            evidence_locator=f"synthetic://{source_name}#summary",
            status=CardStatus.APPROVED,
            quality_score=95,
            quality_issues=[],
            comparison=ComparisonResult(),
            semantic_metadata={
                "card_type": "PROCEDURE_STEP",
                "card_model_version": "synthetic_v1",
                "dedup_status": "NEW",
                "content_quality": 95,
                "publish_status": "INDEXED",
                "retrieval_enabled": True,
                "planning_rag_enabled": True,
                "semantic_fingerprint": fingerprint,
                "semantic_payload": {
                    "generalized_operation": operation,
                    "validation": operation,
                    "rollback": "",
                    "impact_analysis": "无业务影响",
                    "applicable_phases": [phase],
                    "procedure_group": phase,
                    "source_procedure_pointer": f"/synthetic/{source_name}/{phase}",
                },
            },
        )
        case_id = case_id or f"change-order:{source_name}"
        service.store.save_case_bundle(
            case_id=case_id,
            document_id=document_id,
            title=source_name,
            extraction_strategy="synthetic",
        )
        service.store.save_card_lineage(
            card_id,
            case_id=case_id,
            extraction_strategy="synthetic",
            unit_role="PROCEDURE_STEP",
            unit_pointer=f"/synthetic/{source_name}/{phase}",
            source_pointers=[f"/synthetic/{source_name}/{phase}"],
            source_order=0,
        )
        return card_id

    def prepare_pair(self, service: KnowledgeService) -> tuple[int, int]:
        left_id = self.add_procedure_card(
            service,
            source_name="case-alpha",
            title="变更前检查：业务健康状态",
            operation="检查活动告警、服务探针和关键业务指标，确认系统运行正常。",
            phase="PRECHECK",
            fingerprint_seed="left-content",
        )
        right_id = self.add_procedure_card(
            service,
            source_name="case-beta",
            title="变更前检查：业务健康核查",
            operation="核查活动告警、服务探针及关键业务指标，确保系统状态正常。",
            phase="PRECHECK",
            fingerprint_seed="right-content",
        )
        return left_id, right_id

    def test_scan_creates_review_candidate_and_pending_graph_edge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            left_id, right_id = self.prepare_pair(service)

            first = service.semantic_governance.scan(actor="test-scanner")
            second = service.semantic_governance.scan(actor="test-scanner")
            queue = service.semantic_governance.list_proposals()

            self.assertEqual(first["eligible_card_count"], 2)
            self.assertEqual(first["created_proposal_count"], 1)
            self.assertEqual(second["created_proposal_count"], 0)
            self.assertEqual(len(queue["proposals"]), 1)
            proposal = queue["proposals"][0]
            self.assertEqual(proposal["status"], "PENDING_REVIEW")
            self.assertEqual(
                {proposal["left_card_id"], proposal["right_card_id"]},
                {left_id, right_id},
            )
            self.assertGreaterEqual(proposal["similarity_score"], 0.68)
            graph = service.knowledge_graph(status="ALL", limit=50)
            self.assertTrue(
                any(edge["relation_type"] == "SIMILAR_TO" for edge in graph["edges"])
            )

    def test_cross_phase_similarity_creates_relation_but_never_merge_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            operation = "检查当前活动告警，无影响后继续。"
            precheck_id = self.add_procedure_card(
                service,
                source_name="cross-phase-precheck",
                title="变更前检查：活动告警",
                operation=operation,
                phase="PRECHECK",
                fingerprint_seed="cross-phase-identical",
            )
            rollback_id = self.add_procedure_card(
                service,
                source_name="cross-phase-rollback",
                title="回退：活动告警",
                operation=operation,
                phase="ROLLBACK",
                fingerprint_seed="cross-phase-identical",
            )

            report = service.semantic_governance.scan(actor="test-scanner")

            self.assertEqual(report["candidate_count"], 0)
            self.assertEqual(report["created_proposal_count"], 0)
            self.assertEqual(report["cross_phase_relation_count"], 1)
            self.assertEqual(
                service.semantic_governance.list_proposals()["proposals"], []
            )
            graph = service.knowledge_graph(status="ALL", limit=50)
            relation = next(
                edge
                for edge in graph["edges"]
                if edge["relation_type"] == "CROSS_PHASE_SIMILAR"
            )
            self.assertEqual(
                {relation["source"], relation["target"]},
                {f"card:{precheck_id}", f"card:{rollback_id}"},
            )
            for card_id, phase in (
                (precheck_id, "PRECHECK"),
                (rollback_id, "ROLLBACK"),
            ):
                card = service.store.get_card(card_id)
                assert card is not None
                self.assertEqual(card["procedure_group"], phase)
                self.assertTrue(card["retrieval_enabled"])

            # A legacy or manually injected cross-phase proposal is rejected at
            # the final write boundary as well as by the scanner.
            proposal, _created = service.store.upsert_semantic_merge_proposal(
                proposal_id="SMP-CROSSPHASETEST",
                pair_key="cross-phase-test-key",
                left_card_id=precheck_id,
                right_card_id=rollback_id,
                left_fingerprint=str(
                    service.store.get_card(precheck_id)["semantic_fingerprint"]
                ),
                right_fingerprint=str(
                    service.store.get_card(rollback_id)["semantic_fingerprint"]
                ),
                algorithm_version="legacy-test",
                similarity_score=1.0,
                signals={},
                reason="legacy cross-phase proposal",
                proposed_merge={
                    "title": "不应合并",
                    "summary": operation,
                    "generalized_operation": operation,
                    "validation": "",
                    "rollback": "",
                    "impact_analysis": "无影响",
                    "risk_level": "",
                    "risk_control": "",
                    "scenario": "synthetic",
                    "object_type": "service",
                    "object_name": "demo-service",
                    "applicable_versions": [],
                    "prerequisites": [],
                    "keywords": [],
                    "applicable_phases": ["PRECHECK", "ROLLBACK"],
                    "source_card_ids": [precheck_id, rollback_id],
                },
            )
            with self.assertRaisesRegex(SemanticGovernanceError, "不允许合并"):
                service.semantic_governance.accept(
                    proposal["proposal_id"],
                    actor="knowledge-owner",
                    comment="must fail",
                )

    def test_shared_anchors_are_complete_semantic_phrases(self) -> None:
        anchors = explainable_anchors(
            {
                "title": "变更前检查：告警与爆炸半径",
                "generalized_operation": (
                    "执行告警检测，评估客户影响和爆炸半径，"
                    "确认系统参数满足验证条件。"
                ),
                "keywords": ["告警检测", "客户影响", "系统参数"],
            }
        )

        self.assertTrue(
            {"告警检测", "客户影响", "爆炸半径", "系统参数"}.issubset(anchors)
        )
        self.assertTrue({"下影", "于大", "务方", "和应"}.isdisjoint(anchors))

    def test_same_case_cards_never_enter_merge_governance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            shared_case = "change-order:synthetic-shared-case"
            operation = "检查活动告警、服务探针和关键业务指标，确认无影响后继续。"
            self.add_procedure_card(
                service,
                source_name="same-case-step-a",
                title="前检：业务健康检查 A",
                operation=operation,
                phase="PRECHECK",
                fingerprint_seed="same-case-a",
                case_id=shared_case,
            )
            self.add_procedure_card(
                service,
                source_name="same-case-step-b",
                title="前检：业务健康检查 B",
                operation=operation,
                phase="PRECHECK",
                fingerprint_seed="same-case-b",
                case_id=shared_case,
            )

            report = service.semantic_governance.scan(actor="test-scanner")

            self.assertEqual(report["eligible_card_count"], 2)
            self.assertEqual(report["same_case_pair_skipped_count"], 1)
            self.assertEqual(report["cluster_candidate_count"], 0)
            self.assertEqual(service.semantic_governance.list_proposals()["proposals"], [])

    def test_repeated_precheck_forms_one_cross_case_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            operation = (
                "完成容量分析、版本分析、API依赖分析、监控分析、高危操作分析、"
                "客户影响分析、配置检查和备份检查；不适用项记录依据。"
            )
            card_ids = [
                self.add_procedure_card(
                    service,
                    source_name=f"analysis-case-{index}",
                    title="前检：变更分析",
                    operation=operation,
                    phase="PRECHECK",
                    fingerprint_seed=f"analysis-fingerprint-{index}",
                )
                for index in range(5)
            ]

            report = service.semantic_governance.scan(actor="test-scanner")
            proposals = service.semantic_governance.list_proposals()["proposals"]

            self.assertEqual(report["pair_candidate_count"], 10)
            self.assertEqual(report["cluster_candidate_count"], 1)
            self.assertEqual(report["created_cluster_count"], 1)
            self.assertEqual(len(proposals), 1)
            cluster = proposals[0]
            self.assertEqual(cluster["proposal_type"], "CLUSTER")
            self.assertEqual(cluster["member_count"], 5)
            self.assertEqual(cluster["case_count"], 5)
            self.assertEqual(set(cluster["member_card_ids"]), set(card_ids))
            self.assertEqual(
                service.store.list_semantic_merge_proposals(
                    status="PENDING_REVIEW", limit=100
                ),
                [],
            )

    def test_complete_link_does_not_merge_transitive_similarity_chain(self) -> None:
        cards = [
            {"id": 1, "case_id": "case-a"},
            {"id": 2, "case_id": "case-b"},
            {"id": 3, "case_id": "case-c"},
        ]
        clusters, _edge_map = SemanticGovernanceService._build_complete_link_clusters(
            [
                (0.95, cards[0], cards[1], {}),
                (0.94, cards[1], cards[2], {}),
            ]
        )
        self.assertEqual(
            [[card["id"] for card in cluster] for cluster in clusters], [[1, 2]]
        )

    def test_shared_core_acceptance_keeps_case_evidence_and_graph_relations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            operation = "检查活动告警、服务探针和关键业务指标，确认系统运行正常。"
            source_ids = [
                self.add_procedure_card(
                    service,
                    source_name=f"shared-core-{index}",
                    title="前检：通用健康检查",
                    operation=operation,
                    phase="PRECHECK",
                    fingerprint_seed=f"shared-core-fingerprint-{index}",
                )
                for index in range(3)
            ]
            service.semantic_governance.scan()
            cluster = service.semantic_governance.list_proposals()["proposals"][0]

            accepted = service.semantic_governance.decide(
                cluster["proposal_id"],
                decision="ACCEPT",
                actor="knowledge-owner",
                comment="保留案例差异，抽取共享前检核心",
                merge_mode="SHARED_CORE",
            )

            canonical_id = int(accepted["canonical_card_id"])
            self.assertEqual(accepted["accepted_merge_mode"], "SHARED_CORE")
            for source_id in source_ids:
                source = service.store.get_card(source_id)
                assert source is not None
                self.assertEqual(source["dedup_status"], "INSTANCE")
                self.assertEqual(source["publish_status"], "CASE_EVIDENCE")
                self.assertFalse(source["retrieval_enabled"])
                self.assertTrue(
                    any(
                        relation["relation_type"] == "INSTANCE_OF"
                        and int(relation["related_card_id"]) == canonical_id
                        for relation in service.store.list_relations(source_id)
                    )
                )
            graph = service.knowledge_graph(status="APPROVED", limit=100)
            self.assertTrue(
                any(edge["relation_type"] == "INSTANCE_OF" for edge in graph["edges"])
            )

    def test_rejection_suppresses_same_content_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            self.prepare_pair(service)
            service.semantic_governance.scan()
            proposal = service.semantic_governance.list_proposals()["proposals"][0]

            rejected = service.semantic_governance.decide(
                proposal["proposal_id"],
                decision="REJECT",
                actor="knowledge-owner",
                comment="检查目标不同",
            )
            service.semantic_governance.scan()

            self.assertEqual(rejected["status"], "REJECTED")
            self.assertEqual(
                service.semantic_governance.list_proposals()["proposals"], []
            )
            self.assertFalse(
                any(
                    relation["relation_type"] == "SIMILAR_TO"
                    for relation in service.store.list_relations(
                        int(proposal["left_card_id"])
                    )
                )
            )

    def test_accept_creates_canonical_and_disables_source_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            left_id, right_id = self.prepare_pair(service)
            with service.store.connect() as connection:
                for source_id, memory_id in (
                    (left_id, "memory-left"),
                    (right_id, "memory-right"),
                ):
                    connection.execute(
                        """
                        INSERT INTO memory_sync_state
                            (card_id, backend, content_hash, status, memory_count,
                             detail, owner_token, lease_expires_at, attempt, updated_at)
                        VALUES (?, 'mindmemos:vanilla', ?, 'SUCCEEDED', 1,
                                '', '', '', 0, '2026-08-23T00:00:00+00:00')
                        """,
                        (source_id, f"hash-{source_id}"),
                    )
                    connection.execute(
                        """
                        INSERT INTO memory_links
                            (backend, memory_id, card_id, content_hash, created_at)
                        VALUES ('mindmemos:vanilla', ?, ?, ?,
                                '2026-08-23T00:00:00+00:00')
                        """,
                        (memory_id, source_id, f"hash-{source_id}"),
                    )
            service.semantic_governance.scan()
            proposal = service.semantic_governance.list_proposals()["proposals"][0]

            accepted = service.semantic_governance.decide(
                proposal["proposal_id"],
                decision="ACCEPT",
                actor="knowledge-owner",
                comment="确认属于同一通用健康检查",
                merged_card={
                    "title": "通用业务健康检查",
                    "summary": "检查告警、探针和关键业务指标。",
                    "generalized_operation": "检查活动告警、服务探针和关键业务指标，确认目标系统健康。",
                    "applicable_phases": ["PRECHECK"],
                },
            )

            canonical_id = int(accepted["canonical_card_id"])
            self.assertGreater(canonical_id, max(left_id, right_id))
            canonical = service.store.get_card(canonical_id)
            assert canonical is not None
            self.assertEqual(canonical["status"], "APPROVED")
            self.assertEqual(canonical["dedup_status"], "CANONICAL")
            self.assertTrue(canonical["retrieval_enabled"])
            self.assertEqual(canonical["applicable_phases"], ["PRECHECK"])
            self.assertEqual(canonical["procedure_group"], "PRECHECK")
            for source_id in (left_id, right_id):
                source = service.store.get_card(source_id)
                assert source is not None
                self.assertEqual(source["status"], "APPROVED")
                self.assertEqual(source["dedup_status"], "MERGED")
                self.assertEqual(source["publish_status"], "MERGED")
                self.assertFalse(source["retrieval_enabled"])
                relations = service.store.list_relations(source_id)
                self.assertTrue(
                    any(
                        relation["relation_type"] == "MERGED_INTO"
                        and int(relation["related_card_id"]) == canonical_id
                        for relation in relations
                    )
                )
            graph = service.knowledge_graph(status="APPROVED", limit=50)
            self.assertTrue(
                any(edge["relation_type"] == "MERGED_INTO" for edge in graph["edges"])
            )
            hits = service.retriever.search(
                "活动告警 服务探针 关键业务指标",
                statuses=[CardStatus.APPROVED],
                top_k=10,
            )
            self.assertEqual([int(hit.card["id"]) for hit in hits], [canonical_id])
            self.assertEqual(
                service.store.card_ids_for_memory_ids(
                    ["memory-left", "memory-right"], backend="mindmemos:vanilla"
                ),
                {},
            )
            self.assertEqual(
                service.store.count_memory_retirements(backend="mindmemos:vanilla"),
                2,
            )

    def test_content_change_stales_old_decision_identity_and_reproposes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            _left_id, right_id = self.prepare_pair(service)
            service.semantic_governance.scan()
            old = service.semantic_governance.list_proposals()["proposals"][0]
            with service.store.connect() as connection:
                connection.execute(
                    "UPDATE cards SET semantic_fingerprint = ? WHERE id = ?",
                    (hashlib.sha256(b"right-content-v2").hexdigest(), right_id),
                )

            report = service.semantic_governance.scan()
            all_proposals = service.semantic_governance.list_proposals(
                status="ALL"
            )["proposals"]

            self.assertEqual(report["stale_proposal_count"], 1)
            self.assertEqual(len(all_proposals), 2)
            self.assertEqual(
                {proposal["status"] for proposal in all_proposals},
                {"PENDING_REVIEW", "STALE"},
            )
            self.assertIn(
                old["proposal_id"],
                [
                    proposal["proposal_id"]
                    for proposal in all_proposals
                    if proposal["status"] == "STALE"
                ],
            )

    def test_case_rebuild_stales_merge_and_restores_unaffected_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            left_id, right_id = self.prepare_pair(service)
            service.semantic_governance.scan()
            proposal = service.semantic_governance.list_proposals()["proposals"][0]
            accepted = service.semantic_governance.decide(
                proposal["proposal_id"],
                decision="ACCEPT",
                actor="knowledge-owner",
                comment="synthetic merge before rebuild",
            )
            canonical_id = int(accepted["canonical_card_id"])
            left = service.store.get_card(left_id)
            assert left is not None

            purge = service.store.purge_case_for_rebuild(
                "change-order:case-alpha",
                expected_checksum=str(left["source_checksum"]),
                actor="synthetic-operator",
            )

            self.assertEqual(purge["invalidated_semantic_cluster_count"], 1)
            self.assertEqual(purge["invalidated_canonical_count"], 1)
            self.assertEqual(purge["reactivated_semantic_source_card_ids"], [right_id])
            self.assertIsNone(service.store.get_card(left_id))
            canonical = service.store.get_card(canonical_id)
            assert canonical is not None
            self.assertEqual(canonical["dedup_status"], "STALE")
            self.assertEqual(canonical["publish_status"], "SKIPPED")
            self.assertFalse(canonical["retrieval_enabled"])
            right = service.store.get_card(right_id)
            assert right is not None
            self.assertEqual(right["dedup_status"], "NEW")
            self.assertEqual(right["publish_status"], "INDEXED")
            self.assertTrue(right["retrieval_enabled"])
            stale = service.store.get_semantic_merge_cluster(
                proposal["proposal_id"]
            )
            assert stale is not None
            self.assertEqual(stale["status"], "STALE")
            self.assertIsNone(service.store.get_semantic_scan_report())
            graph = service.knowledge_graph(status="ALL", limit=50)
            canonical_node_id = f"card:{canonical_id}"
            self.assertFalse(
                any(
                    edge["relation_type"] in {"SIMILAR_TO", "MERGED_INTO"}
                    and canonical_node_id in {edge["source"], edge["target"]}
                    for edge in graph["edges"]
                )
            )

    def test_background_worker_runs_incremental_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            self.prepare_pair(service)
            worker = SemanticGovernanceWorker(
                service.semantic_governance,
                interval_seconds=60,
                initial_delay_seconds=0,
            )
            worker.start()
            deadline = time.time() + 3
            while time.time() < deadline:
                if service.store.get_semantic_scan_report() is not None:
                    break
                time.sleep(0.02)
            worker.stop()

            report = service.store.get_semantic_scan_report()
            self.assertIsNotNone(report)
            self.assertEqual(report["status"], "COMPLETED")
            self.assertFalse(worker.running)

    def test_web_scan_and_human_reject_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            self.prepare_pair(service)
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                request = Request(
                    base + "/api/semantic-merge-proposals/scan",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request, timeout=5) as response:
                    report = json.loads(response.read().decode("utf-8"))
                self.assertEqual(report["created_proposal_count"], 1)
                with urlopen(
                    base + "/api/semantic-merge-proposals?status=PENDING_REVIEW",
                    timeout=5,
                ) as response:
                    queue = json.loads(response.read().decode("utf-8"))
                proposal_id = queue["proposals"][0]["proposal_id"]
                decision = Request(
                    base
                    + f"/api/semantic-merge-proposals/{proposal_id}/decision",
                    data=json.dumps(
                        {"decision": "REJECT", "comment": "synthetic rejection"}
                    ).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(decision, timeout=5) as response:
                    rejected = json.loads(response.read().decode("utf-8"))
                self.assertEqual(rejected["status"], "REJECTED")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
