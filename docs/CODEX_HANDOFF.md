# Current Handoff

## Current Objective

退役“变更方案生成 DEMO”及旧十个合成云网络案例，以四份按已确认结构编排的合成 JSON 替代，并统一工作台 UI。真实草案 BETA 保留生成、审核和导出边界。

## Completed

- 删除 change_management 整个包、change_web 管理器、Demo CLI/API、会话额度配置、模拟执行/结果页面、旧十份 Markdown、专用测试与旧案例驱动的实验/导出脚本。旧接口返回 404，启动不再自动预置已批准案例；通用 Runtime、知识问答、长期记忆、安全退出等保留。
- 新增 knowledge_platform/static/samples/ 下四份 JSON：容量调整、配置更新、证书轮换、监控阈值调整。外层四阶段路径及步骤 20 个字段名使用已确认脱敏信息；Action 13 字段、整单结果 15 字段和部分类型由用户允许暂拟，样例、UI 和 README 都明确标注，不能作为真实接口契约。
- 知识采集增加样例预览、JSON 下载、标准导入入口；可信问答移入知识库。统一导航分组、图标、首页入口、资产卡、表单、审核、草案、关系图、弹窗和窄屏布局，不加前端依赖或外部资源。
- 修复生命周期图的内联 width 被 CSP 拦截后所有状态显示满条：使用原生 progress 的 value/max，零值正确显示为空。
- 本机服务排空停止后，SQLite backup 备份旧库，精确移除旧 10 案例和 3 份 Demo 执行反馈；未发现其他来源文档。通过标准确定性采集路径导入四份新样例，0 次模型调用，产生 4 个案例包和 24 张 PENDING_REVIEW 卡，未自动批准。原 .env 未改变，当前未配置模型 API；BETA 未启用。新服务已启动于 http://127.0.0.1:8765/。
- 此本地数据替换不是自动数据库迁移；其他部署已有历史数据不会被升级程序自动删除。先备份再按来源清理，禁止全库重置。
- 版本 1.7.0。上下文、决策、README 与字段对照同步更新；旧功能截图明确标注历史版本，删除 Demo 专属截图。

## Validation

- 完整测试：D:/Anaconda/envs/zte/python.exe -m unittest discover -s tests -t . -q，165 tests，40.215 秒，OK，退出码 0。数量下降来自删除已退役模块测试，增加样例端到端回归，保留长期记忆批次和通用安全测试。
- 样例验证：四份均识别为 change_order_shape_v2，每份 Context 1 + Procedure 4 + Outcome 1；Action 引用保留、Outcome 排除规划生成、重复导入幂等、无模型调用；来源证据完整，全部待审核。
- Web 验证：旧 GET/POST Demo 路由 404，样例读取与路径越界拒绝正常；浏览器实际检查桌面/窄屏首页、样例预览、审核四包、问答迁移、BETA 禁用状态和关系图（40 节点/72 边）；刷新后的控制台无 error，生命周期 value/max 正确。
- node --check、compileall、git diff --check 通过。离线构建 wheel 成功，确认含四份 JSON、catalog、README，且无 change_management/change_web。
- 无真实内网数据、API key、数据库、上传文件或本地工件进入提交；未调用外部模型或真实执行工具。

## Known Issues

- 只读复核已用合成数据确认两处草案边界缺口，尚未修复：模型将 `policy=empty` 的步骤核验字段列入 `missing_fields` 时，装配置空后仍错误阻断审核；旧 revision 的审核/导出复核仅检查通用空值集合，没有按 Profile 严格核对字段类型，字符串确认说明为数字 `0` 时仍可通过。正常生成/编辑的装配路径已有类型校验。
- `operate_verified`、`operate_rollback`、`impact_analysis` 是事前指导、事后回填还是混合内容，仍缺内网页面控件或同一步骤前后快照证据；当前不按关键词删除或重分类。
- CASE_CONTEXT 白名单与真实字段名/嵌套位置仍不完全一致。`change_plan[0]` 的非 `result` 字段尚未按计划约束、历史审计和展示信息完成业务分类，不直接加入知识正文或规划检索。
- `result_confirm` 的正常/异常枚举含义、步骤三个子页是否分别保存核验结果、处理记录来源和“4号提权方式”仍待内网人工确认。
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组；README 相似治理截图仍待更新。

## Next Step

等待用户确认 Action/整单结果字段名、类型、枚举及步骤子页归属，再替换四份暂拟 JSON。当前可在本地审核队列逐包查看和审核。既有草案边界问题见上，不在本次 UI/模块清理范围内。

## Last Updated

- Date: 2026-09-07 (Asia/Shanghai)
- Current branch: main
- Package version: 1.7.0
- Publication: 用户已于 2026-09-07 明确授权发布本轮修改，并授权后续本地验证和敏感信息检查通过后自动推送 GitHub main；已写入 AGENTS.md。实现提交为 65054cd；发布完成后以本地 main 与 GitHub refs/heads/main 的 SHA 比对为准。本轮发布操作仅补充授权与交接文档，沿用上述已通过的 165 项测试，无实现变更。
