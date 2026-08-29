# Current Handoff

## Current Objective

消除真实草案中 Profile `type=null` 字段被误报为缺失的问题，并明确本次 `request.parameters` 相对参考案例历史参数的优先级，同时让第二次模型调用具备可核查的触发原因。

## Completed

- `_apply_field_policy` 现在区分字段缺席与合法 JSON `null`：`generated` 字段存在、Profile 类型为 `null` 且值为 `None` 时直接保留，不进入 `missing_fields`。
- 如果模型自己的 `missing_fields` 已包含同一路径，装配器会在确认“字段存在 + type=null + value=null”后移除该误报；nullable 字段缺席仍然阻断。
- string/number/object 等非 null 类型的 `None` 或空字符串仍按原门禁记为缺失；`context.change_scene.name_en` / `unique_id` 是 string，未输入时不会因 nullable 修复被放行。
- 生成 Prompt 明确 `request` 是本次事实源、`approved_cards` 只是模板和证据；本次输入值必须优先并带 `input_refs`，不得用历史 `instance_parameters` 补齐缺失参数。TaskRecord 动作/能力身份字段仍允许来自卡片。
- Web 参数区改名为“本次变更具体参数 JSON”，继续原样映射到 `request.parameters`。参数对象为空时，点击生成会二次确认，提醒模型没有新 IP、账号、端口、阈值或资源规格可用。
- generation diagnostics 新增 `protocol_repair_count` / `protocol_repairs`，记录触发第二次远程请求的 `JSON_PARSE` 或 `DRAFT_ASSEMBLY` 阶段、错误码和截断原因；不保存 Prompt 或响应正文。
- 已确认 missing 只影响 `_validate_assembled` 的最终硬门禁，不会从 `_assemble` 抛异常；此前两次模型请求不能归因于 null missing，下一轮应以新增诊断为准。

## Files Changed

- `knowledge_platform/change_drafts.py`：nullable missing 语义、Prompt 参数优先级和协议修复诊断。
- `knowledge_platform/static/index.html`, `knowledge_platform/static/app.js`：具体参数说明、空参数二次确认和单次 request 复用。
- `tests/test_real_change_drafts.py`：nullable present/absent、非 null 空值、Prompt 和两类 protocol repair 诊断回归。
- `README.md`, `docs/real-change-generation.md`：更新参数输入、nullable 和诊断使用说明。
- `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：记录稳定业务语义与设计边界。

## Validation

- `python -m unittest tests.test_real_change_drafts`：19 tests，全部通过。
- `python -m unittest discover -s tests -t .`：168 tests，全部通过。
- `python -m compileall -q harness knowledge_platform tests`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过（只有 Windows 工作区 LF/CRLF 提示，无 whitespace error）。
- 真实内网 ChangeOrder：外网未运行、未声称通过；真实 137 项误报和历史参数行为需要人工部署后复测。

## Known Issues

- Prompt 已加强本次参数优先级，但本轮刻意没有同时实施卡片值占位化或大范围历史参数正则门禁，以便下一次真实运行单独判断输入/Prompt 修正效果。若仍复制旧值，再基于证据收紧 Prompt 投影。
- 当前 UI 的 JSON 参数映射已确认有效；上次持久化 `request.parameters={}` 表示请求实际没有提交具体参数，不是后端丢失。操作者若需要新值必须填写该 JSON。
- 当前 SchemaProfile/Adapter 仍只严格建模 `change_plan[0].result`，没有覆盖真实 `change_plan[0]` 的全部外层计划字段；这是独立的后续结构问题。

## Work In Progress

None. 代码、文档和合成回归已完成，等待本轮 commit / merge / push。

## Recommended Next Step

内网更新后，用同一需求重新生成，但先在“本次变更具体参数 JSON”中显式填写需要替换的虚构/真实部署参数。检查：nullable Task/Procedure 字段不再出现在 `missing_fields`；只有未输入的 string 字段继续阻断；若 `model_request_count=2`，读取 `generation_diagnostics.protocol_repairs[0]` 的 stage/reason；统计 `input_refs` 是否引用本次 `parameters.*`，并核对操作正文不再沿用参考案例旧值。若旧值仍被复制，再实施卡片 Prompt 参数占位化和历史实例值泄漏门禁。

## Last Updated

- Date: 2026-08-29 (Asia/Shanghai)
- Current branch: `codex/nullable-profile-and-parameter-priority`
- Publication status: validated locally; pending commit and push to `github/main`
- Containing commit: 提交后使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不保存自引用 SHA。
