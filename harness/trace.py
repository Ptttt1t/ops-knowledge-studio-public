from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


class TraceLogger:
    """Writes one structured JSON event per line for replay and debugging."""

    def __init__(self, artifact_dir: Path):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = artifact_dir / f"session-{timestamp}-{uuid4().hex[:8]}.jsonl"

    def log(self, event: str, **data: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
