from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings


class APIError(RuntimeError):
    """Raised when a DeepSeek API request fails or is malformed."""


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
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self.settings.require_api()
        payload: dict[str, Any] = {
            "model": self.settings.model,
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

        raw = ""
        retryable_http_codes = {408, 429, 500, 502, 503, 504}
        for attempt in range(self.settings.api_max_retries + 1):
            try:
                with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                break
            except HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                should_retry = (
                    exc.code in retryable_http_codes
                    and attempt < self.settings.api_max_retries
                )
                if not should_retry:
                    raise APIError(
                        f"DeepSeek API 返回 HTTP {exc.code}: {details[:1500]}"
                    ) from exc
            except URLError as exc:
                if attempt >= self.settings.api_max_retries:
                    raise APIError(f"无法连接 DeepSeek API: {exc.reason}") from exc
            except TimeoutError as exc:
                if attempt >= self.settings.api_max_retries:
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
    ) -> tuple[Any, dict[str, Any] | None]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            message, usage = self.chat(
                messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                thinking_mode="disabled",
            )
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                try:
                    return _extract_json(content), usage
                except json.JSONDecodeError as exc:
                    last_error = exc
            else:
                last_error = APIError("JSON Output 返回了空内容")

            if attempt < retries:
                messages.append({"role": "assistant", "content": content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": "上次输出为空或不是合法 JSON。请仅返回一个完整 JSON 对象。",
                    }
                )
                time.sleep(0.1)

        raise APIError(f"DeepSeek JSON Output 解析失败: {last_error}")


# Backward-compatible name for code that imported the original harness client.
OpenAICompatibleClient = DeepSeekClient
