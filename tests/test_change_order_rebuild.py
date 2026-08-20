from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from knowledge_platform.change_order_cards import CardType
from knowledge_platform.long_term_memory import MindMemOSBridge
from knowledge_platform.service import (
    KnowledgeRequestError,
    KnowledgeService,
    KnowledgeServiceError,
)
from knowledge_platform.web import create_server
from tests.test_change_order_card_builder import (
    FIXTURE,
    base_step,
    empty_step,
    make_payload,
)
from tests.test_platform import FakeDeepSeekClient, make_settings


class TrackingMemoryClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.fail_delete = False

    def add(self, **_kwargs):
        return {
            "code": "ok",
            "request_id": "synthetic-add",
            "data": {
                "memories": [
                    {"memory_id": "synthetic-memory-1"},
                    {"memory_id": "synthetic-memory-2"},
                ]
            },
        }

    def delete(self, *, memory_id: str):
        if self.fail_delete:
            raise RuntimeError("synthetic retirement failure")
        self.deleted.append(memory_id)
        return {"code": "ok", "request_id": "synthetic-delete", "data": {}}


def _shared_validation_and_rollback_step() -> dict[str, object]:
    return base_step(
        check_name="检查参数状态",
        operate_description="检查 parameter-z 当前状态并确认目标服务正常。",
        operate_verified="确认 parameter-z 状态符合预期。",
        operate_rollback="不涉及",
        impact_analysis="无影响",
        sop_step_id="synthetic-shared-check",
    )


def three_procedure_payload(*, ticket_id: str = "SYNTH-REBUILD-A") -> dict[str, object]:
    context = deepcopy(FIXTURE["case_f"])
    context["ticket_id"] = ticket_id
    context["title"] = f"虚构案例 {ticket_id}"
    shared = _shared_validation_and_rollback_step()
    return make_payload(
        context=context,
        groups={
            "check_before_change": [
                base_step(
                    check_name="检查变更前状态",
                    operate_description="检查 synthetic-service 的当前运行状态。",
                    sop_step_id=f"{ticket_id}-precheck",
                )
            ],
            "change_implement": [
                base_step(
                    check_name="修改虚构参数",
                    operate_description="设置 parameter-z 从 old-value -> new-value。",
                    operate_verified="检查 parameter-z == new-value。",
                    operate_rollback="设置 parameter-z 从 new-value -> old-value。",
                    sop_step_id=f"{ticket_id}-implementation",
                )
            ],
            "change_verified": [deepcopy(shared)],
            "change_rollback": [deepcopy(shared)],
        },
    )


def split_sensitive_payload() -> dict[str, object]:
    long_operation = "\n".join(
        (
            "1. 容量参数调整",
            "执行容量配置调整，将 synthetic-service 的容量参数更新为新目标值；"
            "保存配置后逐项检查配置回读、实例状态和容量水位，确保该步骤可独立执行和验证。"
            * 3,
            "2. 监控参数调整",
            "配置 synthetic-service 的监控参数和告警阈值；保存后检查探针、指标和告警状态，"
            "确保监控调整可以独立实施、验证，并在异常时恢复原配置。"
            * 3,
        )
    )
    shared = _shared_validation_and_rollback_step()
    return make_payload(
        groups={
            "check_before_change": [
                base_step(
                    check_name="执行长步骤分析",
                    operate_description=long_operation,
                    sop_step_id="synthetic-long-precheck",
                )
            ],
            "change_implement": [
                base_step(
                    check_name="实施独立配置",
                    operate_description="设置 parameter-y 从 y-old -> y-new。",
                    sop_step_id="synthetic-independent-implementation",
                )
            ],
            "change_verified": [deepcopy(shared)],
            "change_rollback": [deepcopy(shared)],
        },
    )


