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
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from harness.api_client import APIError
from harness.config import ConfigurationError, Settings
from harness.run_store import RunStoreError
from harness.runtime import HarnessRuntime, HarnessRuntimeError, RunQueueFull
from change_management.service import DemoChangeError
from change_management.simulator import SimulationError

from .change_web import ChangeDemoWebManager, ChangeSessionLimitError
from .documents import (
    DocumentError,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    document_capabilities,
)
from .schema import CardStatus
from .runtime_tasks import create_knowledge_runtime
from .security import WebSecurity, WebSecurityError
from .service import KnowledgeRequestError, KnowledgeService, KnowledgeServiceError
from .store import StoreError


CARD_DETAIL_PATTERN = re.compile(r"^/api/cards/(\d+)$")
CARD_REVIEW_PATTERN = re.compile(r"^/api/cards/(\d+)/review$")
CASE_BUNDLE_DETAIL_PATTERN = re.compile(r"^/api/knowledge-case-bundles/(.+)$")
CASE_BUNDLE_REVIEW_PATTERN = re.compile(
    r"^/api/knowledge-case-bundles/(.+)/review$"
)
RUN_DETAIL_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})$")
RUN_EVENTS_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/events$")
RUN_CANCEL_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/cancel$")
RUN_RESUME_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/resume$")
RUN_APPROVAL_PATTERN = re.compile(r"^/api/runs/([0-9a-f]{32})/approvals$")
CHANGE_DEMO_PATTERN = re.compile(r"^/api/change-demos/([0-9A-Za-z_-]+)$")
CHANGE_EXECUTE_PATTERN = re.compile(
    r"^/api/change-demos/([0-9A-Za-z_-]+)/execute$"
)
CHANGE_DECISION_PATTERN = re.compile(
    r"^/api/change-demos/([0-9A-Za-z_-]+)/decision$"
)
CHANGE_FEEDBACK_PATTERN = re.compile(
    r"^/api/change-demos/([0-9A-Za-z_-]+)/publish-feedback$"
)


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
        self.change_demos = ChangeDemoWebManager(service)
        self.security = WebSecurity(service.settings)

    def server_close(self) -> None:
        self.change_demos.close()
        self.runtime.stop()
        super().server_close()


