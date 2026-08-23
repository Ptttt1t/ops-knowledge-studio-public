# 相似知识人工合并治理

## 目标与边界

该模块持续发现“语义可能相同但名称、阶段或表述不同”的已批准 Procedure 卡，并把判断交给人工知识负责人。系统不会自动合并，不使用 LLM 生成候选，不把卡片正文发送到外部服务，也不把历史实例参数直接提升为通用规则。

当前候选范围严格限定为：

- `status = APPROVED`；
- `card_type = PROCEDURE_STEP`；
- `publish_status = INDEXED`；
- `retrieval_enabled = true`；
- 已有稳定 `semantic_fingerprint`；
- 不属于同一 `source_procedure_pointer` 的 Parent/Child lineage。

后台扫描受 `SEMANTIC_SCAN_MAX_CARDS` 与 `SEMANTIC_SCAN_MAX_PROPOSALS` 限制。算法先清理 phase 标题前缀和 rich text，再比较 generalized operation、validation、rollback、对象与 action 锚点；实例参数不参与通用身份。非 fingerprint 完全相同的候选至少需要两个共享的非泛化核心锚点。分数只负责进入人工队列，不是自动合并结论。

## 状态与图谱

候选状态流：

```text
PENDING_REVIEW -> ACCEPTED
               -> REJECTED
               -> DEFERRED -> ACCEPTED / REJECTED
内容或来源状态变化 -> STALE
```

- 待确认时建立 `SIMILAR_TO`，在图谱中显示为虚线候选关系。
- 确认合并后删除 `SIMILAR_TO`，新建一张独立、单调递增 K ID 的 canonical 卡，并建立两条 `MERGED_INTO`。
- 来源卡仍保留 `APPROVED` 和原始证据，但变为 `dedup_status=MERGED`、`publish_status=MERGED`、`retrieval_enabled=false`，因此不会与 canonical 卡同时进入本地检索、向量索引或外部长记忆。
- canonical 卡为 `dedup_status=CANONICAL`、`publish_status=INDEXED`、`retrieval_enabled=true`。
- 排除决策绑定 source card ID、fingerprint 和算法版本。相同内容不重复推送；内容变化会产生新的 pair identity，旧候选变为 `STALE`。

人工编辑 canonical 草案只允许业务字段白名单。标题和 generalized operation 必填；检测到 raw JSON 时拒绝合并。来源卡 fingerprint 或发布状态在确认前发生漂移时，事务失败并要求重新扫描。

## 案例重建一致性

“重建当前案例”会在删除旧卡前查找所有直接或间接依赖这些卡片的已接受合并：

1. 把相关待确认、暂缓和已接受候选标为 `STALE`；
2. 删除对应 `SIMILAR_TO` 与 `MERGED_INTO` 当前关系；
3. 将受影响 canonical 卡改为 `STALE + SKIPPED` 并关闭检索与 planning RAG；
4. 若另一来源卡仍存在且没有参加其他有效合并，恢复为 `NEW + INDEXED`；
5. 回收受影响 canonical 的外部长记忆，随后尝试重新同步恢复的来源卡；
6. 清空已经过时的最近扫描摘要，等待下次增量扫描。

这种处理支持 canonical 再参与后续合并的链式失效，同时保持重建范围只影响依赖当前案例的知识，不清空其他案例。

## 配置

```dotenv
SEMANTIC_GOVERNANCE_ENABLED=false
SEMANTIC_SCAN_INTERVAL_SECONDS=3600
SEMANTIC_PROPOSAL_MIN_SCORE=0.68
SEMANTIC_SCAN_MAX_CARDS=500
SEMANTIC_SCAN_MAX_PROPOSALS=100
```

默认关闭。启用后 Web Server 会启动后台 daemon；也可在页面点击“立即扫描”。扫描间隔最小值与候选上限由配置加载器校验。

## API

- `GET /api/semantic-governance`：开关、worker、计数、策略和最近扫描。
- `GET /api/semantic-merge-proposals?status=PENDING_REVIEW&limit=100`：候选队列。
- `GET /api/semantic-merge-proposals/{proposal_id}`：单个候选及两侧卡片快照。
- `POST /api/semantic-merge-proposals/scan`：立即运行一次增量扫描。
- `POST /api/semantic-merge-proposals/{proposal_id}/decision`：人工决策。

决策请求示例：

```json
{
  "decision": "ACCEPT",
  "comment": "确认属于同一通用健康检查",
  "merged_card": {
    "title": "通用业务健康检查",
    "summary": "检查告警、探针和关键业务指标。",
    "generalized_operation": "检查目标系统的活动告警、服务探针和关键业务指标。",
    "applicable_phases": ["PRECHECK", "VALIDATION"]
  }
}
```

`decision` 也支持 `REJECT` 和 `DEFER`；它们不会创建 canonical 卡。
