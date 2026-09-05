# Current Handoff

## Current Objective

按第一性原理审视近期相似治理和真实草案实现，优先删除已确认无用或错误的逻辑。已修复跨案例相同步骤路径被误判为同源而漏召回的问题，保留证据、人工审核、case/phase 隔离和草案不可执行边界。

## Completed

- 删除扫描器 `_same_lineage()` 及其排除分支：裸 JSON Pointer 只在案例内定位，不能把不同案例的相同路径当作同一来源。案例隔离继续由 `_same_case()` 和 Store 事务执行。
- 删除无调用的旧 pair 候选构造 `proposed_canonical_merge()` 和 `_pair_key()`。历史 pair 的读取、审核及数据库记录仍保留兼容。
- 删除 `update_draft()` 的重复 Profile 转换；共享 `_assemble()` 仍强制应用步骤非模型字段的 `empty` 策略，生成 Prompt 和 Profile 展示也继续使用有效策略。
- 全量测试暴露草案保存的两次事务提交竞态。删除无业务用途的瞬时 `DRAFT` 写入和第二次更新，revision 内容与最终 `READY_FOR_REVIEW` / `BLOCKED` 状态改为同一事务提交。
- 语义治理合成 fixture 改用标准 ProcedureStep JSON Pointer，不再把案例名拼进路径；加强现有扫描测试，明确覆盖不同 case、相同 pointer 的候选生成和幂等重扫。
- 生产代码净删 94 行，未增加依赖、配置、数据迁移、门禁或自动化。版本更新为 1.6.4。
- 上轮 1.6.3 的步骤字段约束继续生效：`result_confirm`、`result_confirm_notes`、`update_user`、`update_time` 在草案中保持类型安全空值；审核/导出仍复核当前硬门禁。

## Files Changed

- `knowledge_platform/semantic_governance.py`
- `knowledge_platform/change_drafts.py`
- `tests/test_semantic_governance.py`
- `tests/test_real_change_drafts.py`
- `README.md`, `pyproject.toml`
- `docs/semantic-governance.md`
- `docs/real-change-generation.md`
- `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`, `docs/CODEX_HANDOFF.md`

## Validation

- 修复前，用标准路径的合成 fixture 运行扫描回归：预期 1 个候选，实际 0；同案例排除测试通过。
- 修复后，`D:\Anaconda\envs\zte\python.exe -m unittest tests.test_semantic_governance tests.test_real_change_drafts -v`：42 tests 全部通过。
- 首次全量 181 tests 有 1 项 HTTP 草案测试失败，实际读到 `DRAFT`。新增独立连接观察每次提交的回归测试：加载修改前的 `save_revision` 时稳定复现 `DRAFT -> READY_FOR_REVIEW` 两次可见提交；删除中间写入后该测试与 HTTP 测试均通过。
- 最终 `D:\Anaconda\envs\zte\python.exe -m unittest discover -s tests -t .`：182 tests 全部通过，98.933 秒，退出码 0。
- `python -m compileall -q harness knowledge_platform tests scripts`、`node --check knowledge_platform/static/app.js`、`git diff --check` 已通过。
- 验证仅使用合成 fixture 和测试临时数据库；未读取真实业务数据库，未调用外部模型或真实执行工具。

## Known Issues

- `operate_verified`、`operate_rollback`、`impact_analysis` 是事前指导、事后回填还是混合内容，仍缺内网页面控件或同一步骤前后快照证据；当前不按关键词删除或重分类。
- CASE_CONTEXT 白名单与真实字段名/嵌套位置仍不完全一致。`change_plan[0]` 的非 `result` 字段尚未按计划约束、历史审计和展示信息完成业务分类，不直接加入知识正文或规划检索。
- `result_confirm` 的正常/异常枚举含义、步骤三个子页是否分别保存核验结果、处理记录来源和“4号提权方式”仍待内网人工确认。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组；README 相似治理截图仍待更新。

## Work In Progress

代码、全量测试、文档同步及完整 diff/敏感信息复核已完成，实现提交为 `8e1bd1a`。公开推送被自动审批拒绝，等待明确发布授权；`github/main` 仍为 `0d1a87bc5b23beb58d30ec4078deae1089ed4601`，尚未包含本轮修复。

## Recommended Next Step

内网由人工升级后重新执行相似知识扫描，确认跨案例、相同步骤路径的相似卡能形成待审核知识簇；无需重建案例或迁移数据库。候选数量可能增加，仍由人工审核。其余页面字段映射按脱敏结构证据逐项确认。

## Last Updated

- Date: 2026-09-05 (Asia/Shanghai)
- Current branch: `main`
- Package version: 1.6.4
- Containing commit: use `git log -1 --oneline -- docs/CODEX_HANDOFF.md`; this file does not store its own commit SHA.
