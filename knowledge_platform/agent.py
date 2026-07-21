from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .retrieval import SearchHit
from .schema import CardStatus

if TYPE_CHECKING:
    from .service import KnowledgeService


AGENT_SYSTEM_PROMPT = """你是可信知识检索 Agent。你的任务不是直接回答，而是使用只读工具为用户问题找到最相关的 APPROVED 知识卡片。

规则：
1. 优先调用 search_approved_knowledge；必要时可以改写检索词并多次搜索。
2. 只对搜索结果中的候选调用 get_approved_card，核对其适用范围、步骤、风险、回退、验证和证据。
3. 不得猜测卡片 ID，不得要求写文件、执行命令或修改知识库。
4. 找到足够证据后停止调用工具，并返回简短 JSON：{"done": true, "reason": "..."}。
5. 没有相关知识时也停止，并返回 {"done": true, "reason": "没有找到相关已审核知识"}。

最终答案由平台基于你选中的卡片另行生成和校验。"""


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_approved_knowledge",
            "description": "在正式知识库中检索 APPROVED 知识卡片。可用不同关键词多次调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "与用户原问题保持同一意图的检索词",
                    },
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": "返回候选数量",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_approved_card",
            "description": "读取此前检索到的一张 APPROVED 卡片详情，不能读取未检索卡片。",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {
                        "type": "integer",
                        "description": "此前 search_approved_knowledge 返回的卡片 ID",
                    }
                },
                "required": ["card_id"],
            },
        },
    },
]


class TrustedKnowledgeAgent:
    """Bounded, read-only tool loop for agentic retrieval.

    The loop can only search and inspect APPROVED knowledge. It cannot mutate
    the knowledge store or execute operating-system tools.
    """

    def __init__(self, service: KnowledgeService, *, max_steps: int):
        self.service = service
        self.max_steps = max(1, min(max_steps, 12))

    def _search(self, query: str, top_k: int) -> list[SearchHit]:
        return self.service.retriever.search(
            query,
            statuses=[CardStatus.APPROVED],
            top_k=min(max(top_k, 1), self.service.settings.retrieval_top_k),
            min_score=self.service.settings.retrieval_min_score,
            min_query_coverage=self.service.settings.retrieval_min_coverage,
        )

    @staticmethod
    def _compact_hit(hit: SearchHit) -> dict[str, Any]:
        card = hit.card
        return {
            "card_id": card["id"],
            "title": card["title"],
            "knowledge_type": card["knowledge_type"],
            "summary": card["summary"],
            "scenario": card["scenario"],
            "object_name": card["object_name"],
            "applicable_versions": card["applicable_versions"],
            "score": round(hit.score, 4),
            "query_coverage": round(hit.query_coverage, 4),
        }

    @staticmethod
    def _card_for_tool(card: dict[str, Any]) -> dict[str, Any]:
        return {
            "card_id": card["id"],
            "title": card["title"],
            "knowledge_type": card["knowledge_type"],
            "summary": card["summary"],
            "scenario": card["scenario"],
            "object_name": card["object_name"],
            "applicable_versions": card["applicable_versions"],
            "prerequisites": card["prerequisites"],
            "procedure_steps": card["procedure_steps"],
            "risks": card["risks"],
            "rollback_steps": card["rollback_steps"],
            "validation_steps": card["validation_steps"],
            "evidence_locator": card["evidence_locator"],
            "evidence_quote": card["evidence_quote"],
        }

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            return {}
        try:
            payload = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def run(self, question: str) -> dict[str, Any]:
        text = question.strip()
        if not text:
            raise ValueError("Agent 问题不能为空")
        self.service.settings.require_api()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        best_hits: dict[int, SearchHit] = {}
        selected_ids: set[int] = set()
        tool_events: list[dict[str, Any]] = []
        planner_usage: list[dict[str, Any]] = []
        fallback_used = False
        steps = 0

        self.service.trace.log(
            "trusted_agent_started", question=text, max_steps=self.max_steps
        )

        for step in range(1, self.max_steps + 1):
            steps = step
            message, usage = self.service.client.chat(messages, tools=AGENT_TOOLS)
            if isinstance(usage, dict):
                planner_usage.append(usage)
            raw_tool_calls = message.get("tool_calls")
            self.service.trace.log(
                "trusted_agent_step",
                step=step,
                has_tool_calls=bool(raw_tool_calls),
                usage=usage,
            )
            if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
                break

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": raw_tool_calls,
            }
            messages.append(assistant_message)

            for raw_call in raw_tool_calls:
                if not isinstance(raw_call, dict):
                    continue
                tool_call_id = str(raw_call.get("id") or f"step-{step}")
                function = raw_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "")
                arguments = self._parse_arguments(function.get("arguments"))
                event: dict[str, Any] = {
                    "step": step,
                    "tool": name,
                    "arguments": arguments,
                    "success": False,
                }

                if name == "search_approved_knowledge":
                    query = str(arguments.get("query") or "").strip()
                    try:
                        requested_top_k = int(
                            arguments.get("top_k")
                            or self.service.settings.retrieval_top_k
                        )
                    except (TypeError, ValueError):
                        requested_top_k = self.service.settings.retrieval_top_k
                    hits = self._search(query, requested_top_k) if query else []
                    for hit in hits:
                        card_id = int(hit.card["id"])
                        previous = best_hits.get(card_id)
                        if previous is None or hit.score > previous.score:
                            best_hits[card_id] = hit
                    result_payload: dict[str, Any] = {
                        "query": query,
                        "hits": [self._compact_hit(hit) for hit in hits],
                    }
                    event.update(
                        success=bool(query),
                        card_ids=[int(hit.card["id"]) for hit in hits],
                    )
                elif name == "get_approved_card":
                    try:
                        card_id = int(arguments.get("card_id"))
                    except (TypeError, ValueError):
                        card_id = -1
                    hit = best_hits.get(card_id)
                    if hit is None:
                        result_payload = {
                            "error": "该卡片不在本次 Agent 已检索候选中"
                        }
                    else:
                        selected_ids.add(card_id)
                        result_payload = {"card": self._card_for_tool(hit.card)}
                        event.update(success=True, card_ids=[card_id])
                else:
                    result_payload = {"error": f"未知或未授权的工具: {name}"}

                tool_events.append(event)
                self.service.trace.log("trusted_agent_tool_result", **event)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "content": json.dumps(
                            result_payload, ensure_ascii=False, default=str
                        ),
                    }
                )

        if not best_hits:
            fallback_used = True
            for hit in self._search(text, self.service.settings.retrieval_top_k):
                best_hits[int(hit.card["id"])] = hit

        if selected_ids:
            answer_hits = [best_hits[card_id] for card_id in selected_ids]
        else:
            answer_hits = list(best_hits.values())
        answer_hits.sort(key=lambda hit: (hit.score, int(hit.card["id"])), reverse=True)
        answer_hits = answer_hits[: self.service.settings.retrieval_top_k]

        result = self.service._answer_from_hits(text, answer_hits)
        result["agent"] = {
            "steps": steps,
            "max_steps": self.max_steps,
            "tool_calls": tool_events,
            "selected_card_ids": [int(hit.card["id"]) for hit in answer_hits],
            "planner_usage": planner_usage,
            "fallback_used": fallback_used,
            "read_only": True,
        }
        self.service.trace.log(
            "trusted_agent_completed",
            question=text,
            steps=steps,
            tool_calls=len(tool_events),
            card_ids=result["agent"]["selected_card_ids"],
            refusal_reason=result.get("refusal_reason"),
        )
        return result
