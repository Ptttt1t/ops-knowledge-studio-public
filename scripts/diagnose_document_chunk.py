from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.config import Settings
from knowledge_platform.documents import chunk_text, read_document
from knowledge_platform.service import KnowledgeService


def main() -> int:
    parser = argparse.ArgumentParser(description="对已上传文档的指定分片执行真实抽取诊断")
    parser.add_argument("file", type=Path)
    parser.add_argument("--chunk-index", type=int, required=True)
    args = parser.parse_args()

    settings = Settings.load(ROOT / ".env")
    document = read_document(args.file)
    chunks = chunk_text(document.content, settings.chunk_size, settings.chunk_overlap)
    if args.chunk_index < 0 or args.chunk_index >= len(chunks):
        raise SystemExit(f"chunk-index 超出范围，当前文档共有 {len(chunks)} 个分片")

    service = KnowledgeService(settings)
    selected = chunks[args.chunk_index]
    results = service._extract_chunk(document.name, selected)
    payload = {
        "source": str(args.file.resolve()),
        "total_chunks": len(chunks),
        "requested_chunk": args.chunk_index,
        "segments_after_retry": len(results),
        "split_depths": [item[3] for item in results],
        "knowledge_cards": [
            card
            for _, result, _, _ in results
            if isinstance(result, dict)
            for card in result.get("knowledge_cards", [])[: service.MAX_CARDS_PER_EXTRACTION]
        ],
    }
    output = ROOT / "artifacts" / "document_chunk_diagnostic.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**payload, "knowledge_cards": len(payload["knowledge_cards"]), "output": str(output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
