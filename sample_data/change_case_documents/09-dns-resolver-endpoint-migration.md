# 生产混合云 DNS 出站端点六步迁移（合成演示）

> **重要：本文全部资源、指标、人员角色和执行记录均为合成演示数据。禁止据此操作真实生产网络。**

## 文档用途

本文是一份可上传到 Ops Knowledge Studio 的云网络运维知识样例，用于演示“知识采集 → 人工审核 → 自然语言辅助研判 → 生成变更单 → 审批执行 → 结果沉淀”。它描述历史合成经验和标准操作约束，不等同于对真实云资源的操作授权。

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 案例 ID | `dns-resolver-endpoint-migration` |
| 演示变更单号 | `CHG-DEMO-DNS-009` |
| 文档版本 | `synthetic-v1` |
| 类别 | DNS / 混合云 |
| 风险等级 | 高（84/100） |
| 计划窗口 | 60 分钟 |
| 区域 | `cn-northwest-1` |
| VPC | `vpc-prod-dns-hub`（`10.180.0.0/16`） |
| 目的网段 | `10.250.53.0/24` |
| 路由类型 | `resolver_endpoint` |
| 受影响服务 | `service-discovery`、`database-client`、`ad-authentication` |
| 重点端口 | `443`、`5432` |

## 背景与目标

6 张业务路由表迁移到新版 DNS 出站解析端点。

将共享、应用和数据域 6 张路由表中的企业 DNS 网段迁移至 dns-outbound-v2。

目标是在保留完整快照、审批和回退能力的前提下，将既有目的网段的下一跳从 `dns-outbound-v1` 修改为 `dns-outbound-v2`。模拟器采用“修改既有路由下一跳”的语义，不新增同目的网段的重复路由。

## 环境事实

- 原下一跳：`dns-outbound-v1`（类型 `resolver_endpoint`，状态 `DEGRADED`，容量 68%）。
- 目标下一跳：`dns-outbound-v2`（类型 `resolver_endpoint`，状态 `UP`，容量 29%）。
- 影响范围：`service-discovery`、`database-client`、`ad-authentication` 访问 `10.250.53.0/24` 的网络路径。
- 执行原则：先金丝雀/低风险波次，验证通过后才进入下一波次；任何硬门禁失败立即停止。

## 前置检查与硬门禁

- 变更请求、实施人和审批人已明确，当前时间位于已批准的变更窗口内。
- 区域 `cn-northwest-1`、VPC `vpc-prod-dns-hub` 及下表全部路由表、子网均存在。
- VPC CIDR `10.180.0.0/16` 与目的网段 `10.250.53.0/24` 格式合法，且无冲突或更具体路由。
- 环境快照显示目的网段当前下一跳确为 `dns-outbound-v1`，snapshot_version 未漂移。
- 目标下一跳 `dns-outbound-v2` 状态为 `UP`，容量利用率 29%（硬门槛：低于 60%）。
- 变更前 TCP 443/5432、丢包、P95 时延和业务探测基线已留存。
- 当前没有与本次资源重叠的并行网络变更，路由和策略已按计划冻结。
- 本次引用的知识卡片状态均为 `APPROVED`，未审核知识不得作为可信执行依据。
- 回退负责人、通知群组和故障升级路径均在线，变更前状态哈希已经固化。

## 路由修改计划

| 顺序 | 波次 | 操作 ID | 路由表 | AZ | 子网 | 目的网段 | 原下一跳 | 目标下一跳 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CANARY | `route-switch-dns-shared-a` | `rtb-dns-shared-a` | `az-a` | `subnet-dns-shared-a` | `10.250.53.0/24` | `dns-outbound-v1` | `dns-outbound-v2` |
| 2 | CANARY | `route-switch-dns-shared-b` | `rtb-dns-shared-b` | `az-b` | `subnet-dns-shared-b` | `10.250.53.0/24` | `dns-outbound-v1` | `dns-outbound-v2` |
| 3 | WAVE-1 | `route-switch-dns-app-a` | `rtb-dns-app-a` | `az-a` | `subnet-dns-app-a` | `10.250.53.0/24` | `dns-outbound-v1` | `dns-outbound-v2` |
| 4 | WAVE-1 | `route-switch-dns-app-b` | `rtb-dns-app-b` | `az-b` | `subnet-dns-app-b` | `10.250.53.0/24` | `dns-outbound-v1` | `dns-outbound-v2` |
| 5 | FINAL | `route-switch-dns-data-a` | `rtb-dns-data-a` | `az-a` | `subnet-dns-data-a` | `10.250.53.0/24` | `dns-outbound-v1` | `dns-outbound-v2` |
| 6 | FINAL | `route-switch-dns-data-b` | `rtb-dns-data-b` | `az-b` | `subnet-dns-data-b` | `10.250.53.0/24` | `dns-outbound-v1` | `dns-outbound-v2` |

每个操作必须使用审批绑定的 `ticket_id + revision + plan_hash + snapshot_version` 参数摘要；参数发生变化时旧审批立即失效，不得复用。

## 标准实施步骤

