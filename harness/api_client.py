from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from weakref import WeakValueDictionary

from .config import Settings


class APIError(RuntimeError):
    """Raised when a DeepSeek API request fails or is malformed."""


class JSONOutputError(APIError):
    """Raised when a model response contains text that is not valid JSON.

    ``raw_output`` is kept in memory so a caller can make one explicit protocol
    repair request. The response body is deliberately excluded from the error
    message because it can contain private business data.
    """

    def __init__(self, error: Exception, *, raw_output: str = ""):
        self.raw_output = raw_output
        self.output_chars = len(raw_output)
        position = getattr(error, "pos", None)
        self.error_position = position if isinstance(position, int) else None
        self.near_output_end = (
            self.error_position is not None
            and self.error_position >= max(0, self.output_chars - 8)
        )
        diagnostics = [f"output_chars={self.output_chars}"]
        if self.error_position is not None:
            diagnostics.extend(
                (
                    f"error_pos={self.error_position}",
                    f"near_output_end={str(self.near_output_end).lower()}",
                )
            )
        super().__init__(
            f"DeepSeek JSON Output 解析失败: {error}; " + ", ".join(diagnostics)
        )


class _AccountModelGate:
    """Coordinate one provider model route across all clients in this process."""

    def __init__(self, limit: int):
        self.limit = limit
        self._condition = threading.Condition()
        self._active = 0
        self._peak_active = 0
        self._waiting_normal = 0
        self._waiting_exclusive = 0
        self._exclusive_active = False
        self._total_requests = 0
        self._exclusive_requests = 0

    @contextmanager
    def lease(self, *, exclusive: bool = False):
        with self._condition:
            if exclusive:
                self._waiting_exclusive += 1
                try:
                    while self._exclusive_active or self._active:
                        self._condition.wait()
                    self._exclusive_active = True
                    self._active = 1
                    self._exclusive_requests += 1
                finally:
                    self._waiting_exclusive -= 1
            else:
                self._waiting_normal += 1
                try:
                    while (
                        self._exclusive_active
                        or self._waiting_exclusive
                        or self._active >= self.limit
                    ):
                        self._condition.wait()
                    self._active += 1
                finally:
                    self._waiting_normal -= 1
            self._total_requests += 1
            self._peak_active = max(self._peak_active, self._active)
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                if exclusive:
                    self._exclusive_active = False
                self._condition.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "scope": "process_account_model",
                "process_id": os.getpid(),
                "max_concurrency": self.limit,
                "active_requests": self._active,
                "peak_active_requests": self._peak_active,
                "waiting_requests": self._waiting_normal + self._waiting_exclusive,
                "waiting_exclusive_requests": self._waiting_exclusive,
                "exclusive_active": self._exclusive_active,
                "total_requests": self._total_requests,
                "exclusive_requests": self._exclusive_requests,
            }


_MODEL_GATES_LOCK = threading.Lock()
_MODEL_GATES: WeakValueDictionary[tuple[str, str, str], _AccountModelGate] = (
    WeakValueDictionary()
)


def _model_gate(settings: Settings, *, model: str, limit: int) -> _AccountModelGate:
    # The API key is never stored in the registry key or diagnostics.
    account_fingerprint = hashlib.sha256(
        settings.api_key.encode("utf-8")
    ).hexdigest()
    key = (settings.base_url.casefold(), model.casefold(), account_fingerprint)
    with _MODEL_GATES_LOCK:
        existing = _MODEL_GATES.get(key)
        if existing is not None:
            if existing.limit != limit:
                raise APIError(
                    f"模型 {model} 在当前进程中配置了不同的并发上限，"
                    "请统一该模型的并发配置后重启"
                )
            return existing
        created = _AccountModelGate(limit)
        _MODEL_GATES[key] = created
        return created


def _read_limited(stream: Any, maximum: int, *, label: str) -> bytes:
    headers = getattr(stream, "headers", None)
    if headers is not None:
        raw_length = headers.get("Content-Length")
        if raw_length:
            try:
                if int(raw_length) > maximum:
                    raise APIError(f"{label}超过 {maximum} 字节限制")
            except ValueError:
                pass
    raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise APIError(f"{label}超过 {maximum} 字节限制")
    return raw


def _extract_json(text: str) -> Any:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        starts = [index for index in (value.find("{"), value.find("[")) if index >= 0]
        if not starts:
            raise
        start = min(starts)
        end = max(value.rfind("}"), value.rfind("]"))
        if end <= start:
            raise
        return json.loads(value[start : end + 1])


