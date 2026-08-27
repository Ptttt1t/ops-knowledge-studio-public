# Current Handoff

## Current Objective

修复真实 ChangeOrder 草案在模型请求超时后被错误当作协议失败重试，导致单次点击产生两个连续远程推理、总耗时约 `2 × DEEPSEEK_TIMEOUT_SECONDS` 并可能叠加供应商并发占用的问题。

## Completed

- 根据内网脱敏诊断确认：单个草案 run 约 240 秒、模型路由 `total_requests +2`，而最小主/备模型探测均约 3 秒成功；本地门禁和单进程部署正常。
- 将协议修复门禁收窄到两类“模型已经返回内容”的错误：`JSONOutputError` 和确定性 `_assemble` 草案结构失败。
- 超时、连接失败、非容量 HTTP 错误及其他模型调用异常现在立即结束生成，不再进入第二轮协议修复。
- 超时仍不自动切换备用模型，因为客户端超时不能证明供应商已经结束原推理；立即切换可能再次占用远程并发槽位。
- 保留现有容量回退：明确的 HTTP 429、`count/limit` 等错误仍可从主模型切换到配置的 Qwen 一次。
- 新增 synthetic 超时回归：即使配置了备用模型，单次草案也只调用主模型一次，并持久化 `GENERATION_FAILED` 与原始脱敏错误。
- README、真实草案说明、长期上下文和技术决策已同步到新的失败分类。
- 检查 PID 诊断：公开仓库启动脚本只管理 `artifacts/server.pid`，没有 `serve.pid`；内网报告的失效 `serve.pid` 属于其部署包装器，本轮未对未知文件路径增加删除逻辑。

## Files Changed

- `knowledge_platform/change_drafts.py`：只有 `JSONOutputError` 可从模型调用阶段进入一次协议修复；传输异常直接失败。
- `tests/test_real_change_drafts.py`：新增超时不重试、不回退、只产生一次模型请求的回归。
- `README.md`, `docs/real-change-generation.md`：澄清协议修复、传输失败和备用模型边界。
- `docs/CODEX_CONTEXT.md`, `docs/DECISIONS.md`：沉淀“客户端超时不得自动重试/切换模型”的长期安全约束。

## Validation

- 专项真实草案回归 6 项：通过。覆盖超时单请求、Malformed JSON 修复、可解析但结构不合规修复、429 不重复、容量切换 Qwen 和协议失败无部分草案。
- `python -m unittest discover -s tests -t .`：158 tests，全部通过。
- 真实内网大草案：本轮代码尚未在内网重新部署验证；不得声称 120 秒以内能够生成成功。
- 网关侧 queue/inference 时间和客户端超时后的远端任务状态：仍待内网管理员确认。

## Known Issues

- 本次修复消除了第二次错误请求，但没有解决大草案本身可能超过 120 秒的 Layer 5 根因；若仍超时，新版本应约 120 秒失败且路由 `total_requests` 只增加 1。
- `DEEPSEEK_TIMEOUT_SECONDS` 当前是全局模型调用超时，不是只针对真实草案的独立配置。是否增加草案专用有界超时，需要先取得网关 queue/inference 时间。
- urllib 非流式调用无法可靠取消供应商侧已经开始的推理；即使本地只请求一次，超时后仍应通过安全退出和网关计数确认远程请求释放。
- 超时不触发 Qwen fallback 是有意安全行为。若未来支持超时切换，必须先有可证明原请求终止的网关取消/幂等协议。
- 内网失效 `serve.pid` 不属于公开仓库当前命名；应由创建该文件的部署脚本按 PID 匹配安全清理。

## Work In Progress

None. 代码、文档和本地回归已完成，当前位于功能分支，尚未合并或推送到 GitHub `main`。

## Recommended Next Step

合并发布后，内网先使用页面“安全退出”停止旧服务并确认网关并发归零，再升级且只启动一个平台进程。只触发一次真实草案并比较 `/api/health.model_requests.routes`：如果仍超时，应看到 run 约 120 秒且 `total_requests +1`。随后由网关管理员提供脱敏的 queue/inference 时间，再决定采用草案专用有界超时、流式读取、缩减上下文/输出预算或更快模型；不要恢复自动超时重试。

## Last Updated

- Date: 2026-08-27 (Asia/Shanghai)
- Current branch: `codex/fix-change-draft-timeout-retry`
- Base commit: `78a7f426be598fbc06928a32b580689c10ba3ad6`
- Publication status: not merged or pushed to `github/main`
- Containing commit: 提交后使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不保存自引用 SHA。
