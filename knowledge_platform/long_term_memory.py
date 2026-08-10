from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from harness.config import Settings

from .schema import CardStatus
from .store import KnowledgeStore


class MindMemOSError(RuntimeError):
    """Raised when the optional MindMemOS backend cannot serve a request."""


class MindMemOSClient:
    """Small stdlib client for the MindMemOS v1 memory API.

    The platform deliberately does not depend on the MindMemOS SDK.  Keeping the
    boundary HTTP-only makes the integration optional and prevents its sizeable
    runtime dependency tree from becoming a requirement for the base product.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max(1024, max_response_bytes)

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        method = "GET"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"
        if authenticated:
            if not self.api_key:
                raise MindMemOSError("MindMemOS API Key 未配置")
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise MindMemOSError(
                f"MindMemOS HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MindMemOSError(f"MindMemOS 连接失败: {exc}") from exc
        if len(raw) > self.max_response_bytes:
            raise MindMemOSError("MindMemOS 响应超过大小限制")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MindMemOSError("MindMemOS 返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise MindMemOSError("MindMemOS 响应不是 JSON 对象")
        code = str(result.get("code") or "ok")
        if code not in {"ok", "queued"}:
            raise MindMemOSError(
                f"MindMemOS 请求失败: {code}: {result.get('message', '')}"
            )
        return result

    def health(self) -> dict[str, Any]:
        return self._request("/healthz", authenticated=False)

    def add(
        self,
        *,
        user_id: str,
        app_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "/v1/memory/add",
            payload={
                "user_id": user_id,
                "app_id": app_id,
                "messages": [{"text": text}],
                "metadata": metadata,
                "prompt_language": "ZH",
                "mode": "sync",
            },
        )

    def search(
        self,
        *,
        user_id: str,
        app_id: str,
        query: str,
        top_k: int,
    ) -> dict[str, Any]:
        return self._request(
            "/v1/memory/search",
            payload={
                "user_id": user_id,
                "app_id": app_id,
                "query": query,
                "top_k": top_k,
                "search_strategy": "fast",
                "rerank": False,
            },
        )


@dataclass(frozen=True)
class MemoryRecall:
    card_ids: list[int]
    diagnostics: dict[str, Any]


class MindMemOSBridge:
    """Governed bridge between MindMemOS memories and local knowledge cards."""

    BACKEND = "mindmemos:vanilla"

    def __init__(
        self,
        settings: Settings,
        store: KnowledgeStore,
        *,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client or MindMemOSClient(
            base_url=settings.mindmemos_base_url,
            api_key=settings.mindmemos_api_key,
            timeout_seconds=settings.mindmemos_timeout_seconds,
            max_response_bytes=settings.model_max_response_bytes,
        )

    @property
    def enabled(self) -> bool:
        return self.settings.mindmemos_enabled

    @property
    def configured(self) -> bool:
        return self.settings.mindmemos_configured

    @staticmethod
    def _card_text(card: dict[str, Any]) -> str:
        lines = [
            f"Ops Knowledge Studio 已审核知识卡片 K{card['id']}",
            f"标题：{card['title']}",
            f"摘要：{card['summary']}",
            f"场景：{card['scenario']}",
            f"对象：{card['object_name']}",
        ]
        labels = (
            ("适用版本", "applicable_versions"),
            ("前置条件", "prerequisites"),
            ("执行步骤", "procedure_steps"),
            ("风险", "risks"),
            ("回退步骤", "rollback_steps"),
            ("验证步骤", "validation_steps"),
            ("关键词", "keywords"),
        )
        for label, field in labels:
            values = card.get(field) or []
            if values:
                lines.append(f"{label}：" + "；".join(str(value) for value in values))
        lines.extend(
            [
                f"本地状态：{card['status']}",
                f"来源：{card['source_ref']}",
                f"证据定位：{card['evidence_locator']}",
            ]
        )
        return "\n".join(lines)

    @classmethod
    def _content_hash(cls, card: dict[str, Any]) -> str:
        return hashlib.sha256(cls._card_text(card).encode("utf-8")).hexdigest()

    def sync_card(self, card: dict[str, Any]) -> dict[str, Any]:
        card_id = int(card["id"])
        if card.get("status") != CardStatus.APPROVED.value:
            return {
                "status": "SKIPPED_NOT_APPROVED",
                "card_id": card_id,
                "memory_count": 0,
            }
        if not self.enabled:
            return {"status": "DISABLED", "card_id": card_id, "memory_count": 0}
        if not self.configured:
            return {
                "status": "NOT_CONFIGURED",
                "card_id": card_id,
                "memory_count": 0,
            }

        content_hash = self._content_hash(card)
        existing = self.store.get_memory_sync_state(card_id, self.BACKEND)
        if (
            existing
            and existing["status"] == "SUCCEEDED"
            and existing["content_hash"] == content_hash
            and int(existing["memory_count"]) > 0
        ):
            return {
                "status": "ALREADY_SYNCED",
                "card_id": card_id,
                "memory_count": int(existing["memory_count"]),
                "content_hash": content_hash,
            }

        started = time.monotonic()
        try:
            response = self.client.add(
                user_id=self.settings.mindmemos_user_id,
                app_id=self.settings.mindmemos_app_id,
                text=self._card_text(card),
                metadata={
                    "source": "ops-knowledge-studio",
                    "card_id": card_id,
                    "local_status": CardStatus.APPROVED.value,
                    "content_hash": content_hash,
                    "source_checksum": str(card.get("source_checksum") or ""),
                },
            )
            data = response.get("data") or {}
            raw_memories = data.get("memories") if isinstance(data, dict) else []
            memory_ids = [
                str(item.get("memory_id"))
                for item in (raw_memories or [])
                if isinstance(item, dict) and item.get("memory_id")
            ]
            if not memory_ids:
                raise MindMemOSError("MindMemOS 写入成功响应中没有 memory_id")
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            self.store.record_memory_sync_success(
                card_id,
                backend=self.BACKEND,
                content_hash=content_hash,
                memory_ids=memory_ids,
                detail={
                    "request_id": response.get("request_id"),
                    "elapsed_ms": elapsed_ms,
                },
            )
            return {
                "status": "SUCCEEDED",
                "card_id": card_id,
                "memory_count": len(memory_ids),
                "content_hash": content_hash,
                "elapsed_ms": elapsed_ms,
            }
        except Exception as exc:
            self.store.record_memory_sync_failure(
                card_id,
                backend=self.BACKEND,
                content_hash=content_hash,
                error=str(exc),
            )
            if isinstance(exc, MindMemOSError):
                raise
            raise MindMemOSError(f"MindMemOS 写入失败: {exc}") from exc

    def sync_approved(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for card in self.store.list_cards(
            CardStatus.APPROVED,
            limit=self.settings.mindmemos_max_sync_cards,
        ):
            try:
                results.append(self.sync_card(card))
            except MindMemOSError as exc:
                results.append(
                    {
                        "status": "FAILED",
                        "card_id": int(card["id"]),
                        "error": str(exc),
                    }
                )
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "processed": len(results),
            "limit": self.settings.mindmemos_max_sync_cards,
            "results": results,
            "stats": self.store.memory_sync_stats(self.BACKEND),
        }

    def recall(self, query: str) -> MemoryRecall:
        base = {
            "backend": self.BACKEND,
            "enabled": self.enabled,
            "configured": self.configured,
            "used": False,
            "status": "DISABLED" if not self.enabled else "NOT_CONFIGURED",
            "memory_hits": 0,
            "mapped_approved_cards": 0,
            "card_ids": [],
        }
        if not self.enabled or not self.configured:
            return MemoryRecall([], base)
        started = time.monotonic()
        try:
            response = self.client.search(
                user_id=self.settings.mindmemos_user_id,
                app_id=self.settings.mindmemos_app_id,
                query=query,
                top_k=self.settings.mindmemos_top_k,
            )
            data = response.get("data") or {}
            raw_memories = data.get("memories") if isinstance(data, dict) else []
            memory_ids = [
                str(item.get("id"))
                for item in (raw_memories or [])
                if isinstance(item, dict) and item.get("id")
            ]
            linked = self.store.card_ids_for_memory_ids(
                memory_ids, backend=self.BACKEND
            )
            card_ids: list[int] = []
            for memory_id in memory_ids:
                card_id = linked.get(memory_id)
                if card_id is None or card_id in card_ids:
                    continue
                card = self.store.get_card(card_id)
                if card is not None and card["status"] == CardStatus.APPROVED.value:
                    card_ids.append(card_id)
            diagnostics = {
                **base,
                "used": True,
                "status": "OK",
                "request_id": response.get("request_id"),
                "memory_hits": len(memory_ids),
                "mapped_approved_cards": len(card_ids),
                "card_ids": card_ids,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
            return MemoryRecall(card_ids, diagnostics)
        except Exception as exc:
            return MemoryRecall(
                [],
                {
                    **base,
                    "used": True,
                    "status": "DEGRADED",
                    "error": str(exc),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        result = {
            "backend": self.BACKEND,
            "enabled": self.enabled,
            "configured": self.configured,
            "base_url": self.settings.mindmemos_base_url,
            "user_id": self.settings.mindmemos_user_id,
            "app_id": self.settings.mindmemos_app_id,
            "stats": self.store.memory_sync_stats(self.BACKEND),
            "health": "NOT_PROBED",
        }
        if not self.enabled:
            result["health"] = "DISABLED"
        elif not self.configured:
            result["health"] = "NOT_CONFIGURED"
        elif probe:
            try:
                response = self.client.health()
                result["health"] = str(response.get("status") or "ok").upper()
            except Exception as exc:
                result["health"] = "UNAVAILABLE"
                result["error"] = str(exc)
        return result
