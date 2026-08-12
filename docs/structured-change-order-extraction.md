# 结构化变更单知识抽取

平台对普通文档继续使用通用文本分片；对符合 ChangeOrder 结构的 JSON，则先由确定性 Adapter 完成结构识别、任务对账、步骤编排和覆盖账本，再调用模型抽取知识。模型不负责猜测 Schema，也不能决定跳过哪些源节点。

## 已确认的真实 Key 映射

当前 Adapter 优先使用已经确认的业务路径，不依赖脱敏分析阶段生成的 `Pxxxx`：

| JSON Pointer | 领域角色 | 处理方式 |
| --- | --- | --- |
| `/data/action_list` | `TASKS_CANONICAL` | TaskRecord 的唯一知识抽取主视图 |
| `/data/change_tool_relate_action` | grouped projection | 仅保存分组与来源信息；对账成功后不重复生成知识卡 |
| `/data/sop_change_step/check_before_change` | `PRECHECK_STEPS` | 变更前检查步骤 |
| `/data/sop_change_step/change_implement` | `IMPLEMENTATION_STEPS` | 变更实施步骤 |
| `/data/sop_change_step/change_verified` | `VALIDATION_STEPS` | 变更验证步骤 |
| `/data/sop_change_step/change_rollback` | `ROLLBACK_STEPS` | 变更回退步骤 |
| `/data/change_plan/0/result` | `EXECUTION_RESULT` | `post_execution` 执行结果 |
| `/code`、`/provider_code`、`/msg` | `API_ENVELOPE` | API 包装字段，默认不进入 RAG |

Procedure 四组元素统一使用 `ProcedureStep`，不为检查、实施、验证和回退各设计一套重复 Schema。若输入没有命中上述真实路径、只能通过结构指纹识别，Adapter 会将候选标记为 `HEURISTIC`，且不会再根据数组位置猜成回滚或验证步骤。

## 领域结构

```text
ChangeOrder
├── planning_context
│   ├── identity
│   ├── service_scope
│   ├── change_context
│   ├── risk_impact
│   ├── execution_context
│   └── governance_context
├── tasks
│   ├── canonical_tasks[]
│   └── grouped_projection[]
├── procedure
│   ├── precheck_steps[]
│   ├── implementation_steps[]
│   ├── validation_steps[]
│   └── rollback_steps[]
└── post_execution
    └── execution_result
```

上下文字段允许暂时合并成一张 `IDENTITY_METADATA_CONTEXT` 卡片，但 lineage 会保存已识别的分类，为后续拆分预留稳定接口：

- `IDENTITY`：ticket_id、title、original_system、create_time；
- `SERVICE_SCOPE`：cloud_service、service、micro_service、affected_service；
- `CHANGE_CONTEXT`：change_scene、change_notes、special_change_type、change guide；
- `RISK_IMPACT`：severity、change_level、customer_sensed、affected_customer、risk_level、impact_risk_level；
- `EXECUTION_CONTEXT`：region、时间窗口、执行人、配合人和审核人；
- `GOVERNANCE_CONTEXT`：审批、高风险检查、授权和通知。

## 抽取流程

```text
JSON 原文
  -> 精确 Key 映射 / 结构指纹识别
  -> TaskRecord 双视图逐项对账
  -> 按完整对象边界生成抽取单元
  -> DeepSeek 对每个单元最多生成一张候选卡片
  -> 证据原文定位与 lineage 保存
  -> 结构覆盖与语义映射状态检查
  -> 人工审核
```

- TaskRecord 对账完全一致时，`action_list` 是唯一抽取源；分组视图只计入已对账的 provenance。
- 数量相等但内容未能逐项对齐时，两份视图都会保留，相关卡片停留在 `DRAFT`。
- ProcedureStep 始终保持源数组顺序，不依赖自动猜测的 sequence 字段重排。
- 单个 ProcedureStep 不会被从对象内部硬截断；超长组以完整 step 为最小单位分卡。
- 每张 Procedure 卡保存 `procedure_group`、`step_start_index`、`step_end_index`、`total_steps_in_group`，便于 RAG 按源顺序恢复完整流程。
- `EXECUTION_RESULT` 属于 `post_execution`：历史已完成工单可以参与经验检索和失败分析，但生成新方案时会被排除，避免结果泄漏。

## 完整性与语义状态

上传完成后的返回值包含类似报告：

```json
{
  "extraction_strategy": "change_order_shape_v2",
  "extraction_report": {
    "case_id": "change-order:<source-sha256>",
    "change_order": {
      "semantic_mapping_status": "CONFIRMED",
      "safe_for_internal_index": true,
      "safe_for_external_publish": false,
      "publish_scope": "INTERNAL_ONLY",
      "task_record": {},
      "procedure": {},
      "post_execution": {},
      "coverage": {
        "structural_coverage_ratio": 1.0,
        "structural_node_coverage_ratio": 1.0
      }
    }
  }
}
```

`structural_coverage_ratio=1` 只表示所有结构节点都已进入抽取单元、API envelope 或重复投影对账范围，不代表业务语义 100% 正确。语义映射另行标记：

- `CONFIRMED`：真实 Key 与领域角色已经确认；
- `HEURISTIC`：只通过结构指纹推断；
- `UNKNOWN`：当前信息不足，不能赋予具体业务角色；
- `CONFLICT`：结构、数量或映射相互冲突，阻断内部入库。

覆盖账本还统计进入抽取单元、已对账重复投影、API envelope、未覆盖路径、`NULL/EMPTY/VALUE` 观测，以及 TaskRecord 匹配和四组 Procedure 步骤数量。

## 内部入库与外部发布是两道门

`safe_for_internal_index` 决定候选卡是否能继续走内部“抽取—审核—入库—检索—生成”流程。TaskRecord 对账冲突、关键结构缺失或存在未覆盖业务节点时，该值为 `false`，候选卡保持 `DRAFT`。

`safe_for_external_publish=false` 和 `publish_scope=INTERNAL_ONLY` 只是数据边界提示，不阻断内部 Demo。当前版本不会因为禁止外发而停止知识抽取、人工审核或本地可信检索。

## 数据边界与生产化

真实工单只能在公司批准的模型端点和部署环境内处理。JSON Pointer、字段名、步骤原文与 ExecutionResult 都可能含内部信息；若要将 extraction report 带出内网，必须先按公司规则脱敏。

进入生产前仍建议补充版本化 Schema、稳定任务 ID、关键字段清单、跨样本 `MISSING/null/empty/value` 基线和 Schema 漂移策略。任何结构报告都不是知识正确性的自动证明，最终候选仍需人工审核。
