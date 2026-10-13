# Current Handoff

## Current Objective

将当前 1.9.0 项目整理为新电脑可直接接续的开发交接。接续分支为 codex/standard-sop-studio，延续既有功能分支发布约束，不更新 main。产品当前优先目标仍是从已上线 SOP 中分析好写法、编写有来源引用的标准模板；真实质量和字段语义由内网人工验证。

## Completed

- 新增 docs/NEW_COMPUTER_HANDOFF.md：当前分支与代码基线、Windows PowerShell 从零克隆/建环境/安装/启动/回归、文档阅读顺序、模块与测试路线图、配置边界、可选合成开发数据迁移、已知问题和优先级、两机 Git 接力、故障处理，以及可粘贴给新电脑 Codex 的首条消息。无需旧电脑聊天或本地 Memory。
- README 增加交接入口并在快速克隆命令中显式指定当前功能分支，避免新电脑默认检出尚无标准 SOP 的 main；.gitignore 增加 .venv/，避免新建开发环境混入 Git。未改变应用代码、版本和业务行为。
- 本轮未读取现有 .env 内容、未迁移或修改已有数据库、未搬运上传文件、未调用真实模型；安装与 smoke 数据全部位于 Git 忽略的 artifacts/handoff-validation/。本轮没有实施实际新电脑数据搬迁，指南中的迁移步骤是可选操作说明。
- 新增“标准化 SOP”工作区：场景/关键词批次、多 JSON 导入、六维模型书写质量报告、优点原文与兼容性说明、人工选择来源、七节模板生成、JSON 编辑、当前版本审核及 Markdown/来源 JSON 导出。与原实例化 ChangeOrder BETA 独立，不要求 SchemaProfile，不接执行工具。
- 用户确认下载 SOP 基本复用 data.sop_change_step 四阶段。适配保留步骤顺序、字段 Pointer 和原文快照，排除 result_confirm/说明、历史更新人员时间和整单执行结果；change_scene/change_notes 仅作场景背景。未知结构通过显式 sections 接口映射，避免猜测静默丢失。指导/回填混合字段语义仍待内网核对。
- 来源库、质量报告和当前标准稿件使用 knowledge.db 三张独立表，不自动加入已批准知识卡；每批最多 500 源，内容幂等，单请求/CLI 导入事务提交。复用 Runtime 后台任务、取消和已成功分析续做；每源分析一次调用，生成一次调用，无隐藏重试或规则结果冒充模型。评分等权六维，明确只评价书写，不证明技术质量。
- 模型默认关闭，STANDARD_SOP_GENERATION_ENABLED=true 显式启用；STANDARD_SOP_TIMEOUT_SECONDS 默认 240。同进程分析/生成串行、模型路由独占，单源分析 60,000 字符、选定生成来源 160,000 字符预算，超限拒绝而不截断。
- 每条指导须引用人工选定来源中的章节原文；不存在引用、未选来源等拒绝保存，显式缺口/冲突及空节阻断批准。编辑增加 revision 并撤销审批，旧版本审核拒绝。当前稿原位替换，无完整历史 revision 浏览。引用存在性校验不证明语义、版本兼容、步骤依赖或实例泛化，人工仍须技术核查。
- 预留 POST /api/standard-sop/batches、/batches/{id}/sources、/batches/{id}/upload 等推送契约；已有内网爬虫负责认证、搜索和下载，平台不访问 source_ref。提供 sop-import --directory CLI，页面小包上传避免逐文件请求限流。详细接口、限制及内网命令见 docs/standard-sop.md。
- 一键全库清空同时删除标准 SOP 批次/来源/报告/草案，沿用原三库备份与忙碌保护；原实例化草案仍保留并失效，Runtime 历史及导出文件保留。
- README、部署指南、共享上下文与设计决策已更新；版本 1.9.0，新增运行截图 docs/images/v1.9-standard-sop-draft.jpg。截图、测试和 CLI 验证全部使用独立合成数据与测试模型固定响应，没有真实内网输入或外部模型调用；测试来源不预置到应用。
- 浏览器修复了任务 GET 返回裸 Run 与 POST 返回 {run} 的读取差异、批次/草案切换残留及在途轮询旧结果、草案标题布局；两个导出按钮独立下载，避免一次点击多文件限制。

## Validation

2026-10-13 新电脑交接整理实测：

