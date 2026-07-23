# Ops Knowledge Studio

[![CI](https://github.com/Ptttt1t/ops-knowledge-studio-public/actions/workflows/ci.yml/badge.svg)](https://github.com/Ptttt1t/ops-knowledge-studio-public/actions/workflows/ci.yml)

一个由原始 AI Agent Harness 升级而来的本地运维知识工程平台。它把“文档进入向量库”扩展为可治理的知识生产闭环：

```text
SOP / 工单 / 复盘 / 变更方案
          ↓
文档分片与来源定位
          ↓
DeepSeek 结构化知识抽取
          ↓
字段质量与证据精确校验
          ↓
重复 / 冲突 / 新版本比较
          ↓
DRAFT / PENDING_REVIEW
          ↓
人工批准 / 驳回 / 替代
          ↓
仅检索 APPROVED 知识
          ↓
带 [K编号] 证据的可信方案
```

## 路线选择

本项目选择“轻量组合一路线”：

- DeepSeek：知识抽取、知识比较和可信方案生成；
- 固定知识卡片 Schema：场景、对象、版本、步骤、风险、回退、验证和证据；
- SQLite：知识生命周期、关系和审计日志；
- 本地混合检索：英数字词项、中文二元词和字段权重，无需额外向量数据库；
- Human-in-the-loop：正式知识必须人工审核；
- 本地 Web 工作台：采集、审核、查询和知识库管理。

这条路线先验证真正有差异的治理闭环，同时保留以后接入 RAGFlow、Neo4j、向量模型或规则引擎的空间。

## 1. 填写 DeepSeek API

直接编辑项目根目录的 `.env`：

```dotenv
DEEPSEEK_API_KEY=你的API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

DeepSeek 使用 OpenAI 兼容的 `POST /chat/completions`。知识抽取使用 `response_format={"type":"json_object"}`。

如使用 DeepSeek 格式的代理服务，只需替换 Key、Base URL 和模型名。不要把 `/chat/completions` 写进 Base URL。

## 2. 部署与启动

运行要求：Python 3.10 及以上版本。Windows 用户推荐使用 Anaconda Prompt：

```bat
conda create -n ops-knowledge-studio python=3.10 -y
conda activate ops-knowledge-studio
git clone https://github.com/Ptttt1t/ops-knowledge-studio-public.git
cd ops-knowledge-studio-public
python -m pip install --upgrade pip
python -m pip install -e .
copy .env.example .env
notepad .env
python run.py init
python -m unittest discover -s tests -v
python run.py serve
```

`python -m pip install -e .` 只需在首次部署和依赖发生变化时执行，不需要每次启动都重新安装。已有符合要求的 Conda 环境也可以直接复用。

Linux 或 macOS 可使用：

```bash
conda create -n ops-knowledge-studio python=3.10 -y
conda activate ops-knowledge-studio
git clone https://github.com/Ptttt1t/ops-knowledge-studio-public.git
cd ops-knowledge-studio-public
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
python run.py init
python -m unittest discover -s tests -v
python run.py serve
```

启动后访问：

- 工作台：<http://127.0.0.1:8765>
- 健康检查：<http://127.0.0.1:8765/api/health>

后续日常启动只需要激活环境、进入仓库并执行：

```bat
conda activate ops-knowledge-studio
cd /d 你的仓库目录
python run.py serve
```

完整的配置、OCR、后台启动、数据备份和升级流程见 [部署指南](docs/deployment.md)。

网页提供：

- 概览：文档、卡片、状态和质量统计；
- 知识采集：上传多种实际文档或粘贴原文，并运行抽取、质量校验和知识比较；
- 审核队列：批准、驳回或以新知识替代旧知识；
- 可信方案：只使用 APPROVED 卡片，并返回来源证据；
- 知识库：按生命周期状态检索和查看完整来源。

启动平台不需要 API；只有知识抽取和可信方案生成会检查密钥。

## 3. 使用 CLI

导入示例 SOP：

```powershell
python run.py ingest --file sample_data\demo_upgrade_sop.md
```

查看待审核知识：

```powershell
python run.py list --status PENDING_REVIEW
```

批准知识：

```powershell
python run.py review --id 1 --action approve --reviewer "你的名字" --comment "证据和适用范围已核对"
```

不调用 API 的本地检索：

```powershell
python run.py search --query "NE-A V3.1-P2 回退"
```

基于已批准知识生成方案：

```powershell
python run.py query --question "NE-A 安装 V3.1-P2 前要检查什么，失败后如何回退？"
```

## 4. 知识卡片与生命周期

每张卡片保存：

- 标题、摘要和知识类型；
- 适用场景、对象、版本和前置条件；
- 操作步骤、风险、回退与验证；
- 来源文档、字符范围、原文证据和内容校验和；
- 质量分和质量问题；
- NEW、DUPLICATE、CONFLICT 或 NEW_VERSION 判断；
- 审核人、审核意见、发布时间和替代关系；
- 全部生命周期审计记录。

状态规则：

- `DRAFT`：质量分不足 65，需要补充；
- `PENDING_REVIEW`：通过基础质量门禁，等待人工审核；
- `APPROVED`：允许进入可信方案检索；
- `REJECTED`：已驳回，不参与正式检索；
- `SUPERSEDED`：已被新版本替代，保留历史但不参与正式检索。

## 5. 支持的文档

平台支持 TXT、Markdown、CSV、JSON、YAML、DOCX、PDF，以及 PNG、JPG、TIFF、BMP、WebP 等常见图片。网页端支持多选上传，单个请求限制为 50 MB。

PDF 文本提取已包含在基础依赖中。需要解析扫描 PDF 或图片时，再安装 OCR 可选依赖：

```powershell
python -m pip install -e ".[ocr]"
```

如果 PaddlePaddle 在特定平台无法从默认软件源安装，请先按照 PaddlePaddle 官方说明安装与系统匹配的 CPU/GPU 版本，再执行上述命令。

处理规则：

- 带文本层的 PDF 优先使用 `pypdf`，速度更快且保留文字准确性；
- 无文本层或文字极少的扫描页使用 PyMuPDF 渲染，再交给 PaddleOCR；
- 图片直接进入 PaddleOCR；
- 当前本地模型为 PP-OCRv4 mobile CPU，支持简体中文和英文；
- OCR 模型在第一次识别时下载到 `data/paddlex_cache`，后续离线复用。

## 6. 测试

全部测试使用模拟 DeepSeek 响应，不会联网，也不会消耗额度：

```powershell
python -m unittest discover -s tests -v
```

真实 PDF/OCR 冒烟测试：

```powershell
python scripts\ocr_smoke_test.py
```

## 7. 代码结构

```text
ops-knowledge-studio-public/
├─ .env                         # DeepSeek API 填写位置
├─ run.py                       # CLI / Web 入口
├─ harness/
│  ├─ config.py                 # 配置和密钥检查
│  ├─ api_client.py             # DeepSeek Chat / JSON Output
│  ├─ model.py                  # 模型客户端协议
│  ├─ run_store.py              # Run、步骤、事件与检查点
│  ├─ runtime.py                # Worker、预算、取消、恢复与审批
│  ├─ tools.py                  # 工具注册、Schema 与风险等级
│  └─ trace.py                  # JSONL 运行轨迹
├─ knowledge_platform/
│  ├─ documents.py              # 文档读取与分片
│  ├─ schema.py                 # 知识卡片与状态模型
│  ├─ store.py                  # SQLite 生命周期与审计
│  ├─ retrieval.py              # 本地混合检索
│  ├─ prompts.py                # 抽取、比较和回答约束
│  ├─ runtime_tasks.py          # 知识任务到 Harness 的适配
│  ├─ service.py                # 知识流水线
│  ├─ web.py                    # 本地 HTTP API
│  └─ static/                   # 网页工作台
├─ docs/                        # 部署、安全与架构说明
├─ sample_data/                 # 演示来源
├─ knowledge_sources/           # 待加工文档目录
├─ data/                        # SQLite 数据库
└─ tests/                       # 离线集成测试
```

## 8. 安全边界

- 默认只监听 `127.0.0.1`，没有登录鉴权，不应直接暴露到公网；
- `.env`、SQLite 数据库和运行轨迹已被 Git 忽略；
- 方案生成只能读取 APPROVED 知识；
- 平台不执行模型生成的系统命令；
- 自动抽取结果不会自动发布。

DeepSeek 官方参考：

- <https://api-docs.deepseek.com/api/create-chat-completion>
- <https://api-docs.deepseek.com/guides/json_mode/>
- <https://api-docs.deepseek.com/guides/tool_calls>

## Harness Runtime v1

知识治理能力现在运行在一个本地、持久化的 Harness Runtime 之上。它不需要 Redis、Docker 或外部队列；运行记录单独保存在 `data/runtime.db`，不会与知识库 `data/knowledge.db` 混用。

每个 Run 都包含任务输入、预算、状态、步骤、事件、检查点、重试次数和最终结果。支持的状态为：

```text
QUEUED -> RUNNING -> SUCCEEDED / FAILED / CANCELLED
                  -> CANCEL_REQUESTED -> CANCELLED
                  -> WAITING_APPROVAL -> QUEUED
RUNNING (程序异常退出后) -> INTERRUPTED -> 可恢复执行
```

当前注册的知识任务：

- `knowledge.ingest_text`
- `knowledge.ingest_file`（仅允许处理 Web 上传目录中的已保存文件）
- `knowledge.query`
- `knowledge.agent_query`
- `knowledge.regrade`

Runtime 还提供：幂等键、并发 Worker 上限、排队上限、取消检查、步骤与 Token/工具调用预算、模型调用适配层，以及按风险等级拦截工具调用。当前知识任务只使用受控的本地知识能力；非只读工具必须显式获批后才允许执行。

在 `.env` 中可调整：

```dotenv
HARNESS_RUNTIME_DB_PATH=data/runtime.db
HARNESS_WORKERS=2
HARNESS_MAX_QUEUED_RUNS=100
HARNESS_SYNC_WAIT_SECONDS=900
```

### Web Run API

旧的同步知识接口继续保留。新接口用于需要可观察、可取消和可恢复的异步任务：

```text
POST /api/runs
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events
POST /api/runs/{run_id}/cancel
POST /api/runs/{run_id}/resume
POST /api/runs/{run_id}/approvals
```

提交一个文本导入任务的请求体如下；可选的 `Idempotency-Key` 请求头可避免网络重试造成重复导入：

```json
{
  "task_type": "knowledge.ingest_text",
  "input": {
    "source_name": "core-network-change.md",
    "source_ref": "change://CHG-2026-001",
    "content": "原始 SOP 或工单复盘内容"
  },
  "budget": {
    "max_steps": 12,
    "timeout_seconds": 900,
    "max_tool_calls": 20,
    "max_total_tokens": 50000
  }
}
```

### CLI Run commands

```powershell
python run.py run-submit --task-type knowledge.regrade --input-json "{}"
python run.py run-list
python run.py run-show --id <run_id> --events
python run.py run-cancel --id <run_id>
python run.py run-resume --id <run_id>
python run.py run-approve-tool --id <run_id> --tool-name <tool_name> --decision APPROVED --actor "reviewer"
```

`run-submit` 和 `run-resume` 默认等待到任务完成或达到 `HARNESS_SYNC_WAIT_SECONDS`。`run-list`、`run-show`、`run-cancel` 和 `run-resume` 都直接读写同一个持久化 Run 存储，因此可用于排障和恢复。

当未来任务注册了非只读工具时，Runtime 会先进入 `WAITING_APPROVAL`，并在 Run 详情中记录工具、原因和审批历史。审批接口的请求体为 `tool_name`、`decision`（`APPROVED` / `REJECTED`）、`actor` 和可选 `comment`；批准后任务会重新排队，任务处理器可通过 Run checkpoint 实现续跑语义；拒绝则以 `TOOL_APPROVAL_REJECTED` 结束。
