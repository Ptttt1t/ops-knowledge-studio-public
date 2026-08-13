# 拟真 ChangeOrder JSON 样例

这里的五份工单全部是合成、脱敏、仅用于内部 Demo 的数据。它们不含真实客户、账号、资源、地址、密钥或执行命令，不能直接用于生产变更。

样例遵循当前已确认的 ChangeOrder v2 外层结构：

- `/data/action_list`：13-field TaskRecord 的唯一主视图；
- `/data/change_tool_relate_action`：同一批任务的分组投影；
- `/data/sop_change_step/check_before_change`：前置检查；
- `/data/sop_change_step/change_implement`：实施步骤；
- `/data/sop_change_step/change_verified`：验证步骤；
- `/data/sop_change_step/change_rollback`：回退步骤；
- `/data/change_plan/0/result`：15-field 历史执行结果。

五个案例覆盖专线路由、NAT 出口、IPsec VPN、安全组微隔离和中转路由域迁移。后两个案例分别包含 12 和 14 个实施步骤；VPN 与中转路由域案例包含执行失败后成功回退的历史结果。

新版结构化抽取会为每条 canonical task 和 ProcedureStep 保存 JSON Pointer、字符范围与 SHA-256，任务/步骤列表不再由模型决定删减或重排。评估报告中的 `content_coverage` 和 `structured_evidence` 可用于核对逐源记录是否全部映射。

重新生成：

```powershell
python scripts/generate_realistic_change_orders.py
```

只检查结构映射、任务对账和切块边界：

```powershell
python scripts/evaluate_realistic_change_orders.py `
  --mode structure `
  --output-dir artifacts/realistic-change-orders-structure
```

不联网运行完整的抽取、质量门禁和 SQLite 持久化闭环：

```powershell
python scripts/evaluate_realistic_change_orders.py `
  --mode offline `
  --output-dir artifacts/realistic-change-orders-offline
```

如需使用已有模型配置做真实抽取，请显式提供 `.env`，输出仍写入独立目录：

```powershell
python scripts/evaluate_realistic_change_orders.py `
  --mode model `
  --env C:\path\to\model.env `
  --output-dir artifacts/realistic-change-orders-model
```
