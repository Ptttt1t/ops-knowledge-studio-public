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

`change_tool_relate_action` 的 group name 和 group count 是动态业务数据。Adapter 要求它至少包含一个数组且至少一个数组非空，但不限制 group 数量上限；所有非空 group 中的记录必须使用一致的 13-field TaskRecord Schema，并与 `action_list` 通过稳定 JSON 序列化和 SHA-256 multiset reconciliation 完整对账。

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
  -> Adapter 确定性生成任务/步骤列表与逐源证据
  -> DeepSeek 生成标题、摘要、场景和风险叙述
  -> 多卡响应稳定合并，不静默截断
  -> Pointer/span/hash 证据矩阵与 lineage 保存
  -> 结构、内容覆盖与语义映射状态检查
  -> 人工审核
```

## ChangeCaseBundle 案例包

命中 `change_order_shape_v2` 后，平台不再只给每张卡写一个逻辑 `case_id`，而是持久化一级 `ChangeCaseBundle`：

- 一个来源文档和内容校验和对应一个 `change-order:<sha256>` 案例包；
- 包保存来源、抽取策略、更新时间和完整 extraction report；
- 子卡继续保持原子化，通过 lineage 保存 `unit_role`、`source_order`、JSON Pointer 和证据矩阵；
- 包状态由子卡实时聚合，可能为 `PENDING_REVIEW`、`PARTIAL`、`APPROVED`、`REJECTED` 或 `SUPERSEDED`；
- 旧数据库若已有 `change_order_shape_v2` lineage，初始化时会自动回填案例包，不改变原卡 ID 和审核状态。

Web 页面默认按案例包聚合显示结构化变更单。整包批准会先在同一 SQLite 事务中验证全部子卡，只有所有证据、覆盖、语义映射和来源哈希均通过后才统一更新状态；整包驳回同样统一提交。单卡审核接口继续保留，普通文档行为不变。对应只读接口为 `GET /api/knowledge-case-bundles` 和 `GET /api/knowledge-case-bundles/{case_id}`，整包审核为 `POST /api/knowledge-case-bundles/{case_id}/review`。

- TaskRecord 对账完全一致时，`action_list` 是唯一抽取源；分组视图只计入已对账的 provenance。
- 数量相等但内容未能逐项对齐时，两份视图都会保留，相关卡片停留在 `DRAFT`。
- ProcedureStep 始终保持源数组顺序，不依赖自动猜测的 sequence 字段重排。
- 单个 ProcedureStep 不会被从对象内部硬截断；超长组以完整 step 为最小单位分卡。
- 每张 Procedure 卡保存 `procedure_group`、`step_start_index`、`step_end_index`、`total_steps_in_group`，便于 RAG 按源顺序恢复完整流程。
- `TASKS_CANONICAL` 和四类 Procedure 的权威列表由 Adapter 逐条渲染，不允许模型删减、合并或重排；模型返回多张卡时只合并表达字段，同一结构单元最终仍保存一张卡。
- 每个输出项保存 `source_pointer`、绝对字符范围和源片段 SHA-256。审批时会重新读取原文复算哈希，并校验输出序号与来源记录一一对应。
- 质量规则按权威 `unit_role` 执行：前检卡不再因缺少回退字段而扣分，验证卡只强制验证内容，回退卡只强制回退内容。普通文档继续使用原有 `knowledge_type` 规则。
- `EXECUTION_RESULT` 属于 `post_execution`：历史已完成工单可以参与经验检索和失败分析，但生成新方案时会被排除，避免结果泄漏。

## 完整性与语义状态

上传完成后的返回值包含类似报告：

```json
{
  "extraction_strategy": "change_order_shape_v2",
  "extraction_report": {
    "case_id": "change-order:<source-sha256>",
    "content_coverage": {
      "status": "COMPLETE",
      "expected_units": 7,
      "generated_cards": 7,
      "expected_source_items": 27,
      "mapped_source_items": 27
    },
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

`content_coverage.status=COMPLETE` 才表示每个抽取单元都已落卡、每个 canonical TaskRecord/ProcedureStep 都建立了逐源映射。它仍不等于业务判断自动正确，但可以证明没有在抽取链路中静默丢记录。新版结构化卡的审批同时要求：内容覆盖完整、证据模式为 `STRUCTURED_JSON_POINTERS`、Pointer/span/hash 可复算且结构 blocker 为空。旧卡保留原审核规则并标记为未执行新版覆盖评估，避免兼容迁移伪造完整状态。

## 内部入库与外部发布是两道门

`safe_for_internal_index` 决定候选卡是否能继续走内部“抽取—审核—入库—检索—生成”流程。TaskRecord 对账冲突、关键结构缺失或存在未覆盖业务节点时，该值为 `false`，候选卡保持 `DRAFT`。

`safe_for_external_publish=false` 和 `publish_scope=INTERNAL_ONLY` 只是数据边界提示，不阻断内部 Demo。当前版本不会因为禁止外发而停止知识抽取、人工审核或本地可信检索。

## 数据边界与生产化

真实工单只能在公司批准的模型端点和部署环境内处理。JSON Pointer、字段名、步骤原文与 ExecutionResult 都可能含内部信息；若要将 extraction report 带出内网，必须先按公司规则脱敏。

进入生产前仍建议补充版本化 Schema、稳定任务 ID、关键字段清单、跨样本 `MISSING/null/empty/value` 基线和 Schema 漂移策略。任何结构报告都不是知识正确性的自动证明，最终候选仍需人工审核。
