# Current Handoff

## Current Objective

以人工确认的真实 ChangeOrder 业务结构为准，退役早期自拟字段的五份 JSON 案例，并核对操作页面信息与当前字段映射的差距。本轮只完成过时样例清理和字段对照，不实现尚未确认的页面字段，也不增加真实变更执行能力。

## Completed

- 删除 `sample_data/realistic_change_orders/` 中五份旧 JSON 和说明，以及专用生成脚本、评估脚本、对应测试模块。这套数据只对齐外层路径和字段数量，不能作为真实字段语义或页面完整性的证据；全部可通过 Git 历史恢复。
- 保留 `tests/fixtures/change_order_card_builder/cases.json`、`tests/test_change_order_card_builder.py` 等已知业务字段的合成测试，以及字段基数/漂移测试。
- 保留独立的 `change_management` 模拟器、十个合成 Demo 案例及其文档导出脚本，不改变真实草案只生成/审核/导出的边界。
- 新增 `docs/change-order-page-field-mapping.md`，逐项区分代码已有映射、仅保存元数据、字段含义待确认和未见明确字段，避免把合成样例缺字段误报为原始真实 JSON 缺失。
- 记录人工确认的 1+1 check：执行人逐步操作，配合人逐步核验、截图和回填；异常时停止后续步骤，回退或其他处置均由人工决定。步骤内验证不改变其所属 phase。
- 记录顶层“影响分析”当前无人填写、暂不纳入要求；“处理记录”是申请至完成的人员/时间流程历史，不能替代步骤核验。
- README、构建说明、长期上下文和设计决策已同步。运行时代码、卡片模型和包版本未改，版本仍为 1.6.2。

## Files Changed

- 删除五份旧 JSON、它们的 README、`scripts/generate_realistic_change_orders.py`、`scripts/evaluate_realistic_change_orders.py`、`tests/test_realistic_change_order_samples.py`。
- 新增 `docs/change-order-page-field-mapping.md`。
- 更新 `README.md`、`docs/structured-change-order-extraction.md`、`docs/CODEX_CONTEXT.md`、`docs/DECISIONS.md` 和本交接文件。

## Validation

- `D:\Anaconda\envs\zte\python.exe -m unittest discover -s tests -t .`：179 tests 全部通过，退出码 0。比上一轮 181 少两项，仅因退役旧五案例专用测试，其他测试保留。
- `D:\Anaconda\envs\zte\python.exe -m compileall -q harness knowledge_platform tests scripts`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过；仅有 Windows LF/CRLF 提示。
- 使用合成值做内存内 Builder 诊断：`change_level`、`change_notes`、`special_change_type`、`change_guide`、`change_guides`、`authorization_reference`、`notification_status` 不进入当前 Context 字段白名单；`result_confirm_notes` 留在步骤 `source_metadata`，不进入整单 Outcome。该诊断用于说明缺口，没有修改这些行为。
- 仅使用合成测试与公开代码；未读取、发送或验证真实内网数据，也未调用真实模型/执行工具。

## Known Issues

- 不能证明页面与之前的完整真实 JSON 一一对应：当前只有代码映射和合成字段样例，没有在外网独立核验完整原始字段清单。
- 优先核对每步三个子页各自的涉及标记、正常/异常选择、核验说明、截图、人员和时间；当前未形成步骤级执行记录，`operate_verified` 仍直接作为 Procedure validation。
- CASE_CONTEXT 字段白名单与 Adapter 识别字段不完全一致；具体缺口、详情链接、范围层级、SOP 模板及处理记录见新对照文档。不得凭字段名猜测“4号提权方式”等未知语义。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组；README 相似治理截图仍待更新。

## Work In Progress

None. 本轮样例清理已完成；字段映射与步骤执行记录的后续实现尚未开始。

## Recommended Next Step

由内网人工依据字段对照核实源路径、类型、枚举含义和填写阶段，仅反馈允许的脱敏结构信息。优先确定逐步骤核验与整单结果/处理记录的不同来源，再用已确认字段和合成值补测试、调整 Builder；不得把真实 JSON、SchemaProfile、截图、账号、地址或执行结果带到外网，不建设真实执行或自动回退能力。

## Last Updated

- Date: 2026-09-04 (Asia/Shanghai)
- Current branch: `main`
- Package version: 1.6.2 (本轮无运行时代码变更)
- Containing commit: use `git log -1 --oneline -- docs/CODEX_HANDOFF.md`; this file does not store its own commit SHA.
