# Current Handoff

## Current Objective

完成 `semantic_cluster_v3` 的审核前成员窄化：知识负责人可从三成员以上候选簇逐张移除误入成员，同时保留原始审核历史，并避免后续扫描把同一内容版本立即塞回原簇。

## Completed

- 相似知识审核页在三成员以上的 `PENDING_REVIEW/DEFERRED` 簇上提供“从本次簇移除”。人工意见必填；两成员簇必须直接接受、驳回或暂缓。
- 成员移除使用单个 `BEGIN IMMEDIATE` 事务：原簇变为 `SUPERSEDED`，原成员快照保留，剩余成员重新计算代表卡、complete-link 分数、共享锚点、差异和 canonical 草案，并形成新的 `PENDING_REVIEW` 簇。
- 新增 `semantic_merge_member_exclusions` 审计表。每次窄化为“被移除成员 × 每个保留成员”记录算法版本、procedure group、两端 card/case/fingerprint、审核人和理由。
- 扫描器只跳过仍与上述 identity 完全匹配的 pair，并报告 `human_exclusion_pair_skipped_count`；任一绑定值变化后重新评估。
- 原簇与新簇的 `SIMILAR_TO` 投影在事务内切换。成员窄化本身不改变任何来源卡的检索状态，只有最终接受才建立 canonical 并让相应来源退出普通检索。
- API 复用 `POST /api/semantic-merge-proposals/{id}/decision`，新增 `REMOVE_MEMBER + member_card_id`；治理参数错误稳定返回 HTTP 400。
- 页面可筛选 `SUPERSEDED` 历史簇。项目版本提升为 1.6.2，共享上下文、设计决策、专项文档和 README 已同步。

## Files Changed

- `knowledge_platform/store.py`：schema/migration、pair 排除读取、成员窄化原子事务、审核日志与新簇投影。
- `knowledge_platform/semantic_governance.py`：可复用知识簇 spec、扫描排除、成员窄化服务和 `REMOVE_MEMBER` 决策。
- `knowledge_platform/web.py`：成员 ID 入参与治理错误的 HTTP 400 映射。
- `knowledge_platform/static/app.js`, `knowledge_platform/static/index.html`：成员移除交互与 `SUPERSEDED` 状态筛选。
- `tests/test_semantic_governance.py`：窄化、重扫、接受、版本变化、错误门禁和 HTTP 回归。
- `README.md`, `docs/semantic-governance.md`, `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：成员窄化的状态、identity 与审计边界。
- `pyproject.toml`：版本 1.6.2。

## Validation

- `D:\Anaconda\envs\zte\python.exe -m unittest tests.test_semantic_governance tests.test_demo_mode_and_card_delete -v`：29 tests，全部通过。
- `D:\Anaconda\envs\zte\python.exe -m unittest discover -s tests -t .`：181 tests，全部通过。
- `D:\Anaconda\envs\zte\python.exe -m py_compile knowledge_platform/store.py knowledge_platform/semantic_governance.py knowledge_platform/web.py tests/test_semantic_governance.py`：通过。
- `D:\Anaconda\envs\zte\python.exe -m compileall -q harness knowledge_platform tests`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过；仅有 Windows LF/CRLF 提示，无 whitespace error。
- 真实内网知识库：外网未运行、未声称验证真实卡片数据。

## Known Issues

- 已接受 canonical 当前不重新作为来源参与聚类；只新增一个高度相似案例时，尚不能单独提交“追加到既有 canonical”的人工审核。
- 审核页可以逐张移除成员，但尚不能一次把一个候选簇手工拆成多个独立分组。
- README 中相似治理截图仍来自 pair 版页面，尚未重拍。

## Work In Progress

None.

## Recommended Next Step

内网人工更新后用可删除测试卡构造四案例候选簇，在页面移除一张成员并填写理由，核对原簇为 `SUPERSEDED`、新簇保留三成员、旧关系已替换、被移除卡仍可检索；再次立即扫描后确认相同版本不会回到新簇。下一项外网开发可优先设计“单个新案例追加审核到既有 canonical”，但不得形成 canonical 链或改写既有接受决策历史。

## Last Updated

- Date: 2026-09-02 (Asia/Shanghai)
- Current branch: `main`
- Target release: 1.6.2 to `github/main`
- Containing commit: use `git log -1 --oneline -- docs/CODEX_HANDOFF.md`; this file does not store its own commit SHA.
