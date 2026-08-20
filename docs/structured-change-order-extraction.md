# ChangeOrder 语义知识卡构建

平台将 ChangeOrder 的结构识别和知识建模明确分成两层。`change_order_shape_v2` Adapter 只负责确认结构、对账任务双视图，并保存 JSON Pointer、字符范围和 SHA-256；`change_order_semantic_builder_v1` 再从 normalized business objects 构建知识卡。Extraction Unit 是证据运输和处理分块，不再等同于 Knowledge Card。

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

只有单个步骤清洗后超过 `CHANGE_ORDER_PROCEDURE_SPLIT_CHARS`，或识别到不少于 `CHANGE_ORDER_SEMANTIC_SECTION_THRESHOLD` 个一级章节时，才允许建立 Parent + semantic child sections。拆分依据标题结构，不按固定字符数切断业务步骤；所有 child 保持相同 `source_pointer` 和 parent source identity。

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
- `publish_status`：`CANDIDATE / SKIPPED`；
- `planning_rag_enabled`：是否允许进入方案生成上下文。

每张卡执行 `has_raw_json`、`has_html_residue`、`has_empty_required_section`、`title_content_consistent`、`source_step_count`、`semantic_unit_count`、`source_fact_count` 和 `inferred_fact_count` QA。正文出现 raw JSON 或 HTML residue 时 `content_quality` 不可能为 100，且审批门禁会拒绝。证据矩阵仍在审批时重新读取来源文档，复算 Pointer/span/SHA-256；语义建模没有削弱原有证据门禁。

## Coverage 与诊断

`structural_source_coverage` 只说明每条 Adapter 来源证据已分配到卡或有明确 `skip_reason`。`semantic_content_coverage` 单独说明源 ProcedureStep 是否已被有效表达或复用。结构归属不再被描述成知识表达完成。

每次结构化 ChangeOrder 构建完成后，系统写入：

```text
<CHANGE_ORDER_CARD_REPORT_DIR>/<source-sha256>/card_build_report.json
```

默认目录为 `artifacts/change_order_card_reports`。报告包含卡数量、Procedure 来源步骤数、语义单元数、reuse 数、双 Coverage、逐卡 QA 和所有跳过原因。合成示例见 [`card_build_report.example.json`](card_build_report.example.json)。

## 数据边界

仓库测试只使用虚构 service、region、cluster、workload、工单号和人员。真实 ChangeOrder、模型 Prompt、生成卡和报告只能留在批准的内网环境。本实现不包含任何针对特定 ticket、卡片 ID、地区、业务名称或固定步骤数量的分支。
