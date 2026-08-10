from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit

from harness.config import Settings


class WebSecurityError(RuntimeError):
    def __init__(self, message: str, *, status: int, code: str):
        super().__init__(message)
        self.status = int(status)
        self.code = code


def generate_access_token() -> tuple[str, str]:
    token = f"oks_{secrets.token_urlsafe(32)}"
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalized_hostname(value: str) -> str:
    raw = value.strip()
    if not raw or any(character in raw for character in ("@", "/", "\\")):
        return ""
    try:
        return ipaddress.ip_address(raw).compressed.lower()
    except ValueError:
        pass
    parsed = None
    try:
        parsed = urlsplit(f"//{raw}")
        hostname = parsed.hostname
    except ValueError:
        hostname = None
    if parsed is None:
        return ""
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        return ""
    normalized = (hostname or "").rstrip(".").lower()
    try:
        return ipaddress.ip_address(normalized).compressed.lower()
    except ValueError:
        return normalized


@dataclass
class _RateWindow:
    timestamps: deque[float]


class SlidingWindowLimiter:
    def __init__(self):
        self._windows: dict[str, _RateWindow] = {}
        self._lock = threading.Lock()

    def consume(self, key: str, limit: int, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - 60.0
        with self._lock:
            window = self._windows.setdefault(key, _RateWindow(deque()))
            while window.timestamps and window.timestamps[0] <= cutoff:
                window.timestamps.popleft()
            if len(window.timestamps) >= limit:
                retry_after = max(1, int(60 - (current - window.timestamps[0])))
                raise WebSecurityError(
                    f"请求过于频繁，请在 {retry_after} 秒后重试",
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                    code="rate_limit_exceeded",
                )
            window.timestamps.append(current)


class WebSecurity:
    EXPENSIVE_PATHS = {
        "/api/ingest-file",
        "/api/ingest-text",
        "/api/query",
        "/api/agent-query",
        "/api/memory/sync",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self._limiter = SlidingWindowLimiter()
        self._allowed_hosts = {
            _normalized_hostname(item) for item in settings.allowed_hosts if item.strip()
        }
        self._allowed_origins = {item.rstrip("/") for item in settings.allowed_origins}

    @property
    def principal(self) -> str:
        return self.settings.shared_actor

    def validate_host(self, host_header: str) -> None:
        hostname = _normalized_hostname(host_header)
        if not hostname or hostname not in self._allowed_hosts:
            raise WebSecurityError(
                "请求 Host 不在允许列表中",
                status=HTTPStatus.MISDIRECTED_REQUEST,
                code="invalid_host",
            )

    def validate_origin(self, origin: str) -> None:
        if origin and origin.rstrip("/") not in self._allowed_origins:
            raise WebSecurityError(
                "请求 Origin 不在允许列表中",
                status=HTTPStatus.FORBIDDEN,
                code="invalid_origin",
            )

    def authenticate(self, authorization: str) -> None:
        if self.settings.auth_mode == "disabled":
            return
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.strip():
            raise WebSecurityError(
                "缺少有效的 Bearer 访问令牌",
                status=HTTPStatus.UNAUTHORIZED,
                code="authentication_required",
            )
        supplied = token_hash(token.strip())
        if not hmac.compare_digest(supplied, self.settings.access_token_hash):
            raise WebSecurityError(
                "访问令牌无效",
                status=HTTPStatus.UNAUTHORIZED,
                code="invalid_access_token",
            )

    def authorize(
        self,
        *,
        host: str,
        origin: str,
        authorization: str,
        path: str,
        method: str,
        client_key: str,
    ) -> str:
        self.validate_host(host)
        self.validate_origin(origin)
        if path != "/api/health/live":
            self.authenticate(authorization)
        # Do not include caller-controlled Authorization text in the key.  The
        # unauthenticated liveness endpoint would otherwise let a client create
        # an unbounded number of limiter buckets by varying that header.
        rate_key = hashlib.sha256(client_key.encode("utf-8")).hexdigest()
        self._limiter.consume(
            f"all:{rate_key}", self.settings.request_rate_per_minute
        )
        if method != "GET":
            self._limiter.consume(
                f"write:{rate_key}", self.settings.write_rate_per_minute
            )
        if path in self.EXPENSIVE_PATHS:
            self._limiter.consume(
                f"expensive:{rate_key}", self.settings.expensive_rate_per_minute
            )
        return self.principal
