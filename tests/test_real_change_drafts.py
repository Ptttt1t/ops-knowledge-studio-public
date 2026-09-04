from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from harness.api_client import APIError, APITimeoutError, JSONOutputError
from knowledge_platform.change_drafts import (
    ChangeDraftBindingError,
    ChangeDraftError,
    ChangeDraftStore,
    GENERATION_SYSTEM_PROMPT,
    PROCEDURE_NON_GENERATED_FIELDS,
    RealChangeDraftService,
)
from knowledge_platform.retrieval import SearchHit
from knowledge_platform.runtime_tasks import create_knowledge_runtime
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
    def __init__(
        self,
        *,
        invalid_first: bool = False,
        always_invalid: bool = False,
        malformed_first: bool = False,
    ):
        super().__init__()
        self.invalid_first = invalid_first
        self.always_invalid = always_invalid
        self.malformed_first = malformed_first
        self.generation_calls: list[tuple[str, str]] = []
        self.generation_kwargs: list[dict] = []

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
        self.generation_kwargs.append(dict(kwargs))
        if self.malformed_first and len(self.generation_calls) == 1:
            invalid_output = '{"title":"broken"'
            try:
                json.loads(invalid_output)
            except json.JSONDecodeError as exc:
                raise JSONOutputError(exc, raw_output=invalid_output) from exc
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


class ConcurrencyTrackingRealDraftClient(RealDraftClient):
    def __init__(self) -> None:
        super().__init__()
        self._active_lock = threading.Lock()
        self.active_model_requests = 0
        self.max_active_model_requests = 0

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if system_prompt != GENERATION_SYSTEM_PROMPT:
            return super().chat_json(system_prompt, user_prompt, **kwargs)
        with self._active_lock:
            self.active_model_requests += 1
            self.max_active_model_requests = max(
                self.max_active_model_requests, self.active_model_requests
            )
        try:
            time.sleep(0.08)
            return super().chat_json(system_prompt, user_prompt, **kwargs)
        finally:
            with self._active_lock:
                self.active_model_requests -= 1


class AccountLimitRealDraftClient(RealDraftClient):
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if system_prompt != GENERATION_SYSTEM_PROMPT:
            return super().chat_json(system_prompt, user_prompt, **kwargs)
        self.generation_calls.append((system_prompt, user_prompt))
        self.generation_kwargs.append(dict(kwargs))
        raise APIError(
            "DeepSeek API 返回 HTTP 429: synthetic account limit count 3 limit 2"
        )


class TimeoutRealDraftClient(RealDraftClient):
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if system_prompt != GENERATION_SYSTEM_PROMPT:
            return super().chat_json(system_prompt, user_prompt, **kwargs)
        self.generation_calls.append((system_prompt, user_prompt))
        self.generation_kwargs.append(dict(kwargs))
        raise APITimeoutError(int(kwargs["timeout_seconds"]))


class CapacityFailoverRealDraftClient(RealDraftClient):
    def chat_json(self, system_prompt, user_prompt, **kwargs):
        if system_prompt != GENERATION_SYSTEM_PROMPT:
            return super().chat_json(system_prompt, user_prompt, **kwargs)
        if kwargs.get("model") == "deepseek-v4-flash":
            self.generation_calls.append((system_prompt, user_prompt))
            self.generation_kwargs.append(dict(kwargs))
            raise APIError("synthetic account quota count 3 limit 2")
        return super().chat_json(system_prompt, user_prompt, **kwargs)


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


def synthetic_scene_reference() -> dict:
    return {
        "abbrev": None,
        "change_target_type": None,
        "coa_auto_auth": None,
        "deleted": None,
        "index": None,
        "name_cn": "虚构容量调整",
        "name_en": "synthetic capacity adjustment",
        "need_customer_review": None,
        "type": None,
        "unique_id": "SYNTH000001",
    }


