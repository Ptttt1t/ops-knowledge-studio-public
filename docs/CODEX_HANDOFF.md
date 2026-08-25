# Current Handoff

## Current Objective

保持已发布的知识治理改进稳定，并确保仓库级共享上下文准确支持两台外网开发电脑之间的 Codex 接力；内网环境不运行 Codex，只承担人工部署和真实数据验证。

## Completed

- `procedure_group` 已成为 Procedure 相似治理的硬约束：只有同阶段卡片进入合并候选，跨阶段高相似只建立 `CROSS_PHASE_SIMILAR` 关系。
- 共享锚点改为结构化字段、技术标识符和完整业务短语，不再向页面展示中文字符滑窗残片。
- Knowledge Graph API 输出节点 cluster/centroid/phase，前端采用 `type centroid + local force` 的 3D 布局，并提供按阶段/类型的图例和颜色。
- 真实变更生成服务使用独立 `BoundedSemaphore`，模型调用（包括协议修复）最大并发固定为 `1`，不影响其他模型路径。
- Demo 安全子功能在 `DEMO_MODE=true` 时默认启用；全库重置和真实草案等破坏性/敏感能力继续默认关闭。
- 新增相应 synthetic tests，并同步 README、`.env.example` 和专题文档。
- 初始化根目录 `AGENTS.md`、长期上下文 `CODEX_CONTEXT.md`、当前交接 `CODEX_HANDOFF.md` 和技术决策 `DECISIONS.md`。
- 已快进合并并发布到公开仓库 `Ptttt1t/ops-knowledge-studio-public` 的 `main`；发布前将原有测试中的私网字面量替换为 RFC 5737 文档地址。
- 已纠正跨电脑环境描述：Codex 接力发生在两台外网电脑之间，工位设备是接外网的老旧笔记本；公司内网不运行 Codex，只由人工部署并验证真实数据。

## Files Changed

- `AGENTS.md`：跨电脑 Codex 恢复顺序、记忆边界、交接协议和 Git 发布约定。
- `docs/CODEX_CONTEXT.md`：当前架构、数据流、业务规则、长期约束和开发约定。
- `docs/CODEX_HANDOFF.md`：本轮最新交接状态。
- `docs/DECISIONS.md`：当前能够从代码与文档确认的重要设计决策。
- `.gitignore`：排除本地 `.codex/`、`.agents/` 状态。
- `.env.example`, `harness/config.py`：真实草案并发配置和 Demo 默认规则。
- `knowledge_platform/change_drafts.py`：真实草案模型调用串行化。
- `knowledge_platform/semantic_governance.py`, `knowledge_platform/store.py`：phase-safe 候选/事务、跨阶段关系和解释性锚点。
- `knowledge_platform/graph_layout.py`, `knowledge_platform/static/{app.js,index.html,styles.css}`：3D 类型簇布局与图例。
- `README.md`, `docs/real-change-generation.md`, `docs/semantic-governance.md`：行为、配置和安全边界说明。
- `tests/test_demo_mode_and_card_delete.py`, `tests/test_knowledge_graph.py`, `tests/test_real_change_drafts.py`, `tests/test_semantic_governance.py`：新增回归场景。

## Validation

- `python -m unittest discover -s tests -t .`：149 tests，全部通过。
- `python -m compileall -q harness knowledge_platform tests`：通过。
- `node --check knowledge_platform/static/app.js`：通过。
- `git diff --check`：通过。
- 本次环境边界纠正只修改共享文档；`git diff --check` 通过，未改动代码，因此未重复运行 149 项测试。
- 3D 页面视觉人工检查：本轮未完成；当时无可连接的浏览器实例，仅验证了 API/layout 数据和 JavaScript 语法。不要将其描述为已视觉验收。
- 真实内网 ChangeOrder：按数据边界未在外网验证。

## Known Issues

- 新 3D cluster 布局仍需在含多个 phase 和关系边的实际内网知识库上人工观察密度、标签和边可读性。
- 相似阈值和业务短语词表仍可能需要根据内网反馈增量校准，但跨 phase 禁止 MERGE 是固定规则。
- GitHub CI 状态应在本轮 push 后从 Actions 页面确认；本文件只记录本地验证。

## Work In Progress

None.

## Recommended Next Step

在工位的外网老旧笔记本上执行 `git pull --ff-only` 获取 `main`，由新的 Codex session 按 `AGENTS.md` 恢复上下文并复跑测试。内网验证是另一条人工流程：按批准方式部署当前版本，用合规的真实案例执行相似扫描和 case rebuild，人工确认跨 phase 关系、同 phase 候选、3D 类型簇和模型并发；只向外网反馈脱敏的计数、phase、关系、错误类型和 QA 状态，绝不把真实数据带到 Codex 环境。

## Last Updated

- Date: 2026-08-25 (Asia/Shanghai)
- Current branch: `main`
- Published remote: `github/main` (`Ptttt1t/ops-knowledge-studio-public`)
- Feature and shared-context commit: `40a2f678a162798b21edcaa7af8ebadb2e2753bd`
- Public-fixture safety follow-up: `12e3cea9b9cbb59fdac2b4860506e7614eb716e5`
- Handoff refresh commit: 使用 `git log -1 --oneline -- docs/CODEX_HANDOFF.md` 获取；不在文件内保存自引用 SHA。