- 新建独立 venv，Python 3.10.12；按 constraints/base.txt 和 editable 安装成功：ops-knowledge-studio 1.9.0、pypdf 6.15.0、typing_extensions 4.16.0；pip 26.2.1、setuptools 84.0.0。pip check 通过，依赖约束在新环境中完成实际验证。
- 本机默认 pip 镜像未找到 setuptools>=83；单次指定官方 PyPI 后可获取。本次沙箱默认临时路径在 build isolation 中报 WinError 5，改用仓库 artifacts 下的 TEMP/TMP 后安装成功。未更改系统代理、pip 全局配置或共享 Conda 环境。
- 完整 unittest：184 tests，42.939 秒，OK，退出码 0。使用独立合成测试库、当前终端回环 NO_PROXY 与 artifacts 下临时目录；日志位于 artifacts/handoff-validation/unittest.log，不提交 Git。
- CLI / HTTP smoke 使用单独 .env.example 副本和空库：init、页面包含标准 SOP、/api/health/live、/api/health、空批次列表均通过；POST 安全退出返回 202 后服务自然退出，进程退出码 0。模型配置为空、外部记忆关闭，没有外部模型调用。
- 同一隔离 smoke 库中，121 份合成 JSON 的 sop-import 目录导入成功，向原批次再导入跳过 121 份重复；standard_sop_sources=121，普通 cards=0，确认独立来源库边界。
- compileall、node --check、文档相对链接检查与 git diff --check 通过。验证机器是 Windows / Python 3.10.12；未在实际新电脑、Python 3.12 或完整 CI 矩阵重新执行，也未做本轮浏览器交互或 OCR / 依赖漏洞审计。

1.9.0 功能开发时的历史验证：完整 184 tests、标准 SOP 专项 11 tests、合成浏览器分析/评分/生成/审批导出/编辑撤审/清除批次检查（控制台 error=0），以及离线 wheel 内容排除检查。历史截图来自合成固定模型，不代表真实质量；本轮没有重新拍摄或重跑浏览器交互。

## Known Issues

- 只读复核已用合成数据确认两处草案边界缺口，尚未修复：模型将 `policy=empty` 的步骤核验字段列入 `missing_fields` 时，装配置空后仍错误阻断审核；旧 revision 的审核/导出复核仅检查通用空值集合，没有按 Profile 严格核对字段类型，字符串确认说明为数字 `0` 时仍可通过。正常生成/编辑的装配路径已有类型校验。
- `operate_verified`、`operate_rollback`、`impact_analysis` 是事前指导、事后回填还是混合内容，仍缺内网页面控件或同一步骤前后快照证据；当前不按关键词删除或重分类。
- CASE_CONTEXT 白名单与真实字段名/嵌套位置仍不完全一致。`change_plan[0]` 的非 `result` 字段尚未按计划约束、历史审计和展示信息完成业务分类，不直接加入知识正文或规划检索。
- `result_confirm` 的正常/异常枚举含义、步骤三个子页是否分别保存核验结果、处理记录来源和“4号提权方式”仍待内网人工确认。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组。

## Next Step

新外网电脑按 docs/NEW_COMPUTER_HANDOFF.md 显式克隆 codex/standard-sop-studio，重建独立 Python 环境和本地配置，先完成合成回归与空库启动验收。只接续代码开发不需要旧数据库、旧密钥、旧聊天或旧 Conda 环境；需要保留合成开发状态时再按停服、同批三库和来源原文件步骤迁移。

产品验证方面，内网人工更新本功能分支后先停服备份并执行 python run.py init，再重启。导入已下载的 SOP，先用少量来源核对结构与字段语义、开启批准的模型路由后核查质量排序/来源兼容/泛化效果，再扩大到 121 份。按 docs/standard-sop.md 将现有爬虫结果推送同一场景批次；只反馈脱敏计数、错误类别与 QA 状态，真实数据和 Prompt 留在内网。

旧实例化草案的两处边界缺口仍未修复，本次标准 SOP 模块不代表这些问题已解决。当前无自动上游抓取、来源真实性认证、完整草案历史版本、客观质量标定或技术正确性自动验证。

## Last Updated

- Date: 2026-10-13 (Asia/Shanghai)
- Current branch: codex/standard-sop-studio
- Package version: 1.9.0
- Publication: 本轮交接文档仅在 codex/standard-sop-studio 提交和发布；功能代码基线 5b2dde15a1bd1c6888b6e8d8dbcd8f58d626791e，交接文档最新提交以 git log 为准。开始整理时已核对远端功能分支与该基线一致，github/main 为 4d963c761d4a1846e0a9f0ff34c6c9800b55698f。推送后核对功能分支本地与远端 SHA，并确认 main 未更新；不以文档整理为由自动合并 main。
