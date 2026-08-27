# Architecture and Technical Decisions

本文件只记录能从当前代码、配置和项目文档明确确认的重要决策。普通修改和短期进度放在 Git history 或 `CODEX_HANDOFF.md`，不在此累积。

## Decision: 确定性证据层与语义知识层分离

Status: Active

### Decision

结构化 ChangeOrder 先由版本化 Adapter 识别真实路径、字段结构、类型、JSON Pointer 和 SHA-256 证据，再交给语义 Card Builder。Extraction Unit 仅是证据运输/处理分块，不直接等同于 Knowledge Card。

### Reason

结构完整性、来源漂移和任务双视图对账可以由程序稳定验证；知识粒度、复用和检索价值属于另一层问题。把两层混在一起会产生上下文碎片、raw JSON 卡和因 chunk 大小导致的错误合并/拆分。

### Alternatives considered

- 每个 extraction chunk 直接生成一张统一模板卡。
- 让 LLM 同时推断 schema、证据范围和知识结构。

### Consequences

Adapter 和 Card Builder 必须保持独立版本与测试；审批时仍需重新验证证据，语义层不得削弱结构门禁。真实 schema 变化应先更新 Profile/Adapter，而不是在提示词中绕过。

## Decision: 使用有类型、分状态的 Knowledge Card 模型

Status: Active

### Decision

核心 Card Type 使用 `CASE_CONTEXT`、`PROCEDURE_STEP`、`EXECUTION_OUTCOME`，并将 review、dedup、quality、publish、retrieval、planning 状态拆开。每个 ChangeOrder 最多一张 Context；默认每个源 ProcedureStep 一张可检索知识；Outcome 不进入 planning RAG。

### Reason

不同业务实体需要不同正文结构和治理生命周期。“已审核”“可检索”“不重复”“质量高”不是同一个结论；历史执行结果也不能污染新变更计划。

### Alternatives considered

- 所有来源统一生成摘要/步骤/风险/回退模板。
- 用单一 `APPROVED` 或 quality score 同时决定审核、去重和发布。

### Consequences

所有查询、同步和生成入口必须组合检查状态。新增 card type 时必须定义模板、检索用途、planning 边界和 QA，而不是只增加一个名称。

## Decision: Procedure section 默认内嵌，Parent/Child 发布互斥

Status: Active

### Decision

一级 section 默认保存在 Parent 的 `operation_sections[]`，普通 checklist 不拆卡。只有步骤同时足够长且多个 section 自包含、可操作、非 noop 时才发布 children；拆分后 Parent 为 `CONTAINER/retrieval_disabled`，children 才 `INDEXED/retrieval_enabled`，最多一层。

### Reason

section 是内容结构，不天然是独立知识。Parent 和全部 Child 同时索引会重复内容、放大卡数量并导致一次检索召回同一来源的多份表达。

### Alternatives considered

- 达到固定 section 数量就拆卡。
- Parent 和 Child 全部进入向量索引，再依赖相似度去重。

### Consequences

Card Builder 必须输出 lineage、skip reason 和 `parent_child_retrieval_collision` QA；索引、长期记忆同步和审批都要过滤 Container。

## Decision: 相似治理由系统提议、人工合并，phase 是强约束

Status: Active

### Decision

后台仅用本地确定性信息发现候选。只有相同 `procedure_group` 的 Procedure 卡允许进入人工合并；跨 PRECHECK/IMPLEMENTATION/VALIDATION/ROLLBACK 的高相似卡保留独立 semantic identity，只建立 `CROSS_PHASE_SIMILAR` 等关系。接受同阶段候选时用单个 SQLite 事务建立 canonical 卡、关系和索引状态变化。

### Reason

相同文本在不同执行阶段具有不同业务上下文和检索用途；自动合并也可能抹掉例外条件或错误继承审核状态。

### Alternatives considered

- 只按 fingerprint/cosine 阈值跨阶段自动合并。
- 完全不做相似治理，让重复卡无限进入索引。

### Consequences

扫描器、服务入口和 Store 事务均必须重复执行 phase 门禁，不能只依赖前端。共享锚点以可解释性为先；人工拒绝、来源漂移和 case rebuild 都会影响候选生命周期。

## Decision: 真实 ChangeOrder 生成是隔离、默认关闭、不可执行的 BETA

Status: Active

### Decision

