# 四份合成 ChangeOrder 结构样例

替代已退役的十份云网络模拟案例。容量调整、配置分批更新、证书轮换、监控阈值调整各一份。所有值均为虚构，不含真实内网数据。

## 已确认与暂拟部分

- 已确认外层路径：action_list、change_tool_relate_action、sop_change_step 四阶段、change_plan/0/result。
- 已确认步骤 20 个字段名：check_name、operate_description、operate_verified、operate_rollback、impact_analysis、operate_commond、command_list、tool_related_actions、action_unique_ids、tool_unique_ids、is_involved_step、result_confirm、result_confirm_notes、label、annotation、operate_doc_list、standardized_items、sop_step_id、update_time、update_user。移除了旧 fixture 为凑数加入的 region/container/cluster_name；不加入报告勘误排除的 actions/action_risk_level。
- **暂拟部分**：Action 的 13 个键、整单结果的 15 个键，沿用通用合成测试形状；字段名称、值类型、枚举均不代表真实接口。用户已允许在真实字段确认前暂拟，后续在这四份 JSON 集中替换。步骤辅助键的类型/值、Action 引用 ID 的表示也仍属合成设定。
- /data 的上下文只使用已报告存在的字段；change_plan 的未知兄弟字段不猜测业务含义，不为凑齐总数扩展。JSON 能被现有 Adapter 识别不代表完整真实接口契约或页面覆盖。

## 业务流程

每份保持操作检测、处理、验证、回退四阶段顺序。各步骤分别给出操作、验证、影响分析和回退指导。配合人逐步核验，异常停止，由人工决定后续处置；回退阶段的存在不代表实际回退。result_confirm 等使用带 SYNTH 标记的虚构历史回填，回退步骤保留 null；这些值只能作来源证据，不得复制到新草案。SYNTH_OK 不是真实状态枚举。

平台不自动批准样例，也不注册执行工具。知识采集页提供 JSON 预览、下载和标准导入；导入后到审核队列整包审核。尚未确认的三个子页各自核验、处理记录和“4号提权方式”不伪装成已实现。细节见 docs/change-order-page-field-mapping.md。
