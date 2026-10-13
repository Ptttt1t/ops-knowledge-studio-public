# 新电脑开发接续指南

整理日期：2026-10-13（Asia/Shanghai）。适用范围：两台可访问公网 GitHub 的外网开发电脑之间接续 Ops Knowledge Studio。内网部署和真实数据验证由人工完成，不在内网运行 Codex。

本文提供从零获取代码、建立环境、确认当前进度和开始下一轮开发的完整路径。最新短期状态始终以 [CODEX_HANDOFF.md](CODEX_HANDOFF.md) 为准，稳定规则以 [CODEX_CONTEXT.md](CODEX_CONTEXT.md)、[DECISIONS.md](DECISIONS.md) 和 [AGENTS.md](../AGENTS.md) 为准；本文不依赖旧电脑的聊天记录或 Codex Memory。

## 1. 先确认要接续的版本

| 项目 | 当前确认值 |
| --- | --- |
| 项目名称 | Ops Knowledge Studio，Python 单机 Web / CLI 运维知识工作台 |
| 公开仓库 | [Ptttt1t/ops-knowledge-studio-public](https://github.com/Ptttt1t/ops-knowledge-studio-public) |
| 当前功能分支 | `codex/standard-sop-studio` |
| 应用版本 | `1.9.0`，以 `pyproject.toml` 为准 |
| 本文整理前的功能代码基线 | `5b2dde15a1bd1c6888b6e8d8dbcd8f58d626791e`，2026-10-09，新增标准 SOP 工作区 |
| 本文整理时的远端 main | `4d963c761d4a1846e0a9f0ff34c6c9800b55698f`，尚未包含 1.9.0 标准 SOP 功能 |
| 开始整理时的工作区 | 干净，无待保留的未提交修改 |
| 发布方向 | 延续当前功能分支，不将这次交接整理合并到 main |

**首次克隆时要显式指定功能分支。** 普通 `git clone` 默认检出 main，会得到上一版。上述 SHA 是本次整理时的已核对快照，不是永久固定版本；交接文档提交后，分支 HEAD 会继续前移。新电脑以实际 fetch 结果和当前交接状态核对最新提交。

旧电脑仓库还有一个指向旧本地目录的 `origin` remote。这是旧电脑的历史配置，新电脑不需要复制，也不要将其当成 GitHub 地址。新克隆将唯一的公网 remote 命名为 `github`，与项目协议一致。

## 2. 最短上手路径：Windows + PowerShell

### 2.1 准备软件

必需：Git、Python **3.10–3.13**。`pyproject.toml` 明确要求 `>=3.10,<3.14`，不要直接用 Python 3.14 或更高版本。基础应用不需要 Node.js、Docker、Redis、独立数据库、前端打包工具或 OCR。

开发可选：Node.js 用于 `node --check` 检查前端语法，`rg` 用于搜索代码。Conda 可替代下述 venv，但不是项目依赖。

打开 PowerShell，先检查：

```powershell
git --version
Get-Command python, py -ErrorAction SilentlyContinue
py -0p
```

`py -0p` 仅适用于已经安装 Python Launcher 的电脑。下面以已安装 Python 3.12 为例；使用 3.10、3.11 或 3.13 时替换版本号。没有 `py` 时，用实际 Python 安装路径运行 `-m venv`，例如 `& "D:/Python312/python.exe" -m venv .venv`；这里的路径只是示意。

旧电脑本次检查发现裸 `python` 指向不可运行的 WindowsApps 别名，而实际可用的是 Conda 中的 Python 3.10.12。新电脑应核对自己的解释器路径，不要照抄旧电脑的绝对路径。

### 2.2 获取当前分支

以下命令把代码放到用户目录下的 `dev/harness`，可自行修改父目录。目标 `harness` 必须是尚不存在的目录；已有仓库按第 9 节更新，不覆盖它。

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE/dev" -Force | Out-Null
Set-Location "$env:USERPROFILE/dev"
git clone --branch codex/standard-sop-studio https://github.com/Ptttt1t/ops-knowledge-studio-public.git harness
Set-Location harness
git remote rename origin github
git status --short
git branch --show-current
git log -3 --oneline
git remote -v
```

期望：分支是 `codex/standard-sop-studio`，remote `github` 指向公开仓库，工作区干净。随后先读第 3 节指定的仓库文档。

### 2.3 建立独立 Python 环境

全部命令在仓库根目录执行。每步成功后再执行下一步；失败时保留错误信息，按第 10 节处理。

```powershell
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe --version
& ./.venv/Scripts/python.exe -m pip install --index-url https://pypi.org/simple --upgrade pip "setuptools>=83"
& ./.venv/Scripts/python.exe -m pip install --index-url https://pypi.org/simple -c constraints/base.txt -e .
& ./.venv/Scripts/python.exe -m pip check
```

直接调用 `.venv/Scripts/python.exe`，不依赖 PowerShell 激活脚本，也不需要修改执行策略。`.venv/` 已纳入 Git 忽略。每次打开新终端仍可使用这个解释器路径。

安装以 `pyproject.toml` 和 `constraints/base.txt` 为准：基础依赖 `pypdf>=6.15,<7`，约束文件固定 `pypdf==6.15.0`；Python 3.13 以下同时约束 `typing_extensions==4.16.0`；构建要求 `setuptools>=83`。没有 npm 安装步骤。

上面显式使用官方 PyPI 是为了绕过旧电脑本次遇到的镜像找不到依赖版本问题，只影响该次命令。若新电脑必须使用单位允许的镜像，替换为已同步这些版本的源，不要为绕过镜像缺包而随意降低仓库依赖约束。

### 2.4 创建本地配置并启动空库

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
& ./.venv/Scripts/python.exe run.py init
& ./.venv/Scripts/python.exe run.py serve
```

浏览器打开 [本地工作台](http://127.0.0.1:8765/)。首次安装为空库；出现零来源、零知识卡是预期行为，应用不预置 Mock 案例。缺少模型 Key 仍可启动、浏览页面以及导入标准 SOP 做结构检查。

在另一个位于仓库根目录的 PowerShell 窗口检查：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/health/live
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/health
```

第二个接口在 `.env.example` 的本地 Demo 配置下无需令牌；启用安全配置后需提供已有令牌。健康接口成功只证明服务可访问，`api_configured=true` 也只证明配置不是占位符，不证明模型认证或生成成功。

退出时使用页面“安全退出”或服务终端 `Ctrl+C`，等待模型和后台操作自然排空、服务退出。关闭浏览器不等于关闭服务。首次接续优先用前台启动，便于直接看到错误。

### 2.5 完成基础回归

服务退出后，在仓库根目录执行：

```powershell
$env:NO_PROXY = (($env:NO_PROXY, "127.0.0.1", "localhost", "::1") -join ",").Trim(",")
New-Item -ItemType Directory -Path artifacts/test-temp -Force | Out-Null
$env:TEMP = (Resolve-Path -LiteralPath artifacts/test-temp).Path
$env:TMP = $env:TEMP
& ./.venv/Scripts/python.exe -m unittest discover -s tests -t .
& ./.venv/Scripts/python.exe -m compileall -q harness knowledge_platform tests
git diff --check
```

安装了 Node.js 再执行：

```powershell
node --check knowledge_platform/static/app.js
```

`NO_PROXY` 和临时目录设置仅影响当前终端及其子进程，关闭该终端即结束，不更改系统代理。测试使用临时数据库、合成来源和测试模型，不需要真实 API Key。当前基线有 **184 个 unittest**；后续新增测试时以当时实际数量为准。首次接续的完成条件是：正确分支、依赖检查通过、空库可启动、基础回归成功。

## 3. 新会话应按这个顺序阅读

| 顺序 | 文件 | 用途 |
| --- | --- | --- |
| 1 | [AGENTS.md](../AGENTS.md) | 开始工作的只读检查、数据边界、验证与发布协议 |
| 2 | [CODEX_CONTEXT.md](CODEX_CONTEXT.md) | 稳定架构、业务状态、术语、长期约束 |
| 3 | [CODEX_HANDOFF.md](CODEX_HANDOFF.md) | 当前完成状态、最近验证、未修复问题和下一步 |
| 4 | [DECISIONS.md](DECISIONS.md) | 重要设计的原因，架构相关修改前阅读 |
| 5 | [README.md](../README.md) | 产品流程、截图、常用命令和整体入口 |
| 6 | [standard-sop.md](standard-sop.md) | 当前优先功能，含爬虫推送接口和导入约束 |

涉及其他模块时再读 [字段映射](change-order-page-field-mapping.md)、[ChangeOrder 构建](structured-change-order-extraction.md)、[相似治理](semantic-governance.md)、[实例化草案](real-change-generation.md)、[部署](deployment.md)、[安全](security-hardening.md) 和 [MindMemOS](mindmemos-integration.md)。

开始改动前核对当前 Git 状态和代码。旧截图、旧版文档和历史分支可以解释历史，不能替代当前实现。尤其注意：本文的版本选择、Python 上限和模型要求比部署指南中的通用首次安装示例更具体。

## 4. 项目现在做到哪里

### 4.1 当前优先目标：标准 SOP 写作

目标是在一个明确场景中，从已上线 SOP 中分析好的写法，人工挑选参考，生成有逐条引用的七节标准模板，编辑审核后导出。该流程独立于实例化 ChangeOrder BETA，不需要 SchemaProfile，不执行任何真实变更。

```text
场景 / 关键词批次
→ 爬虫推送、目录 CLI 或多文件 JSON 上传
→ 原文快照、章节适配和结构报告
→ 逐来源模型书写质量分析
→ 人工选定参考来源
→ 七节带引用标准 SOP
→ 编辑、当前版本审核
→ Markdown / 来源 JSON 导出
```

已实现的关键行为：

- 来源适配支持 `data.sop_change_step` 四阶段及显式 `sections` 格式。保留顺序、原文和 Pointer，排除步骤确认结果、核验说明、更新人员时间及整单历史执行结果。
- 六维评分评价范围、前置条件、步骤清晰度、可核验性、回退、风险；各 0–5 等权换算为 100。它评价书写，尚未校准为企业客观技术质量标准。
- 模板包含 `SCOPE`、`PRECONDITIONS`、`RISKS`、`PRECHECK`、`IMPLEMENTATION`、`VALIDATION`、`ROLLBACK` 七节，分别对应适用范围、前置条件、风险与停止条件、操作前检查、操作实施、操作验证、操作回退。
- 每条指导引用人工选定来源的指定章节原文；quote 至少 12 字符且逐字存在。空章节、显式 gaps/conflicts 阻断批准。引用存在不能自动证明语义正确、技术兼容或实例值泛化正确。
- 编辑替换当前稿、revision 增加并撤销审批；旧 revision 不能批准。当前没有完整历史版本浏览。
- 失败明确停止，无隐藏重试和规则评分替代；已经成功的分析报告保留，下次分析跳过它们。取消不能立即终止已经发出的远端推理。
- 来源、评分和标准稿件存入 `knowledge.db` 三张独立表，不自动注册或批准知识卡。

当前量级和限制：每批最多 500 份来源；每次生成人工选择 1–12 份；单源分析上下文最多 60,000 字符，生成来源上下文最多 160,000 字符，超限拒绝而非截断。121 份规模已做合成回归，真实数据适配与质量尚需内网人工验证。

### 4.2 保留的知识工作台能力

普通文档解析、模型抽取、结构化 ChangeOrder 的确定性证据和卡片构建、案例包审核、可信问答、相似知识治理、3D 关系图仍在。

核心卡类型为 `CASE_CONTEXT`、`PROCEDURE_STEP`、`EXECUTION_OUTCOME`。可信检索组合检查 `APPROVED`、`INDEXED`、`retrieval_enabled`、证据和相关性；历史 Outcome 不进入新方案规划。步骤 section 默认内嵌，发布 children 时 parent 退出检索，避免重复召回。

相似治理只提出跨案例、同阶段候选，人工决定完整等价合并或共享核心；同案例不合并，跨阶段只建立相似关系。JSON Pointer 是案例内定位，不是跨案例来源身份。3D 图和可选 MindMemOS 是 SQLite 事实源的投影，不能代替本地审批。

### 4.3 实例化 ChangeOrder BETA

保留需求参数、1–3 个完整已批准案例包、人工激活的 SchemaProfile、独立模型草案、程序 JSON 装配、硬门禁、revision 审核和导出。默认关闭，不注册真实执行工具。步骤人工核验、人员和时间不属于模型生成字段。已知两处审核边界问题见第 8 节，不应因标准 SOP 已完成而视为修复。

旧合成执行 Demo、十个历史自拟案例、页面内置样例入口及样例接口已退役。四份合成 JSON 仅保留在测试 fixture，不在应用中预置，也不随 wheel 发布。

## 5. 找代码的路线图

| 需要处理的事情 | 主要代码 | 对应回归入口 |
| --- | --- | --- |
| CLI 参数、启动、目录导入 | `run.py`、`knowledge_platform/cli.py` | `test_scripts.py`、`test_standard_sop.py` |
| 配置和模型请求 | `harness/config.py`、`harness/api_client.py`、`harness/model.py` | `test_security_hardening.py`、`test_runtime.py` |
| Run、Worker、取消、检查点 | `harness/runtime.py`、`harness/run_store.py`、`knowledge_platform/runtime_tasks.py` | `test_runtime.py` |
| 标准 SOP 适配、分析、生成、引用、审核 | `knowledge_platform/standard_sop.py` | `test_standard_sop.py` |
| 数据表、状态事务、关系与案例包 | `knowledge_platform/store.py`、`service.py` | `test_platform.py`、`test_case_bundles_and_answer_contract.py` |
| HTTP 路由、健康、限流、排空退出 | `knowledge_platform/web.py` | `test_security_hardening.py`、`test_standard_sop.py` 等 HTTP 测试 |
| 页面和 3D 图 | `knowledge_platform/static/index.html`、`app.js`、`styles.css`、`graph_layout.py` | `test_knowledge_graph.py` + 浏览器人工检查 |
| 文档安全解析、PDF、可选 OCR | `documents.py`、`safe_documents.py`、`document_worker.py` | `test_platform.py`、`test_security_hardening.py` |
| ChangeOrder 结构、证据、双视图 | `change_order_adapter.py` | `test_change_order_extraction.py`、`test_change_order_structured_evidence.py` |
| 语义卡、富文本、拆分、规范化 | `change_order_cards.py` | `test_change_order_card_builder.py`、`test_change_order_rebuild.py` |
| 相似簇、合并、共享核心 | `semantic_governance.py` + Store 事务 | `test_semantic_governance.py`、`test_change_order_structural_merge.py` |
| 实例化草案、Profile、审核与评测 | `change_drafts.py` | `test_real_change_drafts.py` |
| 清空知识库、三库备份与失效 | `service.py`、`store.py`、`web.py` | `test_knowledge_reset.py` |
| 可信检索、只读 Agent、可选长记忆 | `retrieval.py`、`agent.py`、`long_term_memory.py` | `test_case_bundles_and_answer_contract.py`、`test_long_term_memory.py` |

表中的简写代码路径均位于 `knowledge_platform/`，测试均位于 `tests/`。测试数据主要在 `tests/fixtures/structure_samples/` 和 `tests/fixtures/change_order_card_builder/`；其中部分 Action/整单结果字段仍为暂拟，不是已确认的真实 Schema。

标准 SOP 状态表是 `standard_sop_batches`、`standard_sop_sources`、`standard_sop_drafts`，都在知识库。Runtime 注册任务 `standard_sop.analyze` 和 `standard_sop.generate`。普通与结构化采集由 `KnowledgeService` 组织，服务端只使用单机 SQLite，不依赖 ORM 或独立数据库服务。

## 6. 配置与模型边界

### 6.1 不调用模型的第一天

先保持 `.env.example` 的默认配置，验证空库与合成回归。以下功能不需要实际模型：标准 SOP 导入和结构检查、页面浏览、已有知识审核、关系图、确定性相似候选发现。

普通文档抽取和可信问答需要模型。结构化 ChangeOrder 的普通导入入口目前仍要求 `api_configured`，虽然识别后的卡片构建走确定性路径；不要把这条入口与无需模型的 `sop-import` 混淆。

### 6.2 在允许的环境开启标准 SOP 模型

在本地 `.env` 私下填写由批准的模型服务提供的 Key、兼容端点和精确 model ID，再启用 `STANDARD_SOP_GENERATION_ENABLED=true` 并重启。配置项名称如下，**不要将真实值填到本文或提交到 Git**：

| 配置 | 作用 / 当前模板值 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 服务端密钥；模板为占位符 |
| `DEEPSEEK_BASE_URL` | OpenAI-compatible API 基址，按实际批准路由配置 |
| `DEEPSEEK_MODEL` | 网关接受的精确模型 ID；模板中的名称不保证新环境可用 |
| `DEEPSEEK_TIMEOUT_SECONDS` | 普通模型等待上限，120 秒 |
| `DEEPSEEK_MODEL_MAX_CONCURRENCY` | 同一进程同路由普通调用上限，2 |
| `STANDARD_SOP_GENERATION_ENABLED` | 默认 false，只控制标准 SOP 的模型分析/生成 |
| `STANDARD_SOP_TIMEOUT_SECONDS` | 标准 SOP 单次等待上限，240 秒 |
| `REAL_CHANGE_GENERATION_ENABLED` | 默认 false，本次接续无需开启实例化 BETA |
| `CHANGE_DRAFT_TIMEOUT_SECONDS` | 实例化草案专用等待上限，240 秒 |
| `HARNESS_WORKERS` | 默认 2，低资源电脑可显式设为 1 |
| `MINDMEMOS_ENABLED` / `MINDMEMOS_ALLOW_CONTENT_EXPORT` | 默认都为 false，首次接续无需开启 |
| `DEMO_FULL_RESET_ENABLED` | 默认 false，开发接续不需要清空旧库 |

配置解析时，进程环境变量优先于 `.env`。相对数据路径以指定配置文件的父目录解析；`run.py --env <path> <command>` 的 `--env` 放在子命令前。换配置文件位置也可能换数据根目录，排查时要核对实际路径。

标准 SOP 分析/生成在同进程串行、取得模型路由独占槽位，不继承普通 HTTP 重试。实例化草案同样独占所选路由，只允许已收到响应后的协议错误进行一次修复，明确容量错误才可按配置切换一次备用模型；超时不重试、不切换。不要将 `.env.example` 中普通客户端的 retry 配置解释为这些模块允许重试。

这些门禁只覆盖一个进程；其他主机、另一个服务进程和供应商残留推理不受其约束。观察 `/api/health` 的 `model_requests.routes` 和 Run/checkpoint，不用“页面没反应”判断调用已结束。

### 6.3 当前 Demo 配置

模板监听 `127.0.0.1:8765`，`DEMO_MODE=true`，关闭启动令牌、访问令牌和 Host/Origin 检查。这用于本机或隔离可信环境。项目尚无企业身份、RBAC 或职责分离；换电脑不意味着可直接暴露到公网。需要受控部署时按部署/安全文档另行准备。

## 7. 迁移什么、如何保留数据

### 7.1 只接续代码开发：推荐默认路径

GitHub 克隆正确分支 → 新建 Python 环境 → 从 `.env.example` 生成本地配置 → 空库启动 → 合成回归。完成这条路径已经可以继续开发；旧数据库、旧聊天、旧插件、旧 Conda 环境都不是必要条件。

本次整理不读取或复制现有 `.env` 内容，不导出旧数据库，不自动搬运上传文件。以下是迁移说明，不是对旧目录已完成备份的声明。

### 7.2 确实需要旧外网开发状态时

先人工确认旧数据只含允许在两台外网电脑之间转移的合成/公开测试资料。真实内网数据、Prompt、Profile、报告和内部地址只能留在批准的内网环境，不能通过新电脑开发交接带出。

| 项目 | 是否需要迁移 | 说明 |
| --- | --- | --- |
| 已提交代码与 `docs/` | 必需，通过 Git | 当前开发知识均沉淀到仓库 |
| `.env` | 可选，重新配置通常足够 | 含密钥和环境专属路径，受保护渠道传递；不进入 Git |
| `data/knowledge.db` | 仅需保留合成测试状态时 | 知识、证据、审核、关系及标准 SOP 批次/来源/稿件 |
| `data/runtime.db` | 与同批其他库一起保留 | 任务、事件、检查点、审批，不是代码 |
| `data/change_drafts.db` | 与同批其他库一起保留 | Profile、实例化草案 revision、审核和评测 |
| 同名 `-wal`、`-shm` 文件 | 普通停服文件备份若仍存在需同批保留 | 不混入新电脑已有库的 sidecar 文件 |
| `knowledge_sources/uploads/` | 保留来源证据时需要 | 只迁移允许外网流转的资料 |
| 配置的其他来源目录 | 按实际配置核对 | 证据引用可能依赖原文件，不能仅迁移数据库 |
| `data/paddlex_cache/` | 可选 | OCR 模型缓存可重新下载，标准 SOP JSON 流程无需它 |
| `knowledge-reset-backups/` | 可选，受保护存储 | 通常位于知识数据库同级目录，可能含完整旧状态 |
| `artifacts/`、导出文件 | 仅保留确有用途的合成资料 | 日志、报告和输出可能含敏感内容；不上传 GitHub |
| `.venv/`、Conda 环境 | 不迁移 | 在新电脑按依赖约束重建 |
| `.codex/`、`.agents/`、账号登录状态 | 不作为项目迁移内容 | 重新登录或按个人设置处理，不能提交到仓库 |

步骤：

1. 在旧电脑用安全退出 / Ctrl+C 排空，确认 HTTP 服务和所有使用这些库的进程都已退出；不要在活跃写入时复制。
2. 以 `.env` 的实际 `KNOWLEDGE_DB_PATH`、`HARNESS_RUNTIME_DB_PATH`、`CHANGE_DRAFT_DB_PATH`、`KNOWLEDGE_SOURCE_DIR` 为准，备份同一时点三库及必要原文件。默认路径只是默认值。
3. 在新电脑的正确代码版本下重建环境，准备新的本地配置，启动前恢复同一批数据。已有新库时先保护其完整数据，勿直接覆盖运行中的库或留下旧 WAL。
4. 更新旧电脑专属绝对路径；再执行 `run.py init` 补齐 schema，不使用“清空知识库”作为迁移步骤。
5. 重启后核对来源、案例包、卡片、标准 SOP 批次/报告/稿件、Profile、草案与 Run。恢复任务不代表允许再次调用模型，先看状态和配置。
6. 如果出现证据失效，核对原文件和存储的路径。当前没有自动重定位全部旧绝对证据路径的迁移工具；保留可访问路径，或从允许迁移的来源重新导入并重新审核，不绕过漂移检查。

知识清空功能不是备份迁移工具：它会删除标准 SOP 来源与稿件并使实例化草案/Profile 失效。若需从清空备份恢复，遵循 [部署指南的同批三库恢复步骤](deployment.md)，不能混用不同批次；数据库备份不包含上传原文件或外部 MindMemOS 服务。

## 8. 未完成事项与建议顺序

下列区分“已实现但尚待真实验证”和“代码尚有缺口”。本次只整理交接，不修复业务逻辑。

| 优先事项 | 当前事实 | 接续动作 |
| --- | --- | --- |
| 新电脑环境验收 | 安装路径、模型、数据均是本机配置 | 先完成第 2 节，不依赖旧数据库和真实模型 |
| 标准 SOP 真实结构验证 | 合成测试已通过，真实 JSON 的语义仍需内网人工核对 | 内网先导入少量已下载来源，确认 Pointer、顺序、章节和排除字段，再扩大至 121 份 |
| 书写排序与标准稿技术检查 | 合成固定响应证明流程，未证明真实模型质量 | 批准路由下核对评分、版本兼容、引用语义、实例泛化、停止与回退条件 |
| 现有爬虫对接 | 推送契约与目录 CLI 已实现，上游抓取未集成到工作台 | 按 standard-sop.md 接入；爬虫负责认证、搜索、下载、分页，工作台不访问 source_ref |
| 实例化草案问题 A | 模型把 `policy=empty` 核验字段报为 missing 时，装配置空后仍可能误阻断审核 | 定位 `_assemble()`、`missing_fields` 与有效 Profile policy，先补合成回归再修复 |
| 实例化草案问题 B | 旧 revision 审核/导出仅检查通用空值，未按 Profile 严格复核类型；字符串说明为数字 0 仍可能通过 | 定位 `_validate_assembled()`、`review_draft()`、`export_draft()`，覆盖旧 revision 的错类型回归 |
| 步骤字段语义 | operate_verified、operate_rollback、impact_analysis 可能含指导/事后回填混合内容 | 内网人工提供脱敏字段类型、填写时机和结构结论，不按关键词擅自删除或重分类 |
| 案例与页面映射 | CASE_CONTEXT 白名单及 change_plan 非 result 字段分类尚不完整 | 先补脱敏结构证据，按字段映射文档区分计划约束、审计和展示信息 |
| 其他待确认 | result_confirm 枚举、三个子页的独立核验字段、处理记录来源、4号提权方式 | 只接受脱敏结构反馈，不要求真实单据、截图正文或内网日志出网 |
| 相似治理扩展 | 不能向已接受 canonical 追加单个新案例，不能一次把簇拆成多组 | 有具体场景后再设计，不将现有能力描述为已经支持 |

后续开发按用户实际优先级选择任务。标准 SOP 当前最需要内网效果验证；实例化 BETA 再启用前应处理或明确评估两处边界缺口。不要同时扩展身份系统、爬虫、执行工具和新版本系统来完成一个小修复。

设计取舍沿用：优先删除不必要逻辑，其次简化，再优化，最后自动化；保留现有证据、人工审核和安全边界。新门禁须有具体失败案例，不能机械增加控制层。

## 9. 两台电脑之间怎样接力

每次开始：

```powershell
git status --short
git branch --show-current
git remote -v
git fetch github --prune
git log -5 --oneline
```

工作区干净、已在正确分支时再更新：

```powershell
git merge --ff-only github/codex/standard-sop-studio
```

如果有旧电脑未推送的提交或新电脑本地修改，先核对差异，不使用 `reset --hard` 清理，也不覆盖用途不明的内容。交接当天应由一台电脑完成当前任务并推送，再由另一台拉取，避免同分支两边独立推进后不再能快进。

结束一轮有意义的开发：完整 diff / 敏感信息检查 → 与范围匹配的验证 → 覆盖更新 `CODEX_HANDOFF.md` → 必要时更新 `CODEX_CONTEXT.md` / `DECISIONS.md` → 提交与推送 → 核对远端 SHA。交接状态只保留最新有效事实，不追加流水账。

当前阶段延续 `codex/standard-sop-studio`，不因新电脑初始化自动合并 main。用户明确要求“合并”时才按仓库协议基于最新 `github/main` 检查祖先关系、快进本地 main、push 并核对。仓库有一般自动提交发布授权，但本阶段明确的功能分支约束优先。

只暂存本轮明确文件，提交前执行：

```powershell
git diff --check
git diff --stat
git diff
git diff --cached --stat
git diff --cached
```

不要提交 `.env`、Key、Token、Cookie、数据库、上传文件、真实内网数据、artifacts 或本地 agent/session 状态。Git 作者信息和推送认证在新电脑单独配置，不复制旧电脑凭据。公开仓库可匿名克隆，推送需要账户权限。

功能分支推送与核对示例：

```powershell
git push github codex/standard-sop-studio
git rev-parse HEAD
git ls-remote github refs/heads/codex/standard-sop-studio
```

两处 SHA 相同才说明远端确实收到提交。CI 工作流目前触发 `push` 到 main 和 `pull_request`，**仅推送功能分支不保证自动运行 CI**；不能把分支上传成功当成 CI 通过。

## 10. 常见卡点

| 现象 | 首先检查 | 处理方向 |
| --- | --- | --- |
| 没有“标准化 SOP”页面 | `git branch --show-current`、pyproject 版本 | 可能检出 main；先保存现有改动，再切正确分支和安装 |
| python 无法执行 / 跳应用商店 | `Get-Command python,py`、`py -0p` | 用实际安装的解释器建 venv，以 venv 完整路径运行 |
| PowerShell 不能激活环境 | 是否只是 Activate.ps1 执行策略错误 | 直接运行 `.venv/Scripts/python.exe`，无需改系统策略 |
| setuptools / pypdf 找不到版本 | 错误中显示的 pip index 和 Python 版本 | 单次命令指定已同步版本的允许源；不要先改仓库约束 |
| pip 在临时目录报 WinError 5 | 错误是否发生在 pip-build-env 等临时路径 | 本次沙箱通过将当前终端 TEMP/TMP 指向仓库内 artifacts 下的可写临时目录解决；不先修改系统目录权限 |
| pip check 不通过 | 是否混用了系统、Conda 和 venv | 在独立环境按 constraints 重装，不修改旧共享环境 |
| 浏览器访问不了 8765 | 前台终端异常、实际 HOST/PORT、`Get-NetTCPConnection -LocalPort 8765` | 先定位占用进程，避免盲目强杀；排空旧实例或选空闲端口 |
| 本机 HTTP 测试超时 | 当前终端代理和 NO_PROXY | 按第 2.5 节排除回环代理；不先做系统代理/DNS修改 |
| api_configured=false | 占位符、实际 `.env` 路径 | 私下配置 Key 和精确模型 ID，重启；空库启动不需 Key |
| 配置为 true 仍不能分析 SOP | STANDARD_SOP_GENERATION_ENABLED、Run 错误及模型路由 | 功能开关与 Key 分开检查，健康状态不能代替实际调用结果 |
| SOP 导入失败 | 标题、四阶段数组 / sections、布尔 published、请求尺寸 | 以 standard-sop.md 适配；未知结构明确失败，不静默丢字段 |
| SOP 生成 BLOCKED | 空节、gaps/conflicts、来源引用 | 补充相关来源/编辑并重新审核，不绕过门禁 |
| 清空知识库入口不可用 | DEMO_FULL_RESET_ENABLED / DEMO_MODE、未完成 Run | 这是显式开关与忙碌保护；换电脑开发无需开启它 |
| 证据漂移或来源找不到 | 原文件是否迁移、绝对路径、源内容哈希 | 恢复允许的来源路径或重导入审核，不强行标记可信 |
| OCR 不可用 | 是否真的要处理扫描件 | JSON / 文本层 PDF 无需 OCR；确需时按部署指南安装可选依赖并 smoke |
| 后台启动日志报错 | artifacts/server.err.log、server.log | start_server.py 输出 PID 不证明启动成功，还须健康检查；PID 文件也可能过期 |

模型超时只证明客户端等待超过上限，不证明远端推理已结束。标准 SOP 不自动重试；再次操作前结合 Run 和网关状态确认。不要将 transport 错误换成 deterministic fallback 冒充模型成功。

## 11. 可直接粘贴给新电脑 Codex 的首条消息

先将 Codex 工作区设为新克隆的 `harness` 目录，再粘贴下面内容。它只要求环境验收和上下文恢复，不直接授权修复某个业务问题：

```text
请接续当前目录的 Ops Knowledge Studio。

每次面向我的答复先说明实际使用的具体模型；无法确认时不要猜测。
如平台提供可识别的上下文压缩事件，请按本对话单独计数；仅在确认第5次时先给接续摘要并建议新开对话，不伪造次数。

先只读检查 AGENTS.md、docs/NEW_COMPUTER_HANDOFF.md、
docs/CODEX_CONTEXT.md、docs/CODEX_HANDOFF.md、docs/DECISIONS.md，
核对 git status、当前分支、最近日志和 remote，再按真实代码确认实现。

目标版本是1.9.0，当前开发分支 codex/standard-sop-studio。
main 尚未包含这轮标准SOP功能。延续功能分支，不自动合并或推送main。
优先目标是已上线SOP导入、书写评审、人工选参考、带引用标准SOP、审核导出。
现有爬虫负责认证/搜索/下载，Harness只接收JSON推送，不访问source_ref。
真实ChangeOrder BETA保留，但交接中的两处审核边界缺口尚未修复。

这是外网开发电脑，只使用合成/公开测试数据。
真实内网数据、Prompt、SchemaProfile、结果、凭据和内部地址不得带到外网；
内网部署与真实验证由人工完成，只反馈脱敏计数、错误类别、结构和QA结论。
保留用途不明的现有修改，不复制旧数据库或密钥，不绕过审核和证据门禁。

请先按新电脑指南完成独立环境、依赖检查、合成回归和空库启动验收，
报告正确分支、实际Python版本、验证结果、局限及下一步建议。
尚未指定具体业务修复，不要把环境验收扩展成新增功能。
```

## 12. 验证记录与边界

本次新电脑交接整理的实测结果见 [当前交接状态的 Validation](CODEX_HANDOFF.md)。历史 1.9.0 验证包含：184 个 unittest、11 个标准 SOP 专项、121 份合成 JSON CLI 导入与重复去重、合成浏览器分析/生成/审批/编辑撤审/导出检查、前端语法、compileall、wheel 内容和敏感文件排除检查。

历史浏览器截图和评分来自合成库与测试模型固定响应，不证明真实模型写作质量。本次文档整理不构成对内网适配、真实质量排序、真实爬虫接入或 BETA 两处问题的重新验证。新电脑上仍需运行自己的安装、测试与启动验收；不要把旧电脑结果直接记为新电脑已通过。
