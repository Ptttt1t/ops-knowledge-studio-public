# 生产私网终端节点服务四步切换（合成演示）

> **重要：本文全部资源、指标、人员角色和执行记录均为合成演示数据。禁止据此操作真实生产网络。**

## 文档用途

本文是一份可上传到 Ops Knowledge Studio 的云网络运维知识样例，用于演示“知识采集 → 人工审核 → 自然语言辅助研判 → 生成变更单 → 审批执行 → 结果沉淀”。它描述历史合成经验和标准操作约束，不等同于对真实云资源的操作授权。

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 案例 ID | `private-endpoint-service-cutover` |
| 演示变更单号 | `CHG-DEMO-PES-010` |
| 文档版本 | `synthetic-v1` |
| 类别 | PrivateLink |
| 风险等级 | 高（80/100） |
| 计划窗口 | 45 分钟 |
| 区域 | `cn-south-4` |
| VPC | `vpc-prod-analytics`（`10.210.0.0/16`） |
| 目的网段 | `10.254.80.0/24` |
| 路由类型 | `private_endpoint` |
| 受影响服务 | `etl-runner`、`analytics-api`、`object-storage-proxy` |
| 重点端口 | `443`、`5432` |

## 背景与目标

批处理与分析域四张路由表切换到新版私网终端节点服务。

将批处理和分析域 4 张路由表迁移至 endpoint-service-v2，验证私网解析和对象访问。

目标是在保留完整快照、审批和回退能力的前提下，将既有目的网段的下一跳从 `endpoint-service-legacy` 修改为 `endpoint-service-v2`。模拟器采用“修改既有路由下一跳”的语义，不新增同目的网段的重复路由。

## 环境事实

- 原下一跳：`endpoint-service-legacy`（类型 `private_endpoint`，状态 `DEGRADED`，容量 62%）。
- 目标下一跳：`endpoint-service-v2`（类型 `private_endpoint`，状态 `UP`，容量 26%）。
- 影响范围：`etl-runner`、`analytics-api`、`object-storage-proxy` 访问 `10.254.80.0/24` 的网络路径。
- 执行原则：先金丝雀/低风险波次，验证通过后才进入下一波次；任何硬门禁失败立即停止。

## 前置检查与硬门禁

- 变更请求、实施人和审批人已明确，当前时间位于已批准的变更窗口内。
- 区域 `cn-south-4`、VPC `vpc-prod-analytics` 及下表全部路由表、子网均存在。
- VPC CIDR `10.210.0.0/16` 与目的网段 `10.254.80.0/24` 格式合法，且无冲突或更具体路由。
- 环境快照显示目的网段当前下一跳确为 `endpoint-service-legacy`，snapshot_version 未漂移。
- 目标下一跳 `endpoint-service-v2` 状态为 `UP`，容量利用率 26%（硬门槛：低于 60%）。
- 变更前 TCP 443/5432、丢包、P95 时延和业务探测基线已留存。
- 当前没有与本次资源重叠的并行网络变更，路由和策略已按计划冻结。
- 本次引用的知识卡片状态均为 `APPROVED`，未审核知识不得作为可信执行依据。
- 回退负责人、通知群组和故障升级路径均在线，变更前状态哈希已经固化。

## 路由修改计划

| 顺序 | 波次 | 操作 ID | 路由表 | AZ | 子网 | 目的网段 | 原下一跳 | 目标下一跳 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CANARY | `route-switch-pes-batch-a` | `rtb-pes-batch-a` | `az-a` | `subnet-pes-batch-a` | `10.254.80.0/24` | `endpoint-service-legacy` | `endpoint-service-v2` |
| 2 | CANARY | `route-switch-pes-batch-b` | `rtb-pes-batch-b` | `az-b` | `subnet-pes-batch-b` | `10.254.80.0/24` | `endpoint-service-legacy` | `endpoint-service-v2` |
| 3 | ROLLOUT | `route-switch-pes-analytics-a` | `rtb-pes-analytics-a` | `az-a` | `subnet-pes-analytics-a` | `10.254.80.0/24` | `endpoint-service-legacy` | `endpoint-service-v2` |
| 4 | ROLLOUT | `route-switch-pes-analytics-b` | `rtb-pes-analytics-b` | `az-b` | `subnet-pes-analytics-b` | `10.254.80.0/24` | `endpoint-service-legacy` | `endpoint-service-v2` |

每个操作必须使用审批绑定的 `ticket_id + revision + plan_hash + snapshot_version` 参数摘要；参数发生变化时旧审批立即失效，不得复用。

## 标准实施步骤

