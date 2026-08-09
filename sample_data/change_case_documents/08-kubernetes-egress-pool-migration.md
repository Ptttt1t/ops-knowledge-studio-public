# 生产 Kubernetes 多集群出口池十步迁移（合成演示）

> **重要：本文全部资源、指标、人员角色和执行记录均为合成演示数据。禁止据此操作真实生产网络。**

## 文档用途

本文是一份可上传到 Ops Knowledge Studio 的云网络运维知识样例，用于演示“知识采集 → 人工审核 → 自然语言辅助研判 → 生成变更单 → 审批执行 → 结果沉淀”。它描述历史合成经验和标准操作约束，不等同于对真实云资源的操作授权。

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 案例 ID | `kubernetes-egress-pool-migration` |
| 演示变更单号 | `CHG-DEMO-K8S-008` |
| 文档版本 | `synthetic-v1` |
| 类别 | 容器网络 |
| 风险等级 | 高（89/100） |
| 计划窗口 | 90 分钟 |
| 区域 | `cn-east-5` |
| VPC | `vpc-prod-k8s-fleet`（`10.140.0.0/16`） |
| 目的网段 | `0.0.0.0/0` |
| 路由类型 | `nat_gateway` |
| 受影响服务 | `catalog-cluster`、`checkout-cluster`、`order-cluster`、`payment-cluster` |
| 重点端口 | `443`、`5432` |

## 背景与目标

10 张子网路由表按集群和业务域迁移至新版 NAT 出口池。

将 10 张容器子网默认路由按观测、非资金和资金业务三波迁移至 nat-k8s-pool-v2。

目标是在保留完整快照、审批和回退能力的前提下，将既有目的网段的下一跳从 `nat-k8s-legacy` 修改为 `nat-k8s-pool-v2`。模拟器采用“修改既有路由下一跳”的语义，不新增同目的网段的重复路由。

## 环境事实

- 原下一跳：`nat-k8s-legacy`（类型 `nat_gateway`，状态 `DEGRADED`，容量 89%）。
- 目标下一跳：`nat-k8s-pool-v2`（类型 `nat_gateway`，状态 `UP`，容量 33%）。
- 影响范围：`catalog-cluster`、`checkout-cluster`、`order-cluster`、`payment-cluster` 访问 `0.0.0.0/0` 的网络路径。
- 执行原则：先金丝雀/低风险波次，验证通过后才进入下一波次；任何硬门禁失败立即停止。

## 前置检查与硬门禁

- 变更请求、实施人和审批人已明确，当前时间位于已批准的变更窗口内。
- 区域 `cn-east-5`、VPC `vpc-prod-k8s-fleet` 及下表全部路由表、子网均存在。
- VPC CIDR `10.140.0.0/16` 与目的网段 `0.0.0.0/0` 格式合法，且无冲突或更具体路由。
- 环境快照显示目的网段当前下一跳确为 `nat-k8s-legacy`，snapshot_version 未漂移。
- 目标下一跳 `nat-k8s-pool-v2` 状态为 `UP`，容量利用率 33%（硬门槛：低于 60%）。
- 变更前 TCP 443/5432、丢包、P95 时延和业务探测基线已留存。
- 当前没有与本次资源重叠的并行网络变更，路由和策略已按计划冻结。
- 本次引用的知识卡片状态均为 `APPROVED`，未审核知识不得作为可信执行依据。
- 回退负责人、通知群组和故障升级路径均在线，变更前状态哈希已经固化。

## 路由修改计划

| 顺序 | 波次 | 操作 ID | 路由表 | AZ | 子网 | 目的网段 | 原下一跳 | 目标下一跳 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CANARY | `route-switch-k8s-observe-a` | `rtb-k8s-observe-a` | `az-a` | `subnet-k8s-observe-a` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 2 | CANARY | `route-switch-k8s-observe-b` | `rtb-k8s-observe-b` | `az-b` | `subnet-k8s-observe-b` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 3 | WAVE-1 | `route-switch-k8s-catalog-a` | `rtb-k8s-catalog-a` | `az-a` | `subnet-k8s-catalog-a` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 4 | WAVE-1 | `route-switch-k8s-catalog-b` | `rtb-k8s-catalog-b` | `az-b` | `subnet-k8s-catalog-b` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 5 | WAVE-1 | `route-switch-k8s-checkout-a` | `rtb-k8s-checkout-a` | `az-a` | `subnet-k8s-checkout-a` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 6 | WAVE-1 | `route-switch-k8s-checkout-b` | `rtb-k8s-checkout-b` | `az-b` | `subnet-k8s-checkout-b` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 7 | WAVE-2 | `route-switch-k8s-order-a` | `rtb-k8s-order-a` | `az-a` | `subnet-k8s-order-a` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 8 | WAVE-2 | `route-switch-k8s-order-b` | `rtb-k8s-order-b` | `az-b` | `subnet-k8s-order-b` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 9 | WAVE-2 | `route-switch-k8s-payment-a` | `rtb-k8s-payment-a` | `az-a` | `subnet-k8s-payment-a` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |
| 10 | WAVE-2 | `route-switch-k8s-payment-b` | `rtb-k8s-payment-b` | `az-b` | `subnet-k8s-payment-b` | `0.0.0.0/0` | `nat-k8s-legacy` | `nat-k8s-pool-v2` |

