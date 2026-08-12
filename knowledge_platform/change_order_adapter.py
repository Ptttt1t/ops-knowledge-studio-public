from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Iterable

from .documents import DocumentChunk, chunk_text


class ChangeOrderAdapterError(ValueError):
    """Raised when JSON cannot be inspected without losing structural fidelity."""


class SemanticMappingStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    HEURISTIC = "HEURISTIC"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class ChangeOrderUnitRole(str, Enum):
    API_ENVELOPE = "API_ENVELOPE"
    IDENTITY = "IDENTITY"
    SERVICE_SCOPE = "SERVICE_SCOPE"
    CHANGE_CONTEXT = "CHANGE_CONTEXT"
    RISK_IMPACT = "RISK_IMPACT"
    EXECUTION_CONTEXT = "EXECUTION_CONTEXT"
    GOVERNANCE_CONTEXT = "GOVERNANCE_CONTEXT"
    IDENTITY_METADATA_CONTEXT = "IDENTITY_METADATA_CONTEXT"
    TASKS_CANONICAL = "TASKS_CANONICAL"
    TASKS_GROUPED_UNRECONCILED = "TASKS_GROUPED_UNRECONCILED"
    PRECHECK_STEPS = "PRECHECK_STEPS"
    IMPLEMENTATION_STEPS = "IMPLEMENTATION_STEPS"
    VALIDATION_STEPS = "VALIDATION_STEPS"
    ROLLBACK_STEPS = "ROLLBACK_STEPS"
    UNMAPPED_PROCEDURE_STEPS = "UNMAPPED_PROCEDURE_STEPS"
    EXECUTION_RESULT = "EXECUTION_RESULT"


@dataclass(frozen=True)
class JsonSpanNode:
    pointer: str
    kind: str
    value: Any
    value_start: int
    member_start: int
    end: int
    key: str | None = None
    children: tuple["JsonSpanNode", ...] = ()

    @property
    def field_count(self) -> int:
        return len(self.children) if self.kind == "object" else 0


@dataclass(frozen=True)
class ChangeOrderExtractionUnit:
    chunk: DocumentChunk
    role: str
    pointer: str
    source_pointers: tuple[str, ...]
    item_count: int
    semantic_hint: str
    semantic_mapping_status: str = SemanticMappingStatus.HEURISTIC.value
    include_in_rag: bool = True
    include_in_generation: bool = True
    lifecycle_stage: str = "planning_context"
    procedure_group: str | None = None
    step_start_index: int | None = None
    step_end_index: int | None = None
    total_steps_in_group: int | None = None
    context_roles: tuple[str, ...] = ()

    def prompt_context(self) -> str:
        procedure_context = ""
        if self.procedure_group is not None:
            procedure_context = (
                f"procedure_group={self.procedure_group}；"
                f"step_start_index={self.step_start_index}；"
                f"step_end_index={self.step_end_index}；"
                f"total_steps_in_group={self.total_steps_in_group}；"
            )
        return (
            f"结构化变更单单元：role={self.role}；JSON Pointer={self.pointer or '/'}；"
            f"语义映射={self.semantic_mapping_status}；{procedure_context}"
            f"源对象数={self.item_count}。"
            f"{self.semantic_hint}"
        )

    def lineage_metadata(self) -> dict[str, Any]:
        return {
            "semantic_mapping_status": self.semantic_mapping_status,
            "include_in_rag": self.include_in_rag,
            "include_in_generation": self.include_in_generation,
            "lifecycle_stage": self.lifecycle_stage,
            "procedure_group": self.procedure_group,
            "step_start_index": self.step_start_index,
            "step_end_index": self.step_end_index,
            "total_steps_in_group": self.total_steps_in_group,
            "context_roles": list(self.context_roles),
        }


@dataclass(frozen=True)
class ChangeOrderExtractionPlan:
    units: tuple[ChangeOrderExtractionUnit, ...]
    report: dict[str, Any]


