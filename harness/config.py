from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


PLACEHOLDERS = {
    "",
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

    @property
    def api_configured(self) -> bool:
        return self.api_key not in PLACEHOLDERS and self.model not in PLACEHOLDERS

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
        }

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Settings":
        project_root = (
            env_file.resolve().parent
            if env_file is not None
            else Path(__file__).resolve().parents[1]
        )
        values = read_env_file(env_file) if env_file is not None else {}

        base_url = _get(values, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigurationError("DEEPSEEK_BASE_URL 必须以 http:// 或 https:// 开头")

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
            port=_read_int(values, "PLATFORM_PORT", 8765),
            runtime_database_path=_resolve_path(
                project_root, _get(values, "HARNESS_RUNTIME_DB_PATH", "data/runtime.db")
            ),
            runtime_workers=_read_int(values, "HARNESS_WORKERS", 2),
            runtime_max_queued_runs=_read_int(values, "HARNESS_MAX_QUEUED_RUNS", 100),
            runtime_sync_wait_seconds=_read_int(values, "HARNESS_SYNC_WAIT_SECONDS", 900),
        )
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        if settings.runtime_database_path is not None:
            settings.runtime_database_path.parent.mkdir(parents=True, exist_ok=True)
        settings.source_dir.mkdir(parents=True, exist_ok=True)
        return settings
