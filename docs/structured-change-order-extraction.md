# 结构化变更单知识抽取

平台对普通文档继续使用通用文本分片；对符合已验证结构指纹的 JSON 变更单，则先执行确定性结构分析，再调用模型。模型不负责猜测 Schema，也不能决定是否跳过源数据。

## 当前识别基线

第一版 Adapter 对应五份完全脱敏样本中共同出现的结构：

- 根节点为 object；
- 一份非空的 `array[TaskRecord]`，其中 TaskRecord 固定为 13 个字段；
- 一份包含 1 至 3 个 TaskRecord 数组的分组视图；
- 一份包含 4 个数组的 Procedure 容器，每个非空数组元素均为相同的 20-field ProcedureStep；
- 一份独立的 15-field ExecutionResult，约 14 个 scalar 与 1 个 array。

Adapter 不依赖脱敏扫描产生的 `Pxxxx`，也不从字段名或字段值猜测真实业务 Key。与变更单指纹无关的普通 JSON 会自动降级到原有通用抽取流程；若输入已经同时命中多类变更单特征，但候选不唯一或关键容器缺失，则会在调用模型前阻断，防止结构漂移被当成普通文本悄悄处理。

普通文本仍限制为 120,000 字符和 20 个分片。只有通过该结构指纹预检的变更单 JSON 才能使用默认 500,000 字符、40 个结构单元和 12,000 字符单元大小；模型调用总预算仍保持 60 次。未知大 JSON 会在调用模型前被拒绝，避免把扩大容量变成费用或资源耗尽入口。

## 抽取语义

```text
JSON 原文
  -> 结构指纹识别
  -> TaskRecord 双视图逐项对账
  -> 按对象/数组边界生成抽取单元
  -> DeepSeek 对每个单元最多生成一张候选卡片
  -> 证据原文定位
  -> 覆盖率与阻断项检查
  -> 人工审核
```

- TaskRecord 对账完全一致时，扁平数组作为唯一抽取主视图；分组数组只计入“已对账重复投影”，不再重复调用模型。
- TaskRecord 数量相等但内容未能逐项对齐时，两份视图都会保留，生成卡片强制处于 `DRAFT`，不能批准。
- Procedure 第 2、4 组分别赋予 `ROLLBACK_STEPS`、`VALIDATION_STEPS` 角色。
- Procedure 第 1、3 组保持 `PROCEDURE_GROUP_A`、`PROCEDURE_GROUP_C`，不能只根据位置擅自命名。
- ProcedureStep 始终保持源数组顺序，不使用疑似 sequence 字段重新排序。
- ExecutionResult 独立抽取为实际执行结果，不反向改写计划步骤。

每张结构化工单卡片还会保存 lineage：同一 `case_id`、结构角色、JSON Pointer、源顺序以及覆盖的源对象路径。这样卡片可以分开审核和检索，同时仍能恢复其在整份变更中的上下文。

## 完整性报告

上传完成后的返回值包含：

```json
{
  "extraction_strategy": "change_order_shape_v1",
  "extraction_report": {
    "case_id": "change-order:<source-sha256>",
    "change_order": {
      "safe_to_publish": true,
      "task_record": {},
      "procedure": {},
      "execution_result": {},
      "coverage": {}
    }
  }
}
```

覆盖账本分别统计：

- 进入抽取单元的节点和 scalar；
- 已确认是重复投影、因而不重复抽取的节点和 scalar；
- 未覆盖路径；
- `NULL`、`EMPTY`、`VALUE` 的观测数量；
- TaskRecord 匹配、未匹配和指纹碰撞数量；
- 四个 Procedure 组的源槽位和步骤数量。

单份文件不能判断某字段是 `MISSING` 还是该 Schema 本来就没有它，因此报告会将 `MISSING` 标记为未知。生产 Adapter 应在内网使用版本化真实 Key 映射或多样本 Schema 基线补足这一项。

## 发布门禁

结构匹配本身不代表内容正确。以下任一情况都会使 `safe_to_publish=false`，对应卡片加入 `阻断：...` 质量问题并停留在 `DRAFT`：

- TaskRecord 双视图未可靠对账；
- 有 JSON 节点未进入抽取或重复视图对账范围；
- 未来版本增加的关键结构校验失败。

人工审核不能越过这类阻断项。修复真实 Key 映射或 Schema 版本配置后，应重新导入和抽取。

## 仍需真实内网信息

结构指纹让平台现在可以安全处理这五份样本所代表的稳定形态，但它不是最终生产映射。接入真实工单系统前仍需要在内网补充：

- 版本化的真实 Key 到领域字段映射；
- 稳定任务 ID，或经过验证且无碰撞的任务组合指纹；
- 第 1、3 个 Procedure 组的真实语义；
- 关键字段清单与跨样本 `MISSING/null/empty/value` 统计；
- Schema 版本漂移处理规则。

在这些信息确认前，平台只把结果作为待审核知识候选，不把模型输出视为完整性证明。

> 数据边界：结构报告中的 JSON Pointer 会保留来源 Key 名，模型调用也会收到对应结构单元的原文。真实工单只能使用公司批准的模型端点与部署环境；若要把报告带出内网，仍需先对 Key 名和路径做稳定匿名化。