class _JsonSpanParser:
    MAX_DEPTH = 64
    MAX_NODES = 50_000

    def __init__(self, text: str):
        self.text = text
        self.decoder = json.JSONDecoder(parse_constant=self._reject_constant)
        self.node_count = 0

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise ChangeOrderAdapterError(f"JSON 包含非标准数值常量: {value}")

    def parse(self) -> JsonSpanNode:
        node, position = self._parse_value(0, "", 0, member_start=None, key=None)
        position = self._skip_space(position)
        if position != len(self.text):
            raise ChangeOrderAdapterError("JSON 根对象之后存在额外内容")
        return node

    def _skip_space(self, position: int) -> int:
        length = len(self.text)
        while position < length and self.text[position] in " \t\r\n":
            position += 1
        return position

    def _parse_value(
        self,
        position: int,
        pointer: str,
        depth: int,
        *,
        member_start: int | None,
        key: str | None,
    ) -> tuple[JsonSpanNode, int]:
        if depth > self.MAX_DEPTH:
            raise ChangeOrderAdapterError("JSON 嵌套深度超过结构分析限制")
        self.node_count += 1
        if self.node_count > self.MAX_NODES:
            raise ChangeOrderAdapterError("JSON 节点数超过结构分析限制")

        position = self._skip_space(position)
        if position >= len(self.text):
            raise ChangeOrderAdapterError("JSON 值不完整")
        value_start = position
        start = value_start if member_start is None else member_start
        marker = self.text[position]

        if marker == "{":
            position += 1
            children: list[JsonSpanNode] = []
            value: dict[str, Any] = {}
            seen_keys: set[str] = set()
            position = self._skip_space(position)
            if position < len(self.text) and self.text[position] == "}":
                position += 1
                return (
                    JsonSpanNode(
                        pointer,
                        "object",
                        value,
                        value_start,
                        start,
                        position,
                        key,
                        tuple(children),
                    ),
                    position,
                )
            while True:
                position = self._skip_space(position)
                key_start = position
                try:
                    raw_key, position = self.decoder.raw_decode(self.text, position)
                except ChangeOrderAdapterError:
                    raise
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ChangeOrderAdapterError("JSON 对象 Key 无法解析") from exc
                if not isinstance(raw_key, str):
                    raise ChangeOrderAdapterError("JSON 对象 Key 必须是字符串")
                if raw_key in seen_keys:
                    raise ChangeOrderAdapterError(
                        "JSON 含重复 Key，结构化抽取会产生歧义"
                    )
                seen_keys.add(raw_key)
                position = self._skip_space(position)
                if position >= len(self.text) or self.text[position] != ":":
                    raise ChangeOrderAdapterError("JSON 对象缺少冒号")
                position += 1
                child_pointer = pointer + "/" + _escape_pointer(raw_key)
                child, position = self._parse_value(
                    position,
                    child_pointer,
                    depth + 1,
                    member_start=key_start,
                    key=raw_key,
                )
                children.append(child)
                value[raw_key] = child.value
                position = self._skip_space(position)
                if position >= len(self.text):
                    raise ChangeOrderAdapterError("JSON 对象未闭合")
                if self.text[position] == "}":
                    position += 1
                    break
                if self.text[position] != ",":
                    raise ChangeOrderAdapterError("JSON 对象成员之间缺少逗号")
                position += 1
            return (
                JsonSpanNode(
                    pointer,
                    "object",
                    value,
                    value_start,
                    start,
                    position,
                    key,
                    tuple(children),
                ),
                position,
            )

        if marker == "[":
            position += 1
            children = []
            value_list: list[Any] = []
            position = self._skip_space(position)
            if position < len(self.text) and self.text[position] == "]":
                position += 1
                return (
                    JsonSpanNode(
                        pointer,
                        "array",
                        value_list,
                        value_start,
                        start,
                        position,
                        key,
                        tuple(children),
                    ),
                    position,
                )
            index = 0
            while True:
                child_pointer = pointer + f"/{index}"
                child, position = self._parse_value(
                    position,
                    child_pointer,
                    depth + 1,
                    member_start=None,
                    key=None,
                )
                children.append(child)
                value_list.append(child.value)
                index += 1
                position = self._skip_space(position)
                if position >= len(self.text):
                    raise ChangeOrderAdapterError("JSON 数组未闭合")
                if self.text[position] == "]":
                    position += 1
                    break
                if self.text[position] != ",":
                    raise ChangeOrderAdapterError("JSON 数组元素之间缺少逗号")
                position += 1
            return (
                JsonSpanNode(
                    pointer,
                    "array",
                    value_list,
                    value_start,
                    start,
                    position,
                    key,
                    tuple(children),
                ),
                position,
            )

        try:
            value, position = self.decoder.raw_decode(self.text, position)
        except ChangeOrderAdapterError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ChangeOrderAdapterError("JSON 标量值无法解析") from exc
        if isinstance(value, (dict, list)):
            raise ChangeOrderAdapterError("JSON 容器解析状态异常")
        return (
            JsonSpanNode(
                pointer,
                "scalar",
                value,
                value_start,
                start,
                position,
                key,
            ),
            position,
        )


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk(node: JsonSpanNode) -> Iterable[JsonSpanNode]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _is_descendant(pointer: str, ancestor: str) -> bool:
    return pointer == ancestor or ancestor == "" or pointer.startswith(ancestor + "/")


def _record_signature(array: JsonSpanNode, field_count: int) -> tuple[str, ...] | None:
    if array.kind != "array" or not array.children:
        return None
    signature: tuple[str, ...] | None = None
    for child in array.children:
        if child.kind != "object" or child.field_count != field_count:
            return None
        current = tuple(sorted(str(item.key) for item in child.children))
        if signature is None:
            signature = current
        elif current != signature:
            return None
    return signature


def _container_record_signature(
    node: JsonSpanNode,
    *,
    array_count_min: int,
    array_count_max: int,
    field_count: int,
) -> tuple[str, ...] | None:
    if node.kind != "object" or not (
        array_count_min <= len(node.children) <= array_count_max
    ):
        return None
    if not all(child.kind == "array" for child in node.children):
        return None
    signatures = [
        signature
        for child in node.children
        if (signature := _record_signature(child, field_count)) is not None
    ]
    nonempty = [child for child in node.children if child.children]
    if not nonempty or len(signatures) != len(nonempty):
        return None
    if any(signature != signatures[0] for signature in signatures[1:]):
        return None
    return signatures[0]


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _presence_state(node: JsonSpanNode) -> str:
    if node.kind == "scalar":
        if node.value is None:
            return "NULL"
        if node.value == "":
            return "EMPTY"
        return "VALUE"
    return "EMPTY" if not node.children else "VALUE"


