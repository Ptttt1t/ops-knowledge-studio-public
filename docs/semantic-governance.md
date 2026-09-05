# 跨案例相似知识簇治理

## 目标与边界

该模块持续发现“不同案例包中语义相同或共享通用核心”的已批准 Procedure 卡，并把最终治理判断交给人工知识负责人。系统不会自动合并，不调用 LLM 生成候选，不把卡片正文发送到外部服务，也不把历史实例参数直接提升为通用规则。

新扫描候选严格限定为：

- `status = APPROVED`；
- `card_type = PROCEDURE_STEP`；
- `publish_status = INDEXED`；
- `retrieval_enabled = true`；
- 已有稳定 `semantic_fingerprint` 和 `card_lineage.case_id`；
- 每个知识簇中每个 `case_id` 最多一张卡；
- `procedure_group` 相同且结构化对象/action 不冲突。

同一案例包内的卡片不会进入相似合并队列。即使正文高度相似，也只说明案例内部可能存在建模重叠，不代表可以丢失步骤顺序、source pointer 或阶段上下文。扫描、候选写入和最终确认事务都会检查 `case_id`，旧的同案例候选会在下次扫描时变为 `STALE`。

JSON Pointer 只定位各自案例中的字段。不同案例即使都使用 `/data/sop_change_step/check_before_change/0`，也不能据此当成同一来源并跳过相似扫描；案例边界以 `case_id` 判断。

## 从 pair 到 complete-link 知识簇

扫描器仍在本地计算两卡相似度，但 pair 只是聚类证据，不再是主要治理对象。达到阈值的同阶段、跨案例 pair 使用保守的 complete-link 规则形成知识簇：新成员必须与簇内每一个现有成员都达到阈值；不能仅凭 `A≈B`、`B≈C` 推导 `A≈C`。

同一批反复出现的“前检：变更分析”因此表现为一个 N 成员候选簇，而不是 N×(N-1)/2 个重叠提案。簇保存：

- 成员卡和案例 ID；
- medoid/代表卡；
- 最低、平均、最高两两相似度；
- 可解释的共享语义锚点；
- 标题、操作、验证、回退、影响和对象差异；
- 推荐治理方式及人工决策。

共享锚点只来自完整业务短语、标题、对象、action、关键词和参数名，不展示无语义的中文字符滑窗。

## 阶段与关系约束

- `PRECHECK`、`IMPLEMENTATION`、`VALIDATION`、`ROLLBACK` 之间禁止合并；
- 跨阶段达到阈值时只建立 `CROSS_PHASE_SIMILAR`，两张卡继续独立检索；
- 同案例 pair 不建立合并候选，也不建立治理型 `SIMILAR_TO`；
- 待审核知识簇只在代表卡与其他成员之间投影 `SIMILAR_TO`；
- 历史 pair 提案仍可读取和完成，但新扫描只生成知识簇。

## 两种人工确认方式

### FULL_EQUIVALENT

用于整张卡业务语义等价的知识簇。确认后：

- 新建 `dedup_status=CANONICAL`、`publish_status=INDEXED` 的 canonical 卡；
- 来源卡保留 `APPROVED` 和原始证据，但变为 `MERGED + MERGED`；
- 来源卡关闭 retrieval 和 planning RAG；
- 建立 `MERGED_INTO`；
- 本地检索、向量索引和外部长记忆只保留 canonical 投影。

### SHARED_CORE

用于成员共享通用框架，但各案例结论、参数或特殊限制不同的知识簇。确认后：

- 建立一张可检索的共享核心 canonical 卡；
- 来源卡保留为 `dedup_status=INSTANCE`、`publish_status=CASE_EVIDENCE`；
- 来源卡退出普通检索和 planning RAG，但证据、案例归属和内容不删除；
- 建立 `INSTANCE_OF`，图谱可沿 canonical 回到所有案例证据。

“前检：变更分析”通常优先采用共享核心。标题相同只是候选召回信号，不是完整等价证明。人工仍应检查差异区域，并在提交前编辑 canonical 草案。

## 接受前窄化成员

三张及以上卡片的待审核或暂缓知识簇允许逐张移除不属于本次共享语义的成员。该动作不是删除来源卡，也不会直接接受剩余成员：

1. 审核人必须填写判断依据；
2. 原候选变为 `SUPERSEDED`，原始成员快照继续作为不可变审核历史；
3. 系统在同一 SQLite 事务内为“被移除成员 × 每个保留成员”记录 procedure group、card/case/fingerprint 和算法版本绑定的人工排除；
4. 剩余成员重新计算代表卡、complete-link 分数、共享锚点、差异和 canonical 草案，形成新的 `PENDING_REVIEW` 候选；
5. 旧 `SIMILAR_TO` 投影被清理，只投影新候选的代表卡关系；所有来源卡仍保持原检索状态，直到新候选被接受。

