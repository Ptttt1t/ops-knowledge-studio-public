from __future__ import annotations

import json
from pathlib import Path
import unittest

from knowledge_platform.change_order_adapter import (
    build_change_order_extraction_plan,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = PROJECT_ROOT / "sample_data" / "realistic_change_orders"

EXPECTED_CASES = {
    "01-direct_connect_route_failover.json": (6, (5, 6, 5, 4), 23),
    "02-nat_egress_blue_green_migration.json": (8, (6, 8, 6, 6), 29),
    "03-ipsec_vpn_key_and_bgp_cutover.json": (10, (7, 10, 7, 8), 35),
    "04-security_group_micro_segmentation.json": (12, (6, 12, 8, 12), 41),
    "05-transit_hub_route_domain_migration.json": (14, (8, 14, 10, 12), 47),
}

PROCEDURE_MAPPING = {
    "check_before_change": ("PRECHECK_STEPS", "PRECHECK"),
    "change_implement": ("IMPLEMENTATION_STEPS", "IMPLEMENTATION"),
    "change_verified": ("VALIDATION_STEPS", "VALIDATION"),
    "change_rollback": ("ROLLBACK_STEPS", "ROLLBACK"),
}


class RealisticChangeOrderSampleTests(unittest.TestCase):
    def test_samples_follow_confirmed_source_shape(self) -> None:
        paths = sorted(SAMPLE_DIR.glob("*.json"))
        self.assertEqual([path.name for path in paths], list(EXPECTED_CASES))

        for path in paths:
            with self.subTest(path=path.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                data = payload["data"]
                expected_tasks, expected_steps, _ = EXPECTED_CASES[path.name]

                self.assertEqual(
                    {"code", "provider_code", "msg"},
                    {key for key in payload if key != "data"},
                )
                self.assertTrue(data["synthetic_demo_data"])
                self.assertEqual(
                    data["data_classification"],
                    "SYNTHETIC_DEIDENTIFIED_DEMO",
                )
                self.assertTrue(data["title"].startswith("[合成演示]"))

                canonical_tasks = data["action_list"]
                self.assertEqual(len(canonical_tasks), expected_tasks)
                task_keys = set(canonical_tasks[0])
                self.assertEqual(len(task_keys), 13)
                self.assertTrue(all(set(item) == task_keys for item in canonical_tasks))

                grouped = data["change_tool_relate_action"]
                self.assertIsInstance(grouped, dict)
                self.assertGreaterEqual(len(grouped), 1)
                self.assertTrue(
                    all(isinstance(group, list) for group in grouped.values())
                )
                self.assertTrue(any(group for group in grouped.values()))
                projected_tasks = [item for group in grouped.values() for item in group]
                self.assertEqual(projected_tasks, canonical_tasks)

                procedure = data["sop_change_step"]
                self.assertEqual(set(procedure), set(PROCEDURE_MAPPING))
                all_step_keys: set[str] | None = None
                for index, source_key in enumerate(PROCEDURE_MAPPING):
                    steps = procedure[source_key]
                    self.assertEqual(len(steps), expected_steps[index])
                    self.assertTrue(steps)
                    for step in steps:
                        self.assertEqual(len(step), 20)
                        if all_step_keys is None:
                            all_step_keys = set(step)
                        self.assertEqual(set(step), all_step_keys)
                        container_count = sum(
                            isinstance(value, (dict, list)) for value in step.values()
                        )
                        self.assertEqual(container_count, 7)

                execution = data["change_plan"][0]["result"]
                self.assertEqual(len(execution), 15)
                self.assertEqual(
                    sum(isinstance(value, list) for value in execution.values()),
                    1,
                )
                self.assertFalse(
                    any(isinstance(value, dict) for value in execution.values())
                )

    def test_adapter_reconciles_every_sample_without_semantic_guessing(self) -> None:
        for path_name, (expected_tasks, expected_steps, expected_units) in (
            EXPECTED_CASES.items()
        ):
            with self.subTest(path=path_name):
                text = (SAMPLE_DIR / path_name).read_text(encoding="utf-8")
                plan, report = build_change_order_extraction_plan(
                    text,
                    chunk_size=12_000,
                )

                self.assertIsNotNone(plan)
                assert plan is not None
                self.assertTrue(report["matched"])
                self.assertEqual(report["adapter"], "change_order_shape_v2")
                self.assertEqual(report["semantic_mapping_status"], "CONFIRMED")
                self.assertTrue(report["safe_for_internal_index"])
                self.assertFalse(report["safe_for_external_publish"])
                self.assertEqual(report["publish_scope"], "INTERNAL_ONLY")
                self.assertEqual(report["blockers"], [])

                tasks = report["task_record"]
                self.assertEqual(tasks["flat_count"], expected_tasks)
                self.assertEqual(tasks["grouped_count"], expected_tasks)
                self.assertEqual(tasks["exact_record_matches"], expected_tasks)
                self.assertEqual(tasks["flat_unmatched"], 0)
                self.assertEqual(tasks["grouped_unmatched"], 0)
                self.assertTrue(tasks["reconciled"])

                groups = report["procedure"]["groups"]
                self.assertEqual(
                    [group["source_key"] for group in groups],
                    list(PROCEDURE_MAPPING),
                )
                self.assertEqual(
                    [group["role"] for group in groups],
                    [value[0] for value in PROCEDURE_MAPPING.values()],
                )
                self.assertEqual(
                    tuple(group["step_count"] for group in groups),
                    expected_steps,
                )
                self.assertTrue(
                    all(
                        group["semantic_mapping_status"] == "CONFIRMED"
                        for group in groups
                    )
                )

                coverage = report["coverage"]
                self.assertEqual(coverage["structural_coverage_ratio"], 1.0)
                self.assertEqual(coverage["structural_node_coverage_ratio"], 1.0)
                self.assertEqual(coverage["uncovered"], 0)
                self.assertEqual(coverage["nodes_uncovered"], 0)
                self.assertEqual(coverage["excluded_api_envelope"], 3)
                self.assertFalse(report["api_envelope"]["include_in_rag"])
                self.assertEqual(
                    report["post_execution"]["execution_result"]["role"],
                    "EXECUTION_RESULT",
                )
                self.assertFalse(
                    report["post_execution"]["execution_result"][
                        "include_in_generation"
                    ]
                )
                self.assertEqual(len(plan.units), expected_units)
                self.assertFalse(
                    any(unit.role == "TASKS_GROUPED" for unit in plan.units)
                )

                for role, procedure_group in PROCEDURE_MAPPING.values():
                    units = [unit for unit in plan.units if unit.role == role]
                    self.assertTrue(units)
                    self.assertTrue(
                        all(unit.procedure_group == procedure_group for unit in units)
                    )
                    self.assertEqual(units[0].step_start_index, 0)
                    self.assertEqual(
                        units[-1].step_end_index,
                        units[-1].total_steps_in_group - 1,
                    )
                    self.assertEqual(
                        sum(unit.item_count for unit in units),
                        units[0].total_steps_in_group,
                    )
                    for previous, current in zip(units, units[1:]):
                        self.assertEqual(
                            current.step_start_index,
                            previous.step_end_index + 1,
                        )


if __name__ == "__main__":
    unittest.main()
