from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

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
        extra_headers: dict[str, str] | None = None,
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
        headers.update(extra_headers or {})
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
        idempotency_key: str,
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
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    def delete(self, *, memory_id: str) -> dict[str, Any]:
        return self._request(
            "/v1/memory/delete",
            payload={"memory_id": memory_id, "hard": False},
        )

    def search(
        self,
        *,
        user_id: str,
        app_id: str,
        query: str,
        top_k: int,
        score_threshold: float,
    ) -> dict[str, Any]:
        return self._request(
            "/v1/memory/search",
            payload={
                "user_id": user_id,
                "app_id": app_id,
                "query": query,
                "top_k": top_k,
                "search_strategy": "fast",
                "rerank": True,
                "score_threshold": score_threshold,
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
            f"对象类型：{card['object_type']}",
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
        if not self.settings.mindmemos_allow_content_export:
            return {
                "status": "EXPORT_NOT_ALLOWED",
                "card_id": card_id,
                "memory_count": 0,
            }

        content_hash = self._content_hash(card)
        owner_token = uuid4().hex
        claim = self.store.claim_memory_sync(
            card_id,
            backend=self.BACKEND,
            content_hash=content_hash,
            owner_token=owner_token,
            lease_seconds=max(30, self.settings.mindmemos_timeout_seconds * 2 + 10),
        )
        if claim["state"] == "ALREADY_SYNCED":
            return {
                "status": "ALREADY_SYNCED",
                "card_id": card_id,
                "memory_count": int(claim["memory_count"]),
                "content_hash": content_hash,
            }
        if claim["state"] == "SYNC_IN_PROGRESS":
            return {
                "status": "SYNC_IN_PROGRESS",
                "card_id": card_id,
                "memory_count": 0,
                "content_hash": content_hash,
                "lease_expires_at": claim.get("lease_expires_at"),
            }
        if claim["state"] != "CLAIMED":
            return {
                "status": "SKIPPED_NOT_APPROVED",
                "card_id": card_id,
                "memory_count": 0,
            }

        started = time.monotonic()
        try:
            idempotency_key = hashlib.sha256(
                f"{self.BACKEND}:{card_id}:{content_hash}".encode("utf-8")
            ).hexdigest()
            response = self.client.add(
                user_id=self.settings.mindmemos_user_id,
                app_id=self.settings.mindmemos_app_id,
                text=self._card_text(card),
                metadata={
                    "source": "ops-knowledge-studio",
                    "card_id": card_id,
                    "local_status": CardStatus.APPROVED.value,
                    "content_hash": content_hash,
                    "idempotency_key": idempotency_key,
                },
                idempotency_key=idempotency_key,
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
            recorded = self.store.record_memory_sync_success(
                card_id,
                backend=self.BACKEND,
                content_hash=content_hash,
                memory_ids=memory_ids,
                detail={
                    "request_id": response.get("request_id"),
                    "elapsed_ms": elapsed_ms,
                    "idempotency_key": idempotency_key,
                },
                owner_token=owner_token,
            )
            if not recorded["applied"]:
                raise MindMemOSError("长期记忆同步租约已失效，结果未写入本地映射")
            cleanup = self.cleanup_retired_memories(
                limit=max(1, len(recorded["retired_memory_ids"]))
            )
            return {
                "status": "SUCCEEDED",
                "card_id": card_id,
                "memory_count": len(memory_ids),
                "content_hash": content_hash,
                "elapsed_ms": elapsed_ms,
                "retired_memory_ids": recorded["retired_memory_ids"],
                "retirement_cleanup": cleanup,
            }
        except Exception as exc:
            self.store.record_memory_sync_failure(
                card_id,
                backend=self.BACKEND,
                content_hash=content_hash,
                error=str(exc),
                owner_token=owner_token,
            )
            if isinstance(exc, MindMemOSError):
                raise
            raise MindMemOSError(f"MindMemOS 写入失败: {exc}") from exc

    def sync_approved(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        current = 0
        pending: list[dict[str, Any]] = []
        offset = 0
        page_size = 200
        while True:
            page = self.store.list_cards(
                CardStatus.APPROVED, limit=page_size, offset=offset
            )
            if not page:
                break
            for card in page:
                state = self.store.get_memory_sync_state(int(card["id"]), self.BACKEND)
                content_hash = self._content_hash(card)
                if (
                    state
                    and state["status"] == "SUCCEEDED"
                    and state["content_hash"] == content_hash
                    and int(state["memory_count"]) > 0
                ):
                    current += 1
                else:
                    pending.append(card)
            offset += len(page)
            if len(page) < page_size:
                break
        limit = self.settings.mindmemos_max_sync_cards
        for card in pending[:limit]:
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
        retired_unapproved = self.store.retire_unapproved_memory_links(
            backend=self.BACKEND
        )
        cleanup = self.cleanup_retired_memories(limit=max(20, limit * 5))
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "processed": len(results),
            "limit": self.settings.mindmemos_max_sync_cards,
            "already_current": current,
            "remaining": max(0, len(pending) - len(results)),
            "results": results,
            "retired_unapproved_links": retired_unapproved,
            "retirement_cleanup": cleanup,
            "stats": self.store.memory_sync_stats(self.BACKEND),
        }

    def cleanup_retired_memories(self, *, limit: int = 100) -> dict[str, Any]:
        removed = 0
        failed = 0
        for item in self.store.list_memory_retirements(
            backend=self.BACKEND, limit=limit
        ):
            memory_id = str(item["memory_id"])
            try:
                self.client.delete(memory_id=memory_id)
                self.store.record_memory_retirement(
                    backend=self.BACKEND, memory_id=memory_id
                )
                removed += 1
            except Exception as exc:
                self.store.record_memory_retirement(
                    backend=self.BACKEND,
                    memory_id=memory_id,
                    error=str(exc),
                )
                failed += 1
        return {"processed": removed + failed, "removed": removed, "failed": failed}

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
                score_threshold=self.settings.mindmemos_min_relevance_score,
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
                "score_threshold": self.settings.mindmemos_min_relevance_score,
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
            "content_export_allowed": self.settings.mindmemos_allow_content_export,
            "min_relevance_score": self.settings.mindmemos_min_relevance_score,
            "min_local_anchors": self.settings.mindmemos_min_local_anchors,
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
