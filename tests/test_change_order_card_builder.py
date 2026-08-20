from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from knowledge_platform.change_order_adapter import build_change_order_extraction_plan
from knowledge_platform.change_order_cards import (
    CardType,
    ChangeOrderCardBuilder,
    ChangeOrderCardBuilderConfig,
    SemanticKnowledgeCard,
    normalize_rich_text,
    normalize_timestamp,
)
from knowledge_platform.service import KnowledgeService

from tests.test_platform import FakeDeepSeekClient, make_settings


FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "change_order_card_builder"
        / "cases.json"
    ).read_text(encoding="utf-8")
)


def base_step(**overrides: object) -> dict[str, object]:
    step: dict[str, object] = {
        "check_name": "虚构变更步骤",
        "operate_description": "检查虚构服务状态。",
        "operate_verified": "确认虚构服务状态正常。",
        "operate_rollback": "恢复虚构参数。",
        "impact_analysis": "无影响",
        "operate_commond": "",
        "command_list": [],
        "action_risk_level": "low",
        "sop_step_id": "synthetic-step",
        "label": "synthetic",
        "tool_unique_ids": [],
        "action_unique_ids": [],
        "tool_related_actions": [],
        "update_time": 1785772800000,
        "update_user": "synthetic-user",
        "is_involved_step": True,
        "result_confirm_notes": "",
        "region": "region-lab-a",
        "container": "demo-api",
        "cluster_name": "cluster-lab-a",
    }
    step.update(overrides)
    assert len(step) == 20
    return step


def empty_step() -> dict[str, object]:
    return base_step(
        check_name="",
        operate_description="",
        operate_verified="",
        operate_rollback="",
        impact_analysis="",
        operate_commond="",
        command_list=[],
    )


def make_payload(
    *,
    context: dict[str, object] | None = None,
    actions: list[dict[str, object]] | None = None,
    groups: dict[str, list[dict[str, object]]] | None = None,
    result: dict[str, object] | None = None,
) -> dict[str, object]:
    action_rows = deepcopy(actions or FIXTURE["case_e"])
    procedure = groups or {
        "check_before_change": [base_step()],
        "change_implement": [
            base_step(
                check_name="实施虚构变更",
                operate_description="调整 demo-api 的虚构容量参数并保存。",
            )
        ],
        "change_verified": [
            base_step(
                check_name="验证虚构变更",
                operate_description="读取 demo-api 的虚构配置并检查监控状态。",
            )
        ],
        "change_rollback": [
            base_step(
                check_name="回退虚构变更",
                operate_description="恢复 demo-api 的虚构原始参数并复核状态。",
            )
        ],
    }
    return {
        "code": 0,
        "provider_code": "SYNTHETIC_OK",
        "msg": "synthetic-success",
        "data": {
            **deepcopy(context or FIXTURE["case_f"]),
            "action_list": action_rows,
            "change_tool_relate_action": {
                f"synthetic_group_{index + 1}": [deepcopy(action)]
                for index, action in enumerate(action_rows)
            },
            "sop_change_step": procedure,
            "change_plan": [{"result": deepcopy(result or FIXTURE["case_g"])}],
        },
    }


def build(
    payload: dict[str, object],
    config: ChangeOrderCardBuilderConfig | None = None,
):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    plan, report = build_change_order_extraction_plan(text, chunk_size=12_000)
    assert plan is not None, report
    return ChangeOrderCardBuilder(config).build(
        text, plan, source_name="synthetic.json"
    )