def _task_pair_report(
    flat: JsonSpanNode, grouped: JsonSpanNode
) -> dict[str, Any]:
    flat_counter = Counter(_fingerprint(child.value) for child in flat.children)
    grouped_records = [
        record for array in grouped.children for record in array.children
    ]
    grouped_counter = Counter(_fingerprint(child.value) for child in grouped_records)
    matched = sum((flat_counter & grouped_counter).values())
    flat_count = len(flat.children)
    grouped_count = len(grouped_records)
    return {
        "flat_path": flat.pointer or "/",
        "grouped_path": grouped.pointer or "/",
        "flat_count": flat_count,
        "grouped_count": grouped_count,
        "group_sizes": [len(array.children) for array in grouped.children],
        "exact_record_matches": matched,
        "flat_unmatched": flat_count - matched,
        "grouped_unmatched": grouped_count - matched,
        "fingerprint_collisions": sum(
            count - 1 for count in flat_counter.values() if count > 1
        ),
        "reconciled": flat_count == grouped_count == matched,
    }


def _task_pair_score(
    flat: JsonSpanNode, grouped: JsonSpanNode, report: dict[str, Any]
) -> tuple[int, int, int, int]:
    exact = int(report["exact_record_matches"])
    flat_count = int(report["flat_count"])
    return (
        1 if exact == flat_count and flat_count else 0,
        exact,
        flat_count,
        _common_prefix_depth(flat.pointer, grouped.pointer),
    )


def _execution_candidate(node: JsonSpanNode) -> bool:
    if node.kind != "object" or node.field_count != 15:
        return False
    arrays = sum(child.kind == "array" for child in node.children)
    scalars = sum(child.kind == "scalar" for child in node.children)
    return arrays == 1 and scalars == 14


def _candidate_summary(nodes: Iterable[JsonSpanNode]) -> list[str]:
    return [node.pointer or "/" for node in nodes]


EXACT_TASK_PATH = "/data/action_list"
EXACT_GROUPED_TASK_PATH = "/data/change_tool_relate_action"
EXACT_PROCEDURE_PATH = "/data/sop_change_step"
EXACT_EXECUTION_PATH = "/data/change_plan/0/result"
API_ENVELOPE_PATHS = ("/code", "/provider_code", "/msg")
EXACT_PROCEDURE_ROLES = (
    (
        "check_before_change",
        ChangeOrderUnitRole.PRECHECK_STEPS.value,
        "PRECHECK",
        "真实字段 check_before_change：保持源顺序抽取变更前检查步骤。",
    ),
    (
        "change_implement",
        ChangeOrderUnitRole.IMPLEMENTATION_STEPS.value,
        "IMPLEMENTATION",
        "真实字段 change_implement：保持源顺序抽取变更实施步骤。",
    ),
    (
        "change_verified",
        ChangeOrderUnitRole.VALIDATION_STEPS.value,
        "VALIDATION",
        "真实字段 change_verified：保持源顺序抽取变更后验证步骤。",
    ),
    (
        "change_rollback",
        ChangeOrderUnitRole.ROLLBACK_STEPS.value,
        "ROLLBACK",
        "真实字段 change_rollback：保持源顺序抽取回退步骤。",
    ),
)

CONTEXT_ROLE_FIELDS: tuple[tuple[str, set[str]], ...] = (
    (
        ChangeOrderUnitRole.IDENTITY.value,
        {"ticket_id", "title", "original_system", "create_time"},
    ),
    (
        ChangeOrderUnitRole.SERVICE_SCOPE.value,
        {"cloud_service", "service", "micro_service", "affected_service"},
    ),
    (
        ChangeOrderUnitRole.CHANGE_CONTEXT.value,
        {
            "change_scene",
            "change_notes",
            "special_change_type",
            "change_guide",
            "change_guides",
        },
    ),
    (
        ChangeOrderUnitRole.RISK_IMPACT.value,
        {
            "severity",
            "change_level",
            "customer_sensed",
            "affected_customer",
            "risk_level",
            "impact_risk_level",
        },
    ),
    (
        ChangeOrderUnitRole.EXECUTION_CONTEXT.value,
        {
            "region",
            "expected_start_time",
            "expected_end_time",
            "expected_total_time",
            "executors",
            "cooperators",
            "reviewers",
        },
    ),
)


def _node_map(nodes: Iterable[JsonSpanNode]) -> dict[str, JsonSpanNode]:
    return {node.pointer: node for node in nodes}


def _context_role(node: JsonSpanNode) -> str:
    key = str(node.key or "").strip().casefold().replace("-", "_").replace(" ", "_")
    for role, fields in CONTEXT_ROLE_FIELDS:
        if key in fields:
            return role
    if any(
        marker in key
        for marker in (
            "approval",
            "high_risk",
            "authorization",
            "notification",
            "notify",
        )
    ):
        return ChangeOrderUnitRole.GOVERNANCE_CONTEXT.value
    return ChangeOrderUnitRole.IDENTITY_METADATA_CONTEXT.value


def _split_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer:
        return ()
    return tuple(pointer.split("/")[1:])


def _common_prefix_depth(first: str, second: str) -> int:
    left = _split_pointer(first)
    right = _split_pointer(second)
    depth = 0
    for a, b in zip(left, right):
        if a != b:
            break
        depth += 1
    return depth