class KnowledgeRequestHandler(BaseHTTPRequestHandler):
    server: KnowledgeHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        script_policy = "script-src 'self'"
        if self.server.service.settings.csp_allow_inline:
            script_policy += " 'unsafe-inline' 'unsafe-hashes'"
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; {script_policy}; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )

    def _send_json(
        self,
        payload: Any,
        status: int = HTTPStatus.OK,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise WebSecurityError(
                "JSON 接口必须使用 application/json",
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                code="unsupported_content_type",
            )
        raw = self._read_body(
            self.server.service.settings.max_json_bytes,
            "JSON 请求体超过限制",
        )
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
        if self.headers.get("Transfer-Encoding"):
            raise WebSecurityError(
                "不支持 Transfer-Encoding 请求体",
                status=HTTPStatus.BAD_REQUEST,
                code="unsupported_transfer_encoding",
            )
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("无效的 Content-Length") from exc
        if length <= 0:
            return b""
        if length > maximum:
            raise WebSecurityError(
                too_large_message,
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="request_too_large",
            )
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise ValueError("请求体长度与 Content-Length 不一致")
        return payload

    def _read_upload(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise WebSecurityError(
                "文件上传必须使用 multipart/form-data",
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                code="unsupported_content_type",
            )
        raw = self._read_body(
            self.server.service.settings.max_upload_bytes,
            "上传文件超过大小限制",
        )
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
            DemoChangeError,
            SimulationError,
            ValueError,
            json.JSONDecodeError,
        )
        if isinstance(
            exc, (WebSecurityError, KnowledgeRequestError, ChangeSessionLimitError)
        ):
            headers = (
                {"WWW-Authenticate": 'Bearer realm="Ops Knowledge Studio"'}
                if exc.status == HTTPStatus.UNAUTHORIZED
                else None
            )
            self._send_json(
                {"error": str(exc), "code": exc.code},
                exc.status,
                headers=headers,
            )
        elif isinstance(exc, RunQueueFull):
            self._send_json({"error": str(exc), "code": exc.code}, HTTPStatus.TOO_MANY_REQUESTS)
        elif isinstance(exc, expected):
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        else:
            self._send_json(
                {"error": "服务器内部错误", "detail": str(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _authorize_api(self, path: str, method: str) -> str:
        return self.server.security.authorize(
            host=self.headers.get("Host", ""),
            origin=self.headers.get("Origin", ""),
            authorization=self.headers.get("Authorization", ""),
            path=path,
            method=method,
            client_key=self.client_address[0] if self.client_address else "unknown",
        )

    def _validate_static_request(self) -> None:
        self.server.security.validate_host(self.headers.get("Host", ""))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._validate_static_request()
                self._send_static("index.html", "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self._validate_static_request()
                self._send_static("app.js", "application/javascript; charset=utf-8")
                return
            if path == "/styles.css":
                self._validate_static_request()
                self._send_static("styles.css", "text/css; charset=utf-8")
                return
            self._authorize_api(path, "GET")
            if path == "/api/health/live":
                self._send_json({"status": "ok"})
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
                        "long_term_memory": (
                            self.server.service.long_term_memory_status(probe=False)
                        ),
                    }
                )
                return
            if path == "/api/memory/status":
                query = parse_qs(parsed.query)
                probe = query.get("probe", ["false"])[0].lower() in {
                    "1",
                    "true",
                    "yes",
                }
                self._send_json(
                    self.server.service.long_term_memory_status(probe=probe)
                )
                return
            if path == "/api/stats":
                self._send_json(self.server.service.stats())
                return
            if path == "/api/change-cases":
                self._send_json({"cases": self.server.change_demos.cases()})
                return
            if path == "/api/change-demos/latest":
                self._send_json({"session": self.server.change_demos.latest()})
                return
            change_match = CHANGE_DEMO_PATTERN.match(path)
            if change_match:
                self._send_json(self.server.change_demos.describe(change_match.group(1)))
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
            if path == "/api/knowledge-case-bundles":
                query = parse_qs(parsed.query)
                status = query.get("status", [None])[0]
                limit = int(query.get("limit", ["200"])[0])
                offset = int(query.get("offset", ["0"])[0])
                self._send_json(
                    {
                        "case_bundles": self.server.service.list_case_bundles(
                            status=status,
                            limit=limit,
                            offset=offset,
                        )
                    }
                )
                return
            bundle_match = CASE_BUNDLE_DETAIL_PATTERN.match(path)
            if bundle_match:
                bundle = self.server.service.case_bundle_detail(
                    unquote(bundle_match.group(1))
                )
                if bundle is None:
                    self._send_json({"error": "变更案例包不存在"}, HTTPStatus.NOT_FOUND)
                else:
                    self._send_json(bundle)
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
            principal = self._authorize_api(path, "POST")
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
            if path == "/api/change-demos":
                result = self.server.change_demos.create(
                    requested_by=principal,
                    use_model=bool(payload.get("use_model", False)),
                    case_id=str(payload.get("case_id") or "dc-route-failover"),
                )
                self._send_json(result, HTTPStatus.ACCEPTED)
                return
            execute_match = CHANGE_EXECUTE_PATTERN.match(path)
            if execute_match:
                result = self.server.change_demos.start_execution(
                    execute_match.group(1),
                    actor=principal,
                    inject_failure=str(payload.get("inject_failure") or ""),
                )
                self._send_json(result, HTTPStatus.ACCEPTED)
                return
            decision_match = CHANGE_DECISION_PATTERN.match(path)
            if decision_match:
                result = self.server.change_demos.decide(
                    decision_match.group(1),
                    decision=str(payload.get("decision") or ""),
                    actor=principal,
                    comment=str(payload.get("comment") or ""),
                    confirmation=str(payload.get("confirmation") or ""),
                )
                self._send_json(result, HTTPStatus.ACCEPTED)
                return
            feedback_match = CHANGE_FEEDBACK_PATTERN.match(path)
            if feedback_match:
                result = self.server.change_demos.publish_feedback(
                    feedback_match.group(1),
                    actor=principal,
                )
                self._send_json(result, HTTPStatus.CREATED)
                return
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
                    actor=principal,
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
            if path == "/api/memory/sync":
                result = self.server.service.sync_long_term_memory()
                self._send_json(result)
                return
            if path == "/api/search":
                status = str(payload.get("status", CardStatus.APPROVED.value))
                result = self.server.service.search_with_diagnostics(
                    str(payload.get("query", "")),
                    status=status,
                    top_k=int(payload.get("top_k", self.server.service.settings.retrieval_top_k)),
                )
                self._send_json(result)
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
            bundle_review_match = CASE_BUNDLE_REVIEW_PATTERN.match(path)
            if bundle_review_match:
                result = self.server.service.review_case_bundle(
                    unquote(bundle_review_match.group(1)),
                    action=str(payload.get("action", "")),
                    reviewer=principal,
                    comment=str(payload.get("comment", "")),
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
                    reviewer=principal,
                    comment=str(payload.get("comment", "")),
                    supersedes_id=supersedes_id,
                )
                self._send_json(result)
                return
            self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_error(exc)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            principal = self._authorize_api(path, "DELETE")
            match = CARD_DETAIL_PATTERN.match(path)
            if not match:
                self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
                return
            card_id = int(match.group(1))
            if self.server.service.store.get_card(card_id) is None:
                self._send_json({"error": "知识卡片不存在"}, HTTPStatus.NOT_FOUND)
                return
            result = self.server.service.delete_card(card_id, actor=principal)
            self._send_json({"deleted": True, **result})
        except Exception as exc:
            self._handle_error(exc)

    def do_OPTIONS(self) -> None:
        try:
            self.server.security.validate_host(self.headers.get("Host", ""))
            self.server.security.validate_origin(self.headers.get("Origin", ""))
            raise WebSecurityError(
                "跨域预检请求不受支持",
                status=HTTPStatus.FORBIDDEN,
                code="cors_not_allowed",
            )
        except Exception as exc:
            self._handle_error(exc)


def create_server(
    service: KnowledgeService,
    *,
    host: str,
    port: int,
    runtime: HarnessRuntime | None = None,
) -> KnowledgeHTTPServer:
    service.settings.validate_web_security(host)
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
    for message in settings.startup_security_messages():
        print(message)
    print("按 Ctrl+C 停止。平台默认仅监听本机地址。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止平台……")
    finally:
        server.server_close()
