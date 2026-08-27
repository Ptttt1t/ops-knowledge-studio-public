# Ops Knowledge Studio — Codex Context

此文件保存跨电脑、跨 Codex session 的长期项目上下文。它描述当前有效的实现和约束，不是开发日志。开始工作时还应阅读根目录 `AGENTS.md` 和当前状态文件 `CODEX_HANDOFF.md`；需要理解设计原因时阅读 `DECISIONS.md`。

## Project Overview

Ops Knowledge Studio 是一个本地优先、带证据和人工治理的运维知识与 ChangeOrder 能力验证平台。它把文档或结构化 ChangeOrder 转换成可审核的知识卡，只有满足审核、发布和检索状态门禁的知识才能进入可信问答、Agent 辅助或真实变更草案生成上下文。

当前主要目标是验证以下闭环：

- 结构化 ChangeOrder 的确定性识别、语义知识建模、证据追溯和案例包治理；
- 相似知识候选发现、人工确认合并及知识关系变化；
- 基于 `APPROVED` 完整案例包生成可审核但不可执行的真实 ChangeOrder 草案；
- 用隔离的合成云网络 Demo 验证计划、审批、模拟执行、验证、回退和反馈。

产品形态是一个 Python 单机 Web/CLI Demo。HTTP 服务、Harness Worker 和 SQLite 可运行在同一进程或同一主机，无 Redis、Docker 或外部消息队列依赖。默认配置是本机或隔离内网 Demo，不是生产变更系统。

## Architecture

### 主要模块

| 路径 | 职责 |
| --- | --- |
| `run.py`, `knowledge_platform/cli.py` | CLI 入口、服务启动、导入、审核、查询、SchemaProfile 和合成变更命令。 |
| `harness/` | 配置、DeepSeek-compatible 客户端、持久化 Run Runtime、预算、事件、检查点和工具审批。 |
| `knowledge_platform/documents.py` | 文本、PDF、可选 OCR 和安全文档解析。 |
| `knowledge_platform/change_order_adapter.py` | `change_order_shape_v2` 结构指纹、JSON Pointer 证据和任务双视图 reconciliation。 |
| `knowledge_platform/change_order_cards.py` | `change_order_semantic_builder_v2` Card Builder、rich text、deterministic normalization、section publication、复用和报告。 |
| `knowledge_platform/service.py` | 知识采集、模型预算、审核、检索/问答、案例包、case-scoped rebuild 的应用服务。 |
| `knowledge_platform/store.py` | SQLite 知识库、状态事务、证据、关系、案例包、相似治理及图谱投影。 |
| `knowledge_platform/retrieval.py`, `agent.py` | 本地可信检索、回答契约和只读 Agent 辅助。 |
| `knowledge_platform/semantic_governance.py` | 后台相似扫描、同阶段候选、跨阶段关系和人工 canonical 合并。 |
| `knowledge_platform/change_drafts.py` | SchemaProfile、案例推荐、真实草案 revision、硬门禁、审核、导出和留一盲测。 |
| `knowledge_platform/graph_layout.py`, `static/` | 关系图类型簇提示和 Web UI；浏览器执行 type-centroid + local force 的 3D 布局。 |
| `knowledge_platform/long_term_memory.py` | 可选 MindMemOS 映射、白名单同步、回读后本地重新验证和降级。 |
| `change_management/` | 完全合成的变更案例、票据、审批、隔离模拟器、验证/回退和反馈闭环。 |
| `tests/`, `sample_data/` | 单元/集成回归和虚构的同结构 fixture；不得加入真实内网样本。 |
| `docs/` | 部署、安全、ChangeOrder、语义治理、真实草案和共享 Codex 上下文。 |

### 关键数据流

知识采集：

```text
source document
→ safe parsing / ChangeOrder Adapter
→ normalized business objects
→ semantic Card Builder + deterministic QA
→ SQLite candidate cards + evidence + card_build_report
→ human review
→ APPROVED + INDEXED + retrieval_enabled knowledge
```

相似知识治理：

```text
approved retrievable Procedure cards
→ local deterministic scan
→ same phase: human-reviewed merge proposal
→ atomic canonical card + MERGED_INTO

cross phase
→ CROSS_PHASE_SIMILAR relation only; semantic identities remain separate
```

真实草案 BETA：

```text
RealChangeRequest
→ recommend and confirm 1–3 complete APPROVED case bundles
→ active compatible SchemaProfile
→ model GeneratedChangeDraft
→ deterministic real JSON assembly + hard gates
→ revisioned human review
→ approved JSON + provenance report export
```

合成执行 Demo 使用独立 `change_management` 数据库和模拟器，不会被真实草案功能调用。

## Important Constraints