1. 冻结终端节点连接审批
2. 核对新服务后端健康
3. 验证私网 DNS 返回
4. 切换批处理双 AZ
5. 验证 ETL 与对象访问
6. 切换分析域双 AZ
7. 核对跨 AZ 流量与时延
8. 归档连接与路由快照

完成每个波次后，必须先核对有效下一跳并执行健康采样，验证通过后才能进入下一波次。已经成功完成且写入操作日志的步骤在恢复执行时不得重复应用。

## 成功标准

- 每张路由表对 `10.254.80.0/24` 的有效下一跳均为 `endpoint-service-v2`。
- TCP 443 和 TCP 5432 探测成功率均不低于 99.5%。
- 端到端丢包率不高于 1%。
- 端到端 P95 时延不高于 30 ms。
- 目标下一跳容量利用率低于 60%，且受影响服务没有新增严重告警。
- 操作日志、参数摘要、plan_hash、snapshot_version 与审批记录完全对应。

## 主要风险

- 终端节点连接未接受会导致黑洞
- 私网 DNS 缓存可能仍指向旧服务
- 跨 AZ 流量可能增加成本与时延

## 自动回退触发条件

- 终端节点连接状态异常
- 对象访问成功率低于阈值
- 跨 AZ 流量或时延连续两个周期升高
- 任一硬校验失败。
- TCP 成功率、丢包或 P95 时延连续两个模拟采样周期越过成功门槛。
- 执行时计划摘要、资源状态、当前下一跳或 snapshot_version 与审批时不一致。

## 回退步骤

回退严格按照路由修改计划的逆序执行：

1. 宣布停止后续波次，保留告警、指标、路由和操作日志证据。
2. 冻结新的网络修改，确认自动回退属于本次已审批计划。
3. 恢复 `rtb-pes-analytics-b` 中目的网段 `10.254.80.0/24` 的下一跳：`endpoint-service-v2` → `endpoint-service-legacy`。
4. 恢复 `rtb-pes-analytics-a` 中目的网段 `10.254.80.0/24` 的下一跳：`endpoint-service-v2` → `endpoint-service-legacy`。
5. 恢复 `rtb-pes-batch-b` 中目的网段 `10.254.80.0/24` 的下一跳：`endpoint-service-v2` → `endpoint-service-legacy`。
6. 恢复 `rtb-pes-batch-a` 中目的网段 `10.254.80.0/24` 的下一跳：`endpoint-service-v2` → `endpoint-service-legacy`。
7. 逐表确认有效下一跳已恢复为 `endpoint-service-legacy`，不存在更具体路由或传播路由覆盖。
8. 连续观察两个采样周期，确认 TCP 443/5432 成功率、丢包和 P95 时延回到基线。
9. 比对回退后状态哈希与变更前快照；不一致则进入 `FAILED` 并升级人工处置。
10. 通知相关值守人员回退结果，归档执行日志、前后快照和遗留风险。

## 沟通计划

- 通知数据平台、存储和网络值守
- 金丝雀完成后确认 ETL 作业
- 全量完成后同步私网访问指标

## 历史合成证据

知识标题：**历史案例：私网终端节点服务蓝绿切换（合成）**

PrivateLink 切换需同时核对服务接受状态、私网 DNS、后端健康和跨 AZ 流量。

历史结果：历史合成切换完成 4 张路由表迁移，ETL 作业和对象访问全部成功，未产生跨 AZ 异常。

该结果只用于离线演示知识复用；本次执行产生的反馈必须进入 `PENDING_REVIEW`，不得自动升级为 `APPROVED`。

## 建议用于自然语言研判的输入

> 我们计划在 `cn-south-4` 的 `vpc-prod-analytics` 中处理到 `10.254.80.0/24` 的网络路径。当前下一跳 `endpoint-service-legacy` 状态为 DEGRADED、容量 62%，拟切换到 `endpoint-service-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

检索关键词：PrivateLink 私网终端节点 蓝绿切换 私网DNS 对象存储 回退

## 人工复核清单

- 回答是否只引用了状态为 `APPROVED` 的知识卡片，并带有知识编号引用。
- 资源 ID、目的网段、原/目标下一跳、阈值和回退顺序是否与本文一致。
- 模型是否仅用于说明和风险叙述，没有改写资源标识、动作、阈值或回退逻辑。
- 进入执行前是否人工选择了案例 `private-endpoint-service-cutover`，并核对变更单号 `CHG-DEMO-PES-010`。
- 终端确认串是否为 `APPROVE CHG-DEMO-PES-010`；拒绝或输入不精确时不得修改模拟网络。
