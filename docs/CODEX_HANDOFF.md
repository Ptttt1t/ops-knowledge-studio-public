# Current Handoff

## Current Objective

完善 1.7.0 README：纳入已收集的真实变更页面脱敏结构，并展示当前工作台的合成数据运行截图。区分页面信息、已确认 JSON 字段与平台实际覆盖，不将本地截图冒充内网原系统截图。

## Completed

- README 已完成重写，并补充案例级页面信息、四阶段 JSON 路径、单步骤三个子页、1+1 check 人工流程、20 个已报告步骤字段名，以及顶层影响分析、处理记录和整单结果的边界。主题分组不冒充原页面布局，Action 13/结果 15 的未确认字段继续明确暂拟。
- 新增 docs/images/v1.7-*.jpg 四张浏览器原始运行截图：工作台概览、四份样例采集、合成案例包详情和知识关系图。均为 1265×712，保留原始 JPEG 字节，逐图检查后引用；README 明示拍摄日期、合成数据来源、当前审核状态和 COMPLETE 的覆盖范围。
- 本轮仅修改 README、交接文档并新增四张截图；未修改实现、.env、样例内容或卡片审核状态。
- 删除 change_management 整个包、change_web 管理器、Demo CLI/API、会话额度配置、模拟执行/结果页面、旧十份 Markdown、专用测试与旧案例驱动的实验/导出脚本。旧接口返回 404，启动不再自动预置已批准案例；通用 Runtime、知识问答、长期记忆、安全退出等保留。
- 新增 knowledge_platform/static/samples/ 下四份 JSON：容量调整、配置更新、证书轮换、监控阈值调整。外层四阶段路径及步骤 20 个字段名使用已确认脱敏信息；Action 13 字段、整单结果 15 字段和部分类型由用户允许暂拟，样例、UI 和 README 都明确标注，不能作为真实接口契约。
- 知识采集增加样例预览、JSON 下载、标准导入入口；可信问答移入知识库。统一导航分组、图标、首页入口、资产卡、表单、审核、草案、关系图、弹窗和窄屏布局，不加前端依赖或外部资源。
- 修复生命周期图的内联 width 被 CSP 拦截后所有状态显示满条：使用原生 progress 的 value/max，零值正确显示为空。
- 1.7.0 升级时曾备份旧库后按来源移除旧 10 案例和 3 份 Demo 反馈，再导入四份新样例，0 次模型调用、24 张待审核卡。本轮截图前发现本地服务未运行，使用 scripts/start_server.py 启动于 http://127.0.0.1:8765/；浏览器实测当前为 4 个来源、24 张卡，其中 12 张已批准、12 张待审核，本轮未进行批准或驳回。API 未配置，BETA 关闭且 Profile 未激活。
- 此本地数据替换不是自动数据库迁移；其他部署已有历史数据不会被升级程序自动删除。先备份再按来源清理，禁止全库重置。
- 版本仍为 1.7.0；稳定上下文、决策和字段对照继续适用。README 使用本轮运行截图，不再展示旧版本截图。

## Validation

- 本轮验证：26 处 README 相对链接有效，20 个步骤键与脱敏字段报告集合完全一致，JSON 路径均可在字段对照中找到；代码围栏和 details 标签配对正常。四张 JPEG 格式、尺寸与可读性检查通过，合计约 262 KiB；逐图确认只展示合成内容，无凭据或内网原始截图。运行界面概览、采集、案例包、关系图与 BETA 关闭状态实际检查通过，浏览器控制台无 error；关系图为 40 节点、72 边。完整 diff、敏感信息及 git diff --check 检查通过。本轮未重新安装依赖或运行完整业务测试。
- 以下为 1.7.0 实现提交 65054cd 的既有验证记录，本轮没有重复执行：
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
- 既有语义治理仍不支持单个新案例追加到已接受 canonical，也不支持一次把候选簇拆成多组。

## Next Step

后续优先处理已记录的两处草案边界问题；待用户确认 Action/整单结果字段名、类型、枚举及步骤子页归属后，替换四份暂拟 JSON。本轮 README 重写不代表这些实现缺口已解决。

## Last Updated

- Date: 2026-09-09 (Asia/Shanghai)
- Current branch: main
- Package version: 1.7.0
- Publication: 用户已授权本地验证及完整 diff、敏感信息检查通过后自动推送 GitHub main，约定见 AGENTS.md。本轮为 README 结构说明、运行截图与交接更新，发布前基点为 e462ae0；发布后以本地 main 与 GitHub refs/heads/main 的 SHA 比对为准。