class ChangeOrderRebuildTests(unittest.TestCase):
    def make_service(
        self,
        root: Path,
        *,
        split_chars: int = 6000,
        section_threshold: int = 5,
        child_min_chars: int = 160,
    ) -> KnowledgeService:
        settings = replace(
            make_settings(root),
            demo_mode=True,
            demo_rebuild_enabled=True,
            change_order_card_report_dir=root / "reports",
            change_order_procedure_split_chars=split_chars,
            change_order_semantic_section_threshold=section_threshold,
            change_order_child_min_content_chars=child_min_chars,
            change_draft_database_path=root / "data" / "change_drafts.db",
        )
        return KnowledgeService(settings, client=FakeDeepSeekClient())

    @staticmethod
    def write_source(root: Path, name: str, payload: dict[str, object]) -> Path:
        source = root / name
        source.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return source

    @staticmethod
    def procedure_cards(service: KnowledgeService, case_id: str) -> list[dict]:
        detail = service.case_bundle_detail(case_id)
        assert detail is not None
        return [
            card
            for card in detail["cards"]
            if card["card_type"] == CardType.PROCEDURE_STEP.value
        ]

    def test_case_o_same_source_rebuild_bypasses_idempotent_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            self.assertTrue(
                service.settings.public_config()["demo_management"][
                    "rebuild_enabled"
                ]
            )
            source = self.write_source(
                root,
                "synthetic-rebuild-o.json",
                three_procedure_payload(),
            )

            first = service.ingest_file(source)
            old_ids = first["card_ids"]
            self.assertEqual(first["cards_by_role"]["CASE_CONTEXT"], 1)
            self.assertEqual(first["cards_by_role"]["PROCEDURE_STEP"], 3)
            self.assertEqual(first["cards_by_role"]["EXECUTION_OUTCOME"], 1)
            service.review(
                old_ids[0],
                action="approve",
                reviewer="synthetic-reviewer",
            )

            ordinary_second = service.ingest_file(source)
            self.assertTrue(ordinary_second["duplicate_document"])
            self.assertEqual(ordinary_second["card_ids"], old_ids)

            rebuilt = service.rebuild_case_bundle(
                first["case_id"],
                actor="synthetic-operator",
                confirmation="REBUILD_CURRENT_CASE",
            )
            new_ids = rebuilt["card_ids"]
            self.assertFalse(rebuilt["duplicate_document"])
            self.assertTrue(source.is_file())
            self.assertEqual(rebuilt["cards_by_role"]["CASE_CONTEXT"], 1)
            self.assertEqual(rebuilt["cards_by_role"]["PROCEDURE_STEP"], 3)
            self.assertEqual(rebuilt["cards_by_role"]["EXECUTION_OUTCOME"], 1)
            self.assertTrue(set(old_ids).isdisjoint(new_ids))
            self.assertGreater(min(new_ids), max(old_ids))

            report = rebuilt["card_build_report"]
            self.assertEqual(report["build_generation"], 2)
            self.assertEqual(report["rebuild"]["previous_generation"], 1)
            self.assertEqual(report["rebuild"]["current_generation"], 2)
            self.assertEqual(report["rebuild"]["purged_card_count"], len(old_ids))
            self.assertEqual(report["rebuild"]["purged_review_count"], 1)
            self.assertEqual(report["rebuild"]["new_card_count"], len(new_ids))
            archived = root / "reports" / first["case_id"].split(":", 1)[1]
            self.assertTrue(
                (archived / "card_build_report.generation-1.json").is_file()
            )
            for card_id in old_ids:
                self.assertIsNone(service.store.get_card(card_id))

    def test_case_p_builder_logic_change_replaces_old_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(
                root,
                split_chars=200,
                section_threshold=2,
                child_min_chars=80,
            )
            source = self.write_source(
                root,
                "synthetic-rebuild-p.json",
                split_sensitive_payload(),
            )
            first = service.ingest_file(source)
            old_procedures = self.procedure_cards(service, first["case_id"])
            self.assertEqual(len(old_procedures), 5)
            self.assertEqual(
                sum(card["publish_status"] == "CONTAINER" for card in old_procedures),
                1,
            )
            container = next(
                card
                for card in old_procedures
                if card["publish_status"] == "CONTAINER"
            )
            children = [card for card in old_procedures if card["parent_unit_id"]]
            reviewed_container = service.review(
                container["id"],
                action="approve",
                reviewer="synthetic-reviewer",
            )
            self.assertEqual(
                reviewed_container["memory_sync"]["status"],
                "SKIPPED_RETRIEVAL_DISABLED",
            )
            hits = service.retriever.search(
                "容量参数调整 synthetic-service",
                statuses=["PENDING_REVIEW", "APPROVED"],
                top_k=50,
            )
            hit_ids = {int(hit.card["id"]) for hit in hits}
            self.assertNotIn(container["id"], hit_ids)
            self.assertTrue({card["id"] for card in children} & hit_ids)

            service.settings = replace(
                service.settings,
                change_order_procedure_split_chars=100_000,
            )
            rebuilt = service.rebuild_case_bundle(
                first["case_id"],
                actor="synthetic-operator",
                confirmation="REBUILD_CURRENT_CASE",
            )
            current_procedures = self.procedure_cards(service, first["case_id"])
            self.assertEqual(len(current_procedures), 3)
            self.assertEqual(
                sum(card["publish_status"] == "CONTAINER" for card in current_procedures),
                0,
            )
            self.assertEqual(rebuilt["cards_by_role"]["PROCEDURE_STEP"], 3)
            self.assertTrue(
                set(card["id"] for card in old_procedures).isdisjoint(
                    card["id"] for card in current_procedures
                )
            )
            detail = service.case_bundle_detail(first["case_id"])
            assert detail is not None
            self.assertEqual(len(detail["cards"]), rebuilt["extracted_cards"])

    def test_case_q_rebuild_purge_is_case_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source_a = self.write_source(
                root,
                "synthetic-rebuild-q-a.json",
                three_procedure_payload(ticket_id="SYNTH-CASE-A"),
            )
            payload_b = three_procedure_payload(ticket_id="SYNTH-CASE-B")
            payload_b["data"]["affected_service"] = "synthetic-service-b"
            for group in payload_b["data"]["sop_change_step"].values():
                for step in group:
                    step["check_name"] = f"B-{step['check_name']}"
                    step["operate_description"] = (
                        f"case-b 独立内容：{step['operate_description']}"
                    )
                    step["sop_step_id"] = f"case-b-{step['sop_step_id']}"
            source_b = self.write_source(
                root,
                "synthetic-rebuild-q-b.json",
                payload_b,
            )
            result_a = service.ingest_file(source_a)
            result_b = service.ingest_file(source_b)
            detail_b_before = service.case_bundle_detail(result_b["case_id"])
            assert detail_b_before is not None
            ids_b = detail_b_before["card_ids"]
            report_b = service.store.get_extraction_report(
                detail_b_before["document_id"]
            )

            service.rebuild_case_bundle(
                result_a["case_id"],
                actor="synthetic-operator",
                confirmation="REBUILD_CURRENT_CASE",
            )
            detail_b_after = service.case_bundle_detail(result_b["case_id"])
            assert detail_b_after is not None
            self.assertEqual(detail_b_after["card_ids"], ids_b)
            self.assertEqual(detail_b_after["build_generation"], 1)
            self.assertEqual(
                service.store.get_extraction_report(detail_b_after["document_id"]),
                report_b,
            )
            self.assertTrue(all(service.store.get_card(card_id) for card_id in ids_b))

    def test_case_r_k_ids_are_not_reused_but_semantic_identity_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = self.write_source(
                root,
                "synthetic-rebuild-r.json",
                three_procedure_payload(ticket_id="SYNTH-CASE-R"),
            )
            first = service.ingest_file(source)
            old_detail = service.case_bundle_detail(first["case_id"])
            assert old_detail is not None
            old_identity = {
                (
                    card["card_type"],
                    card["lineage"]["unit_pointer"],
                    card["semantic_fingerprint"],
                )
                for card in old_detail["cards"]
            }
            rebuilt = service.rebuild_case_bundle(
                first["case_id"],
                actor="synthetic-operator",
                confirmation="REBUILD_CURRENT_CASE",
            )
            new_detail = service.case_bundle_detail(first["case_id"])
            assert new_detail is not None
            new_identity = {
                (
                    card["card_type"],
                    card["lineage"]["unit_pointer"],
                    card["semantic_fingerprint"],
                )
                for card in new_detail["cards"]
            }
            self.assertEqual(old_identity, new_identity)
            self.assertTrue(
                set(first["card_ids"]).isdisjoint(rebuilt["card_ids"])
            )
            self.assertGreater(min(rebuilt["card_ids"]), max(first["card_ids"]))

    def test_demo_rebuild_http_endpoint_and_production_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = self.write_source(
                root,
                "synthetic-rebuild-api.json",
                three_procedure_payload(ticket_id="SYNTH-REBUILD-API"),
            )
            first = service.ingest_file(source)
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = (
                f"http://127.0.0.1:{server.server_address[1]}"
                f"/api/knowledge-case-bundles/"
                f"{first['case_id'].replace(':', '%3A')}/rebuild"
            )
            request = Request(
                endpoint,
                data=json.dumps(
                    {"confirmation": "REBUILD_CURRENT_CASE"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result["rebuild"]["current_generation"], 2)
                self.assertEqual(result["case_id"], first["case_id"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production = KnowledgeService(
                replace(
                    make_settings(root),
                    demo_mode=False,
                    demo_rebuild_enabled=True,
                ),
                client=FakeDeepSeekClient(),
            )
            self.assertFalse(
                production.settings.public_config()["demo_management"][
                    "rebuild_enabled"
                ]
            )
            with self.assertRaises(KnowledgeRequestError) as caught:
                production.rebuild_case_bundle(
                    "change-order:" + "0" * 64,
                    actor="synthetic-operator",
                    confirmation="REBUILD_CURRENT_CASE",
                )
            self.assertEqual(caught.exception.status, 404)

    def test_rebuild_retires_only_current_case_external_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            memory_settings = replace(
                service.settings,
                mindmemos_enabled=True,
                mindmemos_api_key="synthetic-memory-key",
                mindmemos_allow_content_export=True,
            )
            service.settings = memory_settings
            memory_client = TrackingMemoryClient()
            service.memory = MindMemOSBridge(
                memory_settings,
                service.store,
                client=memory_client,
            )
            source = self.write_source(
                root,
                "synthetic-rebuild-memory.json",
                three_procedure_payload(ticket_id="SYNTH-REBUILD-MEMORY"),
            )
            first = service.ingest_file(source)
            approved = service.review(
                first["card_ids"][0],
                action="approve",
                reviewer="synthetic-reviewer",
            )
            self.assertEqual(approved["memory_sync"]["status"], "SUCCEEDED")

            memory_client.fail_delete = True
            with self.assertRaises(KnowledgeServiceError):
                service.rebuild_case_bundle(
                    first["case_id"],
                    actor="synthetic-operator",
                    confirmation="REBUILD_CURRENT_CASE",
                )
            failed_detail = service.case_bundle_detail(first["case_id"])
            assert failed_detail is not None
            self.assertEqual(failed_detail["card_ids"], [])
            checksum = first["case_id"].split(":", 1)[1]
            self.assertFalse(
                (root / "reports" / checksum / "card_build_report.json").exists()
            )
            self.assertEqual(
                service.store.count_memory_retirements(
                    backend=MindMemOSBridge.BACKEND,
                    case_id=first["case_id"],
                ),
                2,
            )

            memory_client.fail_delete = False
            rebuilt = service.rebuild_case_bundle(
                first["case_id"],
                actor="synthetic-operator",
                confirmation="REBUILD_CURRENT_CASE",
            )
            self.assertEqual(
                set(memory_client.deleted),
                {"synthetic-memory-1", "synthetic-memory-2"},
            )
            self.assertEqual(rebuilt["rebuild"]["current_generation"], 3)
            self.assertEqual(
                rebuilt["memory_retirement_cleanup"],
                {"processed": 2, "removed": 2, "failed": 0},
            )
            self.assertEqual(
                service.store.list_memory_retirements(
                    backend=MindMemOSBridge.BACKEND,
                    limit=10,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
