from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable

from .documents import DocumentChunk, chunk_text


class ChangeOrderAdapterError(ValueError):
    """Raised when JSON cannot be inspected without losing structural fidelity."""


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

    def prompt_context(self) -> str:
        return (
            f"结构化变更单单元：role={self.role}；JSON Pointer={self.pointer or '/'}；"
            f"源对象数={self.item_count}。{self.semantic_hint}"
        )


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
    ) -> None:
        batch: list[JsonSpanNode] = []
        for node in nodes:
            if batch and node.end - batch[0].member_start > self.chunk_size:
                self._flush(batch, role, pointer, semantic_hint)
                batch = []
            if node.end - node.member_start > self.chunk_size:
                if batch:
                    self._flush(batch, role, pointer, semantic_hint)
                    batch = []
                self._add_large_node(node, role, pointer, semantic_hint)
            else:
                batch.append(node)
        if batch:
            self._flush(batch, role, pointer, semantic_hint)

    def _flush(
        self,
        nodes: list[JsonSpanNode],
        role: str,
        pointer: str,
        semantic_hint: str,
    ) -> None:
        start = nodes[0].member_start
        end = nodes[-1].end
        self.units.append(
            {
                "start": start,
                "end": end,
                "role": role,
                "pointer": pointer,
                "source_pointers": tuple(node.pointer for node in nodes),
                "item_count": len(nodes),
                "semantic_hint": semantic_hint,
            }
        )

    def _add_large_node(
        self,
        node: JsonSpanNode,
        role: str,
        pointer: str,
        semantic_hint: str,
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
            builder.add_nodes(
                [node],
                role="IDENTITY_METADATA_CONTEXT",
                pointer=node.pointer,
                semantic_hint=(
                    "这是变更单身份、元数据或其他上下文。只抽取能支撑整单适用范围、"
                    "约束或风险的知识，不要把每个字段拆成卡片。"
                ),
            )
            return

        pending: list[JsonSpanNode] = []

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            context_roots.extend(child.pointer for child in pending)
            builder.add_nodes(
                pending,
                role="IDENTITY_METADATA_CONTEXT",
                pointer=node.pointer,
                semantic_hint=(
                    "这是变更单身份、元数据或其他上下文。只抽取能支撑整单适用范围、"
                    "约束或风险的知识，不要把每个字段拆成卡片。"
                ),
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
    """Detect the observed change-order shape and plan record-aligned extraction.

    Detection intentionally uses structural fingerprints only. It does not name
    real keys, depend on per-file anonymized aliases, or ask a model to infer the
    schema. Ambiguous inputs fall back to the existing generic text pipeline.
    """

    diagnostics: dict[str, Any] = {
        "adapter": "change_order_shape_v1",
        "valid_json": False,
        "matched": False,
        "safe_to_publish": False,
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
    task_group_candidates = [
        node
        for node in nodes
        if _container_record_signature(
            node,
            array_count_min=1,
            array_count_max=3,
            field_count=13,
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
            node,
            array_count_min=4,
            array_count_max=4,
            field_count=20,
        )
        is not None
    ]
    preliminary_execution_candidates = [
        node for node in nodes if _execution_candidate(node)
    ]

    pair_candidates: list[tuple[JsonSpanNode, JsonSpanNode, dict[str, Any]]] = []
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
        first_score = _task_pair_score(
            pair_candidates[0][0], pair_candidates[0][1], pair_candidates[0][2]
        )
        second_score = _task_pair_score(
            pair_candidates[1][0], pair_candidates[1][1], pair_candidates[1][2]
        )
        if first_score == second_score:
            best_pair = None
            diagnostics["blockers"].append("TaskRecord 双视图候选不唯一")

    diagnostics["candidates"] = {
        "flat_task_arrays": _candidate_summary(flat_task_candidates),
        "task_group_containers": _candidate_summary(task_group_candidates),
        "procedure_containers": _candidate_summary(procedure_candidates),
        "execution_results": _candidate_summary(preliminary_execution_candidates),
    }
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
    excluded_for_execution = {flat_tasks.pointer, grouped_tasks.pointer, procedure.pointer}
    execution_candidates = [
        node
        for node in nodes
        if _execution_candidate(node)
        and not any(_is_descendant(node.pointer, path) for path in excluded_for_execution)
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
        diagnostics["candidates"]["execution_results"] = []
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
            diagnostics["candidates"]["execution_results"] = _candidate_summary(
                execution_candidates
            )
            return None, diagnostics
    execution = execution_candidates[0]
    diagnostics["candidates"]["execution_results"] = _candidate_summary(
        execution_candidates
    )

    builder = _UnitBuilder(text, chunk_size)
    builder.add_nodes(
        list(flat_tasks.children),
        role="TASKS_CANONICAL",
        pointer=flat_tasks.pointer,
        semantic_hint=(
            "这是任务扁平主视图。不要为每条 TaskRecord 单独建卡；应合并同一变更范围内"
            "连续且可复用的任务信息。"
        ),
    )
    grouped_reconciled = bool(task_report["reconciled"])
    if not grouped_reconciled:
        for index, array in enumerate(grouped_tasks.children):
            builder.add_nodes(
                list(array.children),
                role="TASKS_GROUPED_UNRECONCILED",
                pointer=array.pointer,
                semantic_hint=(
                    f"这是尚未与扁平视图可靠对齐的任务分组 {index + 1}。不得假定它是重复数据；"
                    "只抽取明确事实，生成结果必须停留在人工复核状态。"
                ),
            )

    procedure_roles = (
        (
            "PROCEDURE_GROUP_A",
            "这是 Procedure 第 1 组，业务语义尚未确认。不得仅凭位置命名前置、执行或其他阶段。",
        ),
        (
            "ROLLBACK_STEPS",
            "这是五份样本中稳定表现为 rollback 的 Procedure 第 2 组；保持源顺序写入回退知识。",
        ),
        (
            "PROCEDURE_GROUP_C",
            "这是 Procedure 第 3 组，业务语义尚未确认。不得仅凭位置命名前置、执行或其他阶段。",
        ),
        (
            "VALIDATION_STEPS",
            "这是五份样本中稳定表现为 validation 的 Procedure 第 4 组；保持源顺序写入验证知识。",
        ),
    )
    procedure_group_report: list[dict[str, Any]] = []
    for index, (array, (role, hint)) in enumerate(
        zip(procedure.children, procedure_roles), start=1
    ):
        builder.add_nodes(
            list(array.children), role=role, pointer=array.pointer, semantic_hint=hint
        )
        procedure_group_report.append(
            {
                "source_slot": index,
                "path": array.pointer or "/",
                "role": role,
                "step_count": len(array.children),
            }
        )

    builder.add_nodes(
        list(execution.children),
        role="EXECUTION_RESULT",
        pointer=execution.pointer,
        semantic_hint=(
            "这是独立 ExecutionResult，表示实际执行结果而不是计划步骤。将实际结果作为 case/验证"
            "证据抽取，不要反向改写 Procedure。"
        ),
    )

    excluded_roots = {
        flat_tasks.pointer,
        grouped_tasks.pointer,
        procedure.pointer,
        execution.pointer,
    }
    context_roots = _add_context_units(builder, root, excluded_roots)
    units = builder.finish()

    assigned_roots = [flat_tasks.pointer, procedure.pointer, execution.pointer, *context_roots]
    if not grouped_reconciled:
        assigned_roots.append(grouped_tasks.pointer)
    accounted_nodes = [node for node in nodes if node is not root]
    scalar_nodes = [node for node in accounted_nodes if node.kind == "scalar"]
    assigned = 0
    reconciled_duplicate = 0
    uncovered_paths: list[str] = []
    for node in scalar_nodes:
        if any(_is_descendant(node.pointer, root_path) for root_path in assigned_roots):
            assigned += 1
        elif grouped_reconciled and _is_descendant(node.pointer, grouped_tasks.pointer):
            reconciled_duplicate += 1
        else:
            uncovered_paths.append(node.pointer or "/")
    total_scalars = len(scalar_nodes)
    accounted = assigned + reconciled_duplicate
    coverage_ratio = accounted / total_scalars if total_scalars else 1.0

    assigned_nodes = 0
    reconciled_duplicate_nodes = 0
    uncovered_node_paths: list[str] = []
    for node in accounted_nodes:
        if any(_is_descendant(node.pointer, root_path) for root_path in assigned_roots):
            assigned_nodes += 1
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
    node_coverage_ratio = (
        (assigned_nodes + reconciled_duplicate_nodes) / node_total
        if node_total
        else 1.0
    )
    presence = Counter(_presence_state(node) for node in accounted_nodes)

    safe_to_publish = (
        grouped_reconciled
        and not uncovered_paths
        and not uncovered_node_paths
        and coverage_ratio == 1.0
        and node_coverage_ratio == 1.0
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
            "safe_to_publish": safe_to_publish,
            "reason": "已启用结构感知变更单抽取",
            "blockers": blockers,
            "warnings": [
                "Procedure 第 1、3 组保持 UNKNOWN 语义，等待真实 Key 映射确认",
                "第一版保持所有源数组顺序，不依赖疑似 sequence 字段重排",
            ],
            "task_record": task_report,
            "procedure": {
                "container_path": procedure.pointer or "/",
                "groups": procedure_group_report,
            },
            "execution_result": {
                "path": execution.pointer or "/",
                "field_count": execution.field_count,
            },
            "coverage": {
                "scalar_total": total_scalars,
                "assigned_for_extraction": assigned,
                "reconciled_duplicate_projection": reconciled_duplicate,
                "uncovered": len(uncovered_paths),
                "coverage_ratio": round(coverage_ratio, 6),
                "uncovered_paths": uncovered_paths[:100],
                "node_total": node_total,
                "nodes_assigned_for_extraction": assigned_nodes,
                "nodes_reconciled_duplicate_projection": reconciled_duplicate_nodes,
                "nodes_uncovered": len(uncovered_node_paths),
                "node_coverage_ratio": round(node_coverage_ratio, 6),
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
                }
                for unit in units
            ],
        }
    )
    return ChangeOrderExtractionPlan(units=units, report=diagnostics), diagnostics
