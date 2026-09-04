# Current Handoff

## Current Objective

修复内网只读审查和外网合成复现已确认的两个问题：Procedure Action 候选被前置空列表遮蔽，以及真实 ChangeOrder 草案能够生成步骤级人工核验/更新字段。未确认语义的页面字段和 `change_plan` 兄弟字段继续保持待确认，不猜测映射。

## Completed

- `_procedure_actions()` 现在跳过空列表并继续检查 `action_unique_ids`、`actions` 等后续候选；非空列表和标量兼容行为保持不变。
- 合成 ProcedureStep fixture 用已观察到的 `result_confirm` 替换未在当前结构观察中出现的 `action_risk_level`，仍保持 20 字段结构；Builder 继续兼容可选的 `action_risk_level`。
- 将 `result_confirm`、`result_confirm_notes`、`update_user`、`update_time` 定义为草案非模型所有字段：
  - 新建 SchemaProfile 对这些字段使用 `empty`；
  - Profile 激活校验拒绝把它们配置为 `generated`、`input`、`not_executed` 或其他 policy；
  - 已有活动 Profile 无需修改数据库，生成、人工编辑和 Profile 展示时会应用同一有效 `empty` 策略；
  - 装配程序忽略模型或草案编辑提供的值，按 Profile 类型写入空值；
  - 最终校验逐阶段、逐步骤检查这些字段，并新增 `procedure_non_generated_fields_sanitized` 硬门禁；
  - 审核和导出都会按当前硬门禁重新验证，升级前已经批准但含非空回填字段的 revision 不能继续导出。
- 生成 Prompt 明确禁止模型生成步骤核验结论、人员和时间；Web 验证结果增加对应中文门禁说明。
- README、页面字段映射、真实草案说明、长期上下文和架构决策已同步；包版本更新为 1.6.3。

## Files Changed

- `knowledge_platform/change_order_cards.py`
- `knowledge_platform/change_drafts.py`
- `knowledge_platform/static/app.js`
- `tests/test_change_order_card_builder.py`
- `tests/test_real_change_drafts.py`
- `README.md`
- `docs/change-order-page-field-mapping.md`
- `docs/real-change-generation.md`
- `docs/CODEX_CONTEXT.md`
- `docs/DECISIONS.md`
- `docs/CODEX_HANDOFF.md`
- `pyproject.toml`

## Validation

- `D:\Anaconda\envs\zte\python.exe -m unittest tests.test_change_order_card_builder tests.test_real_change_drafts -v`：39 tests 全部通过。
- `D:\Anaconda\envs\zte\python.exe -m unittest discover -s tests -t .`：181 tests 全部通过，退出码 0。
- `D:\Anaconda\envs\zte\python.exe -m py_compile ...`、`python -m compileall -q harness knowledge_platform tests scripts`、`node --check knowledge_platform/static/app.js`、`git diff --check`：全部通过；仅有 Windows LF/CRLF 提示。
- 覆盖空 Action 候选回退、新 Profile 策略、旧活动 Profile 运行时收窄、模型值确定性置空、人工编辑置空、最终硬门禁和旧批准 revision 导出复核。
- 所有测试仅使用 synthetic fixture；未读取或固化真实内网 JSON、SchemaProfile、人员、地址、Prompt 或执行结果，也未调用真实执行工具。

## Known Issues

- `operate_verified`、`operate_rollback`、`impact_analysis` 是事前指导、事后回填还是混合内容，仍缺内网页面控件或同一步骤前后快照证据；当前不按关键词删除或重分类。
- CASE_CONTEXT 白名单与真实字段名/嵌套位置仍不完全一致。`change_plan[0]` 的非 `result` 字段尚未按计划约束、历史审计和展示信息完成业务分类，不直接加入知识正文或规划检索。
- `result_confirm` 的正常/异常枚举含义、步骤三个子页是否分别保存核验结果、处理记录来源和“4号提权方式”仍待内网人工确认。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组；README 相似治理截图仍待更新。

## Work In Progress

None. 已完成代码、全量验证、文档同步和最终 diff/敏感信息复核。

## Recommended Next Step

完成全量测试和发布后，由内网人工升级并使用可丢弃草案验证：活动 Profile 展示上述四字段为 `empty`，生成草案对应值为类型安全空值，人工审核与导出正常；不得执行真实变更。其余页面映射仍按脱敏结构信息逐项确认。

## Last Updated

- Date: 2026-09-05 (Asia/Shanghai)
- Current branch: `main`
- Package version: 1.6.3
- Containing commit: use `git log -1 --oneline -- docs/CODEX_HANDOFF.md`; this file does not store its own commit SHA.
