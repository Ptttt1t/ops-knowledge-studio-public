from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import traceback


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.config import Settings  # noqa: E402
from knowledge_platform.service import KnowledgeService  # noqa: E402


CORPUS_DIR = PROJECT_ROOT / "knowledge_sources" / "public_test_corpus" / "prepared"
RESULT_PATH = PROJECT_ROOT / "artifacts" / "public_test" / "import_results.json"


def save_report(report: dict[str, object]) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> int:
    os.environ.setdefault("KNOWLEDGE_CHUNK_SIZE", "12000")
    os.environ.setdefault("KNOWLEDGE_CHUNK_OVERLAP", "500")

    settings = Settings.load(PROJECT_ROOT / ".env")
    settings.require_api()
    service = KnowledgeService(settings)
    files = sorted(path for path in CORPUS_DIR.iterdir() if path.is_file())
    report: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.model,
        "base_url": settings.base_url,
        "chunk_size": settings.chunk_size,
        "files": [],
    }

    failures = 0
    for index, path in enumerate(files, start=1):
        print(f"[{index}/{len(files)}] importing {path.name}", flush=True)
        started = time.perf_counter()
        item: dict[str, object] = {"file": path.name}
        try:
            item["result"] = service.ingest_file(path)
            item["ok"] = True
            result = item["result"]
            print(
                f"  ok: cards={result.get('extracted_cards', 0)} "
                f"pending={result.get('pending_review', 0)}",
                flush=True,
            )
        except Exception as exc:  # continue to test remaining file formats
            failures += 1
            item["ok"] = False
            item["error"] = str(exc)
            item["traceback"] = traceback.format_exc()
            print(f"  failed: {exc}", flush=True)
        item["duration_seconds"] = round(time.perf_counter() - started, 2)
        report["files"].append(item)
        save_report(report)

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["stats"] = service.stats()
    report["failures"] = failures
    save_report(report)
    print(json.dumps(report["stats"], ensure_ascii=False, indent=2), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
