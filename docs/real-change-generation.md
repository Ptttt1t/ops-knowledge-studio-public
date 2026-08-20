# 真实 ChangeOrder 草案生成与盲测

## 定位与边界

“真实变更生成 BETA”用于验证现有已审核知识能否生成符合真实 ChangeOrder 结构的安全草案。它与合成变更 Demo 独立：不创建模拟网络、不注册或调用真实执行工具、不执行草案，也不把生成和审核结果写回知识库。

功能默认由 `REAL_CHANGE_GENERATION_ENABLED=false` 关闭。当前共享 Demo 没有企业身份认证；页面中的申请人和审核人只是自报字段，不能证明真实身份或职责分离。只能在防火墙隔离的可信内网使用，禁止公网暴露。

## 启用前准备

### 1. 准备代表案例

先导入一份与目标内网 ChangeOrder Schema 相同的真实 JSON，并完成整包审核。案例包必须同时满足：

- 状态为 `APPROVED`；
- 内容覆盖状态为 `COMPLETE`；
- `semantic_mapping_status=CONFIRMED`；
- `safe_for_internal_index=true`；
- `action_list` 与程序派生的分组任务视图可完整对账。

### 2. 检查并激活 SchemaProfile

```powershell
python run.py change-schema-profile-inspect --case-id "change-order:<source-sha256>" |
  Out-File -Encoding utf8 .\schema_profile.json
```

检查生成文件中的路径、字段集合、JSON 类型和 policy。常用 policy：

| policy | 含义 |
| --- | --- |
| `generated` | 模型可以生成，但仍必须有输入或知识引用 |
| `input:<path>` | 只从 `RealChangeRequest` 指定路径取值，例如 `input:parameters.device_id` |
| `input_optional` | 输入存在时保留，否则按类型写空值 |
| `fixed` | 由管理员确认并固定的结构值 |
| `empty` | 按字段类型写空值 |
| `not_executed` | 写空值；状态/结果类字符串写 `NOT_EXECUTED` |

所有设备 ID、IP、端口、阈值、超时等具体参数应设为 `input:parameters.<name>`。`execution_fields` 只允许 `empty`、`not_executed` 或安全空值形式的 `fixed`，不能复制历史执行结果。

确认后激活：

```powershell
python run.py change-schema-profile-activate --file .\schema_profile.json --actor schema-admin
python run.py change-schema-profile-show
```

`schema_profile.json` 含真实字段样例和本地来源路径，只能留在内网，不能提交仓库或发给外部模型。

### 3. 启用功能

```dotenv
REAL_CHANGE_GENERATION_ENABLED=true
CHANGE_DRAFT_DB_PATH=data/change_drafts.db
CHANGE_GENERATION_MAX_CASE_BUNDLES=3
CHANGE_GENERATION_MAX_CONTEXT_CARDS=24
```

重启 `python run.py serve` 后打开左侧 **真实变更生成 BETA**。页面会同时显示功能开关、活动 Profile 和来源案例；任一条件缺失都会禁止推荐和生成。

## 正常生成流程

1. 填写目标、场景、区域、服务、对象、当前/目标状态、窗口、影响范围、约束、参数和验证要求。
2. 点击“推荐完整案例包”。系统只从完整且为 `APPROVED` 的案例包中推荐最多三个，并检查字段集合与活动 Profile 一致。
3. 人工确认 1～3 个案例包。系统按原顺序读取上下文、任务、前检、实施、验证和回退卡，排除 `EXECUTION_RESULT`；超出上下文预算时要求减少案例，不会静默截断。
4. 异步生成 `GeneratedChangeDraft`。模型只生成规范化任务、四阶段步骤、风险、缺失项和引用。
5. 程序以 `action_list` 为唯一任务主视图，派生 `change_tool_relate_action`，再做 stable JSON + SHA-256 multiset reconciliation。
6. 操作者可编辑规范化草案。每次保存都会创建新 revision、重新执行全部硬校验，并使旧审批失效。
7. 人工批准当前 revision。批准时再次确认引用卡仍为 `APPROVED` 且内容快照未漂移。
8. 只有 `REVIEW_APPROVED` revision 可以下载 `change_order_draft.json` 和 `provenance_report.json`。

状态流：

```text
GENERATING
  -> READY_FOR_REVIEW -> REVIEW_APPROVED
  -> BLOCKED
  -> GENERATION_FAILED

READY_FOR_REVIEW / REVIEW_APPROVED / REJECTED
  --编辑--> 新 revision -> READY_FOR_REVIEW 或 BLOCKED
```

模型请求失败或输出不符合协议时最多进行一次修复重试。第二次仍失败会保留检索诊断和错误并停止，不会输出部分草案或 deterministic fallback。

## 硬门禁

导出前必须全部满足：

- ChangeOrder Adapter 与活动 SchemaProfile 结构一致；
- 必填字段和 `input:<path>` 参数齐全；
- 前检、实施、验证、回退四阶段均非空；
- 扁平任务与程序派生分组视图完全对账；
- 每个任务和步骤都有输入或知识引用，输出引用覆盖率为 100%；
- 批准和导出时引用卡仍为 `APPROVED`，内容快照未漂移；
- `ExecutionResult` 只含空值或 `NOT_EXECUTED`；
- 设备 ID、IP、端口、阈值、超时等具体值由 `parameters` 引用绑定，不得从历史案例复制或由模型猜测。

原单相似度、任务/步骤匹配率、顺序相似度和专家评分只进入评测报告，不作为导出门禁。

## 留一案例盲测

盲测只把隐藏案例的 planning context 转成 `RealChangeRequest`：

- 目标案例及内容近重复案例不会进入推荐和 Prompt；
- 活动 SchemaProfile 不能来自目标或其近重复案例；
- Prompt 中只提供字段类型和 policy，不携带 Profile 样例值；
- 生成完成后才读取隐藏案例的任务和步骤作为答案；
- 全库模式和盲测模式分别记录，报告中的覆盖率是软指标。

完成后页面可下载 `evaluation_report.json`。20 份真实案例、Profile、Prompt、评测结果和内网路径均不得提交仓库；仓库测试只使用同结构的合成 fixture。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/change-schema-profile` | 查看功能开关和活动 Profile |
| `POST` | `/api/change-drafts/recommend` | 推荐完整案例包 |
| `POST` | `/api/change-drafts` | 创建草案并异步生成 |
| `GET/PATCH` | `/api/change-drafts/{id}` | 查看草案或创建新 revision |
| `POST` | `/api/change-drafts/{id}/review` | 批准或驳回当前 revision |
| `GET` | `/api/change-drafts/{id}/export` | 导出已批准草案和溯源报告 |
| `POST/GET` | `/api/change-evaluations` | 创建或列出留一案例评测 |
| `GET` | `/api/change-evaluations/{id}` | 查看评测状态和报告 |

异步请求同时返回 `run_id` 对应的运行记录，可继续通过现有 `/api/runs/{run_id}` 查看运行状态与事件。

## 正式使用前必须补齐

- 设置 `DEMO_MODE=false` 并启用真实身份认证、RBAC、职责分离与不可抵赖审计；
- 使用 HTTPS、内网反向代理、访问控制和密钥托管；
- 对接 CMDB/监控等只读事实源并验证参数来源；
- 完成预生产演练、双人复核、并发冲突、幂等性与回退失败测试；
- 另行设计真实执行系统，不能把本 BETA 的导出接口当作执行授权。
