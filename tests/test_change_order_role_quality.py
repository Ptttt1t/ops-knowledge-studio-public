from __future__ import annotations

import unittest

from knowledge_platform.schema import KnowledgeCardDraft


SOURCE = "合成变更单原文证据"


def make_card(**overrides: object) -> KnowledgeCardDraft:
    payload: dict[str, object] = {
        "title": "合成变更知识",
        "summary": "用于验证角色化质量规则。",
        "knowledge_type": "procedure",
        "scenario": "变更单结构化抽取",
        "object_name": "demo-resource",
        "evidence_quote": SOURCE,
    }
    payload.update(overrides)
    return KnowledgeCardDraft.from_dict(payload)


class ChangeOrderRoleQualityTests(unittest.TestCase):
    def test_generic_procedure_keeps_legacy_rollback_requirement(self):
        draft = make_card(procedure_steps=["执行变更"])

        _, issues = draft.quality(SOURCE)

        self.assertIn("缺少回退步骤", issues)
        self.assertIn("缺少风险说明", issues)
        self.assertIn("缺少验证步骤", issues)

    def test_precheck_and_implementation_do_not_require_rollback(self):
        for role in ("PRECHECK_STEPS", "IMPLEMENTATION_STEPS"):
            with self.subTest(role=role):
                draft = make_card(procedure_steps=["按源顺序保留完整步骤"])

                score, issues = draft.quality(SOURCE, unit_role=role)

                self.assertEqual(score, 100.0)
                self.assertNotIn("缺少回退步骤", issues)
                self.assertNotIn("缺少风险说明", issues)
                self.assertNotIn("缺少验证步骤", issues)

    def test_validation_role_only_requires_validation_steps(self):
        complete = make_card(validation_steps=["验证连通率"])
        missing = make_card()

        complete_score, complete_issues = complete.quality(
            SOURCE,
            unit_role="VALIDATION_STEPS",
        )
        _, missing_issues = missing.quality(
            SOURCE,
            unit_role="VALIDATION_STEPS",
        )

        self.assertEqual(complete_score, 100.0)
        self.assertEqual(complete_issues, [])
        self.assertEqual(missing_issues, ["缺少验证步骤"])

    def test_rollback_role_only_requires_rollback_steps(self):
        complete = make_card(rollback_steps=["恢复原始下一跳"])
        missing = make_card()

        complete_score, complete_issues = complete.quality(
            SOURCE,
            unit_role="ROLLBACK_STEPS",
        )
        _, missing_issues = missing.quality(
            SOURCE,
            unit_role="ROLLBACK_STEPS",
        )

        self.assertEqual(complete_score, 100.0)
        self.assertEqual(complete_issues, [])
        self.assertEqual(missing_issues, ["缺少回退步骤"])

    def test_execution_result_does_not_require_risk(self):
        draft = make_card(knowledge_type="case")

        score, issues = draft.quality(SOURCE, unit_role="EXECUTION_RESULT")

        self.assertEqual(score, 100.0)
        self.assertNotIn("缺少事件影响或风险说明", issues)

    def test_context_roles_only_apply_their_own_responsibility(self):
        context = make_card()
        risk = make_card()

        context_score, context_issues = context.quality(
            SOURCE,
            unit_role="IDENTITY_METADATA_CONTEXT",
        )
        _, risk_issues = risk.quality(SOURCE, unit_role="RISK_IMPACT")

        self.assertEqual(context_score, 100.0)
        self.assertEqual(context_issues, [])
        self.assertEqual(risk_issues, ["缺少风险说明"])

    def test_unknown_role_keeps_legacy_quality_rules(self):
        draft = make_card(procedure_steps=["执行变更"])

        _, issues = draft.quality(SOURCE, unit_role="FUTURE_UNKNOWN_ROLE")

        self.assertIn("缺少回退步骤", issues)


if __name__ == "__main__":
    unittest.main()
