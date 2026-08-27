# Current Handoff

## Current Objective

为 Demo 平台增加可观察的“安全退出”，避免在模型请求或后台任务仍运行时直接终止主进程，导致供应商侧并发租约未及时释放。

## Completed

- 页面右上角新增 Demo 专用“安全退出”按钮和二次确认；退出期间使用全屏状态层显示在途模型请求、Web 操作和 Worker 数量。
- 新增 `RUNNING -> DRAINING -> STOPPING -> STOPPED` 生命周期。进入 `DRAINING` 后原子拒绝新的 POST/PATCH/DELETE 工作，并返回 `503 platform_draining`。
- 排空协调器会停止相似治理 Worker、主 Runtime 和合成 Demo Runtime 继续领取任务，等待当前进程已跟踪的模型请求、在途操作及 Worker 全部归零后再关闭 HTTP Server。
- 命令行 `Ctrl+C` 复用同一排空流程，不再直接关闭 Server。
- 新增只读退出状态接口 `GET /api/admin/safe-shutdown`，健康接口同步输出生命周期和模型路由活动诊断。
- 新增 `DEMO_SAFE_SHUTDOWN_ENABLED`，遵循 Demo 非破坏性功能默认启用规则；仅在 `DEMO_MODE=true` 时实际开放按钮和退出接口。
- 确认当前相似知识后台扫描是本地 SQLite/确定性相似度计算，不调用模型；文档已修正该诊断边界。

## Files Changed

- `knowledge_platform/web.py`：Server 生命周期、工作 admission、模型/Worker 排空、退出接口及 Ctrl+C 排空入口。
- `harness/runtime.py`, `knowledge_platform/change_web.py`, `knowledge_platform/semantic_governance.py`：停止接单、请求 Worker 退出和活动 Worker 统计。
- `knowledge_platform/static/index.html`, `knowledge_platform/static/styles.css`, `knowledge_platform/static/app.js`：安全退出按钮、确认、排空状态层和轮询。
- `harness/config.py`, `.env.example`：`DEMO_SAFE_SHUTDOWN_ENABLED` 配置和公开 Demo 能力摘要。
- `knowledge_platform/cli.py`：CLI serve 的 Ctrl+C 安全排空。
- `tests/test_demo_mode_and_card_delete.py`：Demo/生产开关、排空等待、拒绝新任务和服务关闭回归。
- `README.md`, `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：使用说明、长期约束和设计决策。

## Validation

- `python -m unittest discover -s tests -t .`：157 tests，全部通过。
- 安全退出 synthetic integration：模拟 1 个活动模型请求时，Server 保持 `DRAINING` 且拒绝新写任务；活动数归零后 Server 才退出，通过。
- 生产模式回归：安全退出接口返回 404，按钮能力配置为关闭，通过。
- `python -m compileall -q harness knowledge_platform tests`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过。
- 真实内网模型端点与供应商并发释放：按环境边界未在外网验证。

## Known Issues

- 安全退出只能排空当前 Python 进程能跟踪的请求，不能终止其他进程、其他主机或供应商已经泄漏的并发计数；后者仍需网关管理员处理。
- urllib 模型调用没有可靠的跨线程远程取消协议，因此安全退出会等待请求自然返回或达到 `DEEPSEEK_TIMEOUT_SECONDS`，长请求会使退出等待时间相应变长。
- 页面连接关闭代表本进程的已跟踪工作已排空，不构成对供应商内部计数系统的绝对证明；内网首次部署仍应观察网关并发计数。

## Work In Progress

None. 功能已合并并发布到 GitHub `main`。

## Recommended Next Step

由内网人工升级，在无任务、单个长模型请求和模型请求加队列任务三种情况下分别点击“安全退出”，确认页面计数归零、Python 进程退出、供应商并发槽位释放。只反馈脱敏后的计数和错误类型，不反馈真实 ChangeOrder、知识卡或内部地址。

## Last Updated

- Date: 2026-08-27 (Asia/Shanghai)
- Current branch: `main`
- Published feature commit: `352c375e2efe93c8431dd8e90efd6c8db623efed`
- Publication status: merged and pushed to `github/main`
- Containing commit: 提交后使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不保存自引用 SHA。
