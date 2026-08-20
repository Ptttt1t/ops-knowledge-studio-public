# ChangeOrder 语义知识卡构建

平台将 ChangeOrder 的结构识别和知识建模明确分成两层。`change_order_shape_v2` Adapter 只负责确认结构、对账任务双视图，并保存 JSON Pointer、字符范围和 SHA-256；`change_order_semantic_builder_v2` 再从 normalized business objects 构建知识卡。Extraction Unit 是证据运输和处理分块，不再等同于 Knowledge Card。

## 流水线

```text
ChangeOrder JSON
  -> ChangeOrder Adapter
  -> normalized business objects
  -> ProcedureStep normalization
  -> semantic unit building
  -> canonical semantic fingerprint / reuse
  -> governed Knowledge Card
  -> card_build_report.json
```

Adapter 继续执行以下硬约束：

- `/data/action_list` 与 `/data/change_tool_relate_action` 使用一致的 13-field TaskRecord Schema，并通过 stable JSON + SHA-256 multiset reconciliation 完整对账；group name 和 group count 是动态数据，不限制上限。
- `/data/sop_change_step` 包含四组 20-field ProcedureStep；每个源 ProcedureStep 都保持独立 Extraction Unit，不能因 chunk 大小与相邻步骤合并。
- `/data/change_plan/0/result` 是 15-field ExecutionResult，生命周期为 `post_execution`。
- API envelope 不进入知识正文或 RAG。

## Card Model

### CASE_CONTEXT

每个 ChangeOrder 最多一张。所有上下文 Extraction Unit 按业务字段合并到：`identity`、`service_scope`、`change_context`、`region`、`risk_impact`、`grayscale_policy`、`rollback_requirement`、`schedule`、`governance`、`tools` 和 `actions`。

无法表达业务含义的结构元数据记录为 `STRUCTURAL_METADATA_ONLY`，不会生成“IDENTITY_METADATA_CONTEXT 结构化知识”一类卡片。`action_list` 只进入 `CASE_CONTEXT.actions`，默认不生成 Procedure Card。

### PROCEDURE_STEP

默认一条源 ProcedureStep 对应一个 canonical procedure unit。字段映射固定为：

| Source field | Semantic field |
| --- | --- |
| `check_name` | `title` |
| `operate_description` | `operation` |
| `operate_verified` | `validation` |
| `operate_rollback` | `rollback` |
| `impact_analysis` | `impact_analysis` |
| `action_risk_level` | `risk_level` |
| `operate_commond` | `operate_command` |
| `command_list` | `command_list` |

`impact_analysis` 不会被自动改名为“风险”。模型区分 `risk_level`、`impact_analysis`、`risk_control` 和 `inferred_risk`；当前确定性 Builder 不制造推断，因此 `inferred_facts` 默认为空。历史实例参数保存在 `instance_parameters`，正文优先使用占位后的 `generalized_operation`。

一级章节默认只保存为 `operation_sections[]`，不是独立知识卡。每个 section 保存 `section_title`、`section_body`、`subitems`、`case_response`、`is_noop`、`is_actionable`、`is_self_contained`、`content_length` 和 `section_path`。`不涉及`、`无影响`、`无需操作` 等仍是有效 Source Fact，只是不单独发布。

只有单个步骤清洗后同时满足以下条件，才允许建立 Parent + semantic child units：

- `clean_content_length >= CHANGE_ORDER_PROCEDURE_SPLIT_CHARS`；
- meaningful section 数量达到 `CHANGE_ORDER_SEMANTIC_SECTION_THRESHOLD`；
- 每个待发布 child 不是 noop，具有正文、明确操作语义、自包含性，并达到 `CHANGE_ORDER_CHILD_MIN_CONTENT_CHARS`。

因此普通 5～10 项 checklist 不会仅凭 section 数量膨胀成多张卡。嵌套编号保存在 `subitems[]`，不会继续形成 grandchild；只有标题而无有效正文的中间层不会发布。拆分依据标题结构，不按固定字符数截断业务步骤。

Publication policy 是互斥的：未拆分时 Parent 为 `publish_status=INDEXED`、`retrieval_enabled=true`；确需拆分时 Parent 为 `publish_status=CONTAINER`、`retrieval_enabled=false`，meaningful children 为 `INDEXED/true`。本地检索、向量索引和外部长记忆同步均过滤非检索单元，审批门禁也拒绝 `parent_child_retrieval_collision=true`。

Procedure 标题始终带 phase 前缀：`前检：`、`实施：`、`验证：`、`回退：`。回退步骤在操作明确恢复原值时使用“恢复……原值”等语义；同一 ChangeOrder 中 phase 相同但目标不同的同名步骤增加确定性 qualifier，避免不同知识使用完全相同标题。

Builder 只在高置信条件下建立关系：实施和验证具有相同 operation target，且验证期望值等于实施目标值时建立 `VALIDATES`；回退与实施具有相同 target 且参数变更方向完全相反时建立 `ROLLBACK_OF`。证据不足时关系保持为空。Parent/Child 使用 `source_procedure_pointer`、`section_path`、`parent_unit_id` 表达确定性 lineage，不走普通相似度去重。