真实草案生成使用结构化 `RealChangeRequest`、人工确认的完整 `APPROVED` 案例包和已激活 `SchemaProfile`。模型只输出独立 `GeneratedChangeDraft`，程序确定性装配真实 JSON、派生任务分组、清空 ExecutionResult 并执行硬门禁。只有模型已返回正文后的 JSON/装配协议失败最多修复一次；超时、连接和 HTTP 传输失败立即停止。草案任务并发固定为 1；草案请求取得所选模型路由的进程内独占槽位并关闭底层隐藏 HTTP retry。允许配置同一兼容端点上的一个备用模型，但只在主模型出现明确容量错误时切换一次，并记录实际路由。只有人工批准 revision 可导出，不提供执行工具。

### Reason

缺失参数、历史结果泄漏、双视图不一致和 schema 漂移都是不能靠自然语言“尽量避免”的安全边界；个人内网模型还有并发限制。

### Alternatives considered

- 让模型直接生成两份任务视图和完整 ChangeOrder JSON。
- 模型失败后使用规则或部分输出兜底。
- 对所有模型错误都轮询或随机切换模型。
- 与合成执行 Demo 共用执行入口。

### Consequences

`REAL_CHANGE_GENERATION_ENABLED` 默认 false，启用前必须在可信内网完成 Profile 和模型检查。具体参数必须来自表单或占位并阻断审核；生成结果不自动写入知识库，也不能宣称为可执行工单。同一进程的普通模型路径仍可按各模型上限并发，但同模型在草案调用前必须排空并暂停；备用模型 ID 必须由部署者按网关真实配置提供。客户端超时不能证明供应商已经取消推理，因此不得在超时后自动重试或切换模型。该门禁不能代替对残留服务进程和其他主机占用的运维检查。

## Decision: Demo 安全功能默认开启，但敏感或破坏性功能例外

Status: Active

### Decision

当 `DEMO_MODE=true` 且未显式设置子功能 flag 时，安全、可逆的 Demo 能力默认启用。全库重置、真实业务数据模型调用等破坏性或敏感能力继续显式 opt-in；当前 `DEMO_FULL_RESET_ENABLED` 和 `REAL_CHANGE_GENERATION_ENABLED` 默认 false。

### Reason

Demo 升级后应能直接体验新增安全能力，同时不能因为默认值便利而扩大数据删除或真实数据外发风险。

### Alternatives considered

- 所有新增 Demo 子功能都默认 false。
- `DEMO_MODE=true` 时无条件开启全部能力。

### Consequences

新增配置必须先判断风险分类，并在 `.env.example`、配置解析和测试中保持一致。不能把“Demo 默认启用”理解成绕过生产总开关或安全例外。

## Decision: SQLite 是事实源，知识图谱和外部长记忆是受治理投影

Status: Active

### Decision

卡片、证据、状态、关系和审核以本地 SQLite 为事实源。3D Knowledge Graph 从 SQLite 实体/关系投影生成，不引入独立图数据库；MindMemOS 是可选后备，只同步白名单并将召回映射回本地卡重新验证。

### Reason

当前产品是单机能力验证，需要可审计、可回退且部署轻量。若图或外部向量服务成为事实源，容易绕过本地审批和漂移门禁。

### Alternatives considered

- 以 Neo4j/独立图数据库作为主存储。
- 直接信任外部向量库返回的正文和状态。

### Consequences

图布局与外部索引可重建，不能承载唯一审核事实。外部服务失败必须降级；任何合并、重建、撤销或状态变化都要同步清理投影映射。

## Decision: Demo 退出先停止接单并自然排空模型请求

Status: Active

### Decision

页面“安全退出”和命令行 `Ctrl+C` 都先进入 `DRAINING`：原子关闭新工作 admission，通知相似治理、主 Runtime 和合成 Demo Runtime 停止领取任务，等待在途 Web 操作、Worker 和所有已跟踪模型路由的活动请求归零，再关闭 HTTP Server。页面退出入口只在 `DEMO_MODE=true` 且 `DEMO_SAFE_SHUTDOWN_ENABLED=true` 时开放。

### Reason

强制结束主服务线程不能保证供应商已结束正在推理的远程请求，可能让个人账号的并发租约继续占用。当前 urllib 调用没有可靠的跨线程远程取消协议，自然返回或客户端超时是更可验证的释放边界。

### Alternatives considered

- 点击按钮后立即调用 `server.shutdown()` 或结束 Python 进程。
- 从另一线程强制关闭底层 socket，并假设供应商同步取消推理。
- 只停止 Web Server，不停止 Runtime 领取队列任务。

### Consequences

退出可能需要等待长模型请求完成，期间页面会显示活动模型、在途操作和 Worker 数量，并拒绝新工作。该机制只能排空本进程能跟踪的请求，不能清理其他进程、其他主机或供应商已泄漏的计数；这种情况仍需网关管理员处理。
