# 生产东西向防火墙服务链十四步切换（合成演示）

> **重要：本文全部资源、指标、人员角色和执行记录均为合成演示数据。禁止据此操作真实生产网络。**

## 文档用途

本文是一份可上传到 Ops Knowledge Studio 的云网络运维知识样例，用于演示“知识采集 → 人工审核 → 自然语言辅助研判 → 生成变更单 → 审批执行 → 结果沉淀”。它描述历史合成经验和标准操作约束，不等同于对真实云资源的操作授权。

## 基本信息

| 字段 | 内容 |
| --- | --- |
| 案例 ID | `east-west-firewall-service-chain` |
| 演示变更单号 | `CHG-DEMO-EWFW-007` |
| 文档版本 | `synthetic-v1` |
| 类别 | 零信任分段 |
| 风险等级 | 极高（95/100） |
| 计划窗口 | 120 分钟 |
| 区域 | `cn-southwest-2` |
| VPC | `vpc-prod-service-mesh`（`10.120.0.0/16`） |
| 目的网段 | `10.160.0.0/12` |
| 路由类型 | `cloud_firewall` |
| 受影响服务 | `api-gateway`、`iam-service`、`database-proxy`、`cicd-runner` |
| 重点端口 | `443`、`5432` |

## 背景与目标

14 张路由表分四波插入新版东西向防火墙服务链。

将 14 张生产路由表的东西向流量分四波导入 cfw-inline-v2，并逐波验证策略、会话和审计。

目标是在保留完整快照、审批和回退能力的前提下，将既有目的网段的下一跳从 `cfw-inline-v1` 修改为 `cfw-inline-v2`。模拟器采用“修改既有路由下一跳”的语义，不新增同目的网段的重复路由。

## 环境事实

- 原下一跳：`cfw-inline-v1`（类型 `cloud_firewall`，状态 `DEGRADED`，容量 71%）。
- 目标下一跳：`cfw-inline-v2`（类型 `cloud_firewall`，状态 `UP`，容量 37%）。
- 影响范围：`api-gateway`、`iam-service`、`database-proxy`、`cicd-runner` 访问 `10.160.0.0/12` 的网络路径。
- 执行原则：先金丝雀/低风险波次，验证通过后才进入下一波次；任何硬门禁失败立即停止。

## 前置检查与硬门禁

- 变更请求、实施人和审批人已明确，当前时间位于已批准的变更窗口内。
- 区域 `cn-southwest-2`、VPC `vpc-prod-service-mesh` 及下表全部路由表、子网均存在。
- VPC CIDR `10.120.0.0/16` 与目的网段 `10.160.0.0/12` 格式合法，且无冲突或更具体路由。
- 环境快照显示目的网段当前下一跳确为 `cfw-inline-v1`，snapshot_version 未漂移。
- 目标下一跳 `cfw-inline-v2` 状态为 `UP`，容量利用率 37%（硬门槛：低于 60%）。
- 变更前 TCP 443/5432、丢包、P95 时延和业务探测基线已留存。
- 当前没有与本次资源重叠的并行网络变更，路由和策略已按计划冻结。
- 本次引用的知识卡片状态均为 `APPROVED`，未审核知识不得作为可信执行依据。
- 回退负责人、通知群组和故障升级路径均在线，变更前状态哈希已经固化。

## 路由修改计划

| 顺序 | 波次 | 操作 ID | 路由表 | AZ | 子网 | 目的网段 | 原下一跳 | 目标下一跳 |
| ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | CANARY | `route-switch-ewfw-shared-a` | `rtb-ewfw-shared-a` | `az-a` | `subnet-ewfw-shared-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 2 | CANARY | `route-switch-ewfw-shared-b` | `rtb-ewfw-shared-b` | `az-b` | `subnet-ewfw-shared-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 3 | WAVE-1 | `route-switch-ewfw-web-a` | `rtb-ewfw-web-a` | `az-a` | `subnet-ewfw-web-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 4 | WAVE-1 | `route-switch-ewfw-web-b` | `rtb-ewfw-web-b` | `az-b` | `subnet-ewfw-web-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 5 | WAVE-1 | `route-switch-ewfw-api-a` | `rtb-ewfw-api-a` | `az-a` | `subnet-ewfw-api-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 6 | WAVE-1 | `route-switch-ewfw-api-b` | `rtb-ewfw-api-b` | `az-b` | `subnet-ewfw-api-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 7 | WAVE-2 | `route-switch-ewfw-iam-a` | `rtb-ewfw-iam-a` | `az-a` | `subnet-ewfw-iam-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 8 | WAVE-2 | `route-switch-ewfw-iam-b` | `rtb-ewfw-iam-b` | `az-b` | `subnet-ewfw-iam-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 9 | WAVE-2 | `route-switch-ewfw-data-a` | `rtb-ewfw-data-a` | `az-a` | `subnet-ewfw-data-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 10 | WAVE-2 | `route-switch-ewfw-data-b` | `rtb-ewfw-data-b` | `az-b` | `subnet-ewfw-data-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 11 | WAVE-3 | `route-switch-ewfw-ops-a` | `rtb-ewfw-ops-a` | `az-a` | `subnet-ewfw-ops-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 12 | WAVE-3 | `route-switch-ewfw-ops-b` | `rtb-ewfw-ops-b` | `az-b` | `subnet-ewfw-ops-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 13 | WAVE-3 | `route-switch-ewfw-cicd-a` | `rtb-ewfw-cicd-a` | `az-a` | `subnet-ewfw-cicd-a` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |
| 14 | WAVE-3 | `route-switch-ewfw-cicd-b` | `rtb-ewfw-cicd-b` | `az-b` | `subnet-ewfw-cicd-b` | `10.160.0.0/12` | `cfw-inline-v1` | `cfw-inline-v2` |