### EXECUTION_OUTCOME

ExecutionResult 单独生成 `EXECUTION_OUTCOME`，并固定 `planning_rag_enabled=false`。它可以用于历史结果检索、失败分析、质量评价和经验反馈，但不会进入新方案生成上下文。

## Rich text 与确定性字段

`normalize_rich_text()` 清理 `p/br/span/strong/a/img`、HTML entity、`&nbsp;` 和 JSON escape。链接 URL 不进入正文；图片转为 attachment/evidence metadata，正文只保留 `[图片证据]`。

timestamp、duration、boolean、number 等值由 Python 规范化。时间戳输出同时保留 `raw`、`normalized`、`timezone` 和 `iso8601`；默认 `CHANGE_ORDER_CARD_TIMEZONE=Asia/Shanghai`。例如 `1785772800000` 明确转换为 `2026-08-04 00:00:00`（Asia/Shanghai），不交给 LLM 换算。

## Semantic reuse

`source_identity` 由来源 Pointer、阶段、步骤索引和源哈希组成；`semantic_fingerprint` 只使用规范化知识内容，不包含 phase 或历史实例身份。相同 fingerprint 或达到 `CHANGE_ORDER_SEMANTIC_REUSE_THRESHOLD` 的高度一致步骤只保留一份 canonical knowledge，并合并 `applicable_phases`、`source_identities`、`source_evidence_refs` 和 `REUSES` 诊断记录。

跨文档完全重复的卡仍可作为审核记录持久化，但状态为 `dedup_status=DUPLICATE`、`publish_status=SKIPPED`，本地检索和 MindMemOS 同步都会排除它。

## 独立治理状态与 QA

新卡同时保存：

- `review_status`：人工审核生命周期，与兼容字段 `status` 同步；
- `dedup_status`：`NEW / REUSED / DUPLICATE`；
- `content_quality`：确定性正文质量分；
- `publish_status`：新语义卡使用 `INDEXED / CONTAINER / SKIPPED`；旧普通文档仍兼容 `CANDIDATE`；
- `retrieval_enabled`：是否允许进入本地检索、向量索引和外部长记忆；
- `planning_rag_enabled`：是否允许进入方案生成上下文。

每张卡执行 `has_raw_json`、`has_html_residue`、`has_empty_required_section`、`title_content_consistent`、`source_step_count`、`semantic_unit_count`、`source_fact_count` 和 `inferred_fact_count` QA。正文出现 raw JSON 或 HTML residue 时 `content_quality` 不可能为 100，且审批门禁会拒绝。证据矩阵仍在审批时重新读取来源文档，复算 Pointer/span/SHA-256；语义建模没有削弱原有证据门禁。

## Coverage 与诊断

`structural_source_coverage` 只说明每条 Adapter 来源证据已分配到卡或有明确 `skip_reason`。`semantic_content_coverage` 单独说明源 ProcedureStep 是否已被有效表达或复用。结构归属不再被描述成知识表达完成。

每次结构化 ChangeOrder 构建完成后，系统写入：

```text
<CHANGE_ORDER_CARD_REPORT_DIR>/<source-sha256>/card_build_report.json
```

默认目录为 `artifacts/change_order_card_reports`。报告包含来源步骤、Parent/Child、INDEXED/CONTAINER、noop、跳过 section、检索碰撞、标题碰撞、`VALIDATES/ROLLBACK_OF`、reuse、双 Coverage、逐卡 lineage/QA 和所有跳过原因。合成示例见 [`card_build_report.example.json`](card_build_report.example.json)。

## Demo 重建当前案例

当 `DEMO_MODE=true` 且 `DEMO_REBUILD_ENABLED=true` 时，案例包页面显示“重建当前案例”。确认后端点 `POST /api/knowledge-case-bundles/{case_id}/rebuild` 只清理该 `change-order:<source_sha256>` 的派生 cards、审核状态、关系、fingerprint、检索/外部长记忆映射、抽取报告和 ingestion 完成态，然后以 `force_rebuild=true` 重跑完整 Adapter → Builder → QA → Store 流程。原始上传文件、documents 行、source content、source_sha256 和结构证据不会删除。

普通 ingest 继续幂等；只有请求体包含 `{"confirmation":"REBUILD_CURRENT_CASE"}` 的显式 Demo 操作会强制重建。旧 K ID 不复用，案例包改用递增 `build_generation` 辅助前后对比。当前报告会替换 `card_build_report.json`，旧报告归档为 `card_build_report.generation-N.json`，并写入 `rebuild.previous_card_count`、`new_card_count` 与各层 purge 计数。`DEMO_FULL_RESET_ENABLED` 仅是保留且默认关闭的可选配置，本版本没有开放全库重置入口。

## 数据边界

仓库测试只使用虚构 service、region、cluster、workload、工单号和人员。真实 ChangeOrder、模型 Prompt、生成卡和报告只能留在批准的内网环境。本实现不包含任何针对特定 ticket、卡片 ID、地区、业务名称或固定步骤数量的分支。