class ChangeOrderCardBuilderTests(unittest.TestCase):
    def test_case_a_single_step_maps_fields_and_cleans_rich_text(self):
        groups = {
            "check_before_change": [base_step(**FIXTURE["case_a"])],
            "change_implement": [base_step(check_name="实施虚构变更")],
            "change_verified": [base_step(check_name="验证虚构变更")],
            "change_rollback": [base_step(check_name="回退虚构变更")],
        }
        result = build(make_payload(groups=groups))
        card = next(
            card
            for card in result.cards
            if card.card_type is CardType.PROCEDURE_STEP
            and card.procedure_group == "PRECHECK"
        )
        payload = card.semantic_payload
        body = card.body_text()
        self.assertEqual(payload["title"], "前检：单步骤容量检查")
        self.assertIn("不涉及业务中断", payload["operation"])
        self.assertEqual(payload["validation"], "确认配置读取成功，无影响。")
        self.assertEqual(payload["rollback"], "不涉及")
        self.assertEqual(payload["impact_analysis"], "无影响")
        self.assertIn("[图片证据]", body)
        self.assertNotIn("<p>", body)
        self.assertNotIn("https://", body)
        self.assertFalse(card.qa["has_raw_json"])
        self.assertFalse(card.qa["has_html_residue"])

    def test_case_b_adjacent_source_steps_remain_separate(self):
        groups = {
            "check_before_change": [
                base_step(**item) for item in FIXTURE["case_b"]
            ],
            "change_implement": [base_step(check_name="实施虚构变更")],
            "change_verified": [base_step(check_name="验证虚构变更")],
            "change_rollback": [base_step(check_name="回退虚构变更")],
        }
        result = build(make_payload(groups=groups))
        prechecks = [
            card
            for card in result.cards
            if card.card_type is CardType.PROCEDURE_STEP
            and card.procedure_group == "PRECHECK"
        ]
        self.assertEqual(len(prechecks), 2)
        self.assertEqual([card.procedure_step_index for card in prechecks], [0, 1])

    def test_case_c_sections_are_parsed_but_retained_in_parent(self):
        groups = {
            "check_before_change": [base_step(**FIXTURE["case_c"])],
            "change_implement": [base_step(check_name="实施虚构变更")],
            "change_verified": [base_step(check_name="验证虚构变更")],
            "change_rollback": [base_step(check_name="回退虚构变更")],
        }
        result = build(make_payload(groups=groups))
        source_pointer = "/data/sop_change_step/check_before_change/0"
        related = [
            card
            for card in result.cards
            if card.card_type is CardType.PROCEDURE_STEP
            and card.source_identities[0]["source_pointer"] == source_pointer
        ]
        self.assertEqual(len(related), 1)
        children = [
            card
            for card in related
            if "semantic_section" in card.semantic_payload
        ]
        self.assertEqual(len(children), 0)
        sections = related[0].semantic_payload["operation_sections"]
        self.assertEqual(len(sections), 8)
        self.assertIn("方式一", sections[-1]["section_body"])
        self.assertIn("方式二", sections[-1]["section_body"])

    def test_case_d_cross_phase_semantic_reuse(self):
        groups = {
            "check_before_change": [base_step(check_name="前检虚构服务")],
            "change_implement": [base_step(check_name="实施虚构变更")],
            "change_verified": [base_step(**FIXTURE["case_d"]["validation"])],
            "change_rollback": [base_step(**FIXTURE["case_d"]["rollback"])],
        }
        result = build(make_payload(groups=groups))
        reused = [
            card
            for card in result.cards
            if set(card.applicable_phases) == {"VALIDATION", "ROLLBACK"}
        ]
        self.assertEqual(len(reused), 1)
        self.assertEqual(result.report["semantic_reuse_count"], 1)
        self.assertEqual(reused[0].dedup_status, "REUSED")
        self.assertEqual(len(reused[0].source_identities), 2)

    def test_case_e_actions_are_metadata_not_procedure_cards(self):
        result = build(make_payload())
        context = next(
            card for card in result.cards if card.card_type is CardType.CASE_CONTEXT
        )
        self.assertEqual(
            [item["action_name"] for item in context.semantic_payload["actions"]],
            ["ActionA", "ActionB", "ActionC"],
        )
        self.assertFalse(
            any(card.title == "ActionA 操作步骤" for card in result.cards)
        )
        self.assertTrue(
            any(
                item["unit_role"] == "TASKS_CANONICAL"
                and item["skip_reason"] == "MERGED_INTO_CASE_CONTEXT"
                for item in result.report["skipped_units"]
            )
        )

    def test_case_f_context_units_merge_to_one_card(self):
        result = build(make_payload())
        contexts = [
            card for card in result.cards if card.card_type is CardType.CASE_CONTEXT
        ]
        self.assertEqual(len(contexts), 1)
        self.assertEqual(result.report["case_context_count"], 1)
        self.assertNotIn("IDENTITY_METADATA_CONTEXT 结构化知识", contexts[0].title)
        self.assertGreaterEqual(
            sum(
                item["skip_reason"] == "MERGED_INTO_CASE_CONTEXT"
                for item in result.report["skipped_units"]
            ),
            1,
        )

    def test_case_g_execution_outcome_is_not_planning_knowledge(self):
        result = build(make_payload())
        outcome = next(
            card
            for card in result.cards
            if card.card_type is CardType.EXECUTION_OUTCOME
        )
        self.assertFalse(outcome.planning_rag_enabled)
        self.assertEqual(outcome.semantic_payload["outcome"]["change_result"], "SUCCESS")
        self.assertEqual(result.report["execution_outcome_count"], 1)

    def test_case_h_timestamp_is_deterministic_and_timezone_explicit(self):
        normalized = normalize_timestamp(
            FIXTURE["case_h"]["timestamp"],
            timezone_name=FIXTURE["case_h"]["timezone"],
        )
        self.assertEqual(normalized["normalized"], FIXTURE["case_h"]["expected"])
        self.assertEqual(normalized["timezone"], "Asia/Shanghai")

    def test_case_i_five_item_checklist_stays_one_indexed_parent(self):
        groups = {
            "check_before_change": [base_step(**FIXTURE["case_i"])],
            "change_implement": [empty_step()],
            "change_verified": [empty_step()],
            "change_rollback": [empty_step()],
        }
        result = build(make_payload(groups=groups))
        procedures = [
            card for card in result.cards if card.card_type is CardType.PROCEDURE_STEP
        ]
        self.assertEqual(len(procedures), 1)
        self.assertEqual(len(procedures[0].semantic_payload["operation_sections"]), 5)
        self.assertTrue(procedures[0].retrieval_enabled)
        self.assertEqual(procedures[0].publish_status, "INDEXED")
        self.assertEqual(result.report["procedure_parent_count"], 1)
        self.assertEqual(result.report["procedure_child_count"], 0)
        self.assertEqual(result.report["indexed_procedure_count"], 1)
        self.assertEqual(result.report["noop_section_count"], 3)

    def test_case_j_analysis_template_with_noops_does_not_split(self):
        groups = {
            "check_before_change": [base_step(**FIXTURE["case_j"])],
            "change_implement": [empty_step()],
            "change_verified": [empty_step()],
            "change_rollback": [empty_step()],
        }
        result = build(make_payload(groups=groups))
        procedure = next(
            card for card in result.cards if card.card_type is CardType.PROCEDURE_STEP
        )
        self.assertEqual(len(procedure.semantic_payload["operation_sections"]), 9)
        self.assertFalse(procedure.parent_unit_id)
        self.assertEqual(result.report["procedure_child_count"], 0)
        self.assertGreaterEqual(result.report["noop_section_count"], 7)

    def test_case_k_nested_numbering_is_retained_as_subitems(self):
        groups = {
            "check_before_change": [base_step(**FIXTURE["case_k"])],
            "change_implement": [empty_step()],
            "change_verified": [empty_step()],
            "change_rollback": [empty_step()],
        }
        result = build(make_payload(groups=groups))
        procedure = next(
            card for card in result.cards if card.card_type is CardType.PROCEDURE_STEP
        )
        sections = procedure.semantic_payload["operation_sections"]
        self.assertEqual(len(sections), 2)
        backup = next(section for section in sections if section["section_title"] == "Backup")
        self.assertEqual([item["title"] for item in backup["subitems"]], ["Backup rule A", "Backup rule B"])
        self.assertEqual(result.report["procedure_child_count"], 0)
        self.assertEqual(
            sum(
                item["skip_reason"] == "NESTED_SECTION_RETAINED_AS_SUBITEM"
                for item in result.report["skipped_sections"]
            ),
            2,
        )

    def test_case_l_genuinely_long_step_indexes_children_only(self):
        source = FIXTURE["case_l"]
        operation = "\n".join(
            f"{index}. {title}\n{source['body_seed'] * 8}"
            for index, title in enumerate(source["section_titles"], start=1)
        )
        groups = {
            "check_before_change": [
                base_step(
                    check_name=source["check_name"],
                    operate_description=operation,
                )
            ],
            "change_implement": [empty_step()],
            "change_verified": [empty_step()],
            "change_rollback": [empty_step()],
        }
        result = build(
            make_payload(groups=groups),
            ChangeOrderCardBuilderConfig(
                long_step_chars=1000,
                semantic_section_threshold=3,
                child_min_content_chars=200,
            ),
        )
        procedures = [
            card for card in result.cards if card.card_type is CardType.PROCEDURE_STEP
        ]
        parent = next(card for card in procedures if not card.parent_unit_id)
        children = [card for card in procedures if card.parent_unit_id]
        self.assertEqual(parent.publish_status, "CONTAINER")
        self.assertFalse(parent.retrieval_enabled)
        self.assertEqual(len(children), 3)
        self.assertTrue(all(card.publish_status == "INDEXED" for card in children))
        self.assertTrue(all(card.retrieval_enabled for card in children))
        self.assertEqual(
            {
                card.semantic_payload["source_procedure_pointer"]
                for card in [parent, *children]
            },
            {"/data/sop_change_step/check_before_change/0"},
        )
        self.assertEqual(result.report["container_procedure_count"], 1)
        self.assertEqual(result.report["indexed_procedure_count"], 3)
        self.assertEqual(result.report["parent_child_retrieval_collision_count"], 0)

    def test_case_m_phase_aware_titles_distinguish_restore(self):
        groups = {
            "check_before_change": [empty_step()],
            "change_implement": [base_step(**FIXTURE["case_m"]["implementation"])],
            "change_verified": [empty_step()],
            "change_rollback": [base_step(**FIXTURE["case_m"]["rollback"])],
        }
        result = build(make_payload(groups=groups))
        titles = {
            card.procedure_group: card.title
            for card in result.cards
            if card.card_type is CardType.PROCEDURE_STEP
        }
        self.assertEqual(titles["IMPLEMENTATION"], "实施：修改系统参数")
        self.assertEqual(titles["ROLLBACK"], "回退：恢复系统参数原值")
        self.assertEqual(result.report["title_collision_count"], 0)

    def test_case_n_high_confidence_relationships_are_deterministic(self):
        groups = {
            "check_before_change": [empty_step()],
            "change_implement": [
                base_step(
                    check_name="Set synthetic parameter",
                    operate_description=FIXTURE["case_n"]["implementation"],
                )
            ],
            "change_verified": [
                base_step(
                    check_name="Verify synthetic parameter",
                    operate_description=FIXTURE["case_n"]["validation"],
                )
            ],
            "change_rollback": [
                base_step(
                    check_name="Restore synthetic parameter",
                    operate_description=FIXTURE["case_n"]["rollback"],
                )
            ],
        }
        result = build(make_payload(groups=groups))
        self.assertEqual(result.report["relationship_count"]["validates"], 1)
        self.assertEqual(result.report["relationship_count"]["rollback_of"], 1)
        self.assertEqual(
            {item["relation_type"] for item in result.relationships},
            {"VALIDATES", "ROLLBACK_OF"},
        )

    def test_service_persists_semantic_model_and_card_build_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                make_settings(root),
                max_change_order_chunks=100,
                change_order_card_report_dir=root / "reports",
            )
            client = FakeDeepSeekClient()
            service = KnowledgeService(settings, client=client)
            result = service.ingest_text(
                source_name="synthetic-change-order.json",
                source_type="json",
                content=json.dumps(make_payload(), ensure_ascii=False, indent=2),
            )
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["cards_by_role"]["CASE_CONTEXT"], 1)
            self.assertEqual(result["cards_by_role"]["EXECUTION_OUTCOME"], 1)
            self.assertTrue(Path(result["card_build_report_path"]).is_file())
            report = json.loads(
                Path(result["card_build_report_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(report["structural_source_coverage"]["status"], "COMPLETE")
            self.assertEqual(report["semantic_content_coverage"]["status"], "COMPLETE")
            self.assertTrue(all(item["card_id"] for item in report["cards"]))
            cards = [service.card_detail(card_id) for card_id in result["card_ids"]]
            self.assertTrue(all(card["card_model_version"] == "change_order_card_model_v2" for card in cards))
            self.assertTrue(all(card["review_status"] == card["status"] for card in cards))
            self.assertTrue(all("{" not in card["evidence_quote"] for card in cards))
            self.assertTrue(all("/data/" not in card["evidence_quote"] for card in cards))
            self.assertFalse(
                any("/data/" in "\n".join(card["procedure_steps"]) for card in cards)
            )

    def test_cross_document_duplicate_is_approved_separately_but_not_indexed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(
                make_settings(root),
                max_change_order_chunks=100,
                change_order_card_report_dir=root / "reports",
            )
            service = KnowledgeService(settings, client=FakeDeepSeekClient())
            first_payload = make_payload()
            first = service.ingest_text(
                source_name="synthetic-a.json",
                source_type="json",
                content=json.dumps(first_payload, ensure_ascii=False, indent=2),
            )
            second_payload = make_payload()
            second_payload["data"]["ticket_id"] = "SYNTH-CO-0002"
            second_payload["data"]["title"] = "另一虚构案例复用相同步骤"
            second_payload["data"]["change_plan"][0]["result"]["change_id"] = "SYNTH-CO-0002"
            second = service.ingest_text(
                source_name="synthetic-b.json",
                source_type="json",
                content=json.dumps(second_payload, ensure_ascii=False, indent=2),
            )
            first_ids = set(first["card_ids"])
            second_cards = [
                service.card_detail(card_id) for card_id in second["card_ids"]
            ]
            duplicates = [
                card
                for card in second_cards
                if card["card_type"] == "PROCEDURE_STEP"
            ]
            self.assertTrue(duplicates)
            self.assertTrue(
                all(card["dedup_status"] == "DUPLICATE" for card in duplicates)
            )
            self.assertTrue(
                all(card["publish_status"] == "SKIPPED" for card in duplicates)
            )
            self.assertEqual(
                second["card_build_report"]["indexed_procedure_count"],
                0,
            )
            hits = service.retriever.search(
                "demo-api 虚构容量参数",
                statuses=["PENDING_REVIEW"],
                top_k=50,
            )
            self.assertTrue(first_ids & {int(hit.card["id"]) for hit in hits})
            self.assertFalse(
                {int(card["id"]) for card in duplicates}
                & {int(hit.card["id"]) for hit in hits}
            )

    def test_normalize_rich_text_preserves_negative_business_facts(self):
        normalized = normalize_rich_text("<p>不涉及&nbsp;业务影响<br>无影响</p>")
        self.assertIn("不涉及", normalized.text)
        self.assertIn("无影响", normalized.text)
        self.assertNotIn("&nbsp;", normalized.text)

    def test_content_qa_caps_raw_json_and_html_pollution(self):
        card = SemanticKnowledgeCard(
            card_type=CardType.PROCEDURE_STEP,
            title="污染检测",
            semantic_payload={
                "title": "污染检测",
                "check_name": "污染检测",
                "operation": '/data/example: {"unsafe": true}\n<p>残留</p>',
                "generalized_operation": '/data/example: {"unsafe": true}\n<p>残留</p>',
                "validation": "",
                "rollback": "",
                "impact_analysis": "",
                "operate_command": "",
                "command_list": [],
                "source_facts": [],
                "inferred_facts": [],
            },
            source_identities=[],
            source_evidence_refs=[],
            source_order=0,
            source_chunk_index=0,
            procedure_group="PRECHECK",
            procedure_step_index=0,
            applicable_phases=["PRECHECK"],
        )
        card.finalize()
        self.assertTrue(card.qa["has_raw_json"])
        self.assertTrue(card.qa["has_html_residue"])
        self.assertLess(card.qa["content_quality"], 100)


if __name__ == "__main__":
    unittest.main()