每个操作必须使用审批绑定的 `ticket_id + revision + plan_hash + snapshot_version` 参数摘要；参数发生变化时旧审批立即失效，不得复用。

## 标准实施步骤

1. 冻结安全策略、地址组和路由表并记录审批版本
2. 导出 14 张路由表、会话量和策略命中基线
3. 比对新旧防火墙策略及对象组哈希
4. 预热新版防火墙节点并验证日志投递
5. 切换共享服务 AZ-A 与 AZ-B 作为金丝雀
6. 验证 DNS、时间同步、制品库和审计链路
7. 切换 Web 与 API 域四张路由表
8. 验证用户登录、网关调用和长连接重建
9. 切换 IAM 与数据域四张路由表
10. 验证认证、数据库代理及最小权限策略
11. 切换运维与 CI/CD 域四张路由表
12. 验证堡垒机、流水线和镜像拉取
13. 全量核对 14 张路由表的有效下一跳
14. 检查策略命中、误阻断、丢包和时延两个周期
15. 归档策略哈希、执行日志和最终网络快照

完成每个波次后，必须先核对有效下一跳并执行健康采样，验证通过后才能进入下一波次。已经成功完成且写入操作日志的步骤在恢复执行时不得重复应用。

## 成功标准

- 每张路由表对 `10.160.0.0/12` 的有效下一跳均为 `cfw-inline-v2`。
- TCP 443 和 TCP 5432 探测成功率均不低于 99.5%。
- 端到端丢包率不高于 1%。
- 端到端 P95 时延不高于 30 ms。
- 目标下一跳容量利用率低于 60%，且受影响服务没有新增严重告警。
- 操作日志、参数摘要、plan_hash、snapshot_version 与审批记录完全对应。

## 主要风险

- 策略差异可能导致大面积误阻断
- 非对称路由会造成有状态会话丢失
- 审计链路异常会形成合规证据缺口

## 自动回退触发条件

- 发现非对称路径或策略哈希不一致
- 关键服务成功率连续两个周期低于阈值
- 防火墙会话或容量高于安全水位
- 任一硬校验失败。
- TCP 成功率、丢包或 P95 时延连续两个模拟采样周期越过成功门槛。
- 执行时计划摘要、资源状态、当前下一跳或 snapshot_version 与审批时不一致。

## 回退步骤

回退严格按照路由修改计划的逆序执行：

1. 宣布停止后续波次，保留告警、指标、路由和操作日志证据。
2. 冻结新的网络修改，确认自动回退属于本次已审批计划。
3. 恢复 `rtb-ewfw-cicd-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
4. 恢复 `rtb-ewfw-cicd-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
5. 恢复 `rtb-ewfw-ops-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
6. 恢复 `rtb-ewfw-ops-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
7. 恢复 `rtb-ewfw-data-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
8. 恢复 `rtb-ewfw-data-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
9. 恢复 `rtb-ewfw-iam-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
10. 恢复 `rtb-ewfw-iam-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
11. 恢复 `rtb-ewfw-api-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
12. 恢复 `rtb-ewfw-api-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
13. 恢复 `rtb-ewfw-web-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
14. 恢复 `rtb-ewfw-web-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
15. 恢复 `rtb-ewfw-shared-b` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
16. 恢复 `rtb-ewfw-shared-a` 中目的网段 `10.160.0.0/12` 的下一跳：`cfw-inline-v2` → `cfw-inline-v1`。
17. 逐表确认有效下一跳已恢复为 `cfw-inline-v1`，不存在更具体路由或传播路由覆盖。
18. 连续观察两个采样周期，确认 TCP 443/5432 成功率、丢包和 P95 时延回到基线。
19. 比对回退后状态哈希与变更前快照；不一致则进入 `FAILED` 并升级人工处置。
20. 通知相关值守人员回退结果，归档执行日志、前后快照和遗留风险。

## 沟通计划

- 联合网络、安全、IAM、数据和研发效能团队值守
- 每个安全域切换后由业务负责人签字确认
- 完成后发布误阻断与审计日志核查结论

## 历史合成证据

知识标题：**历史案例：东西向防火墙服务链分批插入（合成）**

服务链变更需锁定策略版本、完成会话预热，并以共享、前台、核心和运维域逐级放量。

历史结果：历史合成演练完成 14 张路由表切换，策略哈希一致，未发现误阻断，审计日志完整。

该结果只用于离线演示知识复用；本次执行产生的反馈必须进入 `PENDING_REVIEW`，不得自动升级为 `APPROVED`。

## 建议用于自然语言研判的输入

> 我们计划在 `cn-southwest-2` 的 `vpc-prod-service-mesh` 中处理到 `10.160.0.0/12` 的网络路径。当前下一跳 `cfw-inline-v1` 状态为 DEGRADED、容量 71%，拟切换到 `cfw-inline-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

检索关键词：东西向 防火墙 服务链 零信任 14步 策略哈希 会话 回退

## 人工复核清单

- 回答是否只引用了状态为 `APPROVED` 的知识卡片，并带有知识编号引用。
- 资源 ID、目的网段、原/目标下一跳、阈值和回退顺序是否与本文一致。
- 模型是否仅用于说明和风险叙述，没有改写资源标识、动作、阈值或回退逻辑。
- 进入执行前是否人工选择了案例 `east-west-firewall-service-chain`，并核对变更单号 `CHG-DEMO-EWFW-007`。
- 终端确认串是否为 `APPROVE CHG-DEMO-EWFW-007`；拒绝或输入不精确时不得修改模拟网络。
