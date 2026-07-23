from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Callable, Protocol


class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"
    LOCAL_WRITE = "LOCAL_WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    PRIVILEGED = "PRIVILEGED"


class ToolError(RuntimeError):
    """Raised for invalid or unavailable tool calls."""


class ToolApprovalRequired(ToolError):
    """Raised when a non-read-only tool lacks an explicit approval."""


class ToolExecutionContext(Protocol):
    def check_cancelled(self) -> None: ...


ToolHandler = Callable[[dict[str, Any], ToolExecutionContext], Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    timeout_seconds: int = 120
    max_output_bytes: int = 64 * 1024
    handler: ToolHandler = field(repr=False, compare=False, default=lambda _args, _ctx: None)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: Any = None
    error_code: str | None = None
    error_message: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "truncated": self.truncated,
        }


class ToolRegistry:
    """Typed tool registry with conservative input validation and risk gating."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        name = spec.name.strip()
        if not name:
            raise ToolError("工具名称不能为空")
        if name in self._tools:
            raise ToolError(f"工具已注册: {name}")
        if spec.timeout_seconds <= 0 or spec.max_output_bytes <= 0:
            raise ToolError("工具超时和输出上限必须大于 0")
        if spec.input_schema.get("type", "object") != "object":
            raise ToolError("工具输入 Schema 必须是 object")
        self._tools[name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in sorted(self._tools)]

    @staticmethod
    def _matches_type(value: Any, expected: str) -> bool:
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "array":
            return isinstance(value, list)
        if expected == "object":
            return isinstance(value, dict)
        return True

    def validate(self, spec: ToolSpec, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ToolError("工具参数必须是 JSON 对象")
        schema = spec.input_schema
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        missing = sorted(name for name in required if name not in arguments)
        if missing:
            raise ToolError(f"工具参数缺失: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            unexpected = sorted(name for name in arguments if name not in properties)
            if unexpected:
                raise ToolError(f"工具参数不允许: {', '.join(unexpected)}")
        for name, value in arguments.items():
            property_schema = properties.get(name)
            if not isinstance(property_schema, dict):
                continue
            expected = property_schema.get("type")
            if isinstance(expected, str) and not self._matches_type(value, expected):
                raise ToolError(f"工具参数 {name} 类型必须为 {expected}")
            if "minimum" in property_schema and value < property_schema["minimum"]:
                raise ToolError(f"工具参数 {name} 小于最小值")
            if "maximum" in property_schema and value > property_schema["maximum"]:
                raise ToolError(f"工具参数 {name} 大于最大值")
        return arguments

    @staticmethod
    def _truncate(value: Any, maximum: int) -> tuple[Any, bool]:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) <= maximum:
            return value, False
        text = encoded[:maximum].decode("utf-8", errors="ignore")
        return {"truncated_output": text, "original_bytes": len(encoded)}, True

    def execute(
        self,
        name: str,
        arguments: Any,
        context: ToolExecutionContext,
        *,
        approved: bool = False,
    ) -> ToolResult:
        spec = self.get(name)
        if spec is None:
            return ToolResult(False, error_code="UNKNOWN_TOOL", error_message=f"未知工具: {name}")
        try:
            payload = self.validate(spec, arguments)
            if spec.risk_level is not RiskLevel.READ_ONLY and not approved:
                raise ToolApprovalRequired(f"工具 {name} 需要人工批准")
            context.check_cancelled()
            output = spec.handler(payload, context)
            context.check_cancelled()
            output, truncated = self._truncate(output, spec.max_output_bytes)
            return ToolResult(True, output=output, truncated=truncated)
        except ToolApprovalRequired as exc:
            return ToolResult(False, error_code="APPROVAL_REQUIRED", error_message=str(exc))
        except ToolError as exc:
            return ToolResult(False, error_code="TOOL_VALIDATION_ERROR", error_message=str(exc))
        except Exception as exc:
            return ToolResult(False, error_code="TOOL_EXECUTION_ERROR", error_message=str(exc))
