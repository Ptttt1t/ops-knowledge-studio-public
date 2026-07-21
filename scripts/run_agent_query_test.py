from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.config import Settings  # noqa: E402
from knowledge_platform.service import KnowledgeService  # noqa: E402


QUESTION = "nginx ingress 如何按 predictorid 配置一致性哈希并验证？"
RESULT_PATH = (
    PROJECT_ROOT
    / "artifacts"
    / "mini_agent_integration"
    / "agent_query_result.json"
)


def main() -> int:
    service = KnowledgeService(Settings.load(PROJECT_ROOT / ".env"))
    result = service.agent_query(QUESTION)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "question": QUESTION,
                "sources": [source["card_id"] for source in result["sources"]],
                "refusal_reason": result["refusal_reason"],
                "agent": result["agent"],
                "result_path": str(RESULT_PATH),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["sources"] and result["refusal_reason"] is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
