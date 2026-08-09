# 生产云骨干 TGW 路由域四波次迁移（合成演示）

> **重要：本文全部资源、指标、人员角色和执行记录均为合成演示数据。禁止据此操作真实生产网络。**

## 文档用途

本文是一份可上传到 Ops Knowledge Studio 的云网络运维知识样例，用于演示“知识采集 → 人工审核 → 自然语言辅助研判 → 生成变更单 → 审批执行 → 结果沉淀”。它描述历史合成经验和标准操作约束，不等同于对真实云资源的操作授权。

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 案例 ID | `transit-hub-route-domain-migration` |
| 演示变更单号 | `CHG-DEMO-TGW-006` |
| 文档版本 | `synthetic-v1` |
| 类别 | 云骨干 / TGW |
| 风险等级 | 极高（91/100） |
| 计划窗口 | 90 分钟 |
| 区域 | `cn-central-1` |
| VPC | `vpc-prod-transit-hub`（`10.100.0.0/16`） |
| 目的网段 | `10.200.0.0/16` |
| 路由类型 | `transit_gateway` |
| 受影响服务 | `order-platform`、`data-platform`、`bi-reporting`、`shared-services` |
| 重点端口 | `443`、`5432` |

## 背景与目标

12 张业务路由表按四个波次迁移到新版云骨干连接。

将 12 张生产路由表中 10.200.0.0/16 的下一跳从 tgw-core-v1 分四波迁移至 tgw-core-v2。

目标是在保留完整快照、审批和回退能力的前提下，将既有目的网段的下一跳从 `tgw-core-v1` 修改为 `tgw-core-v2`。模拟器采用“修改既有路由下一跳”的语义，不新增同目的网段的重复路由。

## 环境事实

- 原下一跳：`tgw-core-v1`（类型 `transit_gateway`，状态 `DEGRADED`，容量 79%）。
- 目标下一跳：`tgw-core-v2`（类型 `transit_gateway`，状态 `UP`，容量 34%）。
- 影响范围：`order-platform`、`data-platform`、`bi-reporting`、`shared-services` 访问 `10.200.0.0/16` 的网络路径。
- 执行原则：先金丝雀/低风险波次，验证通过后才进入下一波次；任何硬门禁失败立即停止。

## 前置检查与硬门禁

- 变更请求、实施人和审批人已明确，当前时间位于已批准的变更窗口内。
- 区域 `cn-central-1`、VPC `vpc-prod-transit-hub` 及下表全部路由表、子网均存在。
- VPC CIDR `10.100.0.0/16` 与目的网段 `10.200.0.0/16` 格式合法，且无冲突或更具体路由。
- 环境快照显示目的网段当前下一跳确为 `tgw-core-v1`，snapshot_version 未漂移。
- 目标下一跳 `tgw-core-v2` 状态为 `UP`，容量利用率 34%（硬门槛：低于 60%）。
- 变更前 TCP 443/5432、丢包、P95 时延和业务探测基线已留存。
- 当前没有与本次资源重叠的并行网络变更，路由和策略已按计划冻结。
- 本次引用的知识卡片状态均为 `APPROVED`，未审核知识不得作为可信执行依据。
- 回退负责人、通知群组和故障升级路径均在线，变更前状态哈希已经固化。

## 路由修改计划

| 顺序 | 波次 | 操作 ID | 路由表 | AZ | 子网 | 目的网段 | 原下一跳 | 目标下一跳 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CANARY | `route-switch-tgw-ops-a` | `rtb-tgw-ops-a` | `az-a` | `subnet-tgw-ops-a` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 2 | CANARY | `route-switch-tgw-ops-b` | `rtb-tgw-ops-b` | `az-b` | `subnet-tgw-ops-b` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 3 | WAVE-1 | `route-switch-tgw-app-a` | `rtb-tgw-app-a` | `az-a` | `subnet-tgw-app-a` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 4 | WAVE-1 | `route-switch-tgw-app-b` | `rtb-tgw-app-b` | `az-b` | `subnet-tgw-app-b` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 5 | WAVE-1 | `route-switch-tgw-api-a` | `rtb-tgw-api-a` | `az-a` | `subnet-tgw-api-a` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 6 | WAVE-1 | `route-switch-tgw-api-b` | `rtb-tgw-api-b` | `az-b` | `subnet-tgw-api-b` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 7 | WAVE-2 | `route-switch-tgw-data-a` | `rtb-tgw-data-a` | `az-a` | `subnet-tgw-data-a` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 8 | WAVE-2 | `route-switch-tgw-data-b` | `rtb-tgw-data-b` | `az-b` | `subnet-tgw-data-b` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 9 | WAVE-2 | `route-switch-tgw-bi-a` | `rtb-tgw-bi-a` | `az-a` | `subnet-tgw-bi-a` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 10 | WAVE-2 | `route-switch-tgw-bi-b` | `rtb-tgw-bi-b` | `az-b` | `subnet-tgw-bi-b` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 11 | FINAL | `route-switch-tgw-shared-a` | `rtb-tgw-shared-a` | `az-a` | `subnet-tgw-shared-a` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |
| 12 | FINAL | `route-switch-tgw-shared-b` | `rtb-tgw-shared-b` | `az-b` | `subnet-tgw-shared-b` | `10.200.0.0/16` | `tgw-core-v1` | `tgw-core-v2` |

