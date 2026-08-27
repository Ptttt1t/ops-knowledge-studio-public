# Current Handoff

## Current Objective

修复真实 ChangeOrder 草案中对象型 `change_scene` 被自由文本 `scenario` 错误绑定的问题，并确保输入/Profile 的确定性装配错误不会被误判为模型协议错误而再次调用模型。

## Completed

- `RealChangeRequest` 新增 `scenario_reference` 对象；Web 表单分别收集场景中文名、英文名和唯一标识，旧 API 客户端仍可通过 `parameters.change_scene` 传入结构化场景。
- 对象型 `change_scene` 按活动 Profile `sample` 的键集合和子字段类型确定性装配。Profile 样例仅提供结构，不复制任何样例值；样例为 `null` 的字段确定性写 `null`。
- `scenario` 自由文本只作为 `name_cn` 的兼容输入。样例中原本非空而本次缺失的 `name_en`、`unique_id` 等字段写类型安全空值，并进入 `missing_fields` 硬门禁。
- 新增 `REQUEST_PROFILE_BINDING_ERROR`。草案创建和生成开始时都会预检 request/Profile 绑定；类型冲突在模型调用前失败，模型请求数为 0。
- 装配阶段如果仍出现确定性绑定错误，也立即失败且不进入 protocol repair；只有模型负责的 JSON/`GeneratedChangeDraft` 协议错误仍保留一次修复。
- generation diagnostics 新增 `preflight_missing_field_count` 和仅含字段路径的 `preflight_missing_fields`，不保存 Prompt 或真实字段值。
- `CHANGE_DRAFT_TIMEOUT_SECONDS` 仍由代码默认 `240`，并保留 `.env.example` 接口；部署环境可直接覆盖后重启，不影响全局 120 秒 timeout。

## Files Changed

- `knowledge_platform/change_drafts.py`：结构化场景输入、Profile-shaped 装配、确定性绑定预检、错误分类和重试边界。
- `knowledge_platform/static/index.html`, `knowledge_platform/static/app.js`：增加场景英文名/唯一标识输入并发送 `scenario_reference`。
- `tests/test_real_change_drafts.py`：增加对象场景、缺失值、旧 API 兼容、预检 0 请求和晚期绑定错误不重试的合成回归。
- `README.md`, `docs/real-change-generation.md`：更新表单/API、缺失项、`.env` timeout 和协议修复边界。
- `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：记录 Profile 样例不作为实例值及确定性绑定不调用模型的长期约束。

## Validation

- `python -m unittest tests.test_real_change_drafts`：18 tests，全部通过。
- `python -m unittest discover -s tests -t .`：167 tests，全部通过。
- `python -m compileall -q harness knowledge_platform`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过（仅有工作区 LF/CRLF 提示，无 whitespace error）。
- 真实内网 ChangeOrder：外网未运行、未声称成功；需要人工更新部署后验证真实 Profile 与新表单的对应关系。

## Known Issues

- 旧请求若只有 `scenario`，对象型 Profile 会用它填充 `name_cn`，但 `name_en` / `unique_id` 等本次未提供的非空样例字段会使 revision 进入 `BLOCKED`；这是防止复制历史样例值的预期门禁。
- Profile 目前从一份管理员确认的代表案例提取对象子字段类型；若后续真实 schema 的 nullable 子字段类型发生变化，应更新 Profile 管理能力，而不是在草案装配中猜测。
- 真实长草案的网关排队/推理性能问题与本次结构绑定修复相互独立，客户端仍无法拆分 queue 与 inference 时间。

## Work In Progress

None. 代码和文档已完成并通过外网合成回归，等待本轮 commit / merge / push。

## Recommended Next Step

内网更新后重启平台，在页面填写场景中文名、英文名和唯一标识，再对同一真实案例生成一次。应确认 `change_scene` 输出为 Profile 的完整对象键集合、三个实例值均来自本次输入、其余 nullable 字段为 `null`，并确认正常生成只增加 1 次模型请求；若缺少英文名或唯一标识，应看到 `context.change_scene.<field>` 缺失项和 `BLOCKED`，不能出现历史样例值。

## Last Updated

- Date: 2026-08-27 (Asia/Shanghai)
- Current branch: `codex/change-scene-profile-binding`
- Publication status: validated locally; pending commit and push to `github/main`
- Containing commit: 提交后使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不保存自引用 SHA。
