from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from change_management.cases import CHANGE_CASES, ChangeCase


CASE_FILENAMES = {
    "dc-route-failover": "01-dc-route-failover.md",
    "nat-egress-bluegreen": "02-nat-egress-bluegreen.md",
    "firewall-cluster-maintenance": "03-firewall-cluster-maintenance.md",
    "cross-region-dr-activation": "04-cross-region-dr-activation.md",
    "partner-extranet-migration": "05-partner-extranet-migration.md",
    "transit-hub-route-domain-migration": "06-transit-hub-route-domain-migration.md",
    "east-west-firewall-service-chain": "07-east-west-firewall-service-chain.md",
    "kubernetes-egress-pool-migration": "08-kubernetes-egress-pool-migration.md",
    "dns-resolver-endpoint-migration": "09-dns-resolver-endpoint-migration.md",
    "private-endpoint-service-cutover": "10-private-endpoint-service-cutover.md",
}


def bullet_lines(items: tuple[str, ...] | list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def numbered_lines(items: tuple[str, ...] | list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def route_plan_table(case: ChangeCase) -> str:
    rows = [
        "| 顺序 | 波次 | 操作 ID | 路由表 | AZ | 子网 | 目的网段 | 原下一跳 | 目标下一跳 |",
        "| ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, table in enumerate(case.route_tables):
        rows.append(
            "| {order} | {phase} | `{step_id}` | `{table_id}` | `{az}` | `{subnet}` | "
            "`{destination}` | `{source}` | `{target}` |".format(
                order=index + 1,
                phase=case.step_phase(index),
                step_id=case.step_id(index),
                table_id=table["id"],
                az=table["az"],
                subnet=table["subnet"],
                destination=case.destination,
                source=case.from_next_hop,
                target=case.to_next_hop,
            )
        )
    return "\n".join(rows)


def rollback_steps(case: ChangeCase) -> list[str]:
    steps = [
        "宣布停止后续波次，保留告警、指标、路由和操作日志证据。",
        "冻结新的网络修改，确认自动回退属于本次已审批计划。",
    ]
    for table in reversed(case.route_tables):
        steps.append(
            f"恢复 `{table['id']}` 中目的网段 `{case.destination}` 的下一跳："
            f"`{case.to_next_hop}` → `{case.from_next_hop}`。"
        )
    steps.extend(
        [
            f"逐表确认有效下一跳已恢复为 `{case.from_next_hop}`，不存在更具体路由或传播路由覆盖。",
            "连续观察两个采样周期，确认 TCP 443/5432 成功率、丢包和 P95 时延回到基线。",
            "比对回退后状态哈希与变更前快照；不一致则进入 `FAILED` 并升级人工处置。",
            "通知相关值守人员回退结果，归档执行日志、前后快照和遗留风险。",
        ]
    )
    return steps


def recommended_prompt(case: ChangeCase) -> str:
    return (
        f"我们计划在 `{case.region}` 的 `{case.vpc_id}` 中处理到 `{case.destination}` 的网络路径。"
        f"当前下一跳 `{case.from_next_hop}` 状态为 {case.from_status}、容量 {case.from_capacity_percent:g}%，"
        f"拟切换到 `{case.to_next_hop}`。请只引用已审核知识，给出风险、前置检查、分波次步骤、"
        "验证门槛和逆序回退建议，并指出仍需人工确认的内容。"
    )


def render_case(case: ChangeCase) -> str:
    affected_services = "、".join(f"`{name}`" for name in case.affected_services)
    service_ports = "、".join(f"`{port}`" for port in case.service_ports)
    prechecks = [
        "变更请求、实施人和审批人已明确，当前时间位于已批准的变更窗口内。",
        f"区域 `{case.region}`、VPC `{case.vpc_id}` 及下表全部路由表、子网均存在。",
        f"VPC CIDR `{case.vpc_cidr}` 与目的网段 `{case.destination}` 格式合法，且无冲突或更具体路由。",
        f"环境快照显示目的网段当前下一跳确为 `{case.from_next_hop}`，snapshot_version 未漂移。",
        f"目标下一跳 `{case.to_next_hop}` 状态为 `{case.to_status}`，容量利用率 "
        f"{case.to_capacity_percent:g}%（硬门槛：低于 60%）。",
        "变更前 TCP 443/5432、丢包、P95 时延和业务探测基线已留存。",
        "当前没有与本次资源重叠的并行网络变更，路由和策略已按计划冻结。",
        "本次引用的知识卡片状态均为 `APPROVED`，未审核知识不得作为可信执行依据。",
        "回退负责人、通知群组和故障升级路径均在线，变更前状态哈希已经固化。",
    ]
    success_criteria = [
        f"每张路由表对 `{case.destination}` 的有效下一跳均为 `{case.to_next_hop}`。",
        "TCP 443 和 TCP 5432 探测成功率均不低于 99.5%。",
        "端到端丢包率不高于 1%。",
        "端到端 P95 时延不高于 30 ms。",
        "目标下一跳容量利用率低于 60%，且受影响服务没有新增严重告警。",
        "操作日志、参数摘要、plan_hash、snapshot_version 与审批记录完全对应。",
    ]
    triggers = list(case.rollback_triggers) + [
        "任一硬校验失败。",
        "TCP 成功率、丢包或 P95 时延连续两个模拟采样周期越过成功门槛。",
        "执行时计划摘要、资源状态、当前下一跳或 snapshot_version 与审批时不一致。",
    ]

    return f"""# {case.title}

> **重要：本文全部资源、指标、人员角色和执行记录均为合成演示数据。禁止据此操作真实生产网络。**

## 文档用途

本文是一份可上传到 Ops Knowledge Studio 的云网络运维知识样例，用于演示“知识采集 → 人工审核 → 自然语言辅助研判 → 生成变更单 → 审批执行 → 结果沉淀”。它描述历史合成经验和标准操作约束，不等同于对真实云资源的操作授权。

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 案例 ID | `{case.case_id}` |
| 演示变更单号 | `{case.ticket_id}` |
| 文档版本 | `synthetic-v1` |
| 类别 | {case.category} |
| 风险等级 | {case.risk_level}（{case.risk_score}/100） |
| 计划窗口 | {case.window_minutes} 分钟 |
| 区域 | `{case.region}` |
| VPC | `{case.vpc_id}`（`{case.vpc_cidr}`） |
| 目的网段 | `{case.destination}` |
| 路由类型 | `{case.route_type}` |
| 受影响服务 | {affected_services} |
| 重点端口 | {service_ports} |

## 背景与目标

{case.description}

{case.summary}

目标是在保留完整快照、审批和回退能力的前提下，将既有目的网段的下一跳从 `{case.from_next_hop}` 修改为 `{case.to_next_hop}`。模拟器采用“修改既有路由下一跳”的语义，不新增同目的网段的重复路由。

## 环境事实

- 原下一跳：`{case.from_next_hop}`（类型 `{case.from_next_hop_type}`，状态 `{case.from_status}`，容量 {case.from_capacity_percent:g}%）。
- 目标下一跳：`{case.to_next_hop}`（类型 `{case.to_next_hop_type}`，状态 `{case.to_status}`，容量 {case.to_capacity_percent:g}%）。
- 影响范围：{affected_services} 访问 `{case.destination}` 的网络路径。
- 执行原则：先金丝雀/低风险波次，验证通过后才进入下一波次；任何硬门禁失败立即停止。

## 前置检查与硬门禁

{bullet_lines(prechecks)}

## 路由修改计划

{route_plan_table(case)}

每个操作必须使用审批绑定的 `ticket_id + revision + plan_hash + snapshot_version` 参数摘要；参数发生变化时旧审批立即失效，不得复用。

## 标准实施步骤

{numbered_lines(list(case.procedure_steps))}

完成每个波次后，必须先核对有效下一跳并执行健康采样，验证通过后才能进入下一波次。已经成功完成且写入操作日志的步骤在恢复执行时不得重复应用。

## 成功标准

{bullet_lines(success_criteria)}

## 主要风险

{bullet_lines(list(case.risks))}

## 自动回退触发条件

{bullet_lines(triggers)}

## 回退步骤

回退严格按照路由修改计划的逆序执行：

{numbered_lines(rollback_steps(case))}

## 沟通计划

{bullet_lines(list(case.communication_plan))}

## 历史合成证据

知识标题：**{case.knowledge_title}**

{case.knowledge_summary}

历史结果：{case.historical_outcome}

该结果只用于离线演示知识复用；本次执行产生的反馈必须进入 `PENDING_REVIEW`，不得自动升级为 `APPROVED`。

## 建议用于自然语言研判的输入

> {recommended_prompt(case)}

检索关键词：{case.search_query}

## 人工复核清单

- 回答是否只引用了状态为 `APPROVED` 的知识卡片，并带有知识编号引用。
- 资源 ID、目的网段、原/目标下一跳、阈值和回退顺序是否与本文一致。
- 模型是否仅用于说明和风险叙述，没有改写资源标识、动作、阈值或回退逻辑。
- 进入执行前是否人工选择了案例 `{case.case_id}`，并核对变更单号 `{case.ticket_id}`。
- 终端确认串是否为 `APPROVE {case.ticket_id}`；拒绝或输入不精确时不得修改模拟网络。
"""


def render_readme() -> str:
    case_rows = [
        "| 序号 | 案例 | 变更单号 | 路由表数 | 标准步骤数 | 推荐体验 |",
        "| ---: | --- | --- | ---: | ---: | --- |",
    ]
    for index, case in enumerate(CHANGE_CASES, start=1):
        filename = CASE_FILENAMES[case.case_id]
        recommendation = "快速入门" if index == 1 else ("复杂流程" if len(case.procedure_steps) >= 10 else "常规流程")
        case_rows.append(
            f"| {index} | [{case.label}]({filename}) | `{case.ticket_id}` | "
            f"{len(case.route_tables)} | {len(case.procedure_steps)} | {recommendation} |"
        )

    prompts = []
    for index, case in enumerate(CHANGE_CASES, start=1):
        prompts.append(
            f"### {index}. {case.label}\n\n"
            f"对应案例：`{case.case_id}` / `{case.ticket_id}`\n\n"
            f"> {recommended_prompt(case)}"
        )

    prompt_sections = "\n\n".join(prompts)

    return f"""# 十个云网络变更案例：知识采集到执行体验包

> **本目录全部内容均为合成演示数据，不连接真实云账号，不得用于真实生产变更。**

本体验包把当前变更中心的十个案例整理为可直接上传的 Markdown 运维文档。推荐先用第 1 个案例完整走通，再体验第 6～9 个十几步复杂案例。

## 案例目录

{chr(10).join(case_rows)}

## 一次完整体验怎么走

### 0. 保持 SSH 隧道运行

如果服务部署在当前阿里云服务器上，请在 **Windows PowerShell** 中保持端口转发命令所在窗口运行，然后访问：

`http://127.0.0.1:8876`

关闭这个 SSH 窗口会断开本地访问，但不会停止服务器上的 systemd 服务。

### 1. 知识采集

1. 进入“知识采集”。
2. 先上传 `01-dc-route-failover.md`；也可以一次多选本目录全部十份案例文档。
3. 等待解析完成，确认生成的知识卡片标题、步骤、风险和验证项与原文一致。
4. 文档中出现的资源和指标全部是合成数据，不应替换为真实凭据。

### 2. 人工审核

1. 进入知识审核队列。
2. 检查来源、资源标识、操作步骤、成功阈值和回退逻辑。
3. 将本次要使用的知识卡片人工批准为 `APPROVED`。
4. 保留其他未审核卡片的原状态，用来验证未批准知识不会进入可信回答。

### 3. 自然语言辅助研判

1. 进入“变更方案生成”。
2. 在“基于已审核知识辅助研判”中粘贴下方对应案例的推荐输入。
3. 点击“检索可信知识”或“Agent 辅助研判”。
4. 核对回答是否引用了已审核知识，是否覆盖前置检查、分波次步骤、验证门槛和逆序回退。
5. 如果回答缺少引用、引用未审核卡片或改变了资源 ID/阈值，不要进入执行。

### 4. 生成并开始变更

1. 仍在“变更方案生成”页面，从案例库手动选择与文档一致的案例卡片。
2. 点击“生成变更单”，查看风险分、环境快照、硬门禁、操作步骤、`plan_hash` 和审批摘要。
3. 确认审批前模拟网络状态没有变化。
4. 点击开始/审批操作，按界面提示输入精确确认串：`APPROVE <当前变更单号>`。
5. 执行期间观察金丝雀、后续波次、验证和可能的自动回退。

### 5. 查看结果并沉淀反馈

1. 进入“变更结果”，检查最终状态、每步执行记录、验证报告和前后状态哈希。
2. 成功路径应为 `SUCCEEDED`；注入验证失败时应为 `ROLLED_BACK`，且原下一跳和状态哈希恢复。
3. 将执行反馈发布到审核队列。
4. 确认反馈知识候选是 `PENDING_REVIEW`，不会自动成为 `APPROVED`。

## 当前版本的重要边界

自然语言研判与可执行变更单位于同一个“变更方案生成”页面，但当前版本不会把自然语言回答自动绑定为 `case_id`，也不会直接执行。你需要根据回答和文档，**手动选择对应案例**，再生成、核对和审批变更单。这个人工绑定是有意保留的安全门禁。

## 推荐自然语言输入

{prompt_sections}

## 验收记录建议

每走一个案例，建议记录以下信息：

- 上传文件名与生成的知识卡片 ID。
- 审核人、审核结果和 `APPROVED` 时间。
- 自然语言输入、可信知识引用和人工修订点。
- 选择的 `case_id`、变更单号、revision、plan_hash 与 snapshot_version。
- 审批前后的模拟网络状态哈希。
- 最终状态、失败/回退原因和反馈知识候选 ID。

这样十个案例走完后，你会得到一套可审计的“知识输入—方案依据—执行记录—经验反馈”演示证据。
"""


def export_documents(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if len(CHANGE_CASES) != 10:
        raise RuntimeError(f"Expected 10 change cases, found {len(CHANGE_CASES)}")
    if set(CASE_FILENAMES) != {case.case_id for case in CHANGE_CASES}:
        raise RuntimeError("CASE_FILENAMES does not match CHANGE_CASES")

    written = []
    readme_path = output_dir / "README.md"
    readme_path.write_text(render_readme(), encoding="utf-8")
    written.append(readme_path)
    for case in CHANGE_CASES:
        path = output_dir / CASE_FILENAMES[case.case_id]
        path.write_text(render_case(case), encoding="utf-8")
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the ten synthetic change cases as Markdown documents.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "sample_data" / "change_case_documents",
        help="Directory to receive README.md and the ten Markdown case documents.",
    )
    args = parser.parse_args()
    written = export_documents(args.output_dir.resolve())
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
