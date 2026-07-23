from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ModelClient(Protocol):
    """Minimal provider-neutral contract used by Harness task handlers."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, str] | None = None,
        max_tokens: int | None = None,
        thinking_mode: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]: ...

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        retries: int = 2,
        max_tokens: int | None = None,
    ) -> tuple[Any, dict[str, Any] | None]: ...