class DeepSeekClient:
    """Dependency-free client for DeepSeek's OpenAI-compatible Chat API."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._route_gates_lock = threading.Lock()
        self._route_gates: dict[str, _AccountModelGate] = {}
        self._gate_for(settings.model, settings.model_max_concurrency)

    def _gate_for(self, model: str, limit: int) -> _AccountModelGate:
        route_key = model.casefold()
        with self._route_gates_lock:
            existing = self._route_gates.get(route_key)
            if existing is not None:
                if existing.limit != limit:
                    raise APIError(
                        f"模型 {model} 在同一客户端中配置了不同的并发上限"
                    )
                return existing
            gate = _model_gate(self.settings, model=model, limit=limit)
            self._route_gates[route_key] = gate
            return gate

    @property
    def endpoint(self) -> str:
        return f"{self.settings.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        thinking_mode: str | None = None,
        http_retries: int | None = None,
        exclusive: bool = False,
        model: str | None = None,
        model_max_concurrency: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self.settings.require_api()
        selected_model = (model or self.settings.model).strip()
        if not selected_model:
            raise APIError("模型名称不能为空")
        selected_limit = (
            self.settings.model_max_concurrency
            if model_max_concurrency is None
            else int(model_max_concurrency)
        )
        if not 1 <= selected_limit <= 32:
            raise APIError("模型并发上限必须在 1 到 32 之间")
        route_gate = self._gate_for(selected_model, selected_limit)
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": max_tokens or self.settings.max_tokens,
        }
        mode = self.settings.thinking_mode if thinking_mode is None else thinking_mode
        if mode:
            payload["thinking"] = {"type": mode}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ops-knowledge-studio/1.0",
            },
        )

        maximum_retries = (
            self.settings.api_max_retries
            if http_retries is None
            else int(http_retries)
        )
        if maximum_retries < 0:
            raise APIError("HTTP 重试次数不能小于 0")

        raw = ""
        retryable_http_codes = {408, 429, 500, 502, 503, 504}
        with route_gate.lease(exclusive=exclusive):
            for attempt in range(maximum_retries + 1):
                try:
                    with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                        raw = _read_limited(
                            response,
                            self.settings.model_max_response_bytes,
                            label="DeepSeek API 响应",
                        ).decode("utf-8")
                    break
                except HTTPError as exc:
                    try:
                        details = _read_limited(
                            exc,
                            self.settings.model_max_response_bytes,
                            label="DeepSeek API 错误响应",
                        ).decode("utf-8", errors="replace")
                    except APIError as size_error:
                        details = str(size_error)
                    should_retry = (
                        exc.code in retryable_http_codes
                        and attempt < maximum_retries
                    )
                    if not should_retry:
                        gate = route_gate.snapshot()
                        raise APIError(
                            f"DeepSeek API 返回 HTTP {exc.code}: {details[:1500]}; "
                            f"本进程模型门禁 model={selected_model} "
                            f"active={gate['active_requests']} "
                            f"limit={gate['max_concurrency']} "
                            f"exclusive={str(gate['exclusive_active']).lower()} "
                            f"pid={gate['process_id']}"
                        ) from exc
                except URLError as exc:
                    if attempt >= maximum_retries:
                        raise APIError(f"无法连接 DeepSeek API: {exc.reason}") from exc
                except TimeoutError as exc:
                    if attempt >= maximum_retries:
                        raise APIError("DeepSeek API 请求超时") from exc

                delay = min(
                    self.settings.api_retry_initial_seconds * (2**attempt),
                    self.settings.api_retry_max_seconds,
                )
                if delay > 0:
                    time.sleep(delay)

        try:
            data = json.loads(raw)
            message = data["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise APIError(f"DeepSeek API 响应格式不符合预期: {raw[:1500]}") from exc
        if not isinstance(message, dict):
            raise APIError("DeepSeek API 响应中的 message 不是对象")
        usage = data.get("usage") if isinstance(data, dict) else None
        return message, usage

    def chat_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        message, usage = self.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
        )
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise APIError("DeepSeek API 没有返回可用文本")
        return content.strip(), usage

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        retries: int = 2,
        max_tokens: int | None = None,
        http_retries: int | None = None,
        exclusive: bool = False,
        model: str | None = None,
        model_max_concurrency: int | None = None,
    ) -> tuple[Any, dict[str, Any] | None]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Exception | None = None
        last_content = ""

        for attempt in range(retries + 1):
            message, usage = self.chat(
                messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                thinking_mode="disabled",
                http_retries=http_retries,
                exclusive=exclusive,
                model=model,
                model_max_concurrency=model_max_concurrency,
            )
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                last_content = content.strip()
                try:
                    return _extract_json(last_content), usage
                except json.JSONDecodeError as exc:
                    last_error = exc
            else:
                last_error = APIError("JSON Output 返回了空内容")
                last_content = ""

            if attempt < retries:
                messages.append({"role": "assistant", "content": content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "上次输出为空或不是合法 JSON。请仅返回一个完整 JSON 对象。",
                    }
                )
                time.sleep(0.1)

        raise JSONOutputError(
            last_error or APIError("未知 JSON 解析错误"),
            raw_output=last_content,
        )

    def model_activity(self) -> dict[str, Any]:
        with self._route_gates_lock:
            routes = {
                model: gate.snapshot()
                for model, gate in sorted(self._route_gates.items())
            }
        primary = dict(routes[self.settings.model.casefold()])
        primary["model"] = self.settings.model
        primary["routes"] = routes
        return primary


# Backward-compatible name for code that imported the original harness client.
OpenAICompatibleClient = DeepSeekClient