每个操作必须使用审批绑定的 `ticket_id + revision + plan_hash + snapshot_version` 参数摘要；参数发生变化时旧审批立即失效，不得复用。

## 标准实施步骤

1. 冻结集群节点池扩缩容和出口白名单变更
2. 导出 10 张子网路由表与 NAT 会话基线
3. 确认新版 NAT 地址池已加入全部第三方白名单
4. 验证 SNAT 端口、连接追踪和日志容量
5. 切换观测集群双 AZ 默认路由
6. 验证日志、指标和镜像仓库访问
7. 切换商品与结算前台四张路由表
8. 验证公网 API、Webhook 和依赖下载
9. 切换订单与支付四张路由表
10. 验证支付回调、风控和消息投递
11. 核对十张路由表及出口公网地址
12. 观察 SNAT 端口利用率和失败连接两个周期
13. 解除冻结并归档迁移证据

完成每个波次后，必须先核对有效下一跳并执行健康采样，验证通过后才能进入下一波次。已经成功完成且写入操作日志的步骤在恢复执行时不得重复应用。

## 成功标准

- 每张路由表对 `0.0.0.0/0` 的有效下一跳均为 `nat-k8s-pool-v2`。
- TCP 443 和 TCP 5432 探测成功率均不低于 99.5%。
- 端到端丢包率不高于 1%。
- 端到端 P95 时延不高于 30 ms。
- 目标下一跳容量利用率低于 60%，且受影响服务没有新增严重告警。
- 操作日志、参数摘要、plan_hash、snapshot_version 与审批记录完全对应。

## 主要风险

- 第三方白名单遗漏会导致回调失败
- SNAT 端口耗尽会造成间歇性连接失败
- 长连接重建可能影响消息消费

## 自动回退触发条件

- 资金类回调或风控请求失败
- SNAT 端口利用率高于60%
- 连接失败率连续两个周期越过阈值
- 任一硬校验失败。
- TCP 成功率、丢包或 P95 时延连续两个模拟采样周期越过成功门槛。
- 执行时计划摘要、资源状态、当前下一跳或 snapshot_version 与审批时不一致。

## 回退步骤

回退严格按照路由修改计划的逆序执行：

1. 宣布停止后续波次，保留告警、指标、路由和操作日志证据。
2. 冻结新的网络修改，确认自动回退属于本次已审批计划。
3. 恢复 `rtb-k8s-payment-b` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
4. 恢复 `rtb-k8s-payment-a` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
5. 恢复 `rtb-k8s-order-b` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
6. 恢复 `rtb-k8s-order-a` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
7. 恢复 `rtb-k8s-checkout-b` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
8. 恢复 `rtb-k8s-checkout-a` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
9. 恢复 `rtb-k8s-catalog-b` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
10. 恢复 `rtb-k8s-catalog-a` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
11. 恢复 `rtb-k8s-observe-b` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
12. 恢复 `rtb-k8s-observe-a` 中目的网段 `0.0.0.0/0` 的下一跳：`nat-k8s-pool-v2` → `nat-k8s-legacy`。
13. 逐表确认有效下一跳已恢复为 `nat-k8s-legacy`，不存在更具体路由或传播路由覆盖。
14. 连续观察两个采样周期，确认 TCP 443/5432 成功率、丢包和 P95 时延回到基线。
15. 比对回退后状态哈希与变更前快照；不一致则进入 `FAILED` 并升级人工处置。
16. 通知相关值守人员回退结果，归档执行日志、前后快照和遗留风险。

## 沟通计划

- 通知容器平台、支付、风控和外联值守
- 每个集群波次同步出口 IP 与回调结果
- 完成后发送 NAT 容量和连接统计

## 历史合成证据

知识标题：**历史案例：Kubernetes 多集群 NAT 出口池迁移（合成）**

多集群出口迁移需核对白名单、SNAT 端口容量、连接追踪和第三方回调，再按业务等级分波放量。

历史结果：历史合成演练完成 10 张默认路由迁移，SNAT 峰值利用率 42%，支付回调全部成功。

该结果只用于离线演示知识复用；本次执行产生的反馈必须进入 `PENDING_REVIEW`，不得自动升级为 `APPROVED`。

## 建议用于自然语言研判的输入

> 我们计划在 `cn-east-5` 的 `vpc-prod-k8s-fleet` 中处理到 `0.0.0.0/0` 的网络路径。当前下一跳 `nat-k8s-legacy` 状态为 DEGRADED、容量 89%，拟切换到 `nat-k8s-pool-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

检索关键词：Kubernetes 多集群 NAT 出口池 SNAT 白名单 十步迁移 回退

## 人工复核清单

- 回答是否只引用了状态为 `APPROVED` 的知识卡片，并带有知识编号引用。
- 资源 ID、目的网段、原/目标下一跳、阈值和回退顺序是否与本文一致。
- 模型是否仅用于说明和风险叙述，没有改写资源标识、动作、阈值或回退逻辑。
- 进入执行前是否人工选择了案例 `kubernetes-egress-pool-migration`，并核对变更单号 `CHG-DEMO-K8S-008`。
- 终端确认串是否为 `APPROVE CHG-DEMO-K8S-008`；拒绝或输入不精确时不得修改模拟网络。
