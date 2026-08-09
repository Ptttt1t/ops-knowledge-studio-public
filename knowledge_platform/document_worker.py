from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from .documents import DocumentLimits, read_document


def _apply_resource_limits(timeout_seconds: int) -> None:
    if os.name == "nt":
        return
    try:
        import resource

        memory_limit = 768 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        cpu_limit = max(timeout_seconds, 1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
    except (ImportError, OSError, ValueError):
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--limits-json", required=True)
    parser.add_argument("--timeout-seconds", type=int, required=True)
    args = parser.parse_args()
    result_path = args.result.resolve()
    try:
        raw_limits = json.loads(args.limits_json)
        if not isinstance(raw_limits, dict):
            raise ValueError("limits-json 必须是对象")
        limits = DocumentLimits(**raw_limits)
        _apply_resource_limits(args.timeout_seconds)
        document = read_document(args.path, limits=limits)
        payload = {"ok": True, "document": asdict(document)}
        exit_code = 0
    except Exception as exc:
        payload = {"ok": False, "error": str(exc), "type": type(exc).__name__}
        exit_code = 2
    result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
