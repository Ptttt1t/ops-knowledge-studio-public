# Current Handoff

## Current Objective

收口 `semantic_cluster_v3` 内网代码审查中确认的治理一致性问题：手动删卡必须失效相关知识簇与 canonical，失效扫描不得受 UI 500 条列表上限影响。

## Completed

- `delete_card` 现在使用 `BEGIN IMMEDIATE`，删除前查找该卡作为成员或 canonical 所属的待审核、暂缓或已接受知识簇。
- 受影响知识簇在同一 SQLite 事务内变为 `STALE`，簇内 `SIMILAR_TO` 和指向派生 canonical 的 `MERGED_INTO/INSTANCE_OF` 投影被删除。
- 已接受簇的派生 canonical 变为 `STALE + SKIPPED`、退出 retrieval/planning RAG，相关长记忆映射进入回收队列。
- 其他来源成员只有在不再受其他 accepted pair/知识簇约束时恢复 `NEW + INDEXED`；知识簇成员行保留作为历史审计。
- `stale_semantic_merge_clusters` 改为事务内批量读取全部 `PENDING_REVIEW/DEFERRED` 簇及成员，不再调用最多 500 条的 UI 列表方法。
- 知识簇 upsert 增加 `BEGIN IMMEDIATE`，两个连接并发刷新同一 `cluster_key` 时串行化读-改-写。
- 知识簇和历史 pair 列表增加 ID 最终排序键，同状态、同分数、同时间时结果稳定。
- 项目版本提升为 1.6.1，长期上下文、专项文档和 README 已与代码一致。

## Files Changed

- `knowledge_platform/store.py`：删卡的簇/canonical 失效、来源恢复、长记忆回收、全量 stale 扫描、upsert 事务和稳定排序。
- `knowledge_platform/semantic_governance.py`：合并知识簇/历史 pair 队列的稳定 ID tie-breaker。
- `tests/test_semantic_governance.py`：三成员簇删卡、已接受簇删卡、501 条 stale、稳定排序和并发 upsert 回归。
- `README.md`, `docs/semantic-governance.md`, `docs/CODEX_CONTEXT.md`：删卡与全量失效规则。
- `pyproject.toml`：版本 1.6.1。

## Validation

- `python -m unittest tests.test_semantic_governance tests.test_demo_mode_and_card_delete -v`：25 tests，全部通过。
- `python -m unittest discover -s tests -t .`：177 tests，全部通过。
- `python -m py_compile knowledge_platform/store.py knowledge_platform/semantic_governance.py tests/test_semantic_governance.py`：通过。
- `git diff --check`：通过；仅有 Windows LF/CRLF 提示，无 whitespace error。
- 真实内网知识库：外网未运行、未声称验证真实卡片数据。

## Known Issues

- 已接受 canonical 当前不重新作为来源参与聚类；只新增一个高度相似案例时，尚不能单独提交“追加到既有 canonical”的人工审核。
- 知识簇审核页尚不支持接受前移除单个成员或手工拆簇。
- README 中相似治理截图仍来自 pair 版页面，尚未重拍。

## Work In Progress

None.

## Recommended Next Step

内网更新后用合成或可删除的测试卡做一次手动删除：先确认三个不同案例的卡形成知识簇，再删除一张非代表卡，核对簇变为 `STALE`、图上无遗留 `SIMILAR_TO`、派生 canonical 已下架、其他案例卡恢复检索。之后再执行“立即扫描”确认可以按当前内容重新形成候选。

## Last Updated

- Date: 2026-09-01 (Asia/Shanghai)
- Current branch: `main`
- Target release: 1.6.1 to `github/main`
- Containing commit: use `git log -1 --oneline -- docs/CODEX_HANDOFF.md`; this file does not store its own commit SHA.