class RealChangeDraftTests(unittest.TestCase):
    def test_generated_null_value_is_present_not_missing(self) -> None:
        missing: list[str] = ["tasks[0].nullable_present"]
        result = RealChangeDraftService._apply_field_policy(
            {"nullable_present": None, "required_text": None},
            {
                "nullable_present": {"type": "null", "policy": "generated"},
                "nullable_absent": {"type": "null", "policy": "generated"},
                "required_text": {"type": "string", "policy": "generated"},
            },
            {},
            location="tasks[0]",
            missing=missing,
        )

        self.assertIsNone(result["nullable_present"])
        self.assertIsNone(result["nullable_absent"])
        self.assertEqual(result["required_text"], "")
        self.assertNotIn("tasks[0].nullable_present", missing)
        self.assertIn("tasks[0].nullable_absent", missing)
        self.assertIn("tasks[0].required_text", missing)

    def test_existing_draft_database_gets_diagnostics_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy-change-drafts.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE change_drafts (
                        draft_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL DEFAULT '',
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        selected_case_ids TEXT NOT NULL,
                        held_out_case_id TEXT NOT NULL DEFAULT '',
                        profile_id TEXT NOT NULL,
                        current_revision INTEGER NOT NULL DEFAULT 0,
                        model_error TEXT NOT NULL DEFAULT '',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            store = ChangeDraftStore(database_path)
            connection = store.connect()
            try:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(change_drafts)")
                }
            finally:
                connection.close()

            self.assertIn("model_error_code", columns)
            self.assertIn("generation_diagnostics_json", columns)

    def make_service(
        self, root: Path, client: RealDraftClient, **setting_overrides
    ) -> KnowledgeService:
        settings = replace(
            make_settings(root),
            real_change_generation_enabled=True,
            change_draft_database_path=root / "data" / "change_drafts.db",
            change_generation_max_context_cards=24,
            **setting_overrides,
        )
        return KnowledgeService(settings, client=client)

    def prepare_case(
        self,
        service: KnowledgeService,
        root: Path,
        name: str = "case",
        *,
        payload: dict | None = None,
    ) -> dict:
        source_path = root / f"{name}.json"
        source_path.write_text(
            source_json(payload or make_semantic_payload()), encoding="utf-8"
        )
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

    def prepare_object_scene_case(
        self, service: KnowledgeService, root: Path, name: str = "object-scene"
    ) -> dict:
        payload = make_semantic_payload()
        payload["data"]["change_scene"] = synthetic_scene_reference()
        return self.prepare_case(service, root, name=name, payload=payload)

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
            self.assertTrue(
                all(
                    call["timeout_seconds"] == 240
                    for call in client.generation_kwargs
                )
            )
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
            self.assertTrue(
                all(
                    prompt_payload["schema_profile"]["procedure_fields"][name][
                        "policy"
                    ]
                    == "empty"
                    for name in PROCEDURE_NON_GENERATED_FIELDS
                )
            )
            self.assertFalse(
                any(
                    "sample" in spec
                    for spec in prompt_payload["schema_profile"]["task_fields"].values()
                )
            )
            self.assertTrue(
                all(
                    record.get(name) in (None, "", False, 0, [], {})
                    for records in change["data"]["sop_change_step"].values()
                    for record in records
                    for name in PROCEDURE_NON_GENERATED_FIELDS
                    if name in record
                )
            )

            tampered = deepcopy(change)
            tampered["data"]["sop_change_step"]["change_implement"][0][
                "result_confirm_notes"
            ] = "synthetic model claimed that a human check passed"
            tampered_validation = service.change_drafts._validate_assembled(
                tampered,
                normalized=generated["revision"]["normalized"],
                provenance=generated["revision"]["provenance"],
            )
            self.assertFalse(tampered_validation["passed"])
            self.assertIn(
                "PROCEDURE_NON_GENERATED_FIELDS_NOT_SANITIZED",
                tampered_validation["hard_failures"],
            )
            self.assertFalse(
                tampered_validation["gates"][
                    "procedure_non_generated_fields_sanitized"
                ]
            )
            diagnostics = generated["generation_diagnostics"]
            budget = diagnostics["generation_budget"]
            performance = diagnostics["generation_performance"]
            self.assertEqual(budget["timeout_seconds"], 240)
            self.assertEqual(budget["max_output_tokens"], 8192)
            self.assertEqual(budget["max_context_cards"], 24)
            self.assertEqual(budget["selected_case_count"], 1)
            self.assertGreater(budget["prompt_chars"], 0)
            self.assertGreaterEqual(budget["prompt_bytes"], budget["prompt_chars"])
            self.assertGreaterEqual(
                budget["available_context_card_count"],
                budget["selected_context_card_count"],
            )
            self.assertEqual(performance["model_request_count"], 2)
            self.assertEqual(performance["timeout_seconds"], 240)
            self.assertGreaterEqual(performance["elapsed_seconds"], 0)
            self.assertGreater(performance["response_chars"], 0)
            self.assertEqual(performance["total_tokens"], 125)
            self.assertEqual(diagnostics["protocol_repair_count"], 1)
            self.assertEqual(
                diagnostics["protocol_repairs"][0]["stage"], "DRAFT_ASSEMBLY"
            )
            self.assertIn(
                "模型草案缺少 tasks",
                diagnostics["protocol_repairs"][0]["reason"],
            )
            self.assertIn(
                "request 是本次变更的事实源",
                client.generation_calls[0][0],
            )
            self.assertEqual(
                generated["revision"]["model_usage"]["generation_budget"], budget
            )
            self.assertEqual(
                generated["revision"]["model_usage"]["generation_performance"],
                performance,
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

            legacy_unsafe_change = deepcopy(change)
            legacy_unsafe_change["data"]["sop_change_step"]["change_implement"][0][
                "result_confirm_notes"
            ] = "synthetic stale approval with a claimed human result"
            connection = service.change_drafts.store.connect()
            try:
                connection.execute(
                    "UPDATE change_draft_revisions SET change_json = ? "
                    "WHERE draft_id = ? AND revision = ?",
                    (
                        json.dumps(legacy_unsafe_change, ensure_ascii=False),
                        draft["draft_id"],
                        approved["current_revision"],
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(ChangeDraftError, "当前硬门禁"):
                service.change_drafts.export_draft(draft["draft_id"])

            edited = service.change_drafts.update_draft(
                draft["draft_id"],
                approved["revision"]["normalized"],
                actor="pilot-editor",
            )
            self.assertEqual(edited["current_revision"], 2)
            self.assertEqual(edited["status"], "READY_FOR_REVIEW")
            with self.assertRaisesRegex(ChangeDraftError, "只有人工审核通过"):
                service.change_drafts.export_draft(draft["draft_id"])

    def test_object_change_scene_is_built_from_input_with_profile_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_object_scene_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            request = real_request()
            request["scenario"] = "虚构链路切换"
            request["scenario_reference"] = {
                "name_cn": "虚构链路切换",
                "name_en": "synthetic route cutover",
                "unique_id": "SCENE000001",
            }

            draft = service.change_drafts.create_draft(
                request,
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )
            generated = service.change_drafts.generate(draft["draft_id"])

            scene = generated["revision"]["change"]["data"]["change_scene"]
            self.assertEqual(set(scene), set(synthetic_scene_reference()))
            self.assertEqual(scene["name_cn"], "虚构链路切换")
            self.assertEqual(scene["name_en"], "synthetic route cutover")
            self.assertEqual(scene["unique_id"], "SCENE000001")
            self.assertTrue(
                all(
                    scene[name] is None
                    for name in set(scene)
                    - {"name_cn", "name_en", "unique_id"}
                )
            )
            self.assertNotEqual(
                scene["unique_id"], synthetic_scene_reference()["unique_id"]
            )
            self.assertEqual(len(client.generation_calls), 1)

    def test_missing_scene_reference_values_block_without_copying_profile_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_object_scene_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])

            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )
            generated = service.change_drafts.generate(draft["draft_id"])

            scene = generated["revision"]["change"]["data"]["change_scene"]
            missing = generated["revision"]["normalized"]["missing_fields"]
            self.assertEqual(scene["name_cn"], real_request()["scenario"])
            self.assertEqual(scene["name_en"], "")
            self.assertEqual(scene["unique_id"], "")
            self.assertIn("context.change_scene.name_en", missing)
            self.assertIn("context.change_scene.unique_id", missing)
            self.assertEqual(generated["status"], "BLOCKED")
            self.assertEqual(len(client.generation_calls), 1)
            self.assertEqual(
                generated["generation_diagnostics"]["generation_budget"][
                    "preflight_missing_field_count"
                ],
                2,
            )

    def test_invalid_scene_binding_fails_before_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_object_scene_case(service, root)
            self.activate_profile(service, case["case_id"])
            request = real_request()
            request["parameters"] = {"change_scene": "not-an-object"}

            with self.assertRaisesRegex(
                ChangeDraftError, "parameters.change_scene 类型错误"
            ) as raised:
                service.change_drafts.create_draft(
                    request,
                    selected_case_ids=[case["case_id"]],
                    actor="pilot-operator",
                )

            self.assertEqual(
                raised.exception.code, "REQUEST_PROFILE_BINDING_ERROR"
            )
            self.assertEqual(len(client.generation_calls), 0)

    def test_late_deterministic_binding_error_does_not_protocol_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )

            def fail_assembly(_normalized, **_kwargs):
                raise ChangeDraftBindingError("synthetic deterministic binding")

            service.change_drafts._assemble = fail_assembly  # type: ignore[method-assign]
            with self.assertRaisesRegex(
                ChangeDraftBindingError, "synthetic deterministic binding"
            ):
                service.change_drafts.generate(draft["draft_id"])

            self.assertEqual(len(client.generation_calls), 1)
            failed = service.change_drafts.store.get_draft(draft["draft_id"])
            assert failed is not None
            self.assertEqual(
                failed["model_error_code"], "REQUEST_PROFILE_BINDING_ERROR"
            )
            self.assertEqual(
                failed["generation_diagnostics"]["model_request_count"], 1
            )

    def test_parameters_change_scene_remains_supported_for_api_clients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_object_scene_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            request = real_request()
            request["parameters"] = {
                "change_scene": {
                    "name_cn": "虚构参数切换",
                    "name_en": "synthetic parameter cutover",
                    "unique_id": "SCENE000002",
                }
            }

            draft = service.change_drafts.create_draft(
                request,
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )
            generated = service.change_drafts.generate(draft["draft_id"])

            scene = generated["revision"]["change"]["data"]["change_scene"]
            self.assertEqual(scene["name_cn"], "虚构参数切换")
            self.assertEqual(scene["name_en"], "synthetic parameter cutover")
            self.assertEqual(scene["unique_id"], "SCENE000002")
            self.assertEqual(generated["status"], "READY_FOR_REVIEW")
            self.assertEqual(len(client.generation_calls), 1)

    def test_real_change_generation_model_calls_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = ConcurrencyTrackingRealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            drafts = [
                service.change_drafts.create_draft(
                    real_request(),
                    selected_case_ids=[case["case_id"]],
                    actor=f"pilot-{index}",
                )
                for index in range(3)
            ]
            results: list[dict] = []
            errors: list[Exception] = []

            def generate(draft_id: str) -> None:
                try:
                    results.append(service.change_drafts.generate(draft_id))
                except Exception as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            threads = [
                threading.Thread(target=generate, args=(draft["draft_id"],))
                for draft in drafts
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 3)
            self.assertEqual(
                {result["status"] for result in results}, {"READY_FOR_REVIEW"}
            )
            self.assertEqual(client.max_active_model_requests, 1)
            self.assertEqual(service.settings.change_draft_model_max_concurrency, 1)
            self.assertTrue(client.generation_kwargs)
            self.assertTrue(
                all(item.get("exclusive") is True for item in client.generation_kwargs)
            )
            self.assertTrue(
                all(item.get("http_retries") == 0 for item in client.generation_kwargs)
            )
            self.assertTrue(
                all(
                    item.get("model") == service.settings.model
                    for item in client.generation_kwargs
                )
            )

    def test_account_limit_error_does_not_trigger_hidden_or_protocol_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = AccountLimitRealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )

            with self.assertRaisesRegex(APIError, "HTTP 429"):
                service.change_drafts.generate(draft["draft_id"])

            self.assertEqual(len(client.generation_calls), 1)
            self.assertEqual(client.generation_kwargs[0]["http_retries"], 0)
            self.assertTrue(client.generation_kwargs[0]["exclusive"])
            failed = service.change_drafts.store.get_draft(draft["draft_id"])
            assert failed is not None
            self.assertEqual(failed["status"], "GENERATION_FAILED")

    def test_timeout_stops_after_one_request_without_protocol_retry_or_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = TimeoutRealDraftClient()
            service = self.make_service(
                root,
                client,
                change_draft_fallback_model="synthetic-qwen-27b",
                change_draft_fallback_model_max_concurrency=6,
            )
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )

            with self.assertRaisesRegex(
                ChangeDraftError,
                "超过当前 240 秒等待上限，本次未自动重试",
            ) as raised:
                service.change_drafts.generate(draft["draft_id"])

            self.assertEqual(raised.exception.code, "MODEL_TIMEOUT")
            self.assertEqual(len(client.generation_calls), 1)
            self.assertEqual(client.generation_kwargs[0]["http_retries"], 0)
            self.assertEqual(client.generation_kwargs[0]["timeout_seconds"], 240)
            self.assertEqual(
                client.generation_kwargs[0]["model"], "deepseek-v4-flash"
            )
            self.assertTrue(client.generation_kwargs[0]["exclusive"])
            failed = service.change_drafts.store.get_draft(draft["draft_id"])
            assert failed is not None
            self.assertEqual(failed["status"], "GENERATION_FAILED")
            self.assertEqual(failed["model_error_code"], "MODEL_TIMEOUT")
            self.assertEqual(
                failed["model_error"],
                "真实变更草案生成超过当前 240 秒等待上限，本次未自动重试。",
            )
            diagnostics = failed["generation_diagnostics"]
            self.assertEqual(diagnostics["error_code"], "MODEL_TIMEOUT")
            self.assertEqual(
                diagnostics["generation_performance"]["model_request_count"], 1
            )
            self.assertEqual(
                diagnostics["generation_budget"]["selected_context_cards"],
                diagnostics["generation_budget"]["selected_context_card_count"],
            )

    def test_timeout_diagnostics_are_preserved_in_runtime_step_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = TimeoutRealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )
            runtime = create_knowledge_runtime(service, worker_count=1)
            try:
                submitted, created = runtime.submit(
                    "change.generate_real_draft",
                    {"draft_id": draft["draft_id"], "actor": "pilot-operator"},
                )
                completed = runtime.wait(submitted["id"], timeout_seconds=5)
            finally:
                runtime.stop()

            self.assertTrue(created)
            assert completed is not None
            self.assertEqual(completed["status"], "FAILED")
            self.assertEqual(completed["error_code"], "MODEL_TIMEOUT")
            checkpoint = runtime.store.latest_checkpoint(submitted["id"])
            assert checkpoint is not None
            state = checkpoint["state"]
            self.assertEqual(state["phase"], "real_change_draft_generation_failed")
            self.assertEqual(state["error_code"], "MODEL_TIMEOUT")
            self.assertEqual(
                state["generation_diagnostics"]["model_request_count"], 1
            )
            steps = runtime.store.list_steps(submitted["id"])
            self.assertEqual(steps[-1]["error_code"], "MODEL_TIMEOUT")

    def test_capacity_error_automatically_uses_configured_fallback_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = CapacityFailoverRealDraftClient()
            service = self.make_service(
                root,
                client,
                change_draft_fallback_model="synthetic-qwen-27b",
                change_draft_fallback_model_max_concurrency=6,
            )
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )

            generated = service.change_drafts.generate(draft["draft_id"])

            self.assertEqual(generated["status"], "READY_FOR_REVIEW")
            self.assertEqual(len(client.generation_calls), 2)
            self.assertEqual(
                [call["model"] for call in client.generation_kwargs],
                ["deepseek-v4-flash", "synthetic-qwen-27b"],
            )
            self.assertEqual(
                [call["model_max_concurrency"] for call in client.generation_kwargs],
                [2, 6],
            )
            routing = generated["revision"]["model_usage"]["model_routing"]
            self.assertEqual(routing["selected_model"], "synthetic-qwen-27b")
            self.assertTrue(routing["fallback_used"])
            self.assertTrue(routing["primary_capacity_failure"])
            self.assertEqual(
                routing["attempted_models"],
                ["deepseek-v4-flash", "synthetic-qwen-27b"],
            )

    def test_protocol_failure_stops_without_partial_or_fallback_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient(always_invalid=True)
            service = self.make_service(
                root,
                client,
                change_draft_fallback_model="synthetic-qwen-27b",
                change_draft_fallback_model_max_concurrency=6,
            )
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
            self.assertEqual(
                [call["model"] for call in client.generation_kwargs],
                ["deepseek-v4-flash", "deepseek-v4-flash"],
            )
            self.assertEqual(failed["status"], "GENERATION_FAILED")
            self.assertIsNone(failed["revision"])

    def test_malformed_json_gets_one_explicit_protocol_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient(malformed_first=True)
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            self.activate_profile(service, case["case_id"])
            self.force_bundle_hits(service, case["case_id"])
            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )

            generated = service.change_drafts.generate(draft["draft_id"])

            self.assertEqual(generated["status"], "READY_FOR_REVIEW")
            self.assertEqual(len(client.generation_calls), 2)
            self.assertTrue(
                all(call.get("retries") == 0 for call in client.generation_kwargs)
            )
            self.assertTrue(
                all(
                    call.get("timeout_seconds") == 240
                    for call in client.generation_kwargs
                )
            )
            self.assertTrue(
                all(
                    call.get("max_tokens")
                    == service.settings.change_generation_max_output_tokens
                    for call in client.generation_kwargs
                )
            )
            self.assertIn(
                '"previous_output":"{\\"title\\":\\"broken\\""',
                client.generation_calls[1][1],
            )
            self.assertEqual(
                generated["generation_diagnostics"]["protocol_repair_count"], 1
            )
            self.assertEqual(
                generated["generation_diagnostics"]["protocol_repairs"][0][
                    "stage"
                ],
                "JSON_PARSE",
            )

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

            unsafe_procedure_profile = deepcopy(profile)
            unsafe_procedure_profile["procedure_fields"][
                "result_confirm_notes"
            ]["policy"] = "generated"
            with self.assertRaisesRegex(ChangeDraftError, "只允许 empty"):
                service.change_drafts.activate_schema_profile(
                    unsafe_procedure_profile, actor="schema-admin"
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

    def test_legacy_active_profile_is_safely_overridden_during_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = RealDraftClient()
            service = self.make_service(root, client)
            case = self.prepare_case(service, root)
            profile = service.change_drafts.inspect_schema_profile(case["case_id"])
            legacy_profile = deepcopy(profile)
            for name in PROCEDURE_NON_GENERATED_FIELDS:
                legacy_profile["procedure_fields"][name]["policy"] = "generated"
            service.change_drafts.store.activate_profile(
                legacy_profile, actor="legacy-schema-admin"
            )
            displayed_profile = service.change_drafts.profile_status()["active_profile"]
            self.assertTrue(
                all(
                    displayed_profile["procedure_fields"][name]["policy"] == "empty"
                    for name in PROCEDURE_NON_GENERATED_FIELDS
                )
            )
            self.force_bundle_hits(service, case["case_id"])

            draft = service.change_drafts.create_draft(
                real_request(),
                selected_case_ids=[case["case_id"]],
                actor="pilot-operator",
            )
            generated = service.change_drafts.generate(draft["draft_id"])

            prompt = json.loads(client.generation_calls[0][1].split("\n\n", 1)[0])
            self.assertTrue(
                all(
                    prompt["schema_profile"]["procedure_fields"][name]["policy"]
                    == "empty"
                    for name in PROCEDURE_NON_GENERATED_FIELDS
                )
            )
            self.assertTrue(generated["revision"]["validation"]["passed"])
            self.assertTrue(
                generated["revision"]["validation"]["gates"][
                    "procedure_non_generated_fields_sanitized"
                ]
            )
            procedure = generated["revision"]["change"]["data"][
                "sop_change_step"
            ]
            self.assertTrue(
                all(
                    record.get(name) in (None, "", False, 0, [], {})
                    for records in procedure.values()
                    for record in records
                    for name in PROCEDURE_NON_GENERATED_FIELDS
                    if name in record
                )
            )

            edited_normalized = deepcopy(generated["revision"]["normalized"])
            edited_step = edited_normalized["procedure"]["change_implement"][0][
                "record"
            ]
            edited_step.update(
                {
                    "result_confirm": "synthetic-human-result",
                    "result_confirm_notes": "synthetic-human-notes",
                    "update_user": "synthetic-human",
                    "update_time": 1900000000000,
                }
            )
            edited = service.change_drafts.update_draft(
                draft["draft_id"], edited_normalized, actor="pilot-editor"
            )
            edited_step = edited["revision"]["change"]["data"]["sop_change_step"][
                "change_implement"
            ][0]
            self.assertTrue(
                all(
                    edited_step[name] in (None, "", False, 0, [], {})
                    for name in PROCEDURE_NON_GENERATED_FIELDS
                )
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
