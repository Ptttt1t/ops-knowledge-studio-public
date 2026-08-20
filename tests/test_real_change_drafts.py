from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from knowledge_platform.change_drafts import ChangeDraftError, GENERATION_SYSTEM_PROMPT
from knowledge_platform.retrieval import SearchHit
from knowledge_platform.service import KnowledgeService
from knowledge_platform.web import create_server
from tests.test_change_order_extraction import (
    StructuralFakeClient,
    make_change_order,
    source_json,
)
from tests.test_platform import make_settings
from tests.test_change_order_card_builder import make_payload as make_semantic_payload


ROLE_FIELD = {
    "TASKS_CANONICAL": "procedure_steps",
    "PRECHECK_STEPS": "procedure_steps",
    "IMPLEMENTATION_STEPS": "procedure_steps",
    "VALIDATION_STEPS": "validation_steps",
    "ROLLBACK_STEPS": "rollback_steps",
}


class RealDraftClient(StructuralFakeClient):
    def __init__(self, *, invalid_first: bool = False, always_invalid: bool = False):
        super().__init__()
        self.invalid_first = invalid_first
        self.always_invalid = always_invalid
        self.generation_calls: list[tuple[str, str]] = []

    @staticmethod
    def _value(type_name: str, label: str):
        return {
            "string": label,
            "integer": 1,
            "number": 1.0,
            "boolean": False,
            "array": [],
            "object": {},
            "null": None,
        }[type_name]

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if system_prompt != GENERATION_SYSTEM_PROMPT:
            return super().chat_json(system_prompt, user_prompt, **kwargs)
        self.generation_calls.append((system_prompt, user_prompt))
        if self.always_invalid or (self.invalid_first and len(self.generation_calls) == 1):
            return {"invalid": True}, {"total_tokens": 5}
        prompt = json.loads(user_prompt.split("\n\n", 1)[0])
        approved_cards = prompt["approved_cards"]
        context = next(
            card for card in approved_cards if card["unit_role"] == "CASE_CONTEXT"
        )
        procedures = {
            phase: card
            for card in approved_cards
            if card["unit_role"] == "PROCEDURE_STEP"
            for phase in card.get("applicable_phases") or []
        }
        task_specs = prompt["schema_profile"]["task_fields"]
        procedure_specs = prompt["schema_profile"]["procedure_fields"]

        def record(specs, prefix):
            return {
                name: self._value(str(spec["type"]), f"{prefix}-{name}")
                for name, spec in specs.items()
            }

        def source(role):
            if role == "TASKS_CANONICAL":
                card = context
                field = "actions"
                index = 0
            else:
                phase = {
                    "PRECHECK_STEPS": "PRECHECK",
                    "IMPLEMENTATION_STEPS": "IMPLEMENTATION",
                    "VALIDATION_STEPS": "VALIDATION",
                    "ROLLBACK_STEPS": "ROLLBACK",
                }[role]
                card = procedures[phase]
                field = "generalized_operation"
                index = None
            return {
                "card_id": card["card_id"],
                "field": field,
                "index": index,
            }

        task = {
            "group": "pilot-group",
            "record": record(task_specs, "task"),
            "source_refs": [source("TASKS_CANONICAL")],
            "input_refs": [],
        }
        procedure = {}
        for key, role in (
            ("check_before_change", "PRECHECK_STEPS"),
            ("change_implement", "IMPLEMENTATION_STEPS"),
            ("change_verified", "VALIDATION_STEPS"),
            ("change_rollback", "ROLLBACK_STEPS"),
        ):
            procedure[key] = [
                {
                    "record": record(procedure_specs, key),
                    "source_refs": [source(role)],
                    "input_refs": [],
                }
            ]
        identity = context
        return (
            {
                "title": "真实变更能力试验草案",
                "summary": "只生成、校验和审核，不执行。",
                "tasks": [task],
                "procedure": procedure,
                "risks": [
                    {
                        "text": "变更可能影响业务，应严格执行验证与回退。",
                        "source_refs": [
                            {
                                "card_id": identity["card_id"],
                                "field": "summary",
                                "index": None,
                            }
                        ],
                        "input_refs": [],
                    }
                ],
                "missing_fields": [],
            },
            {"total_tokens": 120},
        )


