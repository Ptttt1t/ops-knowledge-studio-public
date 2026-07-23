from __future__ import annotations

from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from harness.api_client import APIError
from harness.config import ConfigurationError, Settings
from harness.run_store import RunStoreError
from harness.runtime import HarnessRuntime, HarnessRuntimeError, RunQueueFull

from .documents import (
    DocumentError,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    document_capabilities,
)
from .schema import CardStatus
from .runtime_tasks import create_knowledge_runtime
from .service import KnowledgeService, KnowledgeServiceError
from .store import StoreError


MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CARD_DETAIL_PATTERN = re.compile(r"^/api/cards/(\d+)$")
CARD_REVIEW_PATTERN = re.compile(r"^/api/cards/(\d+)/review$")
RUN_DETAIL_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})$")
RUN_EVENTS_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/events$")
RUN_CANCEL_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/cancel$")
RUN_RESUME_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/resume$")
RUN_APPROVAL_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/approvals$")


class KnowledgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        service: KnowledgeService,
        static_dir: Path,
        runtime: HarnessRuntime,
    ):
        super().__init__(address, KnowledgeRequestHandler)
        self.service = service
        self.static_dir = static_dir
        self.runtime = runtime

    def server_close(self) -> None:
        self.runtime.stop()
        super().server_close()


