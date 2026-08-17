from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import urlopen

from knowledge_platform.service import KnowledgeService
from knowledge_platform.web import create_server

from tests.test_platform import FakeDeepSeekClient, SOURCE_TEXT, make_settings


class KnowledgeGraphTests(unittest.TestCase):
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
                self.assertIn('data-page="graph">知识关系图', page)
                self.assertIn('id="knowledge-graph-svg"', page)

                with urlopen(f"{base}/app.js", timeout=5) as response:
                    script = response.read().decode("utf-8")
                self.assertIn("/api/knowledge-graph", script)
                self.assertIn("renderKnowledgeGraphDetail", script)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
