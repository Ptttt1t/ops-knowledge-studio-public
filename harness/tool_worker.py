from __future__ import annotations

import importlib
import json
import sys
from typing import Any, Callable


def _resolve_entrypoint(value: str) -> Callable[[dict[str, Any], dict[str, Any]], Any]:
    module_name, separator, function_name = value.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError("隔离工具入口必须是 module:function")
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError("隔离工具入口不可调用")
    return function


def main() -> int:
    entrypoint = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("隔离工具输入必须是 JSON 对象")
        arguments = payload.get("arguments")
        context = payload.get("context")
        if not isinstance(arguments, dict) or not isinstance(context, dict):
            raise TypeError("隔离工具参数或上下文无效")
        output = _resolve_entrypoint(entrypoint)(arguments, context)
        response = {"ok": True, "output": output}
    except Exception as exc:
        response = {
            "ok": False,
            "error_code": "TOOL_PROCESS_ERROR",
            "error_message": str(exc),
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
