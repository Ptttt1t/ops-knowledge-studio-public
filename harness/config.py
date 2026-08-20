from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import string
from typing import Mapping
from urllib.parse import urlsplit


PLACEHOLDERS = {
    "",
    "MINDMEMOS_API_KEY_HERE",
    "PASTE_YOUR_API_KEY_HERE",
    "YOUR_DEEPSEEK_API_KEY_HERE",
    "YOUR_MODEL_NAME_HERE",
}


class ConfigurationError(RuntimeError):
    """Raised when platform configuration is missing or invalid."""


def read_env_file(path: Path) -> dict[str, str]:
    """Read a small dotenv file without mutating process-wide environment."""
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _get(values: Mapping[str, str], name: str, default: str = "") -> str:
    return os.getenv(name, values.get(name, default)).strip()


def _read_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(values, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数，当前值为 {raw!r}") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} 必须大于 0")
    return value


def _read_nonnegative_int(
    values: Mapping[str, str], name: str, default: int
) -> int:
    raw = _get(values, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数，当前值为 {raw!r}") from exc
    if value < 0:
        raise ConfigurationError(f"{name} 不能小于 0")
    return value


def _read_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(values, name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字，当前值为 {raw!r}") from exc


def _read_bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(values, name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ConfigurationError(f"{name} 必须是 true 或 false，当前值为 {raw!r}")


def _has_setting(values: Mapping[str, str], name: str) -> bool:
    return name in os.environ or name in values


def _read_csv(values: Mapping[str, str], name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in _get(values, name, default).split(",") if item.strip())


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _resolve_path(project_root: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    project_root: Path
    api_key: str
    base_url: str
    model: str
    thinking_mode: str
    timeout_seconds: int
    api_max_retries: int
    api_retry_initial_seconds: float
    api_retry_max_seconds: float
    max_tokens: int
    temperature: float
    database_path: Path
    source_dir: Path
    chunk_size: int
    chunk_overlap: int
    retrieval_top_k: int
    retrieval_min_score: float
    retrieval_min_coverage: float
    agent_max_steps: int
    host: str
    port: int
    runtime_database_path: Path | None = None
    runtime_workers: int = 2
    runtime_max_queued_runs: int = 100
    runtime_sync_wait_seconds: int = 900
    demo_mode: bool = False
    startup_token_required: bool = False
    access_token_required: bool = False
    request_boundary_checks_enabled: bool = True
    csp_allow_inline: bool = False
    allow_insecure_model_http: bool = False
    auth_mode: str = "disabled"
    access_token_hash: str = ""
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    allowed_origins: tuple[str, ...] = ()
    shared_actor: str = "shared-operator"
    request_rate_per_minute: int = 120
    write_rate_per_minute: int = 30
    expensive_rate_per_minute: int = 5
    max_json_bytes: int = 256 * 1024
    max_upload_bytes: int = 10 * 1024 * 1024
    max_text_chars: int = 120_000
    max_change_order_json_chars: int = 500_000
    max_document_chunks: int = 20
    max_change_order_chunks: int = 40
    change_order_chunk_size: int = 12_000
    change_order_card_timezone: str = "Asia/Shanghai"
    change_order_procedure_split_chars: int = 6000
    change_order_semantic_section_threshold: int = 5
    change_order_child_min_content_chars: int = 160
    change_order_semantic_reuse_threshold: float = 0.92
    change_order_card_report_dir: Path | None = None
    demo_rebuild_enabled: bool = True
    demo_full_reset_enabled: bool = False
    max_model_calls_per_ingest: int = 60
    max_cards_per_document: int = 30
    max_concurrent_ingestions: int = 1
    max_docx_entries: int = 1000
    max_docx_uncompressed_bytes: int = 50 * 1024 * 1024
    max_docx_xml_bytes: int = 10 * 1024 * 1024
    max_archive_compression_ratio: int = 100
    max_pdf_pages: int = 100
    max_ocr_pages: int = 20
    max_image_pixels: int = 25_000_000
    document_parse_timeout_seconds: int = 120
    model_max_response_bytes: int = 2 * 1024 * 1024
    change_max_active_sessions: int = 3
    change_max_retained_sessions: int = 20
    change_active_ttl_seconds: int = 2 * 60 * 60
    change_terminal_ttl_seconds: int = 24 * 60 * 60
    mindmemos_enabled: bool = False
    mindmemos_base_url: str = "http://127.0.0.1:8000"
    mindmemos_api_key: str = ""
    mindmemos_user_id: str = "ops-knowledge-studio"
    mindmemos_app_id: str = "ops-knowledge-studio"
    mindmemos_timeout_seconds: int = 60
    mindmemos_top_k: int = 10
    mindmemos_max_sync_cards: int = 20
    mindmemos_max_semantic_cards: int = 1
    mindmemos_min_relevance_score: float = 0.65
    mindmemos_min_local_anchors: int = 2
    mindmemos_allow_content_export: bool = False
    trace_retention_days: int = 7
    trace_max_files: int = 50
    trace_hmac_key: str = ""
    real_change_generation_enabled: bool = False
    change_draft_database_path: Path | None = None
    change_generation_max_case_bundles: int = 3
    change_generation_max_context_cards: int = 24

    @property
    def api_configured(self) -> bool:
        return self.api_key not in PLACEHOLDERS and self.model not in PLACEHOLDERS

    @property
    def effective_access_token_required(self) -> bool:
        """Keep direct and legacy Settings(auth_mode="token") callers compatible."""

        return self.access_token_required or self.auth_mode == "token"

    def startup_security_messages(self) -> list[str]:
        prefix = "[DEMO MODE]" if self.demo_mode else "[PRODUCTION MODE]"
        authentication_enabled = (
            self.startup_token_required or self.effective_access_token_required
        )
        return [
            f"{prefix} Authentication {'enabled' if authentication_enabled else 'disabled'}",
            f"{prefix} Startup token "
            f"{'required' if self.startup_token_required else 'disabled'}",
            f"{prefix} Access token "
            f"{'required' if self.effective_access_token_required else 'disabled'}",
        ]

    @property
    def mindmemos_configured(self) -> bool:
        return (
            self.mindmemos_enabled
            and self.mindmemos_api_key not in PLACEHOLDERS
            and bool(self.mindmemos_base_url)
        )

    def require_api(self) -> None:
        if not self.api_configured:
            raise ConfigurationError(
                "DeepSeek API 尚未配置。请在 .env 中填写 DEEPSEEK_API_KEY，"
                "并确认 DEEPSEEK_BASE_URL 与 DEEPSEEK_MODEL。"
            )

    def public_config(self) -> dict[str, object]:
        return {
            "api_configured": self.api_configured,
            "base_url": self.base_url,
            "model": self.model,
            "thinking_mode": self.thinking_mode or "provider_default",
            "api_max_retries": self.api_max_retries,
            "database_path": str(self.database_path),
            "source_dir": str(self.source_dir),
            "retrieval_top_k": self.retrieval_top_k,
            "retrieval_min_score": self.retrieval_min_score,
            "retrieval_min_coverage": self.retrieval_min_coverage,
            "agent_max_steps": self.agent_max_steps,
            "runtime_workers": self.runtime_workers,
            "runtime_max_queued_runs": self.runtime_max_queued_runs,
            "auth_mode": self.auth_mode,
            "demo_mode": self.demo_mode,
            "startup_token_required": self.startup_token_required,
            "access_token_required": self.effective_access_token_required,
            "request_boundary_checks_enabled": self.request_boundary_checks_enabled,
            "csp_allow_inline": self.csp_allow_inline,
            "allow_insecure_model_http": self.allow_insecure_model_http,
            "shared_actor": self.shared_actor,
            "request_limits": {
                "json_bytes": self.max_json_bytes,
                "upload_bytes": self.max_upload_bytes,
                "text_chars": self.max_text_chars,
                "change_order_json_chars": self.max_change_order_json_chars,
                "document_chunks": self.max_document_chunks,
                "change_order_chunks": self.max_change_order_chunks,
                "change_order_chunk_size": self.change_order_chunk_size,
                "change_order_card_timezone": self.change_order_card_timezone,
                "change_order_procedure_split_chars": self.change_order_procedure_split_chars,
                "change_order_semantic_section_threshold": self.change_order_semantic_section_threshold,
                "change_order_child_min_content_chars": self.change_order_child_min_content_chars,
                "change_order_semantic_reuse_threshold": self.change_order_semantic_reuse_threshold,
                "model_calls_per_ingest": self.max_model_calls_per_ingest,
                "concurrent_ingestions": self.max_concurrent_ingestions,
            },
            "change_session_limits": {
                "active": self.change_max_active_sessions,
                "retained": self.change_max_retained_sessions,
                "active_ttl_seconds": self.change_active_ttl_seconds,
                "terminal_ttl_seconds": self.change_terminal_ttl_seconds,
            },
            "real_change_generation": {
                "enabled": self.real_change_generation_enabled,
                "max_case_bundles": self.change_generation_max_case_bundles,
                "max_context_cards": self.change_generation_max_context_cards,
                "unauthenticated_pilot": self.demo_mode,
            },
            "demo_management": {
                "rebuild_enabled": self.demo_mode and self.demo_rebuild_enabled,
                "full_reset_enabled": self.demo_mode and self.demo_full_reset_enabled,
            },
            "long_term_memory": {
                "enabled": self.mindmemos_enabled,
                "configured": self.mindmemos_configured,
                "backend": "mindmemos:vanilla",
                "base_url": self.mindmemos_base_url,
                "user_id": self.mindmemos_user_id,
                "app_id": self.mindmemos_app_id,
                "timeout_seconds": self.mindmemos_timeout_seconds,
                "top_k": self.mindmemos_top_k,
                "max_sync_cards": self.mindmemos_max_sync_cards,
                "max_semantic_cards": self.mindmemos_max_semantic_cards,
                "min_relevance_score": self.mindmemos_min_relevance_score,
                "min_local_anchors": self.mindmemos_min_local_anchors,
                "content_export_allowed": self.mindmemos_allow_content_export,
                "recall_policy": "fallback_only",
                "trust_policy": "local_approved_and_relevance_gated",
                "exported_fields": [
                    "card_id",
                    "title",
                    "summary",
                    "scenario",
                    "object_type",
                    "object_name",
                    "applicable_versions",
                    "prerequisites",
                    "procedure_steps",
                    "risks",
                    "rollback_steps",
                    "validation_steps",
                    "keywords",
                ],
            },
            "trace_policy": {
                "query_text": "redacted",
                "retention_days": self.trace_retention_days,
                "max_files": self.trace_max_files,
                "integrity": "hmac-sha256" if self.trace_hmac_key else "sha256-chain",
            },
        }

    def validate_web_security(self, host: str | None = None) -> None:
        bind_host = (host or self.host).strip()
        if self.auth_mode not in {"token", "disabled"}:
            raise ConfigurationError("PLATFORM_AUTH_MODE 只能是 token 或 disabled")
        if self.startup_token_required or self.effective_access_token_required:
            digest = self.access_token_hash.removeprefix("sha256:")
            if (
                not self.access_token_hash.startswith("sha256:")
                or len(digest) != 64
                or any(character not in string.hexdigits for character in digest)
            ):
                raise ConfigurationError(
                    "PLATFORM_ACCESS_TOKEN_HASH 缺失或格式无效；请先运行 "
                    "python run.py generate-access-token"
                )
        elif self.request_boundary_checks_enabled and not _is_loopback_host(bind_host):
            raise ConfigurationError("关闭 Web 鉴权时只允许监听回环地址")
        if self.request_boundary_checks_enabled and not self.allowed_hosts:
            raise ConfigurationError("PLATFORM_ALLOWED_HOSTS 不能为空")

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        project_root = (
            env_file.resolve().parent
            if env_file is not None
            else Path(__file__).resolve().parents[1]
        )
        values = read_env_file(env_file) if env_file is not None else {}

        # The packaged experience is an internal Demo by default, including
        # existing local .env files created before DEMO_MODE was introduced.
        # Production deployments must opt in explicitly with DEMO_MODE=false.
        demo_mode = _read_bool(values, "DEMO_MODE", True)
        allow_insecure_model_http = _read_bool(
            values, "DEEPSEEK_ALLOW_INSECURE_HTTP", demo_mode
        )

        base_url = _get(values, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        parsed_base_url = urlsplit(base_url)
        if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
            raise ConfigurationError("DEEPSEEK_BASE_URL 必须以 http:// 或 https:// 开头")
        if (
            parsed_base_url.scheme == "http"
            and not _is_loopback_host(parsed_base_url.hostname)
            and not allow_insecure_model_http
        ):
            raise ConfigurationError("DEEPSEEK_BASE_URL 使用 HTTP 时只允许本机回环地址")

        mindmemos_base_url = _get(
            values, "MINDMEMOS_BASE_URL", "http://127.0.0.1:8000"
        ).rstrip("/")
        parsed_mindmemos_url = urlsplit(mindmemos_base_url)
        if (
            parsed_mindmemos_url.scheme not in {"http", "https"}
            or not parsed_mindmemos_url.hostname
        ):
            raise ConfigurationError(
                "MINDMEMOS_BASE_URL 必须以 http:// 或 https:// 开头"
            )
        if (
            parsed_mindmemos_url.scheme == "http"
            and not _is_loopback_host(parsed_mindmemos_url.hostname)
        ):
            raise ConfigurationError(
                "MINDMEMOS_BASE_URL 使用 HTTP 时只允许本机回环地址"
            )

        thinking_mode = _get(values, "DEEPSEEK_THINKING", "disabled").lower()
        if thinking_mode not in {"", "enabled", "disabled"}:
            raise ConfigurationError(
                "DEEPSEEK_THINKING 只能是 enabled、disabled 或留空"
            )

        chunk_size = _read_int(values, "KNOWLEDGE_CHUNK_SIZE", 6000)
        chunk_overlap = _read_int(values, "KNOWLEDGE_CHUNK_OVERLAP", 500)
        if chunk_overlap >= chunk_size:
            raise ConfigurationError("KNOWLEDGE_CHUNK_OVERLAP 必须小于 KNOWLEDGE_CHUNK_SIZE")

        retrieval_min_score = _read_float(values, "KNOWLEDGE_MIN_SCORE", 10.0)
        if retrieval_min_score < 0:
            raise ConfigurationError("KNOWLEDGE_MIN_SCORE 不能小于 0")
        retrieval_min_coverage = _read_float(values, "KNOWLEDGE_MIN_COVERAGE", 0.15)
        if not 0 <= retrieval_min_coverage <= 1:
            raise ConfigurationError("KNOWLEDGE_MIN_COVERAGE 必须在 0 到 1 之间")
        mindmemos_min_relevance_score = _read_float(
            values, "MINDMEMOS_MIN_RELEVANCE_SCORE", 0.65
        )
        if not 0 <= mindmemos_min_relevance_score <= 1:
            raise ConfigurationError(
                "MINDMEMOS_MIN_RELEVANCE_SCORE 必须在 0 到 1 之间"
            )
        api_retry_initial_seconds = _read_float(
            values, "DEEPSEEK_RETRY_INITIAL_SECONDS", 0.5
        )
        api_retry_max_seconds = _read_float(
            values, "DEEPSEEK_RETRY_MAX_SECONDS", 4.0
        )
        if api_retry_initial_seconds < 0 or api_retry_max_seconds < 0:
            raise ConfigurationError("DeepSeek 重试等待时间不能小于 0")
        if api_retry_initial_seconds > api_retry_max_seconds:
            raise ConfigurationError(
                "DEEPSEEK_RETRY_INITIAL_SECONDS 不能大于 DEEPSEEK_RETRY_MAX_SECONDS"
            )

        platform_port = _read_int(values, "PLATFORM_PORT", 8765)
        default_origins = (
            f"http://127.0.0.1:{platform_port},http://localhost:{platform_port}"
        )
        legacy_auth_mode = _get(values, "PLATFORM_AUTH_MODE", "").lower()
        if legacy_auth_mode and legacy_auth_mode not in {"token", "disabled"}:
            raise ConfigurationError("PLATFORM_AUTH_MODE 只能是 token 或 disabled")
        if _has_setting(values, "ACCESS_TOKEN_REQUIRED"):
            access_token_required = _read_bool(
                values, "ACCESS_TOKEN_REQUIRED", not demo_mode
            )
        elif demo_mode:
            access_token_required = False
        else:
            access_token_required = legacy_auth_mode != "disabled"
        startup_token_required = _read_bool(
            values, "STARTUP_TOKEN_REQUIRED", not demo_mode
        )
        request_boundary_checks_enabled = _read_bool(
            values, "PLATFORM_REQUEST_BOUNDARY_CHECKS_ENABLED", not demo_mode
        )
        csp_allow_inline = _read_bool(
            values, "PLATFORM_CSP_ALLOW_INLINE", demo_mode
        )
        auth_mode = "token" if access_token_required else "disabled"
        settings = cls(
            project_root=project_root,
            api_key=_get(values, "DEEPSEEK_API_KEY", "YOUR_DEEPSEEK_API_KEY_HERE"),
            base_url=base_url.rstrip("/"),
            model=_get(values, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
            thinking_mode=thinking_mode,
            timeout_seconds=_read_int(values, "DEEPSEEK_TIMEOUT_SECONDS", 120),
            api_max_retries=_read_nonnegative_int(values, "DEEPSEEK_MAX_RETRIES", 2),
            api_retry_initial_seconds=api_retry_initial_seconds,
            api_retry_max_seconds=api_retry_max_seconds,
            max_tokens=_read_int(values, "DEEPSEEK_MAX_TOKENS", 4096),
            temperature=_read_float(values, "DEEPSEEK_TEMPERATURE", 0.1),
            database_path=_resolve_path(
                project_root, _get(values, "KNOWLEDGE_DB_PATH", "data/knowledge.db")
            ),
            source_dir=_resolve_path(
                project_root, _get(values, "KNOWLEDGE_SOURCE_DIR", "knowledge_sources")
            ),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            retrieval_top_k=_read_int(values, "KNOWLEDGE_TOP_K", 6),
            retrieval_min_score=retrieval_min_score,
            retrieval_min_coverage=retrieval_min_coverage,
            agent_max_steps=_read_int(values, "AGENT_MAX_STEPS", 4),
            host=_get(values, "PLATFORM_HOST", "127.0.0.1"),
            port=platform_port,
            runtime_database_path=_resolve_path(
                project_root, _get(values, "HARNESS_RUNTIME_DB_PATH", "data/runtime.db")
            ),
            runtime_workers=_read_int(values, "HARNESS_WORKERS", 2),
            runtime_max_queued_runs=_read_int(values, "HARNESS_MAX_QUEUED_RUNS", 100),
            runtime_sync_wait_seconds=_read_int(values, "HARNESS_SYNC_WAIT_SECONDS", 900),
            demo_mode=demo_mode,
            startup_token_required=startup_token_required,
            access_token_required=access_token_required,
            request_boundary_checks_enabled=request_boundary_checks_enabled,
            csp_allow_inline=csp_allow_inline,
            allow_insecure_model_http=allow_insecure_model_http,
            auth_mode=auth_mode,
            access_token_hash=_get(values, "PLATFORM_ACCESS_TOKEN_HASH", ""),
            allowed_hosts=_read_csv(
                values, "PLATFORM_ALLOWED_HOSTS", "127.0.0.1,localhost,::1"
            ),
            allowed_origins=_read_csv(
                values, "PLATFORM_ALLOWED_ORIGINS", default_origins
            ),
            shared_actor=_get(values, "PLATFORM_SHARED_ACTOR", "shared-operator")
            or "shared-operator",
            request_rate_per_minute=_read_int(
                values, "PLATFORM_REQUESTS_PER_MINUTE", 120
            ),
            write_rate_per_minute=_read_int(values, "PLATFORM_WRITES_PER_MINUTE", 30),
            expensive_rate_per_minute=_read_int(
                values, "PLATFORM_EXPENSIVE_PER_MINUTE", 5
            ),
            max_json_bytes=_read_int(values, "PLATFORM_MAX_JSON_BYTES", 256 * 1024),
            max_upload_bytes=_read_int(
                values, "DOCUMENT_MAX_UPLOAD_BYTES", 10 * 1024 * 1024
            ),
            max_text_chars=_read_int(values, "KNOWLEDGE_MAX_TEXT_CHARS", 120_000),
            max_change_order_json_chars=_read_int(
                values, "KNOWLEDGE_MAX_CHANGE_ORDER_JSON_CHARS", 500_000
            ),
            max_document_chunks=_read_int(
                values, "KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT", 20
            ),
            max_change_order_chunks=_read_int(
                values, "KNOWLEDGE_MAX_CHANGE_ORDER_CHUNKS", 40
            ),
            change_order_chunk_size=_read_int(
                values, "KNOWLEDGE_CHANGE_ORDER_CHUNK_SIZE", 12_000
            ),
            change_order_card_timezone=_get(
                values, "CHANGE_ORDER_CARD_TIMEZONE", "Asia/Shanghai"
            ),
            change_order_procedure_split_chars=_read_int(
                values, "CHANGE_ORDER_PROCEDURE_SPLIT_CHARS", 6000
            ),
            change_order_semantic_section_threshold=_read_int(
                values, "CHANGE_ORDER_SEMANTIC_SECTION_THRESHOLD", 5
            ),
            change_order_child_min_content_chars=_read_int(
                values, "CHANGE_ORDER_CHILD_MIN_CONTENT_CHARS", 160
            ),
            change_order_semantic_reuse_threshold=_read_float(
                values, "CHANGE_ORDER_SEMANTIC_REUSE_THRESHOLD", 0.92
            ),
            change_order_card_report_dir=_resolve_path(
                project_root,
                _get(
                    values,
                    "CHANGE_ORDER_CARD_REPORT_DIR",
                    "artifacts/change_order_card_reports",
                ),
            ),
            demo_rebuild_enabled=_read_bool(
                values, "DEMO_REBUILD_ENABLED", True
            ),
            demo_full_reset_enabled=_read_bool(
                values, "DEMO_FULL_RESET_ENABLED", False
            ),
            max_model_calls_per_ingest=_read_int(
                values, "KNOWLEDGE_MAX_MODEL_CALLS_PER_INGEST", 60
            ),
            max_cards_per_document=_read_int(
                values, "KNOWLEDGE_MAX_CARDS_PER_DOCUMENT", 30
            ),
            max_concurrent_ingestions=_read_int(
                values, "KNOWLEDGE_MAX_CONCURRENT_INGESTIONS", 1
            ),
            max_docx_entries=_read_int(values, "DOCUMENT_MAX_DOCX_ENTRIES", 1000),
            max_docx_uncompressed_bytes=_read_int(
                values, "DOCUMENT_MAX_DOCX_UNCOMPRESSED_BYTES", 50 * 1024 * 1024
            ),
            max_docx_xml_bytes=_read_int(
                values, "DOCUMENT_MAX_DOCX_XML_BYTES", 10 * 1024 * 1024
            ),
            max_archive_compression_ratio=_read_int(
                values, "DOCUMENT_MAX_COMPRESSION_RATIO", 100
            ),
            max_pdf_pages=_read_int(values, "DOCUMENT_MAX_PDF_PAGES", 100),
            max_ocr_pages=_read_int(values, "DOCUMENT_MAX_OCR_PAGES", 20),
            max_image_pixels=_read_int(values, "DOCUMENT_MAX_IMAGE_PIXELS", 25_000_000),
            document_parse_timeout_seconds=_read_int(
                values, "DOCUMENT_PARSE_TIMEOUT_SECONDS", 120
            ),
            model_max_response_bytes=_read_int(
                values, "MODEL_MAX_RESPONSE_BYTES", 2 * 1024 * 1024
            ),
            change_max_active_sessions=_read_int(
                values, "CHANGE_MAX_ACTIVE_SESSIONS", 3
            ),
            change_max_retained_sessions=_read_int(
                values, "CHANGE_MAX_RETAINED_SESSIONS", 20
            ),
            change_active_ttl_seconds=_read_int(
                values, "CHANGE_ACTIVE_TTL_SECONDS", 2 * 60 * 60
            ),
            change_terminal_ttl_seconds=_read_int(
                values, "CHANGE_TERMINAL_TTL_SECONDS", 24 * 60 * 60
            ),
            mindmemos_enabled=_read_bool(values, "MINDMEMOS_ENABLED", False),
            mindmemos_base_url=mindmemos_base_url,
            mindmemos_api_key=_get(values, "MINDMEMOS_API_KEY", ""),
            mindmemos_user_id=_get(
                values, "MINDMEMOS_USER_ID", "ops-knowledge-studio"
            )
            or "ops-knowledge-studio",
            mindmemos_app_id=_get(
                values, "MINDMEMOS_APP_ID", "ops-knowledge-studio"
            )
            or "ops-knowledge-studio",
            mindmemos_timeout_seconds=_read_int(
                values, "MINDMEMOS_TIMEOUT_SECONDS", 60
            ),
            mindmemos_top_k=_read_int(values, "MINDMEMOS_TOP_K", 10),
            mindmemos_max_sync_cards=_read_int(
                values, "MINDMEMOS_MAX_SYNC_CARDS", 20
            ),
            mindmemos_max_semantic_cards=_read_int(
                values, "MINDMEMOS_MAX_SEMANTIC_CARDS", 1
            ),
            mindmemos_min_relevance_score=mindmemos_min_relevance_score,
            mindmemos_min_local_anchors=_read_int(
                values, "MINDMEMOS_MIN_LOCAL_ANCHORS", 2
            ),
            mindmemos_allow_content_export=_read_bool(
                values, "MINDMEMOS_ALLOW_CONTENT_EXPORT", False
            ),
            trace_retention_days=_read_int(values, "TRACE_RETENTION_DAYS", 7),
            trace_max_files=_read_int(values, "TRACE_MAX_FILES", 50),
            trace_hmac_key=_get(values, "TRACE_HMAC_KEY", ""),
            real_change_generation_enabled=_read_bool(
                values, "REAL_CHANGE_GENERATION_ENABLED", False
            ),
            change_draft_database_path=_resolve_path(
                project_root,
                _get(values, "CHANGE_DRAFT_DB_PATH", "data/change_drafts.db"),
            ),
            change_generation_max_case_bundles=_read_int(
                values, "CHANGE_GENERATION_MAX_CASE_BUNDLES", 3
            ),
            change_generation_max_context_cards=_read_int(
                values, "CHANGE_GENERATION_MAX_CONTEXT_CARDS", 24
            ),
        )
        if settings.change_max_active_sessions > settings.change_max_retained_sessions:
            raise ConfigurationError(
                "CHANGE_MAX_ACTIVE_SESSIONS 不能大于 CHANGE_MAX_RETAINED_SESSIONS"
            )
        if settings.mindmemos_min_local_anchors <= 0:
            raise ConfigurationError("MINDMEMOS_MIN_LOCAL_ANCHORS 必须大于 0")
        if settings.trace_retention_days <= 0 or settings.trace_max_files <= 0:
            raise ConfigurationError("TRACE_RETENTION_DAYS 和 TRACE_MAX_FILES 必须大于 0")
        if not 1 <= settings.change_generation_max_case_bundles <= 3:
            raise ConfigurationError("CHANGE_GENERATION_MAX_CASE_BUNDLES 必须在 1 到 3 之间")
        if settings.change_generation_max_context_cards <= 0:
            raise ConfigurationError("CHANGE_GENERATION_MAX_CONTEXT_CARDS 必须大于 0")
        if not 0 <= settings.change_order_semantic_reuse_threshold <= 1:
            raise ConfigurationError(
                "CHANGE_ORDER_SEMANTIC_REUSE_THRESHOLD 必须在 0 到 1 之间"
            )
        if settings.change_order_child_min_content_chars <= 0:
            raise ConfigurationError(
                "CHANGE_ORDER_CHILD_MIN_CONTENT_CHARS 必须大于 0"
            )
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        if settings.runtime_database_path is not None:
            settings.runtime_database_path.parent.mkdir(parents=True, exist_ok=True)
        if settings.change_draft_database_path is not None:
            settings.change_draft_database_path.parent.mkdir(parents=True, exist_ok=True)
        if settings.change_order_card_report_dir is not None:
            settings.change_order_card_report_dir.mkdir(parents=True, exist_ok=True)
        settings.source_dir.mkdir(parents=True, exist_ok=True)
        return settings