class KnowledgeRequestHandler(BaseHTTPRequestHandler):
    server: KnowledgeHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = self.server.static_dir / filename
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body(MAX_REQUEST_BYTES, "请求体超过 5 MB 限制")
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _run_detail(self, run_id: str) -> dict[str, Any] | None:
        run = self.server.runtime.store.get_run(run_id)
        if run is None:
            return None
        run["steps"] = self.server.runtime.store.list_steps(run_id)
        run["latest_checkpoint"] = self.server.runtime.store.latest_checkpoint(run_id)
        run["tool_approvals"] = self.server.runtime.store.list_tool_approvals(run_id)
        return run

    def _read_body(self, maximum: int, too_large_message: str) -> bytes:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("无效的 Content-Length") from exc
        if length <= 0:
            return b""
        if length > maximum:
            raise ValueError(too_large_message)
        return self.rfile.read(length)

    def _read_upload(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("文件上传必须使用 multipart/form-data")
        raw = self._read_body(MAX_UPLOAD_BYTES, "上传文件超过 50 MB 限制")
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
                "utf-8"
            )
            + raw
        )
        if not message.is_multipart():
            raise ValueError("无法解析上传请求")
        for part in message.iter_parts():
            field_name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if field_name != "file" or not filename:
                continue
            safe_name = Path(str(filename).replace("\\", "/")).name.strip()
            payload = part.get_payload(decode=True) or b""
            if not safe_name or not payload:
                raise ValueError("上传文件名或内容为空")
            return safe_name, payload
        raise ValueError("上传请求中缺少 file 字段")

    def _save_upload(self, filename: str, payload: bytes) -> Path:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
            supported = "、".join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))
            raise ValueError(f"不支持 {suffix or '无扩展名'} 文件；当前支持：{supported}")
        stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", Path(filename).stem)
        stem = stem.strip("._-")[:80] or "document"
        upload_dir = self.server.service.settings.source_dir / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        stored_name = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
            f"{uuid4().hex[:8]}_{stem}{suffix}"
        )
        destination = (upload_dir / stored_name).resolve()
        if destination.parent != upload_dir.resolve():
            raise ValueError("非法上传路径")
        destination.write_bytes(payload)
        return destination

    def _handle_error(self, exc: Exception) -> None:
        expected = (
            APIError,
            ConfigurationError,
            DocumentError,
            KnowledgeServiceError,
            StoreError,
            RunStoreError,
            HarnessRuntimeError,
            ValueError,
            json.JSONDecodeError,
        )
        if isinstance(exc, RunQueueFull):
            self._send_json({"error": str(exc), "code": exc.code}, HTTPStatus.TOO_MANY_REQUESTS)
        elif isinstance(exc, expected):
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        else:
            self._send_json(
                {"error": "服务器内部错误", "detail": str(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._send_static("index.html", "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self._send_static("app.js", "application/javascript; charset=utf-8")
                return
            if path == "/styles.css":
                self._send_static("styles.css", "text/css; charset=utf-8")
                return
            if path == "/api/health":
                self._send_json(
                    {
                        "status": "ok",
                        "platform": "Ops Knowledge Studio",
                        "config": self.server.service.settings.public_config(),
                        "document_processing": document_capabilities(),
                        "runtime": {
                            "task_types": self.server.runtime.task_types(),
                            "worker_count": self.server.runtime.worker_count,
                            "max_queued_runs": self.server.runtime.max_queued_runs,
                        },
                    }
                )
                return
            if path == "/api/stats":
                self._send_json(self.server.service.stats())
                return
            if path == "/api/runs":
                query = parse_qs(parsed.query)
                status = query.get("status", [None])[0]
                limit = int(query.get("limit", ["100"])[0])
                self._send_json(
                    {"runs": self.server.runtime.store.list_runs(status=status, limit=limit)}
                )
                return
            run_match = RUN_DETAIL_PATTERN.match(path)
            if run_match:
                run = self._run_detail(run_match.group(1))
                if run is None:
                    self._send_json({"error": "Run not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(run)
                return
            events_match = RUN_EVENTS_PATTERN.match(path)
            if events_match:
                run_id = events_match.group(1)
                if self.server.runtime.store.get_run(run_id) is None:
                    self._send_json({"error": "Run not found"}, HTTPStatus.NOT_FOUND)
                    return
                query = parse_qs(parsed.query)
                after_id = int(query.get("after_id", ["0"])[0])
                limit = int(query.get("limit", ["200"])[0])
                self._send_json(
                    {
                        "events": self.server.runtime.store.list_events(
                            run_id, after_id=after_id, limit=limit
                        )
                    }
                )
                return
            if path == "/api/cards":
                query = parse_qs(parsed.query)
                status = query.get("status", [None])[0]
                limit = int(query.get("limit", ["200"])[0])
                cards = self.server.service.store.list_cards(status=status, limit=limit)
                self._send_json({"cards": cards})
                return
            match = CARD_DETAIL_PATTERN.match(path)
            if match:
                card = self.server.service.card_detail(int(match.group(1)))
                if card is None:
                    self._send_json({"error": "知识卡片不存在"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(card)
                return
            self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/ingest-file":
                filename, file_payload = self._read_upload()
                saved_path = self._save_upload(filename, file_payload)
                try:
                    result = self.server.service.ingest_file(
                        saved_path, source_name=filename
                    )
                except Exception:
                    try:
                        saved_path.unlink(missing_ok=True)
                    except OSError as cleanup_error:
                        self.server.service.trace.log(
                            "knowledge_upload_cleanup_failed",
                            path=str(saved_path),
                            error=str(cleanup_error),
                        )
                    raise
                result["upload"] = {
                    "original_name": filename,
                    "stored_path": str(saved_path),
                    "bytes": len(file_payload),
                }
                self._send_json(result, HTTPStatus.CREATED)
                return
            payload = self._read_json()
            if path == "/api/runs":
                task_type = str(payload.get("task_type") or "").strip()
                run_input = payload.get("input", {})
                budget = payload.get("budget")
                if not isinstance(run_input, dict):
                    raise ValueError("Run input must be a JSON object")
                if budget is not None and not isinstance(budget, dict):
                    raise ValueError("Run budget must be a JSON object")
                idempotency_key = self.headers.get("Idempotency-Key", "").strip()
                if not idempotency_key:
                    idempotency_key = str(payload.get("idempotency_key") or "").strip()
                if len(idempotency_key) > 256:
                    raise ValueError("Idempotency-Key must be at most 256 characters")
                run, created = self.server.runtime.submit(
                    task_type,
                    run_input,
                    budget=budget,
                    idempotency_key=idempotency_key or None,
                )
                self._send_json(
                    {
                        "run": run,
                        "created": created,
                        "poll_url": f"/api/runs/{run['id']}",
                        "events_url": f"/api/runs/{run['id']}/events",
                    },
                    HTTPStatus.ACCEPTED if created else HTTPStatus.OK,
                )
                return
            cancel_match = RUN_CANCEL_PATTERN.match(path)
            if cancel_match:
                run = self.server.runtime.cancel(cancel_match.group(1))
                if run is None:
                    self._send_json({"error": "Run not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json({"run": run})
                return
            resume_match = RUN_RESUME_PATTERN.match(path)
            if resume_match:
                run = self.server.runtime.resume(resume_match.group(1))
                if run is None:
                    self._send_json({"error": "Run not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json({"run": run}, HTTPStatus.ACCEPTED)
                return
            approval_match = RUN_APPROVAL_PATTERN.match(path)
            if approval_match:
                run = self.server.runtime.decide_tool_approval(
                    approval_match.group(1),
                    str(payload.get("tool_name") or ""),
                    decision=str(payload.get("decision") or ""),
                    actor=str(payload.get("actor") or ""),
                    comment=str(payload.get("comment") or ""),
                )
                if run is None:
                    self._send_json({"error": "Run not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(
                        {"run": run},
                        HTTPStatus.ACCEPTED
                        if run["status"] == "QUEUED"
                        else HTTPStatus.OK,
                    )
                return
            if path == "/api/ingest-text":
                result = self.server.service.ingest_text(
                    source_name=str(payload.get("source_name", "")),
                    source_ref=str(payload.get("source_ref", "manual://web-input")),
                    content=str(payload.get("content", "")),
                )
                self._send_json(result, HTTPStatus.CREATED)
                return
            if path == "/api/search":
                status = str(payload.get("status", CardStatus.APPROVED.value))
                result = self.server.service.search(
                    str(payload.get("query", "")),
                    status=status,
                    top_k=int(payload.get("top_k", self.server.service.settings.retrieval_top_k)),
                )
                self._send_json({"hits": result})
                return
            if path == "/api/query":
                result = self.server.service.query(str(payload.get("question", "")))
                self._send_json(result)
                return
            if path == "/api/agent-query":
                result = self.server.service.agent_query(
                    str(payload.get("question", ""))
                )
                self._send_json(result)
                return
            match = CARD_REVIEW_PATTERN.match(path)
            if match:
                raw_supersedes = payload.get("supersedes_id")
                supersedes_id = int(raw_supersedes) if raw_supersedes not in (None, "") else None
                result = self.server.service.review(
                    int(match.group(1)),
                    action=str(payload.get("action", "")),
                    reviewer=str(payload.get("reviewer", "")),
                    comment=str(payload.get("comment", "")),
                    supersedes_id=supersedes_id,
                )
                self._send_json(result)
                return
            self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_error(exc)


def create_server(
    service: KnowledgeService,
    *,
    host: str,
    port: int,
    runtime: HarnessRuntime | None = None,
) -> KnowledgeHTTPServer:
    static_dir = Path(__file__).resolve().parent / "static"
    instance = runtime or create_knowledge_runtime(
        service,
        worker_count=service.settings.runtime_workers,
        max_queued_runs=service.settings.runtime_max_queued_runs,
    )
    return KnowledgeHTTPServer((host, port), service, static_dir, instance)


def serve(settings: Settings, service: KnowledgeService | None = None) -> None:
    instance = service or KnowledgeService(settings)
    server = create_server(instance, host=settings.host, port=settings.port)
    print(f"Ops Knowledge Studio 已启动：http://{settings.host}:{settings.port}")
    print("按 Ctrl+C 停止。平台默认仅监听本机地址。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止平台……")
    finally:
        server.server_close()