class _UnitBuilder:
    def __init__(self, text: str, chunk_size: int):
        self.text = text
        self.chunk_size = chunk_size
        self.units: list[dict[str, Any]] = []

    def add_nodes(
        self,
        nodes: list[JsonSpanNode],
        *,
        role: str,
        pointer: str,
        semantic_hint: str,
        semantic_mapping_status: str = SemanticMappingStatus.HEURISTIC.value,
        include_in_rag: bool = True,
        include_in_generation: bool = True,
        lifecycle_stage: str = "planning_context",
        procedure_group: str | None = None,
        total_steps_in_group: int | None = None,
        context_roles: tuple[str, ...] = (),
        preserve_node_boundaries: bool = False,
    ) -> None:
        metadata = {
            "semantic_mapping_status": semantic_mapping_status,
            "include_in_rag": include_in_rag,
            "include_in_generation": include_in_generation,
            "lifecycle_stage": lifecycle_stage,
            "procedure_group": procedure_group,
            "total_steps_in_group": total_steps_in_group,
            "context_roles": context_roles,
        }
        batch: list[tuple[int, JsonSpanNode]] = []
        for source_index, node in enumerate(nodes):
            if batch and node.end - batch[0][1].member_start > self.chunk_size:
                self._flush(batch, role, pointer, semantic_hint, metadata)
                batch = []
            if node.end - node.member_start > self.chunk_size:
                if batch:
                    self._flush(batch, role, pointer, semantic_hint, metadata)
                    batch = []
                if preserve_node_boundaries:
                    self._flush(
                        [(source_index, node)], role, pointer, semantic_hint, metadata
                    )
                else:
                    self._add_large_node(
                        source_index, node, role, pointer, semantic_hint, metadata
                    )
            else:
                batch.append((source_index, node))
        if batch:
            self._flush(batch, role, pointer, semantic_hint, metadata)

    def _flush(
        self,
        indexed_nodes: list[tuple[int, JsonSpanNode]],
        role: str,
        pointer: str,
        semantic_hint: str,
        metadata: dict[str, Any],
    ) -> None:
        nodes = [node for _, node in indexed_nodes]
        start = nodes[0].member_start
        end = nodes[-1].end
        procedure_group = metadata.get("procedure_group")
        self.units.append(
            {
                "start": start,
                "end": end,
                "role": role,
                "pointer": pointer,
                "source_pointers": tuple(node.pointer for node in nodes),
                "item_count": len(nodes),
                "semantic_hint": semantic_hint,
                **metadata,
                "step_start_index": indexed_nodes[0][0] if procedure_group else None,
                "step_end_index": indexed_nodes[-1][0] if procedure_group else None,
            }
        )

    def _add_large_node(
        self,
        source_index: int,
        node: JsonSpanNode,
        role: str,
        pointer: str,
        semantic_hint: str,
        metadata: dict[str, Any],
    ) -> None:
        fragment = self.text[node.member_start : node.end]
        for part in chunk_text(fragment, self.chunk_size, 0):
            self.units.append(
                {
                    "start": node.member_start + part.char_start,
                    "end": node.member_start + part.char_end,
                    "role": role,
                    "pointer": pointer,
                    "source_pointers": (node.pointer,),
                    "item_count": 1,
                    "semantic_hint": semantic_hint
                    + " 单个源对象过长，本单元是其连续片段；不要补全片段外内容。",
                    **metadata,
                    "step_start_index": (
                        source_index if metadata.get("procedure_group") else None
                    ),
                    "step_end_index": (
                        source_index if metadata.get("procedure_group") else None
                    ),
                }
            )

    def finish(self) -> tuple[ChangeOrderExtractionUnit, ...]:
        ordered = sorted(self.units, key=lambda item: (item["start"], item["end"]))
        result: list[ChangeOrderExtractionUnit] = []
        for index, item in enumerate(ordered):
            start = int(item["start"])
            end = int(item["end"])
            result.append(
                ChangeOrderExtractionUnit(
                    chunk=DocumentChunk(
                        index=index,
                        char_start=start,
                        char_end=end,
                        content=self.text[start:end],
                    ),
                    role=str(item["role"]),
                    pointer=str(item["pointer"]),
                    source_pointers=tuple(item["source_pointers"]),
                    item_count=int(item["item_count"]),
                    semantic_hint=str(item["semantic_hint"]),
                    semantic_mapping_status=str(item["semantic_mapping_status"]),
                    include_in_rag=bool(item["include_in_rag"]),
                    include_in_generation=bool(item["include_in_generation"]),
                    lifecycle_stage=str(item["lifecycle_stage"]),
                    procedure_group=(
                        str(item["procedure_group"])
                        if item.get("procedure_group") is not None
                        else None
                    ),
                    step_start_index=item.get("step_start_index"),
                    step_end_index=item.get("step_end_index"),
                    total_steps_in_group=item.get("total_steps_in_group"),
                    context_roles=tuple(item.get("context_roles") or ()),
                )
            )
        return tuple(result)


def _add_context_units(
    builder: _UnitBuilder,
    root: JsonSpanNode,
    excluded_roots: set[str],
) -> list[str]:
    context_roots: list[str] = []

    def contains_excluded(node: JsonSpanNode) -> bool:
        return any(
            excluded != node.pointer and _is_descendant(excluded, node.pointer)
            for excluded in excluded_roots
        )

    def visit(node: JsonSpanNode) -> None:
        if node.pointer in excluded_roots:
            return
        if not contains_excluded(node):
            context_roots.append(node.pointer)
            role = _context_role(node)
            builder.add_nodes(
                [node],
                role=role,
                pointer=node.pointer,
                semantic_hint=(
                    "这是变更单上下文。只抽取能支撑整单适用范围、约束或风险的知识，"
                    "不要把每个字段拆成卡片。"
                ),
                semantic_mapping_status=(
                    SemanticMappingStatus.CONFIRMED.value
                    if role != ChangeOrderUnitRole.IDENTITY_METADATA_CONTEXT.value
                    else SemanticMappingStatus.UNKNOWN.value
                ),
                context_roles=(role,),
            )
            return

        pending: list[JsonSpanNode] = []

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            context_roots.extend(child.pointer for child in pending)
            roles = tuple(dict.fromkeys(_context_role(child) for child in pending))
            role = (
                roles[0]
                if len(roles) == 1
                else ChangeOrderUnitRole.IDENTITY_METADATA_CONTEXT.value
            )
            builder.add_nodes(
                pending,
                role=role,
                pointer=node.pointer,
                semantic_hint=(
                    "这是变更单上下文集合。只抽取能支撑整单适用范围、约束或风险的知识，"
                    "不要把每个字段拆成卡片；字段级分类保存在 lineage/context_roles。"
                ),
                semantic_mapping_status=(
                    SemanticMappingStatus.CONFIRMED.value
                    if all(
                        item != ChangeOrderUnitRole.IDENTITY_METADATA_CONTEXT.value
                        for item in roles
                    )
                    else SemanticMappingStatus.UNKNOWN.value
                ),
                context_roles=roles,
            )
            pending = []

        for child in node.children:
            if child.pointer in excluded_roots or contains_excluded(child):
                flush()
                visit(child)
            else:
                pending.append(child)
        flush()

    visit(root)
    return context_roots


