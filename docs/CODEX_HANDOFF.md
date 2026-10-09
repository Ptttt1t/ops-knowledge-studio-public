# Current Handoff

## Current Objective

完成 1.9.0 标准 SOP 模块，优先从已上线 SOP 中分析好写法并编写标准模板。用户本轮最新要求：新建 codex/standard-sop-studio 分支，验证后提交并推送该分支，不更新 main；本轮要求优先于长期自动发布 main 授权。

## Completed

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

- 完整 unittest：184 tests，49.253 秒，OK，退出码 0。之后只补充 multipart 上传测试断言，标准 SOP 专项 11 tests 再通过，1.774 秒；完整测试之后生产代码未改动。
- 专项覆盖：121 源导入与去重、坏来源请求原子性、原始结构适配及 Pointer 解析、质量引用/整数类型、超时停止/成功报告续做、未上线来源排除、模型开关/预算拒绝、并发排他、生成/冲突/审核/导出、修改撤审与旧版拒绝、全库清空、HTTP JSON/multipart 接入及 Runtime 查询裸 Run 契约。
- 另行 CLI smoke：121 份合成 JSON 目录导入成功，追加重复导入跳过 121 份，无模型调用。
- 浏览器独立合成服务实测：分析完成与评分展示、选择来源、生成七节及 7 处引用、批准并成功下载 Markdown、保存后 v2 待审且导出禁用、清除批次选择后旧草案内容清空。控制台 error 为零；来源与模型均为合成固定响应，真实质量未验证。已保存 JPEG 截图。
- node --check、compileall、git diff --check 通过；README/接入/部署文档相对链接检查通过。离线 wheel 1.9.0 构建成功，包含 standard_sop.py 与新 UI，不含测试/样例/数据库/配置秘密；生成的根 build 目录已清理。
- 测试沿用进程级 NO_PROXY=127.0.0.1,localhost,::1 及 artifacts/test-temp 临时目录。未修改用户 .env、系统代理或实际数据库，未清空真实知识。

## Known Issues

- 只读复核已用合成数据确认两处草案边界缺口，尚未修复：模型将 `policy=empty` 的步骤核验字段列入 `missing_fields` 时，装配置空后仍错误阻断审核；旧 revision 的审核/导出复核仅检查通用空值集合，没有按 Profile 严格核对字段类型，字符串确认说明为数字 `0` 时仍可通过。正常生成/编辑的装配路径已有类型校验。
- `operate_verified`、`operate_rollback`、`impact_analysis` 是事前指导、事后回填还是混合内容，仍缺内网页面控件或同一步骤前后快照证据；当前不按关键词删除或重分类。
- CASE_CONTEXT 白名单与真实字段名/嵌套位置仍不完全一致。`change_plan[0]` 的非 `result` 字段尚未按计划约束、历史审计和展示信息完成业务分类，不直接加入知识正文或规划检索。
- `result_confirm` 的正常/异常枚举含义、步骤三个子页是否分别保存核验结果、处理记录来源和“4号提权方式”仍待内网人工确认。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组。

## Next Step

内网人工更新本功能分支后先停服备份并执行 python run.py init，再重启。导入已下载的 SOP，先用少量来源核对结构与字段语义、开启批准的模型路由后核查质量排序/来源兼容/泛化效果，再扩大到 121 份。按 docs/standard-sop.md 将现有爬虫结果推送同一场景批次；只反馈脱敏计数、错误类别与 QA 状态，真实数据和 Prompt 留在内网。

旧实例化草案的两处边界缺口仍未修复，本次标准 SOP 模块不代表这些问题已解决。当前无自动上游抓取、来源真实性认证、完整草案历史版本、客观质量标定或技术正确性自动验证。

## Last Updated

- Date: 2026-10-09 (Asia/Shanghai)
- Current branch: codex/standard-sop-studio
- Package version: 1.9.0
- Publication: github/main 基点 4d963c761d4a1846e0a9f0ff34c6c9800b55698f；用户本轮要求仅发布功能分支，不合并/推送 main。推送后核对功能分支本地与远端 SHA；main 保持上述基点。