- 跨电脑 Codex 接力只发生在两台外网开发电脑之间：一台常用外网电脑，以及一台放在工位但接入外网的老旧笔记本。公司内网环境不允许使用 Codex，也不能依赖内网 Codex session 完成开发或诊断。
- 内网环境仅由人工按批准流程部署代码并使用真实 ChangeOrder 验证。内网数据、卡片、Prompt、SchemaProfile 和报告不出网；外网 Codex 只能接收人工整理的脱敏计数、错误类型、结构特征和 QA 结果。
- Python 支持范围是 3.10–3.13；CI 在 Windows 和 Linux 的四个版本上运行基础测试。基础依赖只有 `pypdf`，OCR 是可选依赖。
- 真实业务数据只能在批准的内网运行。公开 GitHub 只允许虚构 service、region、cluster、workload、人员、工单号和 URL。
- `.env`、SQLite 数据库、上传源文件、模型缓存和 `artifacts/` 不进入 Git；GitHub 不是数据备份介质。
- 模型端点通过 `.env` 使用 DeepSeek-compatible API。只有模型已返回正文后的 JSON/草案协议失败可有一次修复；超时、连接或 HTTP 传输失败必须立即停止，不能重试、自动切换模型或输出 deterministic fallback 冒充模型结果。
- 同一进程、同一 base URL/API Key/实际 model ID 的调用共享模型路由门禁；主模型普通路径默认最大并发 `DEEPSEEK_MODEL_MAX_CONCURRENCY=2`。真实变更草案任务之间固定并发 `1`，且请求取得所选模型路由的独占槽位，因此不会与本进程内同模型的抽取、问答或其他 Runtime Worker 重叠。不同 model ID 的门禁和上限相互独立。
- 真实草案关闭底层隐藏 HTTP retry；草案服务只对已收到响应正文后的 JSON/装配协议失败进行一次显式修复。可通过 `CHANGE_DRAFT_FALLBACK_MODEL` 配置同一兼容端点上的备用模型；只有 429、`count/limit` 等明确容量错误才从主模型切换一次。超时不能证明远端推理已结束，因此既不重试也不切换备用模型。实际路由写入 revision，避免混淆不同模型的能力结果。`/api/health.model_requests.routes` 提供各路由的进程级活动/等待/峰值诊断，但不能约束其他进程或其他主机。
- `DEMO_MODE=true` 时，安全且可逆的 Demo 子功能默认启用。破坏性或敏感例外仍默认关闭，当前包括 `DEMO_FULL_RESET_ENABLED=false` 和 `REAL_CHANGE_GENERATION_ENABLED=false`。
- 真实草案生成必须有已激活且结构一致的 SchemaProfile，且第一版只生成/审核/导出，不注册真实执行工具。
- Web 当前使用共享 token 而非组织级身份。公网暴露、多租户、正式审计和职责分离均不在当前实现能力内。
- 默认持久化是单主机 SQLite：`data/knowledge.db`、`data/runtime.db`、`data/change_drafts.db`。升级前应停服备份，再执行 `python run.py init`。
- `HARNESS_WORKERS` 可以处理多个 Run，但单个 Run 内步骤按顺序执行；这与真实草案模型并发信号量是两个不同边界。
- Demo 安全退出采用 admission close + natural drain：先拒绝新工作并停止后台 Worker 领任务，再等待在途 Web 操作和模型请求归零，最后关闭 HTTP 服务。浏览器关闭不等于服务退出；`Ctrl+C` 和页面按钮都应走排空流程，避免供应商侧请求未正常完成而占用并发槽位。

## Important Domain Knowledge

### ChangeOrder 和知识卡

- Extraction Unit 是来源证据/处理单元，不等于 Knowledge Card。
- Card Type 至少分为 `CASE_CONTEXT`、`PROCEDURE_STEP`、`EXECUTION_OUTCOME`；每个 ChangeOrder 最多一个 Context。
- 四个 procedure group 是 `PRECHECK`、`IMPLEMENTATION`、`VALIDATION`、`ROLLBACK`。Phase 是业务语义和相似合并的强约束。
- `/data/action_list` 是 Action/Capability metadata，不是有序 Procedure；它是生成草案时唯一任务主视图，`change_tool_relate_action` 必须由程序派生并通过稳定 JSON + SHA-256 multiset reconciliation 对账。
- 默认一条源 ProcedureStep 对应一个可检索单元。section 默认只留在 `operation_sections[]`；仅当步骤确实过长且多个 section 自包含、可操作、非 noop 时才发布 child。
- Parent/Child 发布互斥：普通 Parent 为 `INDEXED/true`；拆分 Parent 为 `CONTAINER/false`，只有 meaningful children 为 `INDEXED/true`。
- `EXECUTION_OUTCOME.planning_rag_enabled` 固定为 `false`，历史执行结果不能默认进入新方案生成。
- 实例具体值存入 `instance_parameters`，RAG 优先使用 `generalized_operation`。timestamp、duration、boolean、enum、number 等由 Python 确定性规范化，不交给 LLM 计算。
- 原始 JSON、HTML 和完整图片 URL不得进入知识正文；证据使用 JSON Pointer、范围和 SHA-256 保存，审核时重新校验漂移。

