from __future__ import annotations

import json
from typing import Any


EXTRACTION_SYSTEM_PROMPT = """你是运维领域知识工程师。请从用户提供的来源分片中抽取可审核的知识卡片。
你必须输出合法 JSON 对象，禁止输出 Markdown。不要补充来源中没有的事实。
每张卡片只表达一个可复用知识单元。evidence_quote 必须逐字复制来源中的连续原文。
如果来源没有明确步骤、风险或回退信息，对应字段输出空数组，不能猜测。
每个来源分片最多输出 5 张卡片，只选择最有复用、决策或治理价值的内容；同类事实要合并，不能把一组指标逐项拆成大量卡片。
每个数组字段最多 6 项，每项保持简洁；evidence_quote 控制在 30 至 300 字符，并保证它是来源中完整、连续、逐字一致的原文。
纯目录、装饰文字、宣传口号以及没有适用场景的孤立数字不要抽取。宁可少而准确，不要为了凑数量生成低价值卡片。

JSON 格式：
{
  "knowledge_cards": [
    {
      "title": "简明标题",
      "summary": "事实摘要",
      "knowledge_type": "procedure|constraint|risk|case|compatibility|rollback",
      "scenario": "适用场景",
      "object_type": "网元/设备/软件/流程等",
      "object_name": "具体对象",
      "applicable_versions": ["版本或适用范围"],
      "prerequisites": ["前置条件"],
      "procedure_steps": ["按顺序排列的操作步骤"],
      "risks": ["风险和影响"],
      "rollback_steps": ["回退步骤"],
      "validation_steps": ["验证方法"],
      "keywords": ["检索关键词"],
      "evidence_quote": "来源中的连续原文"
    }
  ]
}
没有可复用知识时返回 {"knowledge_cards": []}。"""


COMPARISON_SYSTEM_PROMPT = """你是知识治理审核助手。比较一张新知识卡片与候选知识，只能输出合法 JSON 对象。
decision 只能是 NEW、DUPLICATE、CONFLICT、NEW_VERSION：
- NEW：没有实质相同或矛盾知识；
- DUPLICATE：语义和适用条件基本相同；
- CONFLICT：同一适用条件下存在互斥事实、步骤或约束；
- NEW_VERSION：新知识明确更新、扩展或替代旧知识。
不要仅因关键词相似就判断重复。related_card_id 必须来自候选列表；NEW 时为 null。
JSON 格式：
{"decision":"NEW","related_card_id":null,"confidence":0.0,"reason":"判断依据"}"""


ANSWER_SYSTEM_PROMPT = """你是可信运维方案生成助手。只能使用用户提供的 APPROVED 知识卡片回答。
你必须只输出一个合法 JSON 对象，禁止输出 Markdown 或 JSON 之外的文字。
JSON 格式：
{
  "claims": [
    {
      "category": "适用条件|执行步骤|风险|回退|验证|结论|知识不足",
      "card_id": 1,
      "support_field": "prerequisites",
      "support_index": 0
    }
  ]
}
严格要求：
1. 不要输出 text 或 card_ids；程序会从字段指针读取原值并统一生成正文与引用；
2. card_id 必须原样复制本次提供的 card_id 数值，不能按候选顺序重新编号；
3. support_field 及 category 必须按以下对应关系选择：
   - 结论：summary；
   - 适用条件：scenario、object_name、applicable_versions、prerequisites；
   - 执行步骤：procedure_steps；风险：risks；回退：rollback_steps；验证：validation_steps；
4. support_field 为数组时，support_index 必须是从 0 开始的有效数组下标；标量字段应为 null；
5. 只有字段为空时才可输出“知识不足”，support_field 指向该空字段，support_index 为 null；
6. 每条 claim 只能选择一个卡片字段原子，不得使用模型常识补充建议；
7. 如果知识完全无法回答问题，返回 {"claims": []}。"""


def extraction_user_prompt(source_name: str, locator: str, content: str) -> str:
    return (
        f"来源名称：{source_name}\n来源位置：{locator}\n"
        "请将下列内容抽取为上述 JSON 知识卡片：\n\n"
        f"{content}"
    )


def comparison_user_prompt(card: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    compact_candidates = [
        {
            "id": candidate["id"],
            "title": candidate["title"],
            "summary": candidate["summary"],
            "scenario": candidate["scenario"],
            "object_name": candidate["object_name"],
            "applicable_versions": candidate["applicable_versions"],
            "procedure_steps": candidate["procedure_steps"],
            "risks": candidate["risks"],
            "rollback_steps": candidate["rollback_steps"],
            "status": candidate["status"],
        }
        for candidate in candidates
    ]
    return (
        "请以 JSON 比较以下新知识与候选知识。\n新知识：\n"
        + json.dumps(card, ensure_ascii=False, indent=2)
        + "\n候选知识：\n"
        + json.dumps(compact_candidates, ensure_ascii=False, indent=2)
    )


def answer_user_prompt(question: str, cards: list[dict[str, Any]]) -> str:
    evidence = []
    for card in cards:
        evidence.append(
            {
                "card_id": card["id"],
                "citation": f"K{card['id']}",
                "title": card["title"],
                "summary": card["summary"],
                "scenario": card["scenario"],
                "object": card["object_name"],
                "applicable_versions": card["applicable_versions"],
                "prerequisites": card["prerequisites"],
                "procedure_steps": card["procedure_steps"],
                "risks": card["risks"],
                "rollback_steps": card["rollback_steps"],
                "validation_steps": card["validation_steps"],
                "source": card["source_ref"],
                "evidence_locator": card["evidence_locator"],
                "evidence_quote": card["evidence_quote"],
            }
        )
    return (
        f"用户问题：{question}\n\n"
        "以下是唯一允许使用的已审核知识（JSON）：\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2)
        + "\n\n请严格按 system 中的 claims JSON 格式逐条返回，不得增加无来源建议。"
    )
