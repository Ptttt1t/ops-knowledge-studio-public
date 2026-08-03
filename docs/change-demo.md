# 云网络变更单最小闭环演示

这是一个完全本地、明确标记为合成数据的闭环，用来验证：

```text
环境感知 -> APPROVED知识复用 -> 分层决策 -> 变更单生成
         -> 硬性校验 -> 人工审批 -> 模拟执行 -> 验证/回退
         -> 执行反馈 -> PENDING_REVIEW知识候选
```

## 案例目录

平台预置 10 个明确标记为合成数据的历史变更案例，并以 `APPROVED` 卡片写入知识库：

- `dc-route-failover`：专线路由主备切换，`CHG-DEMO-ROUTE-001`；
- `nat-egress-bluegreen`：NAT 网关蓝绿切换，`CHG-DEMO-NAT-002`；
- `firewall-cluster-maintenance`：云防火墙集群维护切流，`CHG-DEMO-CFW-003`；
- `cross-region-dr-activation`：跨区域灾备链路启用，`CHG-DEMO-DR-004`；
- `partner-extranet-migration`：合作方 VPN 外联迁移，`CHG-DEMO-B2B-005`；
- `transit-hub-route-domain-migration`：云骨干路由域四波次迁移，`CHG-DEMO-TGW-006`，12 个执行步骤；
- `east-west-firewall-service-chain`：东西向防火墙服务链插入，`CHG-DEMO-EWFW-007`，14 个执行步骤；
- `kubernetes-egress-pool-migration`：多集群容器出口池迁移，`CHG-DEMO-K8S-008`，10 个执行步骤；
- `dns-resolver-endpoint-migration`：混合云 DNS 出站端点迁移，`CHG-DEMO-DNS-009`，6 个执行步骤；
- `private-endpoint-service-cutover`：私网终端节点服务切换，`CHG-DEMO-PES-010`，4 个执行步骤。

每个案例都提供独立的区域、VPC、多张路由表、源/目标下一跳、受影响服务、窗口、风险、历史结果和检索关键词。案例定义同时驱动环境模拟、知识检索、变更单生成、故障注入点和前端拓扑，避免界面选择与实际执行脱节。

### 默认场景

- 变更单：`CHG-DEMO-ROUTE-001`
- 区域/VPC：`cn-north-4` / `vpc-prod-core`
- 路由表：`rtb-prod-app-a`、`rtb-prod-app-b`
- 目标网段：`172.20.32.0/20`
- 动作：下一跳由 `dc-primary` 灰度切换至 `dc-standby`
- 验证：有效下一跳、TCP 443/5432 成功率、丢包与 P95 时延
- 回退：任一阶段验证失败时，按 AZ-B、AZ-A 逆序恢复

以上标识均为虚构，程序没有任何真实云 SDK 或凭据入口。

## 运行

### 集成 Web 工作台（推荐演示方式）

```powershell
python run.py serve
```

访问 <http://127.0.0.1:8765>，从左侧进入“变更方案生成”：

1. 从知识案例库选择一个案例，再选择正常闭环或任意具体执行步骤的故障注入并生成变更单；
2. 查看七阶段进度、双 AZ 拓扑、有效下一跳、知识证据、不可变计划哈希和全部硬校验；
3. 在人工门禁输入审批人和页面提示的精确确认串 `APPROVE <变更单号>`；
4. 执行结束后进入独立“变更结果”页，观察执行后拓扑、指标验证、必要时的逆序回退和全量审计轨迹；
5. 在结果页点击“送入知识审核队列”，再到原平台的审核页处理该 `PENDING_REVIEW` 候选。

原“可信方案”能力已合并为方案生成页下方的“基于已审核知识辅助研判”，仍严格只检索 `APPROVED` 卡片。

页面中的每次演示仍使用独立 SQLite 与工件目录；只有最后一步经操作者显式点击后，才会向主知识库写入一张待审核候选。页面不会接收或使用真实云凭据。

### CLI 单命令

```powershell
python run.py demo-change
```

指定非默认案例：

```powershell
python run.py demo-change --case-id nat-egress-bluegreen
```

运行 14 步东西向防火墙服务链案例：

```powershell
python run.py demo-change --case-id east-west-firewall-service-chain
```

审批前程序会打印工单摘要、计划哈希、环境快照哈希、知识引用和校验结果。只有输入与提示完全一致的批准串才会继续；直接回车、输入其他内容或标准输入结束都会拒绝执行，模拟网络保持不变。

演示自动回退：

```powershell
python run.py demo-change --case-id east-west-firewall-service-chain --inject-failure route-switch-ewfw-data-b
```

尝试让 DeepSeek 润色叙述字段：

```powershell
python run.py demo-change --use-model
```

模型只允许返回 `title` 和 `summary`。执行资源、CIDR、下一跳、阈值、步骤和回退均由代码固定；模型失败或输出不合格时，结果会标记为 `deterministic-fallback`。

## 工件

每次运行都在 `artifacts/change_demos/<run>/` 创建隔离目录：

- `change_order.md`：适合人工阅读的变更单；
- `change_package.json`：工单、校验、执行、反馈和审计全集；
- `validation_report.json`：生成前与执行后的验证证据；
- `execution_report.json`：前后状态哈希、步骤、指标和回退记录；
- `feedback.md`：实际结果、偏差、经验和知识候选；
- `runtime_events.json`：生成 Run 与执行 Run 的持久化事件；
- `knowledge.db`、`changes.db`、`cloud_network.db`、`runtime.db`：本次演示的隔离数据库。

审批与具体工具参数的规范化 SHA-256 摘要绑定；工单修订、计划哈希、环境快照或故障注入参数发生变化时，旧审批不会授权新的调用。

## 结果语义

- `SUCCEEDED`：两个可用区切换与验证均成功；
- `ROLLED_BACK`：执行发生验证失败，但自动回退成功，Run 本身正常完成；
- `BLOCKED`：前置校验或环境漂移阻断执行；
- `REJECTED`：人工未输入精确批准串；
- `FAILED`：执行或回退过程无法安全完成。

执行反馈只能生成 `PENDING_REVIEW` 知识候选，不会自动发布为可信知识。