def build_change_order_extraction_plan(
    text: str, *, chunk_size: int
) -> tuple[ChangeOrderExtractionPlan | None, dict[str, Any]]:
    """Build a lossless, record-aligned plan from confirmed keys or fingerprints."""

    diagnostics: dict[str, Any] = {
        "adapter": "change_order_shape_v2",
        "valid_json": False,
        "matched": False,
        "semantic_mapping_status": SemanticMappingStatus.UNKNOWN.value,
        "safe_for_internal_index": False,
        "safe_for_external_publish": False,
        "publish_scope": "INTERNAL_ONLY",
        "warnings": [],
        "blockers": [],
    }
    try:
        root = _JsonSpanParser(text).parse()
    except ChangeOrderAdapterError as exc:
        diagnostics["reason"] = str(exc)
        return None, diagnostics
    if root.kind != "object":
        diagnostics["valid_json"] = True
        diagnostics["reason"] = "JSON 根节点不是 object"
        return None, diagnostics

    diagnostics["valid_json"] = True

    nodes = list(_walk(root))
    nodes_by_path = _node_map(nodes)
    task_group_candidates = [
        node
        for node in nodes
        if _container_record_signature(
            node, array_count_min=1, array_count_max=3, field_count=13
        )
        is not None
    ]
    grouped_array_paths = {
        child.pointer
        for candidate in task_group_candidates
        for child in candidate.children
    }
    flat_task_candidates = [
        node
        for node in nodes
        if node.pointer not in grouped_array_paths
        and _record_signature(node, 13) is not None
    ]
    procedure_candidates = [
        node
        for node in nodes
        if _container_record_signature(
            node, array_count_min=4, array_count_max=4, field_count=20
        )
        is not None
    ]
    preliminary_execution_candidates = [node for node in nodes if _execution_candidate(node)]
    diagnostics["candidates"] = {
        "flat_task_arrays": _candidate_summary(flat_task_candidates),
        "task_group_containers": _candidate_summary(task_group_candidates),
        "procedure_containers": _candidate_summary(procedure_candidates),
        "execution_results": _candidate_summary(preliminary_execution_candidates),
    }

    exact_paths = (
        EXACT_TASK_PATH,
        EXACT_GROUPED_TASK_PATH,
        EXACT_PROCEDURE_PATH,
        EXACT_EXECUTION_PATH,
    )
    exact_present = [path for path in exact_paths if path in nodes_by_path]
    exact_mapping_used = bool(exact_present)
    if exact_mapping_used:
        diagnostics["possible_change_order"] = True
        exact_errors: list[str] = []
        missing = [path for path in exact_paths if path not in nodes_by_path]
        if missing:
            exact_errors.append("真实 Key 映射缺少路径: " + "、".join(missing))
        if not missing:
            flat_tasks = nodes_by_path[EXACT_TASK_PATH]
            grouped_tasks = nodes_by_path[EXACT_GROUPED_TASK_PATH]
            procedure = nodes_by_path[EXACT_PROCEDURE_PATH]
            execution = nodes_by_path[EXACT_EXECUTION_PATH]
            flat_signature = _record_signature(flat_tasks, 13)
            grouped_signature = _container_record_signature(
                grouped_tasks,
                array_count_min=1,
                array_count_max=3,
                field_count=13,
            )
            if flat_signature is None:
                exact_errors.append("/data/action_list 不符合 13-field TaskRecord 数组")
            if grouped_signature is None:
                exact_errors.append(
                    "/data/change_tool_relate_action 不符合 1~3 组 TaskRecord 投影"
                )
            if flat_signature is not None and grouped_signature != flat_signature:
                exact_errors.append("TaskRecord 主视图与分组投影字段 Schema 冲突")
            procedure_signature = _container_record_signature(
                procedure,
                array_count_min=4,
                array_count_max=4,
                field_count=20,
            )
            procedure_keys = {str(child.key) for child in procedure.children}
            expected_procedure_keys = {item[0] for item in EXACT_PROCEDURE_ROLES}
            if procedure_signature is None or procedure_keys != expected_procedure_keys:
                exact_errors.append(
                    "/data/sop_change_step 必须包含四个已确认的 ProcedureStep 数组"
                )
            if not _execution_candidate(execution):
                exact_errors.append(
                    "/data/change_plan/0/result 不符合 15-field ExecutionResult"
                )
        if exact_errors:
            diagnostics.update(
                {
                    "semantic_mapping_status": SemanticMappingStatus.CONFLICT.value,
                    "blockers": exact_errors,
                    "reason": "真实 Key 已出现，但与已确认 ChangeOrder Schema 冲突",
                }
            )
            return None, diagnostics
        task_report = _task_pair_report(flat_tasks, grouped_tasks)
        mapping_status = SemanticMappingStatus.CONFIRMED.value
    else:
        pair_candidates: list[
            tuple[JsonSpanNode, JsonSpanNode, dict[str, Any]]
        ] = []
        for flat in flat_task_candidates:
            flat_signature = _record_signature(flat, 13)
            for grouped in task_group_candidates:
                if flat_signature != _container_record_signature(
                    grouped,
                    array_count_min=1,
                    array_count_max=3,
                    field_count=13,
                ):
                    continue
                report = _task_pair_report(flat, grouped)
                if report["flat_count"] == report["grouped_count"]:
                    pair_candidates.append((flat, grouped, report))
        pair_candidates.sort(
            key=lambda item: _task_pair_score(item[0], item[1], item[2]),
            reverse=True,
        )
        best_pair = pair_candidates[0] if pair_candidates else None
        if len(pair_candidates) > 1:
            first_score = _task_pair_score(*pair_candidates[0])
            second_score = _task_pair_score(*pair_candidates[1])
            if first_score == second_score:
                best_pair = None
                diagnostics["blockers"].append("TaskRecord 双视图候选不唯一")
        diagnostics["possible_change_order"] = sum(
            (
                bool(pair_candidates),
                bool(procedure_candidates),
                bool(preliminary_execution_candidates),
            )
        ) >= 2
        if best_pair is None or len(procedure_candidates) != 1:
            if best_pair is None and not diagnostics["blockers"]:
                diagnostics["blockers"].append("未找到唯一的 13-field TaskRecord 双视图")
            if len(procedure_candidates) != 1:
                diagnostics["blockers"].append("未找到唯一的四组 20-field ProcedureStep")
            diagnostics["reason"] = "结构指纹不足或存在歧义"
            return None, diagnostics
        flat_tasks, grouped_tasks, task_report = best_pair
        procedure = procedure_candidates[0]
        excluded_for_execution = {
            flat_tasks.pointer,
            grouped_tasks.pointer,
            procedure.pointer,
        }
        execution_candidates = [
            node
            for node in preliminary_execution_candidates
            if not any(
                _is_descendant(node.pointer, path) for path in excluded_for_execution
            )
        ]
        execution_candidates.sort(
            key=lambda node: (
                _common_prefix_depth(node.pointer, procedure.pointer)
                + _common_prefix_depth(node.pointer, flat_tasks.pointer),
                -len(_split_pointer(node.pointer)),
            ),
            reverse=True,
        )
        if not execution_candidates:
            diagnostics["blockers"].append("未找到 15-field ExecutionResult")
            diagnostics["reason"] = "结构指纹不完整"
            return None, diagnostics
        if len(execution_candidates) > 1:
            first_rank = (
                _common_prefix_depth(execution_candidates[0].pointer, procedure.pointer)
                + _common_prefix_depth(execution_candidates[0].pointer, flat_tasks.pointer),
                -len(_split_pointer(execution_candidates[0].pointer)),
            )
            second_rank = (
                _common_prefix_depth(execution_candidates[1].pointer, procedure.pointer)
                + _common_prefix_depth(execution_candidates[1].pointer, flat_tasks.pointer),
                -len(_split_pointer(execution_candidates[1].pointer)),
            )
            if first_rank == second_rank:
                diagnostics["blockers"].append("ExecutionResult 候选不唯一")
                diagnostics["reason"] = "结构指纹存在歧义"
                return None, diagnostics
        execution = execution_candidates[0]
        task_report = best_pair[2]
        mapping_status = SemanticMappingStatus.HEURISTIC.value

    builder = _UnitBuilder(text, chunk_size)
    builder.add_nodes(
        list(flat_tasks.children),
        role=ChangeOrderUnitRole.TASKS_CANONICAL.value,
        pointer=flat_tasks.pointer,
        semantic_hint=(
            "这是任务扁平主视图。不要为每条 TaskRecord 单独建卡；应合并同一变更范围内"
            "连续且可复用的任务信息。"
        ),
        semantic_mapping_status=mapping_status,
    )
    task_report.update(
        {
            "canonical_role": ChangeOrderUnitRole.TASKS_CANONICAL.value,
            "canonical_source": flat_tasks.pointer or "/",
            "grouped_projection_source": grouped_tasks.pointer or "/",
            "grouped_projection_include_in_rag": False,
            "semantic_mapping_status": mapping_status,
        }
    )
    grouped_reconciled = bool(task_report["reconciled"])
    if not grouped_reconciled:
        for index, array in enumerate(grouped_tasks.children):
            builder.add_nodes(
                list(array.children),
                role=ChangeOrderUnitRole.TASKS_GROUPED_UNRECONCILED.value,
                pointer=array.pointer,
                semantic_hint=(
                    f"这是尚未与扁平视图可靠对齐的任务分组 {index + 1}。不得假定它是重复数据；"
                    "只抽取明确事实，生成结果必须停留在人工复核状态。"
                ),
                semantic_mapping_status=SemanticMappingStatus.CONFLICT.value,
            )

    procedure_group_report: list[dict[str, Any]] = []
    if exact_mapping_used:
        procedure_children = {str(child.key): child for child in procedure.children}
        procedure_specs = [
            (procedure_children[key], role, group, hint, key)
            for key, role, group, hint in EXACT_PROCEDURE_ROLES
        ]
        procedure_mapping_status = SemanticMappingStatus.CONFIRMED.value
    else:
        procedure_specs = [
            (
                array,
                ChangeOrderUnitRole.UNMAPPED_PROCEDURE_STEPS.value,
                f"UNMAPPED_{index}",
                "仅通过结构指纹识别到 ProcedureStep 组；真实字段未知，不能推断前检、实施、验证或回退语义。",
                str(array.key or f"slot_{index}"),
            )
            for index, array in enumerate(procedure.children, start=1)
        ]
        procedure_mapping_status = SemanticMappingStatus.UNKNOWN.value
    for index, (array, role, group, hint, source_key) in enumerate(
        procedure_specs, start=1
    ):
        builder.add_nodes(
            list(array.children),
            role=role,
            pointer=array.pointer,
            semantic_hint=hint,
            semantic_mapping_status=procedure_mapping_status,
            procedure_group=group,
            total_steps_in_group=len(array.children),
            preserve_node_boundaries=True,
        )
        procedure_group_report.append(
            {
                "source_slot": index,
                "source_key": source_key,
                "path": array.pointer or "/",
                "role": role,
                "procedure_group": group,
                "step_count": len(array.children),
                "semantic_mapping_status": procedure_mapping_status,
            }
        )

    builder.add_nodes(
        list(execution.children),
        role=ChangeOrderUnitRole.EXECUTION_RESULT.value,
        pointer=execution.pointer,
        semantic_hint=(
            "这是独立 ExecutionResult，表示实际执行结果而不是计划步骤。将实际结果作为 case/验证"
            "证据抽取，不要反向改写 Procedure。"
        ),
        semantic_mapping_status=mapping_status,
        include_in_generation=False,
        lifecycle_stage="post_execution",
    )

    api_envelope_paths = [
        path for path in API_ENVELOPE_PATHS if path in nodes_by_path
    ]
    excluded_roots = {
        flat_tasks.pointer,
        grouped_tasks.pointer,
        procedure.pointer,
        execution.pointer,
        *api_envelope_paths,
    }
    context_roots = _add_context_units(builder, root, excluded_roots)
    units = builder.finish()

    assigned_roots = [
        flat_tasks.pointer,
        procedure.pointer,
        execution.pointer,
        *context_roots,
    ]
    if not grouped_reconciled:
        assigned_roots.append(grouped_tasks.pointer)
    accounted_nodes = [node for node in nodes if node is not root]
    scalar_nodes = [node for node in accounted_nodes if node.kind == "scalar"]
    assigned = 0
    excluded_api_envelope = 0
    reconciled_duplicate = 0
    uncovered_paths: list[str] = []
    for node in scalar_nodes:
        if any(_is_descendant(node.pointer, root_path) for root_path in assigned_roots):
            assigned += 1
        elif any(
            _is_descendant(node.pointer, root_path)
            for root_path in api_envelope_paths
        ):
            excluded_api_envelope += 1
        elif grouped_reconciled and _is_descendant(node.pointer, grouped_tasks.pointer):
            reconciled_duplicate += 1
        else:
            uncovered_paths.append(node.pointer or "/")
    total_scalars = len(scalar_nodes)
    accounted = assigned + excluded_api_envelope + reconciled_duplicate
    structural_coverage_ratio = accounted / total_scalars if total_scalars else 1.0

    assigned_nodes = 0
    excluded_api_envelope_nodes = 0
    reconciled_duplicate_nodes = 0
    uncovered_node_paths: list[str] = []
    for node in accounted_nodes:
        if any(_is_descendant(node.pointer, root_path) for root_path in assigned_roots):
            assigned_nodes += 1
        elif any(
            _is_descendant(node.pointer, root_path)
            for root_path in api_envelope_paths
        ):
            excluded_api_envelope_nodes += 1
        elif grouped_reconciled and _is_descendant(node.pointer, grouped_tasks.pointer):
            reconciled_duplicate_nodes += 1
        elif node.kind != "scalar" and any(
            root_path != node.pointer and _is_descendant(root_path, node.pointer)
            for root_path in [*assigned_roots, grouped_tasks.pointer]
        ):
            # A parent container that only connects accounted subtrees carries no
            # independent scalar fact, but remains part of the coverage ledger.
            assigned_nodes += 1
        else:
            uncovered_node_paths.append(node.pointer or "/")
    node_total = len(accounted_nodes)
    structural_node_coverage_ratio = (
        (
            assigned_nodes
            + excluded_api_envelope_nodes
            + reconciled_duplicate_nodes
        )
        / node_total
        if node_total
        else 1.0
    )
    presence = Counter(_presence_state(node) for node in accounted_nodes)

    safe_for_internal_index = (
        grouped_reconciled
        and not uncovered_paths
        and not uncovered_node_paths
        and structural_coverage_ratio == 1.0
        and structural_node_coverage_ratio == 1.0
        and bool(units)
    )
    blockers: list[str] = []
    if not grouped_reconciled:
        blockers.append("TaskRecord 扁平视图与分组视图未能逐项对齐")
    if uncovered_paths or uncovered_node_paths:
        blockers.append("仍有 JSON 节点未进入抽取或重复视图对账范围")
    diagnostics.update(
        {
            "matched": True,
            "semantic_mapping_status": mapping_status,
            "safe_for_internal_index": safe_for_internal_index,
            "safe_for_external_publish": False,
            "publish_scope": "INTERNAL_ONLY",
            "reason": "已启用结构感知变更单抽取",
            "blockers": blockers,
            "warnings": (
                ["所有 ProcedureStep 保持源数组顺序，不依赖疑似 sequence 字段重排"]
                if exact_mapping_used
                else [
                    "Procedure 仅通过结构识别，四组业务语义保持 UNKNOWN",
                    "所有 ProcedureStep 保持源数组顺序，不依赖疑似 sequence 字段重排",
                ]
            ),
            "api_envelope": {
                "role": ChangeOrderUnitRole.API_ENVELOPE.value,
                "paths": api_envelope_paths,
                "include_in_rag": False,
                "semantic_mapping_status": (
                    SemanticMappingStatus.CONFIRMED.value
                    if api_envelope_paths
                    else SemanticMappingStatus.UNKNOWN.value
                ),
            },
            "context_role_catalog": {
                role: sorted(fields) for role, fields in CONTEXT_ROLE_FIELDS
            }
            | {
                ChangeOrderUnitRole.GOVERNANCE_CONTEXT.value: [
                    "approval*",
                    "high_risk*",
                    "authorization*",
                    "notification*",
                ]
            },
            "context_classification": [
                {
                    "path": path or "/",
                    "role": _context_role(nodes_by_path[path]),
                    "semantic_mapping_status": (
                        SemanticMappingStatus.CONFIRMED.value
                        if _context_role(nodes_by_path[path])
                        != ChangeOrderUnitRole.IDENTITY_METADATA_CONTEXT.value
                        else SemanticMappingStatus.UNKNOWN.value
                    ),
                }
                for path in context_roots
                if path in nodes_by_path
            ],
            "task_record": task_report,
            "procedure": {
                "container_path": procedure.pointer or "/",
                "step_schema": "ProcedureStep",
                "groups": procedure_group_report,
            },
            "post_execution": {
                "execution_result": {
                    "path": execution.pointer or "/",
                    "role": ChangeOrderUnitRole.EXECUTION_RESULT.value,
                    "field_count": execution.field_count,
                    "include_in_generation": False,
                    "semantic_mapping_status": mapping_status,
                }
            },
            "coverage": {
                "scalar_total": total_scalars,
                "assigned_for_extraction": assigned,
                "excluded_api_envelope": excluded_api_envelope,
                "reconciled_duplicate_projection": reconciled_duplicate,
                "uncovered": len(uncovered_paths),
                "structural_coverage_ratio": round(
                    structural_coverage_ratio, 6
                ),
                "uncovered_paths": uncovered_paths[:100],
                "node_total": node_total,
                "nodes_assigned_for_extraction": assigned_nodes,
                "nodes_excluded_api_envelope": excluded_api_envelope_nodes,
                "nodes_reconciled_duplicate_projection": reconciled_duplicate_nodes,
                "nodes_uncovered": len(uncovered_node_paths),
                "structural_node_coverage_ratio": round(
                    structural_node_coverage_ratio, 6
                ),
                "uncovered_node_paths": uncovered_node_paths[:100],
                "observed_presence": {
                    "NULL": presence.get("NULL", 0),
                    "EMPTY": presence.get("EMPTY", 0),
                    "VALUE": presence.get("VALUE", 0),
                    "MISSING": None,
                    "missing_note": "单份 JSON 无法判断缺失字段；需结合版本化 Key 映射或多样本 Schema。",
                },
            },
            "extraction_units": [
                {
                    "index": unit.chunk.index,
                    "role": unit.role,
                    "path": unit.pointer or "/",
                    "char_start": unit.chunk.char_start,
                    "char_end": unit.chunk.char_end,
                    "item_count": unit.item_count,
                    "source_pointers": list(unit.source_pointers),
                    **unit.lineage_metadata(),
                }
                for unit in units
            ],
        }
    )
    return ChangeOrderExtractionPlan(units=units, report=diagnostics), diagnostics