后续扫描会跳过仍与人工排除记录完全匹配的 pair，因此不会把同一内容版本立即塞回原簇。任一 procedure group、card ID、case identity、fingerprint 或算法版本变化后，该排除不再代替新一轮判断。两成员簇不能继续移除成员，应直接驳回；当前页面也不支持一次把一个簇任意拆成多个分组。

## 状态与漂移

```text
PENDING_REVIEW -> ACCEPTED
               -> REJECTED
               -> DEFERRED -> ACCEPTED / REJECTED
               -> SUPERSEDED -> 新的窄化 PENDING_REVIEW
成员、fingerprint、case identity、阶段或算法变化 -> STALE
```

知识簇 identity 绑定算法版本、成员 card ID、case ID 和 semantic fingerprint。相同内容的已驳回簇不会重复推送；成员或内容变化会产生新的簇 identity。最终事务会重新验证来源卡仍是 `APPROVED + INDEXED + retrieval_enabled`，并再次验证 case 唯一性和 phase 一致性。

## 案例重建与手动删卡一致性

“重建当前案例”会精确失效包含该案例成员的待审核、暂缓或已接受知识簇：

1. 知识簇标为 `STALE`；
2. 删除相关 `SIMILAR_TO`、`MERGED_INTO` 或 `INSTANCE_OF` 当前投影；
3. 派生 canonical 变为 `STALE + SKIPPED` 并退出检索；
4. 仍存在且没有参加其他有效治理结果的其他案例成员恢复为 `NEW + INDEXED`；
5. 回收受影响的长期记忆投影并清空过期扫描摘要；
6. 重建完成后等待下一轮扫描以新 Card ID 和 fingerprint 重新形成候选。

清理始终按 case scope 执行，不影响无关案例。

手动删除单张卡片时也使用同类的事务边界：

1. 删除前查找该卡作为成员或 canonical 所属的待审核、暂缓或已接受知识簇；
2. 簇标为 `STALE`，并删除该簇成员之间的全部 `SIMILAR_TO`；
3. 已接受簇的 canonical 变为 `STALE + SKIPPED`，退出本地检索和外部长记忆投影；
4. 其他成员只有在没有参与其他 accepted pair/知识簇时才恢复为 `NEW + INDEXED`；
5. `semantic_merge_cluster_members` 保留历史成员 identity 作为审计记录，不会伪造为“从未加入该簇”。

面向页面的列表查询最多返回 500 条，但扫描器的过期失效步骤直接遍历全部 `PENDING_REVIEW/DEFERRED` 簇，不受 UI 上限影响。

## 配置

```dotenv
SEMANTIC_GOVERNANCE_ENABLED=true
SEMANTIC_SCAN_INTERVAL_SECONDS=3600
SEMANTIC_PROPOSAL_MIN_SCORE=0.68
SEMANTIC_SCAN_MAX_CARDS=500
SEMANTIC_SCAN_MAX_PROPOSALS=100
```

`SEMANTIC_PROPOSAL_MIN_SCORE` 现在是 complete-link 每条成员边的最低门槛。`DEMO_MODE=true` 且未显式配置时默认开启；生产模式默认关闭，但可显式启用。

## API

为兼容现有页面和客户端，API 路径继续使用 `semantic-merge-proposals`，返回对象通过 `proposal_type=CLUSTER|PAIR` 区分：

- `GET /api/semantic-governance`：开关、worker、簇计数、策略和最近扫描；
- `GET /api/semantic-merge-proposals?status=PENDING_REVIEW&limit=100`：知识簇和历史 pair 队列；
- `GET /api/semantic-merge-proposals/{id}`：单个知识簇或历史 pair；
- `POST /api/semantic-merge-proposals/scan`：立即运行一次增量扫描；
- `POST /api/semantic-merge-proposals/{id}/decision`：人工决策。

移除单个成员并生成窄化候选：

```json
{
  "decision": "REMOVE_MEMBER",
  "member_card_id": 103,
  "comment": "该案例的检查目标不同，不属于本次共享核心"
}
```

该操作仅适用于至少三成员的 `PENDING_REVIEW/DEFERRED` 知识簇，响应返回新的待审核簇，并包含 `superseded_cluster_id` 与 `excluded_card_id`。

共享核心确认示例：

```json
{
  "decision": "ACCEPT",
  "merge_mode": "SHARED_CORE",
  "comment": "抽取通用前检框架，保留各案例结论作为证据",
  "merged_card": {
    "title": "前检：通用变更分析",
    "summary": "变更前完成容量、版本、依赖、监控、风险和备份检查。",
    "generalized_operation": "完成适用于目标变更的分析，不适用项必须记录依据。",
    "applicable_phases": ["PRECHECK"]
  }
}
```

`merge_mode` 支持 `FULL_EQUIVALENT` 和 `SHARED_CORE`。旧客户端不传该字段时保持历史兼容，按完整合并处理。
