# MiniMax Mini-Agent 学习与平台集成报告

分析日期：2026-07-17  
上游仓库：[MiniMax-AI/Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)  
分析提交：`d76a4f6389688cabda39c224a6cdfa274215d47c`（2026-02-14）  
许可证：MIT

## 值得学习的设计

1. **有步骤上限的 Agent Loop**：模型请求、工具调用、工具结果和下一步推理形成清晰循环，并通过 `max_steps` 防止无限执行。
2. **统一工具协议**：工具都有名称、描述、JSON Schema 参数和统一结果，便于接入本地工具及 MCP。
3. **完整运行轨迹**：每次模型请求、响应和工具执行都可追踪，适合调试长任务。
4. **瞬时错误重试**：API 调用使用有上限的指数退避，提高临时网络故障下的成功率。
5. **上下文管理**：接近 token 上限时压缩历史执行过程，保留用户意图。
6. **持久笔记**：把跨会话事实和决策保存为 Session Note。
7. **Skills 渐进加载**：系统提示中只放技能元数据，需要时再加载完整 `SKILL.md`。
8. **MCP 接入**：支持 stdio、SSE 和 Streamable HTTP，并为连接和执行设置超时。

## 本轮已经加入我们的能力

### 只读可信知识 Agent

新增 `knowledge_platform/agent.py`，提供两个工具：

- `search_approved_knowledge`：只能检索 APPROVED 知识；
- `get_approved_card`：只能读取本轮已经检索到的 APPROVED 卡片。

安全约束：

- 默认最多 4 步，代码硬上限为 12 步；
- 不提供 Shell、文件写入、知识修改或发布工具；
- 未授权工具调用只返回错误，不执行任何系统动作；
- Agent 只负责检索和选卡，最终答案仍通过 claims JSON、card ID 和证据来源校验；
- 没有有效候选时使用原问题做一次确定性兜底检索，仍无结果则拒答。

### API 瞬时错误重试

以下错误会按 `0.5s → 1s → ...` 指数退避，并受最大等待时间限制：

- HTTP 408、429、500、502、503、504；
- 网络连接错误；
- 请求超时。

401、403 等配置或权限错误不会盲目重试。

### Agent 步骤轨迹

JSONL 轨迹新增：

- `trusted_agent_started`
- `trusted_agent_step`
- `trusted_agent_tool_result`
- `trusted_agent_completed`

记录步骤、工具参数、成功状态、命中卡片、token 用量和拒答原因，但不记录 API Key。

### 使用入口

CLI：

```powershell
python run.py agent-query --question "问题"
```

HTTP：

```text
POST /api/agent-query
{"question":"问题"}
```

网页“可信方案”页面新增“Agent 检索生成”按钮，原来的单次直接检索仍然保留。

## 真实验证

问题：`nginx ingress 如何按 predictorid 配置一致性哈希并验证？`

执行过程：

1. Agent 将问题改写为 `nginx ingress 一致性哈希 predictorid 配置`；
2. 调用搜索工具，唯一命中 K3；
3. 调用卡片读取工具核对 K3；
4. 停止工具循环；
5. 可信回答层生成 6 条 claims，全部引用 K3，证据为 exact 字符区间。

结果：3 个 Agent 步骤、2 次只读工具调用、无兜底、无拒答、无写操作。

完整运行结果保留在私有开发环境中，公开版不分发带本机路径和运行数据的 `artifacts/` 文件。

## 暂不直接照搬的能力

### Session Note

Mini-Agent 的 JSON 笔记适合通用个人 Agent，但不能直接进入我们的正式知识库。未审核笔记如果参与可信方案，会绕过 DRAFT/PENDING/APPROVED 生命周期。后续应将“会话记忆”和“正式知识”分库，笔记只有经过抽取和审核后才能晋升为知识卡片。

### Shell 和文件写工具

这类工具对编码 Agent 很有价值，但当前平台的目标是可信运维知识治理。现阶段开放系统命令会显著扩大风险面，因此没有接入。

### MCP

MCP 很适合后续接入 CMDB、工单、监控和知识图谱，但需要先设计服务器白名单、工具级权限、超时、输出大小限制和审计。第一版不自动加载外部 MCP。

### 上下文压缩

当前知识 Agent 是单问题、最多 4 步，尚未达到需要压缩的长度。等平台支持多轮任务会话后，再加入“保留用户意图、已选卡片和关键工具结果”的结构化压缩。

### Skills 渐进加载

这是下一项最适合加入的能力：把“升级方案生成、漏洞处置、应急响应、知识审核”等领域流程做成带元数据的技能，Agent 只在命中场景时加载完整指导，避免系统提示无限增长。
