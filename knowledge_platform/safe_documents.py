from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .documents import (
    DocumentError,
    DocumentLimits,
    SourceDocument,
    TEXT_EXTENSIONS,
    read_document,
)


def read_document_safely(
    path: Path,
    *,
    limits: DocumentLimits,
    timeout_seconds: int,
) -> SourceDocument:
    resolved = path.expanduser().resolve()
    if resolved.suffix.lower() in TEXT_EXTENSIONS:
        return read_document(resolved, limits=limits)

    result_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="ops-document-result-", suffix=".json", delete=False
        ) as result_file:
            result_path = Path(result_file.name).resolve()
        command = [
            sys.executable,
            "-m",
            "knowledge_platform.document_worker",
            "--path",
            str(resolved),
            "--result",
            str(result_path),
            "--limits-json",
            json.dumps(asdict(limits), separators=(",", ":")),
            "--timeout-seconds",
            str(timeout_seconds),
        ]
        creation_flags = 0
        if os.name == "nt":
            creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(timeout_seconds, 1) + 5,
                check=False,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocumentError("文档解析超过安全超时，工作进程已终止") from exc

        try:
            result_size = result_path.stat().st_size
        except OSError as exc:
            details = completed.stderr.decode("utf-8", errors="replace")[:1000]
            raise DocumentError(f"文档解析工作进程未返回结果：{details}") from exc
        if result_size > limits.max_text_chars * 6 + 16_384:
            raise DocumentError("文档解析工作进程返回内容超过安全限制")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            details = completed.stderr.decode("utf-8", errors="replace")[:1000]
            raise DocumentError(f"文档解析工作进程异常：{details}") from exc
        if completed.returncode != 0 or not payload.get("ok"):
            raise DocumentError(str(payload.get("error") or "文档解析失败"))
        document_payload = payload.get("document")
        if not isinstance(document_payload, dict):
            raise DocumentError("文档解析工作进程返回格式无效")
        content = str(document_payload.get("content") or "")
        if len(content) > limits.max_text_chars:
            raise DocumentError("文档解析结果超过文本字符限制")
        return SourceDocument(
            name=str(document_payload.get("name") or resolved.name),
            source_type=str(document_payload.get("source_type") or resolved.suffix.lstrip(".")),
            source_ref=str(document_payload.get("source_ref") or resolved),
            content=content,
        )
    finally:
        if result_path is not None:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass
