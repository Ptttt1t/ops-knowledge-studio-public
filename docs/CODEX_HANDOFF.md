# Current Handoff

## Current Objective

为真实 ChangeOrder 草案生成增加独立、可通过 `.env` 调整的客户端等待上限，保留已经验证正确的单请求、并发 1 和超时不重试边界，并补齐不含 Prompt 正文的生成预算与性能诊断。

## Completed

- 新增 `CHANGE_DRAFT_TIMEOUT_SECONDS`，代码和 `.env.example` 默认均为 `240`；部署者可在实际 `.env` 覆盖并通过重启生效。
- 普通 DeepSeek 调用继续使用 `DEEPSEEK_TIMEOUT_SECONDS`（默认 120）；真实草案把专用 timeout 显式传至 `DeepSeekClient.chat_json -> chat -> urlopen`。
- 新增类型化 `APITimeoutError`。真实草案超时转为稳定错误码 `MODEL_TIMEOUT`，页面错误明确说明当前上限且本次未自动重试。
- 保留上一轮安全边界：草案并发固定为 1、HTTP retry 为 0；timeout 不重试、不切 Qwen、不进入 protocol repair。只有已收到响应后的 JSON/装配协议失败允许一次修复，修复请求也使用同一专用 timeout。
- 草案根记录、revision `model_usage` 和 Runtime checkpoint 新增 `generation_budget` / `generation_performance`，记录模型、timeout、案例/卡片计数、Prompt 大小、输出预算、请求起止时间、客户端等待耗时、请求数、响应大小和供应商实际返回的 token usage；不保存 Prompt 正文。
- 旧 `change_drafts.db` 启动时会增量增加错误码和诊断 JSON 列，无需删除既有草案数据库。
- 模型调用前控制台打印 `[CHANGE DRAFT MODEL] model=<id> timeout_seconds=<N>`，便于内网核对实际配置是否传到底层。

## Files Changed

- `harness/config.py`, `.env.example`：新增草案专用 timeout 默认值、环境变量读取、正数校验和公开配置。
- `harness/model.py`, `harness/api_client.py`：增加单次请求 timeout override，并传递到实际 HTTP 调用；新增类型化超时错误。
- `knowledge_platform/change_drafts.py`：应用专用 timeout，持久化错误码、生成预算与性能统计，保持 timeout 单请求失败语义。
- `knowledge_platform/runtime_tasks.py`：成功/失败 checkpoint 保存非敏感生成诊断，Runtime step/run 保留 `MODEL_TIMEOUT`。
- `tests/test_platform.py`, `tests/test_demo_mode_and_card_delete.py`, `tests/test_real_change_drafts.py`：覆盖全局/草案 timeout 分离、env 覆盖、timeout 单请求、protocol repair 和诊断字段。
- `README.md`, `docs/real-change-generation.md`, `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：同步运行配置、诊断和安全边界。

## Validation

- 草案/配置/API 专项回归：49 tests，全部通过。
- `python -m unittest discover -s tests -t .`：162 tests，全部通过。
- `python -m compileall -q harness knowledge_platform`：通过。
- 真实内网 ChangeOrder：外网未运行、未声称成功；仍需人工部署后验证实际等待时间和生成结果。
- 网关 `queue_seconds` / `inference_seconds`：客户端仍不可见，诊断只报告 client wait elapsed。

## Known Issues

- 如果真实大草案仍稳定达到约 240 秒后 timeout，不应继续盲目提高上限；应结合新增的 Prompt/上下文/响应统计和网关侧 queue/inference 证据，再评估 context pruning、输出预算、streaming 或更快模型。
- urllib 非流式请求仍无法可靠取消供应商侧已经开始的推理；本地 timeout 只保证不再发送第二次草案请求，不能证明远端工作已立即结束。
- 超时不触发 Qwen fallback 是有意的安全行为；只有明确容量错误仍允许现有的一次备用路由。

## Work In Progress

None. 功能已合并并发布到 GitHub `main`。

## Recommended Next Step

内网更新后保持 `CHANGE_DRAFT_TIMEOUT_SECONDS=240`，重启并对同类真实草案只触发一次生成。核对控制台 timeout、路由 `total_requests +1`、`generation_budget` 与 `generation_performance`：若在 120～240 秒内成功，基于实际统计再做性能优化；若约 240 秒仍 timeout，则先取得网关 queue/inference 时间，不继续直接增大 timeout。

## Last Updated

- Date: 2026-08-27 (Asia/Shanghai)
- Current branch: `main`
- Published feature commit: `330c28277ec04f56d6c21c4bc825303dce80d97d`
- Publication status: merged and pushed to `github/main`
- Containing commit: 提交后使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不保存自引用 SHA。