def real_request() -> dict:
    return {
        "goal": "完成脱敏结构回归变更",
        "scenario": "route cutover",
        "region": "internal-region-1",
        "services": ["order-service"],
        "objects": ["anonymous-change-target"],
        "current_state": "当前路径稳定，等待维护窗口",
        "target_state": "目标路径生效且业务验证通过",
        "window": {"start": "2026-08-18 23:00 +08:00", "end": "2026-08-19 01:00 +08:00"},
        "impact_scope": "仅限试验业务",
        "constraints": ["不得调用真实执行工具"],
        "parameters": {},
        "validation_requirements": ["确认目标状态", "确认回退可用"],
        "requester": "pilot-operator",
    }


class RealChangeDraftTests(unittest.TestCase):
    def make_service(self, root: Path, client: RealDraftClient) -> KnowledgeService:
        settings = replace(
            make_settings(root),
            real_change_generation_enabled=True,
            change_draft_database_path=root / "data" / "change_drafts.db",
            change_generation_max_context_cards=24,
        )
        return KnowledgeService(settings, client=client)

    def prepare_case(self, service: KnowledgeService, root: Path, name: str = "case") -> dict:
        source_path = root / f"{name}.json"
        source_path.write_text(source_json(make_semantic_payload()), encoding="utf-8")
        result = service.ingest_file(source_path)
        service.review_case_bundle(
            result["case_id"], action="approve", reviewer="schema-admin"
        )
        return result

    @staticmethod
    def force_bundle_hits(service: KnowledgeService, case_id: str) -> None:
        bundle = service.case_bundle_detail(case_id)
        assert bundle is not None

        def trusted_hits(_query, *, top_k, for_generation):
            assert top_k == 50
            assert for_generation
            return (
                [
                    SearchHit(
                        card=card,
                        score=30.0 - index,
                        matched_terms=["change"],
                        query_coverage=1.0,
                    )
                    for index, card in enumerate(bundle["cards"])
                ],
                {"mode": "test", "candidate_count": len(bundle["cards"])},
            )

        service.trusted_search_hits = trusted_hits  # type: ignore[method-assign]

    def activate_profile(self, service: KnowledgeService, case_id: str) -> dict:
        profile = service.change_drafts.inspect_schema_profile(case_id)
        return service.change_drafts.activate_schema_profile(
            profile, actor="schema-admin"
        )

    def test_generate_review_export_and_edit_invalidates_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient(invalid_first=True)
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            profile = self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])

            recommended = service.change_drafts.recommend(real_request())
            self.assertEqual(
                [item["case_id"] for item in recommended["candidates"]],
                [case["case_id"]],
            )
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )
            generated = service.change_drafts.generate(draft["draft_id"])

            self.assertEqual(len(client.generation_calls), 2)
            self.assertEqual(generated["status"], "READY_FOR_REVIEW")
            self.assertTrue(generated["revision"]["validation"]["passed"])
            self.assertTrue(
                all(generated["revision"]["validation"]["gates"].values())
            )
            change = generated["revision"]["change"]
            self.assertEqual(
                change["data"]["action_list"],
                change["data"]["change_tool_relate_action"]["pilot-group"],
            )
            self.assertTrue(
                all(
                    value in (None, "", "NOT_EXECUTED", False, 0, [], {})
                    for value in change["data"]["change_plan"][0]["result"].values()
                )
            )
            prompt_payload = json.loads(client.generation_calls[-1][1].split("\n\n", 1)[0])
            self.assertFalse(
                any(
                    "sample" in spec
                    for spec in prompt_payload["schema_profile"]["task_fields"].values()
                )
            )

            approved = service.change_drafts.review_draft(
                draft["draft_id"],
                decision="APPROVED",
                reviewer="self-reported-reviewer",
                comment="试验审核通过",
            )
            self.assertEqual(approved["status"], "REVIEW_APPROVED")
            exported = service.change_drafts.export_draft(draft["draft_id"])
            self.assertEqual(exported["change_order"], change)
            self.assertEqual(exported["provenance_report"]["profile_id"], profile["profile_id"])

            edited = service.change_drafts.update_draft(
                draft["draft_id"],
                approved["revision"]["normalized"],
                actor="pilot-editor",
            )
            self.assertEqual(edited["current_revision"], 2)
            self.assertEqual(edited["status"], "READY_FOR_REVIEW")
            with self.assertRaisesRegex(ChangeDraftError, "只有人工审核通过"):
                service.change_drafts.export_draft(draft["draft_id"])

    def test_protocol_failure_stops_without_partial_or_fallback_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient(always_invalid=True)
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )

            with self.assertRaises(ChangeDraftError):
                service.change_drafts.generate(draft["draft_id"])
            failed = service.change_drafts.store.get_draft(draft["draft_id"])
            assert failed is not None
            self.assertEqual(len(client.generation_calls), 2)
            self.assertEqual(failed["status"], "GENERATION_FAILED")
            self.assertIsNone(failed["revision"])

    def test_profile_conflict_and_unbound_specific_parameters_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root, RealDraftClient())
            case = self.prepare_case(service, root)
            profile = service.change_drafts.inspect_schema_profile(case["case_id"])
            conflicting = deepcopy(profile)
            first_name = next(iter(conflicting["task_fields"]))
            conflicting["task_fields"][f"{first_name}_drift"] = conflicting[
                "task_fields"
            ].pop(first_name)
            with self.assertRaisesRegex(ChangeDraftError, "字段集合"):
                service.change_drafts.activate_schema_profile(
                    conflicting, actor="schema-admin"
                )

            request = real_request()
            self.assertEqual(
                service.change_drafts._specific_parameter_violations(
                    {"target_ip": "10.20.30.40"},
                    input_refs=[],
                    request=request,
                ),
                ["record.target_ip"],
            )
            request["parameters"] = {"target_ip": "10.20.30.40"}
            self.assertEqual(
                service.change_drafts._specific_parameter_violations(
                    {"target_ip": "10.20.30.40"},
                    input_refs=["parameters.target_ip"],
                    request=request,
                ),
                [],
            )

    def test_blind_target_is_excluded_and_cannot_be_profile_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])

            result = service.change_drafts.recommend(
                real_request(), held_out_case_id=case["case_id"]
            )
            self.assertEqual(result["candidates"], [])
            self.assertIn(
                "BLIND_TARGET_OR_NEAR_DUPLICATE",
                [reason for item in result["rejected"] for reason in item["reasons"]],
            )
            with self.assertRaisesRegex(ChangeDraftError, "SchemaProfile"):
                service.change_drafts.create_evaluation(
                    case["case_id"], actor="evaluation-operator"
                )

    def test_leave_one_out_evaluation_uses_only_independent_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root, RealDraftClient())
            cases = []
            for index, values in enumerate(
                (
                    ("ALPHA ORBIT SATURN DATABASE", "alpha-saturn-telemetry", "mars-1"),
                    ("BETA OCEAN CORAL PAYMENTS", "beta-coral-finance", "ocean-9"),
                )
            ):
                payload = make_semantic_payload()
                payload["data"]["ticket_id"] = f"CHG-BLIND-{index}"
                payload["data"]["title"] = values[0]
                payload["data"]["change_scene"] = values[1]
                payload["data"]["region"] = values[2]
                payload["data"]["affected_service"] = values[0]
                path = root / f"blind-{index}.json"
                path.write_text(source_json(payload), encoding="utf-8")
                result = service.ingest_file(path)
                service.review_case_bundle(
                    result["case_id"], action="approve", reviewer="schema-admin"
                )
                cases.append(result)
            self.activate_profile(service, cases[0]["case_id"])
            bundles = [service.case_bundle_detail(item["case_id"]) for item in cases]
            cards = [card for bundle in bundles if bundle for card in bundle["cards"]]

            def trusted_hits(_query, *, top_k, for_generation):
                return (
                    [
                        SearchHit(
                            card=card,
                            score=40.0 - index,
                            matched_terms=["blind"],
                            query_coverage=1.0,
                        )
                        for index, card in enumerate(cards)
                    ],
                    {"mode": "blind-test"},
                )

            service.trusted_search_hits = trusted_hits  # type: ignore[method-assign]
            evaluation = service.change_drafts.create_evaluation(
                cases[1]["case_id"], actor="evaluation-operator"
            )
            completed = service.change_drafts.run_evaluation(
                evaluation["evaluation_id"], actor="evaluation-operator"
            )

            self.assertEqual(completed["status"], "COMPLETED")
            report = completed["report"]
            self.assertEqual(report["selected_case_ids"], [cases[0]["case_id"]])
            self.assertTrue(report["leakage_check"]["target_excluded"])
            self.assertNotIn(cases[1]["case_id"], report["selected_case_ids"])
            self.assertTrue(report["soft_scores_only"])
            self.assertIn("task_matching", report["soft_metrics"])
            self.assertEqual(report["soft_metrics"]["expert_review"]["status"], "PENDING")

    def test_http_recommend_async_generate_review_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root, RealDraftClient())
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            server = create_server(service, host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request_json(path: str, *, method: str = "GET", payload=None):
                body = None if payload is None else json.dumps(payload).encode("utf-8")
                request = Request(
                    base + path,
                    data=body,
                    headers={"Content-Type": "application/json"} if body else {},
                    method=method,
                )
                with urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))

            try:
                status, recommendations = request_json(
                    "/api/change-drafts/recommend",
                    method="POST",
                    payload={"request": real_request()},
                )
                self.assertEqual(status, 200)
                selected = [recommendations["candidates"][0]["case_id"]]
                status, created = request_json(
                    "/api/change-drafts",
                    method="POST",
                    payload={"request": real_request(), "selected_case_ids": selected},
                )
                self.assertEqual(status, 202)
                draft_id = created["draft"]["draft_id"]
                deadline = time.monotonic() + 10
                draft = created["draft"]
                while draft["status"] == "GENERATING" and time.monotonic() < deadline:
                    time.sleep(0.05)
                    _, draft = request_json(f"/api/change-drafts/{draft_id}")
                self.assertEqual(draft["status"], "READY_FOR_REVIEW")

                status, reviewed = request_json(
                    f"/api/change-drafts/{draft_id}/review",
                    method="POST",
                    payload={
                        "decision": "APPROVED",
                        "reviewer": "self-reported-http-reviewer",
                        "comment": "接口集成测试",
                    },
                )
                self.assertEqual(status, 200)
                self.assertEqual(reviewed["status"], "REVIEW_APPROVED")
                status, exported = request_json(f"/api/change-drafts/{draft_id}/export")
                self.assertEqual(status, 200)
                self.assertEqual(exported["revision"], 1)
                self.assertTrue(exported["validation_report"]["passed"])

                status, edited = request_json(
                    f"/api/change-drafts/{draft_id}",
                    method="PATCH",
                    payload={"normalized": reviewed["revision"]["normalized"]},
                )
                self.assertEqual(status, 200)
                self.assertEqual(edited["current_revision"], 2)
                self.assertEqual(edited["status"], "READY_FOR_REVIEW")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
