# 十个云网络变更案例：知识采集到执行体验包

> **本目录全部内容均为合成演示数据，不连接真实云账号，不得用于真实生产变更。**

本体验包把当前变更中心的十个案例整理为可直接上传的 Markdown 运维文档。推荐先用第 1 个案例完整走通，再体验第 6～9 个十几步复杂案例。

## 案例目录

| 序号 | 案例 | 变更单号 | 路由表数 | 标准步骤数 | 推荐体验 |
| ---: | --- | --- | ---: | ---: | --- |
| 1 | [专线路由主备切换](01-dc-route-failover.md) | `CHG-DEMO-ROUTE-001` | 2 | 4 | 快速入门 |
| 2 | [NAT 网关蓝绿切换](02-nat-egress-bluegreen.md) | `CHG-DEMO-NAT-002` | 2 | 4 | 常规流程 |
| 3 | [云防火墙集群维护切流](03-firewall-cluster-maintenance.md) | `CHG-DEMO-CFW-003` | 2 | 4 | 常规流程 |
| 4 | [跨区域灾备链路启用](04-cross-region-dr-activation.md) | `CHG-DEMO-DR-004` | 2 | 4 | 常规流程 |
| 5 | [合作方 VPN 外联迁移](05-partner-extranet-migration.md) | `CHG-DEMO-B2B-005` | 2 | 4 | 常规流程 |
| 6 | [云骨干路由域分批迁移](06-transit-hub-route-domain-migration.md) | `CHG-DEMO-TGW-006` | 12 | 15 | 复杂流程 |
| 7 | [东西向防火墙服务链插入](07-east-west-firewall-service-chain.md) | `CHG-DEMO-EWFW-007` | 14 | 15 | 复杂流程 |
| 8 | [多集群容器出口池迁移](08-kubernetes-egress-pool-migration.md) | `CHG-DEMO-K8S-008` | 10 | 13 | 复杂流程 |
| 9 | [混合云 DNS 出站端点迁移](09-dns-resolver-endpoint-migration.md) | `CHG-DEMO-DNS-009` | 6 | 10 | 复杂流程 |
| 10 | [私网终端节点服务切换](10-private-endpoint-service-cutover.md) | `CHG-DEMO-PES-010` | 4 | 8 | 常规流程 |

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

### 1. 专线路由主备切换

对应案例：`dc-route-failover` / `CHG-DEMO-ROUTE-001`

> 我们计划在 `cn-north-4` 的 `vpc-prod-core` 中处理到 `172.20.32.0/20` 的网络路径。当前下一跳 `dc-primary` 状态为 DEGRADED、容量 72%，拟切换到 `dc-standby`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 2. NAT 网关蓝绿切换

对应案例：`nat-egress-bluegreen` / `CHG-DEMO-NAT-002`

> 我们计划在 `cn-east-3` 的 `vpc-prod-commerce` 中处理到 `0.0.0.0/0` 的网络路径。当前下一跳 `nat-old` 状态为 DEGRADED、容量 86%，拟切换到 `nat-green`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 3. 云防火墙集群维护切流

对应案例：`firewall-cluster-maintenance` / `CHG-DEMO-CFW-003`

> 我们计划在 `cn-south-1` 的 `vpc-prod-shared` 中处理到 `10.80.0.0/16` 的网络路径。当前下一跳 `cfw-primary` 状态为 DEGRADED、容量 67%，拟切换到 `cfw-standby`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 4. 跨区域灾备链路启用

对应案例：`cross-region-dr-activation` / `CHG-DEMO-DR-004`

> 我们计划在 `cn-east-2` 的 `vpc-prod-data` 中处理到 `172.31.64.0/20` 的网络路径。当前下一跳 `peering-primary` 状态为 DEGRADED、容量 74%，拟切换到 `vpn-dr`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 5. 合作方 VPN 外联迁移

对应案例：`partner-extranet-migration` / `CHG-DEMO-B2B-005`

> 我们计划在 `cn-north-9` 的 `vpc-prod-b2b` 中处理到 `192.168.120.0/24` 的网络路径。当前下一跳 `vpn-legacy` 状态为 DEGRADED、容量 63%，拟切换到 `vpn-new`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 6. 云骨干路由域分批迁移

对应案例：`transit-hub-route-domain-migration` / `CHG-DEMO-TGW-006`

> 我们计划在 `cn-central-1` 的 `vpc-prod-transit-hub` 中处理到 `10.200.0.0/16` 的网络路径。当前下一跳 `tgw-core-v1` 状态为 DEGRADED、容量 79%，拟切换到 `tgw-core-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 7. 东西向防火墙服务链插入

对应案例：`east-west-firewall-service-chain` / `CHG-DEMO-EWFW-007`

> 我们计划在 `cn-southwest-2` 的 `vpc-prod-service-mesh` 中处理到 `10.160.0.0/12` 的网络路径。当前下一跳 `cfw-inline-v1` 状态为 DEGRADED、容量 71%，拟切换到 `cfw-inline-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 8. 多集群容器出口池迁移

对应案例：`kubernetes-egress-pool-migration` / `CHG-DEMO-K8S-008`

> 我们计划在 `cn-east-5` 的 `vpc-prod-k8s-fleet` 中处理到 `0.0.0.0/0` 的网络路径。当前下一跳 `nat-k8s-legacy` 状态为 DEGRADED、容量 89%，拟切换到 `nat-k8s-pool-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 9. 混合云 DNS 出站端点迁移

对应案例：`dns-resolver-endpoint-migration` / `CHG-DEMO-DNS-009`

> 我们计划在 `cn-northwest-1` 的 `vpc-prod-dns-hub` 中处理到 `10.250.53.0/24` 的网络路径。当前下一跳 `dns-outbound-v1` 状态为 DEGRADED、容量 68%，拟切换到 `dns-outbound-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

### 10. 私网终端节点服务切换

对应案例：`private-endpoint-service-cutover` / `CHG-DEMO-PES-010`

> 我们计划在 `cn-south-4` 的 `vpc-prod-analytics` 中处理到 `10.254.80.0/24` 的网络路径。当前下一跳 `endpoint-service-legacy` 状态为 DEGRADED、容量 62%，拟切换到 `endpoint-service-v2`。请只引用已审核知识，给出风险、前置检查、分波次步骤、验证门槛和逆序回退建议，并指出仍需人工确认的内容。

## 验收记录建议

每走一个案例，建议记录以下信息：

- 上传文件名与生成的知识卡片 ID。
- 审核人、审核结果和 `APPROVED` 时间。
- 自然语言输入、可信知识引用和人工修订点。
- 选择的 `case_id`、变更单号、revision、plan_hash 与 snapshot_version。
- 审批前后的模拟网络状态哈希。
- 最终状态、失败/回退原因和反馈知识候选 ID。

这样十个案例走完后，你会得到一套可审计的“知识输入—方案依据—执行记录—经验反馈”演示证据。
