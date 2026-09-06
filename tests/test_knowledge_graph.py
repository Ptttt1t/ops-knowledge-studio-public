from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from knowledge_platform.service import KnowledgeService
from knowledge_platform.schema import CardStatus, ComparisonResult, KnowledgeCardDraft
from knowledge_platform.graph_layout import cluster_distance
from knowledge_platform.web import create_server

from tests.test_platform import FakeDeepSeekClient, SOURCE_TEXT, make_settings


class KnowledgeGraphTests(unittest.TestCase):
    @staticmethod
    def _add_phase_card(
        service: KnowledgeService, phase: str, index: int
    ) -> int:
        title = f"{phase} synthetic step {index}"
        content = f"{title}\nOperate on synthetic parameter {index}."
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id, created = service.store.add_document(
            title,
            "json",
            f"synthetic://graph/{phase}/{index}",
            checksum,
            content,
        )
        assert created
        chunk_id = service.store.add_chunk(
            document_id, 0, 0, len(content), content
        )
        draft = KnowledgeCardDraft(
            title=title,
            summary=content,
            knowledge_type="procedure",
            scenario="synthetic graph layout",
            object_type="service",
            object_name="graph-demo-service",
            procedure_steps=[content],
            evidence_quote=content,
        )
        return service.store.add_card(
            draft,
            document_id=document_id,
            chunk_id=chunk_id,
            evidence_locator=f"synthetic://graph/{phase}/{index}",
            status=CardStatus.APPROVED,
            quality_score=95,
            quality_issues=[],
            comparison=ComparisonResult(),
            semantic_metadata={
                "card_type": "PROCEDURE_STEP",
                "publish_status": "INDEXED",
                "retrieval_enabled": True,
                "semantic_fingerprint": hashlib.sha256(
                    f"{phase}-{index}".encode("utf-8")
                ).hexdigest(),
                "semantic_payload": {
                    "procedure_group": phase,
                    "applicable_phases": [phase],
                    "generalized_operation": content,
                },
            },
        )

    def _service_with_version_relation(
        self, root: Path
    ) -> tuple[KnowledgeService, int, int]:
        client = FakeDeepSeekClient()
        service = KnowledgeService(make_settings(root), client=client)
        first = service.ingest_text(
            source_name="旧版本知识",
            source_ref="doc://knowledge-graph/old",
            content=SOURCE_TEXT,
        )["card_ids"][0]
        service.review(first, action="approve", reviewer="graph-tester")
        client.comparison_decision = "NEW_VERSION"
        client.related_card_id = first
        second = service.ingest_text(
            source_name="候选新版本知识",
            source_ref="doc://knowledge-graph/new",
            content=SOURCE_TEXT.replace("十五分钟", "二十分钟"),
        )["card_ids"][0]
        return service, first, second

    def test_graph_projects_sources_objects_cards_and_explicit_relations(self):
        with tempfile.TemporaryDirectory() as directory:
            service, first, second = self._service_with_version_relation(
                Path(directory)
            )

            graph = service.knowledge_graph(status="ALL", limit=50)

            self.assertEqual(graph["meta"]["nodes_by_kind"]["card"], 2)
            self.assertEqual(graph["meta"]["nodes_by_kind"]["source"], 2)
            self.assertEqual(graph["meta"]["nodes_by_kind"]["object"], 1)
            self.assertEqual(graph["meta"]["explicit_relation_count"], 1)
            self.assertEqual(
                {node["id"] for node in graph["nodes"] if node["kind"] == "card"},
                {f"card:{first}", f"card:{second}"},
            )
            relation = next(edge for edge in graph["edges"] if edge["explicit"])
            self.assertEqual(relation["source"], f"card:{second}")
            self.assertEqual(relation["target"], f"card:{first}")
            self.assertEqual(relation["relation_type"], "CANDIDATE_VERSION_OF")

            approved = service.knowledge_graph(status="APPROVED", limit=50)
            self.assertEqual(approved["meta"]["nodes_by_kind"]["card"], 1)
            self.assertEqual(approved["meta"]["explicit_relation_count"], 0)
            self.assertEqual(
                [node["id"] for node in approved["nodes"] if node["kind"] == "card"],
                [f"card:{first}"],
            )

    def test_graph_api_and_page_are_available(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self._service_with_version_relation(Path(directory))
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urlopen(f"{base}/api/knowledge-graph?status=ALL&limit=50", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    graph = json.load(response)
                self.assertEqual(graph["meta"]["nodes_by_kind"]["card"], 2)
                self.assertEqual(graph["meta"]["explicit_relation_count"], 1)

                with urlopen(f"{base}/", timeout=5) as response:
                    page = response.read().decode("utf-8")
                self.assertIn('data-page="graph"', page)
                self.assertIn('id="knowledge-graph-canvas"', page)
                self.assertIn('id="knowledge-graph-tooltip"', page)
                self.assertIn('id="graph-rotate-left"', page)
                self.assertIn('id="graph-rotate-right"', page)
                self.assertIn("拖动旋转", page)

                with urlopen(f"{base}/app.js", timeout=5) as response:
                    script = response.read().decode("utf-8")
                self.assertIn("/api/knowledge-graph", script)
                self.assertIn("renderKnowledgeGraphDetail", script)
                self.assertIn("projectKnowledgeGraphNode", script)
                self.assertIn("graphCompactLabel", script)
                self.assertIn('return `K${node.entity_id}`', script)
                self.assertIn("cluster_centroid", script)
                self.assertIn("CROSS_PHASE_SIMILAR", script)
                self.assertIn("INSTANCE_OF", script)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_phase_clusters_are_spatially_separated_and_keep_relationships(self):
        with tempfile.TemporaryDirectory() as directory:
            service = KnowledgeService(
                make_settings(Path(directory)), client=FakeDeepSeekClient()
            )
            cards_by_phase: dict[str, list[int]] = {}
            for phase in ("PRECHECK", "IMPLEMENTATION", "VALIDATION", "ROLLBACK"):
                cards_by_phase[phase] = [
                    self._add_phase_card(service, phase, index)
                    for index in range(2)
                ]
            service.store.add_relation(
                cards_by_phase["VALIDATION"][0],
                cards_by_phase["IMPLEMENTATION"][0],
                relation_type="VALIDATES",
                confidence=0.97,
                reason="synthetic relationship",
            )

            graph = service.knowledge_graph(status="ALL", limit=50)
            card_nodes = [node for node in graph["nodes"] if node["kind"] == "card"]
            self.assertEqual(
                graph["meta"]["layout_strategy"],
                "type_centroid_local_force_v1",
            )
            self.assertEqual(
                {node["cluster"] for node in card_nodes},
                {"PRECHECK", "IMPLEMENTATION", "VALIDATION", "ROLLBACK"},
            )
            same_phase_distances: list[float] = []
            cross_phase_distances: list[float] = []
            for left_index, left in enumerate(card_nodes):
                for right in card_nodes[left_index + 1 :]:
                    distance = math.dist(
                        tuple(left["cluster_centroid"][axis] for axis in ("x", "y", "z")),
                        tuple(right["cluster_centroid"][axis] for axis in ("x", "y", "z")),
                    )
                    if left["cluster"] == right["cluster"]:
                        same_phase_distances.append(distance)
                    else:
                        cross_phase_distances.append(distance)
            self.assertLess(
                sum(same_phase_distances) / len(same_phase_distances),
                sum(cross_phase_distances) / len(cross_phase_distances),
            )
            self.assertGreater(cluster_distance("PRECHECK", "ROLLBACK"), 200)
            self.assertTrue(
                any(edge["relation_type"] == "VALIDATES" for edge in graph["edges"])
            )


if __name__ == "__main__":
    unittest.main()
