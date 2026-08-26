# Current Handoff

## Current Objective

修复真实 ChangeOrder 草案生成触发模型账户限额 `count=3, limit=2`，并允许在主模型容量不足时自动切换到部署者配置的 Qwen 备用模型。

## Completed

- 确认旧实现只用 `CHANGE_DRAFT_MODEL_MAX_CONCURRENCY=1` 串行化草案任务，无法阻止抽取、问答或其他 Runtime Worker 与草案共享同一模型并发。
- 确认 `chat_json(retries=0)` 不会关闭底层 `DEEPSEEK_MAX_RETRIES=2`；一次草案尝试此前仍可能访问网关三次。
- 新增同进程共享模型路由门禁：按 base URL、API Key 指纹和实际 model ID 协调；主模型默认上限为 `DEEPSEEK_MODEL_MAX_CONCURRENCY=2`。
- 真实草案模型请求在所选模型路由上取得独占槽位，并显式使用 `http_retries=0`；草案任务总并发仍固定为 `1`。
- 新增可选容量回退：主模型仅在 HTTP 429、`count/limit`、账户限额等明确容量错误时，尝试 `CHANGE_DRAFT_FALLBACK_MODEL` 一次。协议、内容或硬门禁错误不会切换模型。
- 主模型和备用模型使用独立并发门禁；备用模型上限由 `CHANGE_DRAFT_FALLBACK_MODEL_MAX_CONCURRENCY` 配置。第一版两者共用 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY`。
- 每个生成 revision 的 `model_usage.model_routing` 保存实际模型、回退标志、尝试顺序和主模型容量失败标志；草案页面显示实际模型及自动切换状态。
- `/api/health` 和 `/api/stats` 的 `model_requests.routes` 输出各模型路由的进程 ID、当前/峰值/等待请求及独占状态，不包含 API Key。
- README、`.env.example`、真实草案文档、长期上下文和技术决策已同步。

## Files Changed

- `harness/api_client.py`：按模型路由的进程共享门禁、独占 lease、模型 override、HTTP retry override 和限额诊断。
- `harness/config.py`, `.env.example`：主/备模型并发与备用模型配置、校验和公开摘要。
- `harness/model.py`：模型客户端协议增加重试、独占、模型和模型并发参数。
- `knowledge_platform/change_drafts.py`：容量错误识别、主模型优先及单次备用模型切换、路由 usage 记录。
- `knowledge_platform/service.py`, `knowledge_platform/web.py`：健康与统计接口输出模型请求诊断。
- `knowledge_platform/static/app.js`：草案结果显示实际模型和自动回退状态。
- `tests/test_platform.py`, `tests/test_real_change_drafts.py`, `tests/test_demo_mode_and_card_delete.py`：路由门禁、隐藏重试、容量回退、非容量错误不回退和配置回归。
- `README.md`, `docs/real-change-generation.md`, `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：配置、运行边界和设计原因。

## Validation

- `python -m unittest discover -s tests -t .`：156 tests，全部通过。
- 并发 synthetic test：同模型普通调用峰值不超过 2，独占草案与同模型普通调用无碰撞；不同 model route 可以独立运行并使用不同上限。
- 容量 synthetic test：全局 HTTP retry 为 2 时，草案 429 路径仍只访问主模型一次；配置备用模型后按 `primary -> fallback` 成功，并记录实际路由。
- 协议 synthetic test：即使已配置备用模型，协议/内容失败也只在主模型执行一次显式修复，不切换 Qwen。
- `python -m compileall -q harness knowledge_platform tests`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过。
- 真实内网模型端点：按环境边界未在外网验证。

## Known Issues

- 备用 Qwen 的网关精确 model ID 未知，必须由内网部署者从实际模型网关配置确认；不能直接把“qwen3.8 27b”展示名称当作 ID。
- 模型门禁是单进程协调器，不能控制残留的第二个平台进程、其他主机或供应商尚未释放的旧请求。
- 第一版备用模型必须与主模型使用同一 OpenAI-compatible base URL 和 API Key；尚不支持备用端点或备用凭据。
- 若两个模型都发生容量或协议错误，系统按安全约束停止，不输出部分草案或确定性兜底。

## Work In Progress

None. 功能分支已通过本地验证，尚未合并到 GitHub `main`。

## Recommended Next Step

发布后由内网人工确认网关接受的 Qwen 精确 model ID，在 `.env` 设置 `CHANGE_DRAFT_FALLBACK_MODEL=<实际ID>` 和经供应商确认的 `CHANGE_DRAFT_FALLBACK_MODEL_MAX_CONCURRENCY`。升级时先停止旧服务并只启动一个实例，再生成一份草案；检查页面实际模型以及 revision 的 `model_usage.model_routing`。若仍失败，只反馈脱敏错误类型和 `/api/health.model_requests.routes`，不要反馈 Prompt、真实卡片或 ChangeOrder 内容。

## Last Updated

- Date: 2026-08-26 (Asia/Shanghai)
- Current branch: `codex/account-model-concurrency-gate`
- Base published `main`: `e4ec7d0a0584214897a0dcaebb2ec25770d3dc1e`
- Publication status: local validated, not yet merged/pushed to `github/main`
- Containing commit: 提交后使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不保存自引用 SHA。
