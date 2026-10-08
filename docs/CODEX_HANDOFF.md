# Current Handoff

## Current Objective

完成 1.8.0：增加内网实验用的知识库清空按钮，删除应用内置 Mock 案例。按既有授权，本地验证及完整 diff/敏感信息检查后自动推送 github/main。

## Completed

- 知识库新增“实验数据管理 → 清空知识库”。沿用 DEMO_MODE=true 与 DEMO_FULL_RESET_ENABLED=true 两个开关；全库清空默认关闭，生产模式拒绝。确认后先备份知识/草案/Runtime 三库，再清空来源索引、知识卡、案例包、关系、审核、语义治理与导入缓存。
- 在途 Web 操作、模型请求、语义扫描、未完成 Runtime 任务、有效采集/记忆同步租约存在时拒绝清空；清空期间拒绝新写请求和重复清空，安全退出等待本次清空完成。备份失败不删除；普通事务异常回滚知识和草案变更。
- 保留原始上传文件、模型配置、报告、日志与历史任务。旧 Profile 停用，草案阻断并撤销 revision 审核，评测失效。保留外部记忆退休队列、清除本地映射，不重置卡片 ID，避免旧引用误绑定新卡。成功后刷新页面清理旧结果显示。
- 新备份位于知识库旁 knowledge-reset-backups/<UTC 时间>-<随机后缀>/，每批固定包含 knowledge.db、change_drafts.db、runtime.db。已加入 .gitignore。部署指南说明停服、保护当前数据及 WAL、同批三库恢复；不承诺 WAL 跨库断电原子性或多进程业务隔离。
- 删除样例采集面板、预览/下载/导入逻辑、/api/sample-cases 接口、catalog 和包资源。四份原 JSON 原样移至 tests/fixtures/structure_samples，仅作合成回归测试；删除未被使用的 sample_data/demo_upgrade_sop.md。升级不会自动删除任何已部署库中的旧案例。
- 更新 README、共享上下文、设计决策、字段对应与部署说明。删除已失效的样例入口截图，增加 v1.8-knowledge-reset.jpg 窄屏截图；历史 1.7 截图明确标记版本与合成数据背景。
- 本轮未读取或清空用户实际数据库，未修改 .env。所有清空验证均在独立临时合成库，无外部模型或真实执行工具调用。

## Validation

- 完整 unittest：172 tests，59.427 秒，OK，退出码 0；随后补充 revision 审核撤销/回滚和同步租约/旧退休队列断言，清空专项 8 tests 再次全部通过。生产代码未在完整测试后改动。
- 专项覆盖：多案例全清、三库备份 quick_check、备份原卡片数、草案/Profile/评测失效、同源重新导入且 ID 增长、原文件保留、备份失败不删除、删除失败回滚、繁忙拒绝、并发排他、HTTP 开关与确认要求。
- 测试环境设置 NO_PROXY=127.0.0.1,localhost,::1，TMP/TEMP 指向仓库 artifacts/test-temp。首轮系统代理导致 HTTP 502，系统临时目录的沙箱替换权限导致历史日志测试失败；上述进程级环境修正后通过，未改系统代理或日志实现。
- 浏览器实测独立合成服务：入口、确认文本、默认页面布局及窄屏布局可读；采集页不再出现内置样例，控制台未见 error。临时随机端口未列入测试配置 Origin，浏览器确认后的请求被现有 Origin 校验拒绝，未验证浏览器成功清空路径；成功清空、备份与重导入由 HTTP/服务专项测试验证。原临时沙箱服务无法跨进程连接，改在沙箱外仅监听回环地址完成 UI 检查。
- node --check、compileall、git diff --check 通过；文档相对链接检查通过，四份 fixture 与原文件内容一致。离线 wheel 构建成功，包含清空入口/实现、不含 samples 或 tests；首次构建的临时/缓存目录权限失败，通过仓库临时目录和 --no-cache-dir 解决。
- 已复核完整 diff、截图和 31 个变更路径，并检查凭据模式与备份目录忽略规则；数据库、凭据、原文件、本地工件和内网数据不进入提交。

## Known Issues

- 只读复核已用合成数据确认两处草案边界缺口，尚未修复：模型将 `policy=empty` 的步骤核验字段列入 `missing_fields` 时，装配置空后仍错误阻断审核；旧 revision 的审核/导出复核仅检查通用空值集合，没有按 Profile 严格核对字段类型，字符串确认说明为数字 `0` 时仍可通过。正常生成/编辑的装配路径已有类型校验。
- `operate_verified`、`operate_rollback`、`impact_analysis` 是事前指导、事后回填还是混合内容，仍缺内网页面控件或同一步骤前后快照证据；当前不按关键词删除或重分类。
- CASE_CONTEXT 白名单与真实字段名/嵌套位置仍不完全一致。`change_plan[0]` 的非 `result` 字段尚未按计划约束、历史审计和展示信息完成业务分类，不直接加入知识正文或规划检索。
- `result_confirm` 的正常/异常枚举含义、步骤三个子页是否分别保存核验结果、处理记录来源和“4号提权方式”仍待内网人工确认。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组。

## Next Step

内网人工更新后，在实验部署显式开启清空开关并重启；先完成/取消在途任务，再用已获准的真实数据验证清空、重导入和对比流程。真实数据继续留在内网。

后续仍需处理两处草案边界缺口，并确认 Action/整单结果字段、类型与枚举，以及步骤子页归属；本次清空功能不代表这些缺口已解决。

## Last Updated

- Date: 2026-10-09 (Asia/Shanghai)
- Current branch: main
- Package version: 1.8.0
- Publication: 已获取最新 github/main，发布前基点为 7768fd6；推送后以本地 main 和 GitHub refs/heads/main 的 SHA 比对为准。用户自动发布授权见 AGENTS.md。
