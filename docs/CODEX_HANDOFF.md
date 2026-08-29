# Current Handoff

## Current Objective

将相似知识治理从同阶段两两提案升级为 case-aware 跨案例知识簇，禁止同一案例包内卡片进入合并队列，并支持把高频 Procedure 整理为完整 canonical 或共享核心。

## Completed

- 新扫描只读取带 `card_lineage.case_id` 的 `APPROVED + INDEXED + retrieval_enabled` Procedure 来源卡；同案例 pair 在相似度计算前跳过。
- 扫描、历史 pair 写入和最终确认事务均校验 case identity；旧同案例/旧算法候选在下次扫描时自动 `STALE`。
- 新增 `semantic_merge_clusters` / `semantic_merge_cluster_members`，每个知识簇每个案例最多一张卡。
- pair 相似度现在只是聚类证据；采用 complete-link，成员必须两两满足阈值，不用连通分量传播相似。
- 多案例重复 Procedure 形成一个 N 成员知识簇，保存代表卡、最低/平均/最高分、共享锚点和字段差异；历史 pair API/数据保持兼容读取和决策。
- 人工支持 `FULL_EQUIVALENT` 与 `SHARED_CORE`：完整合并建立 `MERGED_INTO`；共享核心把来源卡保留为 `INSTANCE + CASE_EVIDENCE` 并建立 `INSTANCE_OF`。两种方式都关闭来源成员的普通 retrieval/planning RAG。
- 案例重建会精确失效包含该案例成员的知识簇、下架派生 canonical、删除关系并恢复仍有效的其他案例成员。
- Web 页面改为知识簇审核，展示成员案例、簇代表、分数区间、共享锚点和字段差异；可分别选择完整合并或共享核心。
- README、长期上下文、架构决策和专项文档已更新；项目版本提升为 1.6.0。

## Files Changed

- `knowledge_platform/semantic_governance.py`：case-aware 扫描、complete-link 聚类、簇提案、双治理模式和兼容层。
- `knowledge_platform/store.py`：知识簇表、成员 case 唯一约束、簇状态事务、canonical 创建及 case rebuild 一致性。
- `knowledge_platform/service.py`：案例重建日志和 report 增加失效知识簇计数。
- `knowledge_platform/web.py`：人工决策透传 `merge_mode`。
- `knowledge_platform/static/index.html`, `app.js`, `styles.css`：多成员知识簇审核 UI、差异区和图谱 `INSTANCE_OF` 展示。
- `tests/test_semantic_governance.py`, `tests/test_knowledge_graph.py`：同案例隔离、多案例聚簇、complete-link、共享核心、重建和图谱回归。
- `README.md`, `docs/semantic-governance.md`, `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：当前方案与长期决策。
- `pyproject.toml`：版本 1.6.0。

## Validation

- `python -m unittest tests.test_semantic_governance tests.test_knowledge_graph tests.test_demo_mode_and_card_delete -v`：23 tests，全部通过。
- `python -m unittest discover -s tests -t .`：172 tests，全部通过。
- `python -m py_compile knowledge_platform/store.py knowledge_platform/semantic_governance.py knowledge_platform/web.py`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过；仅有 Windows LF/CRLF 提示，无 whitespace error。
- 真实内网知识库：外网未运行、未声称验证真实卡片聚簇结果。

## Known Issues

- 已接受 canonical 当前不重新作为来源卡参与聚类，以避免 canonical-of-canonical 链。后续只新增一张高度相似案例卡时，需要等至少另一张新案例卡形成新簇；尚未实现“向既有 canonical 提交单成员扩展审核”。
- 知识簇页面支持查看全部成员和差异，但本轮没有实现审核前手工移除单个成员或拆分簇；遇到边界成员时应先 `REJECT/DEFER`，调整阈值或内容后重新扫描。
- README 中既有相似治理截图来自上一版 pair 页面，文字和当前代码已更新，但截图尚未重拍。

## Work In Progress

None.

## Recommended Next Step

在内网更新后先运行一次“立即扫描”，记录 `same_case_pair_skipped_count`、`pair_candidate_count` 和 `cluster_candidate_count`。重点人工检查：“前检：变更分析”是否形成一个跨案例簇；成员 `case_id` 是否全部唯一；最低相似度是否足够；应选择完整等价还是共享核心。确认共享核心后，在图谱中核对来源卡保留在各案例下、普通检索只返回 canonical、关系为 `INSTANCE_OF`。若高频新增场景需要逐例挂接，下一轮优先实现既有 canonical 扩展审核。

## Last Updated

- Date: 2026-08-29 (Asia/Shanghai)
- Current branch: `main`
- Target release: 1.6.0 to `github/main`
- Containing commit: use `git log -1 --oneline -- docs/CODEX_HANDOFF.md`; this file does not store its own commit SHA.