### 状态与治理

- `review_status`、`dedup_status`、`content_quality`、`publish_status`、`retrieval_enabled`、`planning_rag_enabled` 是独立维度。
- `APPROVED` 本身不代表能检索；普通可信检索还要求已发布、允许检索、证据未漂移并通过语义相关性门禁。
- 同一 procedure group 的高度相似 Procedure 可以进入人工合并候选。不同 group 无论文本或 fingerprint 多相似都不得 MERGE，只能建立 `CROSS_PHASE_SIMILAR`、复用或业务关系。
- 自动扫描只提出候选，不自动合并。人工接受时事务性创建 canonical 卡，并使来源卡退出检索；拒绝与来源 fingerprint 绑定。
- `VALIDATES` 和 `ROLLBACK_OF` 只在 operation target 和参数方向证据足够时建立；证据不足时为空，不猜测。
- K 编号是展示 ID，不是稳定语义身份。案例重建使用新的单调递增 ID；稳定定位依赖 case ID、source pointer、semantic fingerprint 和 card type。

## Current Technical Approach

- ChangeOrder 结构层使用 `change_order_shape_v2`；动态 task group 不设上限，只约束每组为 TaskRecord 数组且与 flat view 完全对账。
- 语义层使用 `change_order_semantic_builder_v2` / card model v2；rich text、时间、section、QA 和报告主要为确定性 Python 逻辑。
- 相似治理使用 `semantic_candidate_v2` 和 `semantic_merge_v2`。共享锚点来自结构化字段、技术标识符和完整业务短语，不向用户展示中文字符滑窗片段。
- 相似治理后台扫描只执行本地 SQLite/确定性相似度计算，不调用模型；不要把扫描 Worker 存活误判为 API 并发占用。
- 知识图谱是 SQLite cards/cases/objects/sources/relations 的投影视图，不是独立图数据库。当前布局策略为 `type_centroid_local_force_v1`，颜色和空间簇共同表达节点类型/phase，显式关系边仍参与局部力布局。
- 真实草案由模型只生成独立 `GeneratedChangeDraft`，程序负责 SchemaProfile 驱动的真实 JSON 装配、ExecutionResult 清空、任务双视图和全部硬门禁。
- 可选 MindMemOS 只作语义后备。同步内容有白名单，召回必须映射回本地 card 并重新验证；外部失败退回本地检索。

## Known Long-term Issues

- 正式生产仍缺组织身份、RBAC/职责分离、CMDB、工单、监控、密钥托管、独立索引服务、容量/灾备设计和真实执行适配器。
- 共享 token 只能支持受控 Demo，操作者自报身份不能作为正式审计证据。
- SQLite 与进程内后台任务适合单机验证，不是多实例高可用架构。
- 3D 图布局和相似阈值需要继续用不出网的内网数据做人工可用性校准；真实内容与结果不得带回公开仓库。
- 全局 Demo 知识库重置只有配置预留，当前 Web 未开放入口；case-scoped rebuild 已实现。
- 真实变更生成、SchemaProfile 和留一盲测仍是 BETA，不能据此宣称具备生产自动变更能力。

## Development Conventions

- 开始工作按根目录 `AGENTS.md` 做只读恢复；代码、配置和 Git 状态优先于聊天记忆。
- 外网电脑 A/B 通过 GitHub `pull / commit / push` 接力；不要把“工位外网笔记本”称为内网电脑。内网真实验证是独立的人工环节，不运行 Codex。
- 使用 `rg` / `rg --files` 定位代码。保持改动范围聚焦，不覆盖用途不明的用户修改。
- 新业务逻辑必须配 synthetic unit/integration tests。禁止把任何真实 ChangeOrder、卡片、Prompt、路径或内部 URL 固化进 fixture。
- 常用本地验证：

```text
python -m unittest discover -s tests -t .
python -m compileall -q harness knowledge_platform tests
node --check knowledge_platform/static/app.js
git diff --check
```

- `python run.py init` 只补齐 schema/migration，不应清空数据库。普通 ingest 保持幂等，只有显式 case rebuild 才 purge 当前案例并强制重跑。
- 新配置写入 `harness/config.py` 和 `.env.example`，并在相关文档说明默认值、安全边界和作用范围。
- 一轮有意义的工作结束时更新 `CODEX_HANDOFF.md`；只有长期事实改变时更新本文件，只有重要设计选择改变时更新 `DECISIONS.md`。
- 有实质修改且测试、完整 diff 和敏感信息检查通过后，默认直接发布，不再等待用户重复要求“合并”；用户明确要求保留分支/暂不发布，或验证与远端状态不安全时例外。发布时基于最新 `github/main` 快进本地 `main`，push 后比较本地与远端 SHA。