1. 提前降低关键域名 TTL
2. 冻结条件转发规则
3. 导出查询成功率与缓存基线
4. 切换共享服务双 AZ
5. 验证 AD 与服务发现
6. 切换应用域双 AZ
7. 验证内部 API 解析
8. 切换数据域双 AZ
9. 观察 SERVFAIL、NXDOMAIN 和时延
10. 恢复 TTL 并归档证据

完成每个波次后，必须先核对有效下一跳并执行健康采样，验证通过后才能进入下一波次。已经成功完成且写入操作日志的步骤在恢复执行时不得重复应用。

## 成功标准

- 每张路由表对 `10.250.53.0/24` 的有效下一跳均为 `dns-outbound-v2`。
- TCP 443 和 TCP 5432 探测成功率均不低于 99.5%。
- 端到端丢包率不高于 1%。
- 端到端 P95 时延不高于 30 ms。
- 目标下一跳容量利用率低于 60%，且受影响服务没有新增严重告警。
- 操作日志、参数摘要、plan_hash、snapshot_version 与审批记录完全对应。

## 主要风险

- 条件转发遗漏会导致局部解析失败
- 负缓存可能延长故障影响
- AD 域名解析异常会影响认证

## 自动回退触发条件

- 关键域名解析结果不一致
- SERVFAIL 比例连续两个周期越过阈值
- AD 或数据库连接出现解析错误
- 任一硬校验失败。
- TCP 成功率、丢包或 P95 时延连续两个模拟采样周期越过成功门槛。
- 执行时计划摘要、资源状态、当前下一跳或 snapshot_version 与审批时不一致。

## 回退步骤

回退严格按照路由修改计划的逆序执行：

1. 宣布停止后续波次，保留告警、指标、路由和操作日志证据。
2. 冻结新的网络修改，确认自动回退属于本次已审批计划。
3. 恢复 `rtb-dns-data-b` 中目的网段 `10.250.53.0/24` 的下一跳：`dns-outbound-v2` → `dns-outbound-v1`。
4. 恢复 `rtb-dns-data-a` 中目的网段 `10.250.53.0/24` 的下一跳：`dns-outbound-v2` → `dns-outbound-v1`。
5. 恢复 `rtb-dns-app-b` 中目的网段 `10.250.53.0/24` 的下一跳：`dns-outbound-v2` → `dns-outbound-v1`。
6. 恢复 `rtb-dns-app-a` 中目的网段 `10.250.53.0/24` 的下一跳：`dns-outbound-v2` → `dns-outbound-v1`。
7. 恢复 `rtb-dns-shared-b` 中目的网段 `10.250.53.0/24` 的下一跳：`dns-outbound-v2` → `dns-outbound-v1`。
8. 恢复 `rtb-dns-shared-a` 中目的网段 `10.250.53.0/24` 的下一跳：`dns-outbound-v2` → `dns-outbound-v1`。
9. 逐表确认有效下一跳已恢复为 `dns-outbound-v1`，不存在更具体路由或传播路由覆盖。
10. 连续观察两个采样周期，确认 TCP 443/5432 成功率、丢包和 P95 时延回到基线。
11. 比对回退后状态哈希与变更前快照；不一致则进入 `FAILED` 并升级人工处置。
12. 通知相关值守人员回退结果，归档执行日志、前后快照和遗留风险。

## 沟通计划

- 通知 DNS、AD、应用和数据库值守
- 每个业务域切换后发布解析抽样结果
- 完成后同步 TTL 恢复时间

## 历史合成证据

知识标题：**历史案例：混合云 DNS 出站解析端点迁移（合成）**

DNS 迁移需提前降低 TTL，核对条件转发规则，并逐域验证解析正确率和缓存行为。

历史结果：历史合成迁移完成 6 张路由表切换，关键域名解析正确率 100%，无 SERVFAIL 增量。

该结果只用于离线演示知识复用；本次执行产生的反馈必须进入 `PENDING_REVIEW`，不得自动升级为 `APPROVED`。

## 建议用于自然语言研判的输入

> 我们计划在 `cn-northwest-1` 的 `vpc-prod-dns-hub` 中处理到 `10.250.53.0/24` 的网络路径。当前下一跳 `dns-outbound-v1` 状态为 DEGRADED、容量 68%，拟切换到 `dns-outbound-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

检索关键词：混合云 DNS 出站端点 条件转发 TTL SERVFAIL 分批迁移 回退

## 人工复核清单

- 回答是否只引用了状态为 `APPROVED` 的知识卡片，并带有知识编号引用。
- 资源 ID、目的网段、原/目标下一跳、阈值和回退顺序是否与本文一致。
- 模型是否仅用于说明和风险叙述，没有改写资源标识、动作、阈值或回退逻辑。
- 进入执行前是否人工选择了案例 `dns-resolver-endpoint-migration`，并核对变更单号 `CHG-DEMO-DNS-009`。
- 终端确认串是否为 `APPROVE CHG-DEMO-DNS-009`；拒绝或输入不精确时不得修改模拟网络。
