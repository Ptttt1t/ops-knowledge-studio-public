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

ProcedureStep 中的 `result_confirm`、`result_confirm_notes`、`update_user`、`update_time` 固定使用 `empty`。这些字段保留 Profile 声明的 JSON 类型，但不由模型生成；人工或源系统只能在草案生成流程之外按正式流程填写。生成服务也会对已有活动 Profile 应用相同的有效策略，因此升级后不依赖修改数据库中的旧 Profile 才能获得保护。Profile 查看接口显示有效策略。

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
CHANGE_GENERATION_MAX_OUTPUT_TOKENS=8192
CHANGE_DRAFT_TIMEOUT_SECONDS=240
DEEPSEEK_MODEL_MAX_CONCURRENCY=2
CHANGE_DRAFT_MODEL_MAX_CONCURRENCY=1
CHANGE_DRAFT_FALLBACK_MODEL=
CHANGE_DRAFT_FALLBACK_MODEL_MAX_CONCURRENCY=2
```

`DEEPSEEK_MODEL_MAX_CONCURRENCY` 是同一 Python 进程内按 base URL、API Key 指纹和实际 model ID 共享的主模型门禁，默认允许普通模型路径并发 `2`。`CHANGE_DRAFT_MODEL_MAX_CONCURRENCY` 继续固定为 `1`；真实草案请求会取得所选模型路由的独占槽位，等待该模型正在运行的抽取/问答结束，并阻止新的同模型请求在本次调用期间进入。不同模型使用独立门禁，因此备用 Qwen 可以配置自己的上限。

`CHANGE_DRAFT_FALLBACK_MODEL` 留空时禁用模型切换。启用时必须填写当前 DeepSeek-compatible 网关接受的精确 Qwen model ID，而不是界面展示名；第一版备用模型与主模型共用 `DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY`。主模型只有在 HTTP 429、错误包含 `count/limit`、账户限额或等价容量信号时，才会切换备用模型一次。协议解析、内容错误和硬门禁失败不会触发换模型。实际选用模型、是否回退和尝试顺序写入 revision 的 `model_usage.model_routing`。

`CHANGE_DRAFT_TIMEOUT_SECONDS` 是真实草案专用的客户端等待上限，代码默认 `240`，也可直接在部署环境的 `.env` 中覆盖，例如 `CHANGE_DRAFT_TIMEOUT_SECONDS=360`；修改后需要重启服务。普通抽取、问答和其他 DeepSeek 请求仍使用 `DEEPSEEK_TIMEOUT_SECONDS`（默认 `120`）。该时间包含客户端等待网关排队和生成完成的总时间，客户端无法据此拆分 queue 与 inference 耗时。

草案调用显式设置 HTTP retry 为 `0`，避免全局 `DEEPSEEK_MAX_RETRIES=2` 将一次草案尝试放大为三次网关请求。只有供应商已经返回正文但 JSON 无法解析，或者 JSON 可解析但模型负责的 `GeneratedChangeDraft` 协议不合规时，程序才允许一次独立协议修复。`RealChangeRequest` 与 Profile 的字段/类型绑定属于确定性输入错误，会在模型调用前预检；它即使在装配阶段暴露也立即失败，错误码为 `REQUEST_PROFILE_BINDING_ERROR`，不会进入协议修复。超时、连接失败和非容量 HTTP 错误同样不得作为协议修复再次请求；超时也不自动切换备用模型，因为客户端超时不能证明供应商侧原推理已经结束。若主、备模型都不可用，则保留错误并停止，不输出部分草案或规则兜底。

每个草案根记录和当前 revision 的 `model_usage` 会保存 `generation_budget` / `generation_performance`。字段包含模型、timeout、案例和上下文卡计数、Prompt 字符/字节数、最大输出 token、模型请求次数、起止时间、客户端等待耗时和响应大小；只有供应商可靠返回 usage 时才保存 token 计数。Runtime 成功或失败 checkpoint 也保留同一组统计，但不保存 Prompt 或完整 payload。达到草案等待上限时错误码为 `MODEL_TIMEOUT`，页面显示“真实变更草案生成超过当前 N 秒等待上限，本次未自动重试”。

诊断接口：

```text
GET /api/health
→ model_requests:
  process_id
  max_concurrency
  active_requests
  waiting_requests
  waiting_exclusive_requests
  exclusive_active
  peak_active_requests
  total_requests
  routes:
    <model-id>: <该模型的上述门禁状态>
```

该协调器是进程级而不是跨主机锁。如果网关仍报告 `count > limit`，而对应 model route 显示本进程 `active_requests <= max_concurrency`，应检查是否残留多个平台服务进程、另一台机器或网关尚未释放的旧请求。部署升级时应先正常停止旧服务，再只启动一个新服务实例。

重启 `python run.py serve` 后打开左侧 **真实变更生成 BETA**。页面会同时显示功能开关、活动 Profile 和来源案例；任一条件缺失都会禁止推荐和生成。

## 正常生成流程

1. 填写目标、场景中文名、场景英文名、场景唯一标识、区域、服务、对象、当前/目标状态、窗口、影响范围、约束、参数和验证要求。页面“本次变更具体参数 JSON”会原样进入 `request.parameters`；新 IP、账号、端口、阈值和资源规格必须在这里显式提供。API 请求使用 `scenario_reference={name_cn,name_en,unique_id}`；兼容旧客户端的高级方式是 `parameters.change_scene` 对象。
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

模型已经返回内容但输出不符合 JSON/`GeneratedChangeDraft` 协议时，最多进行一次修复重试。输入/Profile 类型冲突不是模型协议问题：程序会在远程调用前阻断；缺失的结构化场景值进入 revision 的 `missing_fields` 并阻断审核；第二次协议修复仍失败时保留检索诊断和错误并停止，不会输出部分草案或 deterministic fallback。`generation_diagnostics.protocol_repairs` 会记录触发第二次请求的 `JSON_PARSE` 或 `DRAFT_ASSEMBLY` 阶段、错误码和截断原因，因此不能再仅凭请求数猜测重试原因。

## 硬门禁

导出前必须全部满足：

- ChangeOrder Adapter 与活动 SchemaProfile 结构一致；
- `generated` 字段存在且 Profile 类型为 `null`、值为 JSON `null` 时视为合法完成，不进入缺失项；字段缺席或 string/number 等非 null 类型为空仍进入缺失项；
- Profile 为对象的 `change_scene` 按 `sample` 的键和子类型确定性装配；样例值不复制。样例中非空但本次未输入的子字段必须出现在缺失项中；
- 必填字段和 `input:<path>` 参数齐全；
- 前检、实施、验证、回退四阶段均非空；
- 扁平任务与程序派生分组视图完全对账；
- 每个任务和步骤都有输入或知识引用，输出引用覆盖率为 100%；
- 批准和导出时引用卡仍为 `APPROVED`，内容快照未漂移；
- `ExecutionResult` 只含空值或 `NOT_EXECUTED`；
- 四阶段每个步骤的 `result_confirm`、`result_confirm_notes`、`update_user`、`update_time` 只能保持类型安全空值；模型输出和人工编辑都不能在本 BETA 中填入这些执行后字段；
- 设备 ID、IP、端口、阈值、超时等具体值由 `parameters` 引用绑定，不得从历史案例复制或由模型猜测。

生成 Prompt 明确规定 `request` 是本次事实源、`approved_cards` 只是模板和证据；输入值优先于历史卡片值。若 `request.parameters={}`，页面会提示操作者确认本次确实不需要具体参数。TaskRecord 的动作/能力身份字段可以继续引用卡片，它们不等同于本次设备或资源参数。

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
