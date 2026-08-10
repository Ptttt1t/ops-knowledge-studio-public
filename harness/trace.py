from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from uuid import uuid4


class TraceLogger:
    """Writes redacted, hash-chained JSON events with bounded retention."""

    _SECRET_KEY = re.compile(
        r"(?:api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)",
        re.IGNORECASE,
    )
    _TEXT_KEY = re.compile(
        r"^(?:question|query|prompt|user_prompt|system_prompt|content|text)$",
        re.IGNORECASE,
    )
    _BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE)

    def __init__(
        self,
        artifact_dir: Path,
        *,
        retention_days: int = 7,
        max_files: int = 50,
        hmac_key: str = "",
    ):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir = artifact_dir
        self.retention_days = max(1, retention_days)
        self.max_files = max(1, max_files)
        self._hmac_key = hmac_key.encode("utf-8")
        self._previous_hash = ""
        self._lock = threading.Lock()
        self._scrub_existing_files()
        self._enforce_retention()
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = artifact_dir / f"session-{timestamp}-{uuid4().hex[:8]}.jsonl"
        self.path.touch(exist_ok=False)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @classmethod
    def _redact(cls, value: Any, *, key: str = "") -> Any:
        if cls._SECRET_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): cls._redact(item_value, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [cls._redact(item, key=key) for item in value]
        if isinstance(value, str):
            if cls._TEXT_KEY.search(key):
                encoded = value.encode("utf-8")
                return {
                    "redacted": True,
                    "length": len(value),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            return cls._BEARER.sub("Bearer [REDACTED]", value)
        return value

    def _enforce_retention(self) -> None:
        files = sorted(
            self.artifact_dir.glob("session-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        cutoff = time.time() - self.retention_days * 24 * 60 * 60
        for index, path in enumerate(files):
            try:
                if index >= self.max_files - 1 or path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _scrub_existing_files(self) -> None:
        """Redact retained legacy JSONL files and rebuild their integrity chain."""

        for path in self.artifact_dir.glob("session-*.jsonl"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            previous_hash = ""
            rewritten: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    raw = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": "legacy_unparseable_record",
                        "raw_sha256": hashlib.sha256(
                            line.encode("utf-8", errors="replace")
                        ).hexdigest(),
                    }
                if not isinstance(raw, dict):
                    raw = {"event": "legacy_non_object_record", "value_type": type(raw).__name__}
                raw.pop("record_hash", None)
                raw.pop("previous_hash", None)
                raw.pop("integrity", None)
                record = dict(self._redact(raw))
                record["integrity"] = (
                    "hmac-sha256" if self._hmac_key else "sha256-chain"
                )
                record["previous_hash"] = previous_hash
                canonical = json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                previous_hash = self._record_hash(canonical)
                record["record_hash"] = previous_hash
                rewritten.append(json.dumps(record, ensure_ascii=False, default=str))
            temporary = path.with_suffix(path.suffix + ".scrub")
            try:
                temporary.write_text(
                    "\n".join(rewritten) + ("\n" if rewritten else ""),
                    encoding="utf-8",
                )
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            except OSError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _record_hash(self, payload: bytes) -> str:
        if self._hmac_key:
            return hmac.new(self._hmac_key, payload, hashlib.sha256).hexdigest()
        return hashlib.sha256(payload).hexdigest()

    def log(self, event: str, **data: Any) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **self._redact(data),
            "integrity": "hmac-sha256" if self._hmac_key else "sha256-chain",
            "previous_hash": self._previous_hash,
        }
        with self._lock:
            record["previous_hash"] = self._previous_hash
            canonical = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            record_hash = self._record_hash(canonical)
            record["record_hash"] = record_hash
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._previous_hash = record_hash
