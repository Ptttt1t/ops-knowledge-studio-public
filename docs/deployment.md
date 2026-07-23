# Ops Knowledge Studio 部署指南

本文面向从 GitHub 获取代码并在本地或受控内网环境部署的用户。项目默认是单机应用：HTTP 服务、Harness Worker 和 SQLite 都运行在同一个 Python 进程或同一台主机上，不依赖 Redis、Docker 或外部消息队列。

## 1. 环境要求

- Python 3.10 或更高版本；
- Git；
- Windows、Linux 或 macOS；
- DeepSeek API Key，仅在知识抽取和可信回答时需要；
- 至少 2 GB 可用磁盘空间；启用 PaddleOCR 后建议预留更多空间存放模型缓存。

检查环境：

```text
python --version
git --version
```

## 2. Windows + Conda 首次部署

在 Anaconda Prompt 中执行：

```bat
conda create -n ops-knowledge-studio python=3.10 -y
conda activate ops-knowledge-studio
git clone https://github.com/Ptttt1t/ops-knowledge-studio-public.git
cd ops-knowledge-studio-public
python -m pip install --upgrade pip
python -m pip install -e .
copy .env.example .env
notepad .env
```

在 `.env` 中至少填写：

```dotenv
DEEPSEEK_API_KEY=你的API密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
PLATFORM_HOST=127.0.0.1
PLATFORM_PORT=8765
```

不要将真实 `.env` 提交到 Git；仓库已默认忽略该文件。

初始化、测试并启动：

```bat
python run.py init
python -m unittest discover -s tests -v
python run.py serve
```

浏览器访问 <http://127.0.0.1:8765>，健康检查访问 <http://127.0.0.1:8765/api/health>。

## 3. Linux 或 macOS 首次部署

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

## 4. 启用 PDF 图片与 OCR

带文本层的 PDF 使用基础依赖 `pypdf`，无需安装 OCR。扫描 PDF 和图片识别需要：

```text
python -m pip install -e ".[ocr]"
python scripts/ocr_smoke_test.py
```

可选依赖包括 PaddlePaddle、PaddleOCR 和 PyMuPDF。PaddlePaddle 的 CPU/GPU 安装方式可能因操作系统和硬件不同而变化；如果默认安装失败，请先按 PaddlePaddle 官方安装说明安装匹配版本，再重新执行项目安装命令。

OCR 首次运行可能下载模型到 `data/paddlex_cache`。该目录不会进入 Git，后续运行会复用本地缓存。

## 5. 日常启动与后台启动

前台启动：

```bat
conda activate ops-knowledge-studio
cd /d 你的仓库目录
python run.py serve
```

后台启动：

```text
python scripts/start_server.py
```

后台启动后，PID、标准输出和错误日志位于 `artifacts/`。该目录不会被提交到 Git。

停止服务时应先找到 `artifacts/server.pid` 对应的进程，再使用操作系统的正常进程停止方式，避免在任务写入 SQLite 时强制关机。

## 6. 数据目录与备份

需要持久化的数据包括：

- `.env`：API 和本地配置；
- `data/knowledge.db`：知识卡片、关系、状态和审核记录；
- `data/runtime.db`：Harness Run、事件、步骤、检查点和审批记录；
- `knowledge_sources/uploads/`：用户上传的原始文件；
- `data/paddlex_cache/`：可重新下载的 OCR 模型缓存；
- `artifacts/`：可选运行日志。

备份前先停止服务，然后复制 `.env`、两个 SQLite 数据库和上传目录到受保护位置。不要把这些文件作为 GitHub 备份，它们可能包含 API Key、业务文档或内部知识。

## 7. 升级现有部署

先停止服务并完成数据备份，然后执行：

```text
git pull --ff-only
python -m pip install -e .
python run.py init
python -m unittest discover -s tests -v
python run.py serve
```

`python run.py init` 可以重复执行：它只会补齐缺失的数据表和迁移记录，不会清空现有知识。

## 8. 运行参数

Harness Runtime 默认配置：

```dotenv
HARNESS_RUNTIME_DB_PATH=data/runtime.db
HARNESS_WORKERS=2
HARNESS_MAX_QUEUED_RUNS=100
HARNESS_SYNC_WAIT_SECONDS=900
```

资源较少的机器可以把 `HARNESS_WORKERS` 设置为 `1`。多个 Worker 会并发处理不同 Run，但每个 Run 内部仍按步骤顺序执行。

## 9. 安全边界

当前 Web 服务没有账号登录、租户隔离和公网级鉴权，因此：

- 保持 `PLATFORM_HOST=127.0.0.1`；
- 不要直接将 8765 端口暴露到互联网；
- 内网多人使用时，应在前面增加带 TLS、身份认证和访问日志的反向代理；
- 用防火墙限制来源地址；
- 不要把生产数据库、业务文档或 `.env` 上传到 Issue、PR 或公开日志；
- 自动抽取结果仍需人工审核，只有 `APPROVED` 卡片会进入可信回答。

## 10. 故障检查

服务无法启动时按顺序检查：

```text
python --version
python -m pip check
python run.py init
python -m unittest discover -s tests -v
python run.py serve
```

文档解析异常时访问 `/api/health`，检查 `document_processing` 中的 PDF/OCR 能力，再查看 `artifacts/server.err.log`。