每个操作必须使用审批绑定的 `ticket_id + revision + plan_hash + snapshot_version` 参数摘要；参数发生变化时旧审批立即失效，不得复用。

## 标准实施步骤

1. 冻结 TGW 路由传播、静态路由和关联关系变更
2. 导出 12 张业务路由表及有效下一跳快照
3. 核对新版 TGW 连接、BGP 会话和目标前缀通告
4. 校验新旧路由域无更具体前缀冲突
5. 切换运维网 AZ-A 路由并验证堡垒机连接
6. 切换运维网 AZ-B 路由并观察一个采样周期
7. 切换应用与 API 网段四张路由表
8. 验证订单链路、服务发现和东西向调用
9. 切换数据与 BI 网段四张路由表
10. 验证数据库只读连接、任务积压和报表查询
11. 切换共享服务双 AZ 路由表
12. 执行 12 张路由表有效下一跳全量核对
13. 观察丢包、P95 时延和 TGW 容量两个周期
14. 比对变更后状态哈希与操作日志
15. 解除路由策略冻结并发送变更完成通知

完成每个波次后，必须先核对有效下一跳并执行健康采样，验证通过后才能进入下一波次。已经成功完成且写入操作日志的步骤在恢复执行时不得重复应用。

## 成功标准

- 每张路由表对 `10.200.0.0/16` 的有效下一跳均为 `tgw-core-v2`。
- TCP 443 和 TCP 5432 探测成功率均不低于 99.5%。
- 端到端丢包率不高于 1%。
- 端到端 P95 时延不高于 30 ms。
- 目标下一跳容量利用率低于 60%，且受影响服务没有新增严重告警。
- 操作日志、参数摘要、plan_hash、snapshot_version 与审批记录完全对应。

## 主要风险

- 传播路由与静态路由重叠可能形成黑洞
- 跨业务域一次性迁移会放大故障半径
- 数据域长连接重建可能产生任务积压

## 自动回退触发条件

- 任一波次出现有效下一跳偏离
- 核心业务成功率或时延连续两个周期越过阈值
- TGW 新连接容量高于60%
- 任一硬校验失败。
- TCP 成功率、丢包或 P95 时延连续两个模拟采样周期越过成功门槛。
- 执行时计划摘要、资源状态、当前下一跳或 snapshot_version 与审批时不一致。

## 回退步骤

回退严格按照路由修改计划的逆序执行：

1. 宣布停止后续波次，保留告警、指标、路由和操作日志证据。
2. 冻结新的网络修改，确认自动回退属于本次已审批计划。
3. 恢复 `rtb-tgw-shared-b` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
4. 恢复 `rtb-tgw-shared-a` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
5. 恢复 `rtb-tgw-bi-b` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
6. 恢复 `rtb-tgw-bi-a` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
7. 恢复 `rtb-tgw-data-b` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
8. 恢复 `rtb-tgw-data-a` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
9. 恢复 `rtb-tgw-api-b` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
10. 恢复 `rtb-tgw-api-a` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
11. 恢复 `rtb-tgw-app-b` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
12. 恢复 `rtb-tgw-app-a` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
13. 恢复 `rtb-tgw-ops-b` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
14. 恢复 `rtb-tgw-ops-a` 中目的网段 `10.200.0.0/16` 的下一跳：`tgw-core-v2` → `tgw-core-v1`。
15. 逐表确认有效下一跳已恢复为 `tgw-core-v1`，不存在更具体路由或传播路由覆盖。
16. 连续观察两个采样周期，确认 TCP 443/5432 成功率、丢包和 P95 时延回到基线。
17. 比对回退后状态哈希与变更前快照；不一致则进入 `FAILED` 并升级人工处置。
18. 通知相关值守人员回退结果，归档执行日志、前后快照和遗留风险。

## 沟通计划

- T-30 分钟通知网络、应用、数据和安全值守
- 每个波次完成后发布验证结论
- 最终观察结束后同步状态哈希和遗留项

## 历史合成证据

知识标题：**历史案例：云骨干 TGW 路由域分批迁移（合成）**

大规模路由域迁移必须先冻结传播策略，再按运维、应用、数据和共享服务四波次放量。

历史结果：历史合成演练完成 12 张路由表四波次迁移，最大 P95 时延 23 ms，未触发逆序回退。

该结果只用于离线演示知识复用；本次执行产生的反馈必须进入 `PENDING_REVIEW`，不得自动升级为 `APPROVED`。

## 建议用于自然语言研判的输入

> 我们计划在 `cn-central-1` 的 `vpc-prod-transit-hub` 中处理到 `10.200.0.0/16` 的网络路径。当前下一跳 `tgw-core-v1` 状态为 DEGRADED、容量 79%，拟切换到 `tgw-core-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

检索关键词：TGW 云骨干 路由域 传播路由 四波次 大规模迁移 回退

## 人工复核清单

- 回答是否只引用了状态为 `APPROVED` 的知识卡片，并带有知识编号引用。
- 资源 ID、目的网段、原/目标下一跳、阈值和回退顺序是否与本文一致。
- 模型是否仅用于说明和风险叙述，没有改写资源标识、动作、阈值或回退逻辑。
- 进入执行前是否人工选择了案例 `transit-hub-route-domain-migration`，并核对变更单号 `CHG-DEMO-TGW-006`。
- 终端确认串是否为 `APPROVE CHG-DEMO-TGW-006`；拒绝或输入不精确时不得修改模拟网络。
