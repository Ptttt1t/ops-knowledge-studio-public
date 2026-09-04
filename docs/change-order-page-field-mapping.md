# 变更单页面与 JSON 字段对应

## 结论与证据边界

截至 2026-09-04，现有实现不能证明 JSON 与人工操作页面完全对应，也没有完整表达逐步骤的 1+1 check 回填和单据处理历史。

本对照使用：

- 用户人工描述的页面层级、字段标签与操作流程；不含真实字段值、账号、截图、地址或工单。
- 当前 `change_order_adapter.py` 的路径/角色映射，以及 `change_order_cards.py` 的字段白名单和实际转换代码。
- `tests/test_change_order_card_builder.py` 和 `tests/fixtures/change_order_card_builder/cases.json` 中的合成字段样例。

这里没有可独立核验的完整原始真实 JSON 字段清单。因此“未见明确字段”只表示当前代码和保留的样例不能建立对应，不能断言原始接口没有该字段；内容也可能来自另一个详情接口。Builder 支持一个字段名，也不证明真实 JSON 一定使用该字段名。

早期自拟字段的五份 JSON 案例及其专用生成、评估脚本已退役，不再作为真实业务字段或页面覆盖的证据。字段基数/漂移测试和独立合成执行 Demo 仍保留。外网测试一律使用合成值。

必须区分三种差距：源 JSON 是否提供、字段与页面语义是否确认、Builder 是否已经利用。结构覆盖率、单元测试通过和合成 fixture 完整均不等于真实页面覆盖率。

## 已确认的人工流程

- 真实变更采用 1+1 check：执行人逐步操作，配合人逐步核验、截图并回填。
- 每一步有核验，不只是完成所有实施步骤后才执行“03 操作验证”。
- 异常时不得继续后续步骤；是否回退、重新操作或采用其他处置由人工决定。平台不增加真实执行、自动重试、自动回退或自动继续能力。
- 步骤内部的验证/回退内容不会改变其所属 phase；存在回退说明不等于实际回退过。
- “操作验证”的指导内容与实际核验回填需按来源字段和填写时机区分；正常/异常的人工确认不能作为未来方案的预期结果。
- 顶层“影响分析”当前处于试运行且无人填写，暂不纳入构建要求；步骤内部的同名子页仍需保留。
- “处理记录”记录申请、流转、执行至完成的操作人员和操作时间，不等于逐步骤核验记录，也不等于知识卡审核。

这些是已确认的业务边界，不表示对应数据模型已经落地。

## 案例级页面字段

下表字段默认位于 `/data/`。有相近字段时只列候选，不凭英文名称推断等价关系。

| 页面信息 | 当前字段或映射 | 覆盖情况与缺口 |
| --- | --- | --- |
| 标题、GOMSC 序列号 | `title`、`ticket_id` → `identity` | 标题/工单标识已有位置；`ticket_id` 是否就是页面的 GOMSC 序列号仍需来源确认。 |
| 变更级别 | Adapter 识别 `change_level`；Builder 支持 `severity` | `change_level` 尚未列入 Builder 的 Context 白名单，不能用 `severity` 自动替代。 |
| 变更环境 | 无明确专用映射 | 不得以 `region` 推断环境。 |
| 变更场景 | `change_scene` → `change_context` | 已有承载位置；对象内部键需以实际来源为准。 |
| 工单来源、原始系统单号 | `original_system` → `identity` | 只能证明存在原始系统字段，不能同时代表来源和原始单号。 |
| 当前责任人、抄送人 | 无明确专用映射 | `executors` / `cooperators` / `reviewers` 是不同角色，不能直接替代责任人或抄送列表。 |
| 重大变更 | Adapter 识别 `special_change_type` | 类型不等于重大变更布尔值；Builder 未纳入该键。 |
| 变更服务 | `service`、`cloud_service`、`micro_service` → `service_scope` | 已有部分服务字段；仍需确认页面各层级对应。 |
| 变更范围中的云服务、微服务组、微服务 | `cloud_service`、`service`、`micro_service` | 未确认独立的微服务组映射；不能假设 `service` 就是微服务组。 |
| 变更范围中的 Region、Zone | `region`、`zone` → `region` | Builder 有承载位置；详情链接是否返回这些字段尚未核验。 |
| 变更范围中的 Ring 类型、AZ 类型、Pod、Schema、Hosts | 无完整的专用范围映射；Builder 支持 `az`，步骤样例还有 `container`、`cluster_name` | AZ 标识不等于 AZ 类型，容器/集群参数不等于页面范围明细；不能将自由文本出现的值算作完整结构覆盖。 |
| 需求来源 | 无明确专用映射；Builder 支持 `change_reason` | 变更原因不等于需求来源。 |
| 备注信息 | Adapter 识别 `change_notes` | Builder 未纳入该键，属于识别与语义构建之间的缺口。 |
| 变更能力：手工、自动化、工具化 | Action 样例含 `capability` | Action 的能力描述不等于案例级三种变更方式，尚无确认映射。 |
| 4号提权方式 | 无确认映射 | 保留准确页面标签；含义、可选值和源键未知，不解释成 4A 或其他概念。 |
| 变更描述、变更影响 | Builder 支持 `change_content`、`impact_scope` 等键 | 可承载相近内容，但尚未确认页面两字段的实际来源键和语义。 |
| 客户感知 | `customer_sensed` → `risk_impact` | 已有承载位置；枚举含义仍须来源确认。 |
| 客户感知说明 | 无确认的独立映射 | `affected_customer` 是受影响客户，不能自动等同于感知说明。 |
| 业务面中断、管理面中断 | 无确认的两个独立字段 | 不能从整体风险等级或影响文本中推断填写值。 |
| 变更风险等级 | `risk_level`、`impact_risk_level` → `risk_impact` | 有相近字段，尚需区分页面含义，不能混同变更级别。 |
| 变更灰度 | Builder 支持 `grayscale_policy` / `gray_policy` / `canary_policy` / `batch_policy` | 有承载位置；样例或别名支持不能证明真实 Ring 字段及详情已覆盖。 |
| 变更回退时间要求 | Builder 支持 `rollback_requirement` / `rollback_policy` / `rollback_condition` | 有文本位置，未确认页面回退时长字段；预计总时长不是回退时长。 |
| 受影响服务详情 | `affected_service` → `service_scope` | 有摘要字段；详情链接的完整服务列表和范围未确认。 |
| 变更指导书 | Adapter 识别 `change_guide`、`change_guides` | Builder 未纳入这些键，指导书及其附件不能算作已完整进入 Context。 |
| 客户授权、通知客户 | Builder 支持 `authorization`、`notification`；Adapter 还按关键词识别同类键 | 只支持部分名字，未建立页面精确对应；Adapter 识别的其他别名不会自动进入 Builder 白名单。 |
| 变更 SOP 模板 | `/data/sop_change_step` 有步骤 | 步骤内容不等于模板 ID、名称、版本和附件；这些模板信息未确认。 |
| Action & 命令 | `/data/action_list`、`/data/change_tool_relate_action`；步骤的 `operate_commond`、`command_list` | Action 双视图及步骤命令已有结构；页面命令清单与各步骤的准确关联仍需确认，不把 Action 列表当有序 Procedure。 |

## 变更实施：阶段与步骤内部子页

四阶段路径已有映射：

| 页面阶段 | JSON 路径 | procedure_group |
| --- | --- | --- |
| 01 操作检测 | `/data/sop_change_step/check_before_change` | `PRECHECK` |
| 02 操作处理 | `/data/sop_change_step/change_implement` | `IMPLEMENTATION` |
| 03 操作验证 | `/data/sop_change_step/change_verified` | `VALIDATION` |
| 04 操作回退 | `/data/sop_change_step/change_rollback` | `ROLLBACK` |

下表字段位于各阶段数组的单个步骤对象内。

| 页面信息 | 当前字段或转换 | 覆盖情况与缺口 |
| --- | --- | --- |
| 步骤名称、操作步骤 | `check_name` → `title`；`operate_description` → `operation` | 已有主要文本映射，保持源步骤顺序。 |
| 选择 Action | `_procedure_actions()` 按顺序读取 `tool_related_actions`、`action_unique_ids`、`actions` | 空列表不会再遮蔽后续候选；真实填充形式及页面选择值仍需核验，不根据“有数组”就视为完整关联。 |
| 有/无可变参数 | 无确认的专用开关 | `instance_parameters` 是从已识别键或操作文本取得的实例参数，不能代替页面是否有可变参数的选择。 |
| 操作验证 | `operate_verified` → `validation` | 当前是直接文本映射，尚未按指导要求和实际核验回填分离。 |
| 结果确认、核验说明 | 合成步骤样例含 `result_confirm`、`result_confirm_notes` | 当前留在 `source_metadata`，没有形成步骤级 Outcome；草案生成将两者确定性置空并用硬门禁阻止模型或草案编辑填入。正常/异常枚举含义仍待确认。 |
| 影响分析文字、涉及/不涉及 | `impact_analysis` → `impact_analysis`；样例含 `is_involved_step` | 单个涉及标记尚未确定属于哪个子页，不能同时当作影响分析和回退的两个开关。 |
| 影响分析子页的结果确认及证据 | 没有明确的独立转换 | 不能证明与操作实施子页的结果确认分别保存、分别关联。 |
| 操作回退说明、涉及/不涉及 | `operate_rollback` → `rollback`；`is_involved_step` 仅留元数据 | 有文本，未区分计划回退、是否需要回退、是否实际执行回退。 |
| 回退子页的结果确认及证据 | 没有明确的独立转换 | 不能用单一确认说明证明各子页核验都已覆盖。 |
| 图片、文字证据 | 对主要富文本字段执行清洗，图片留 attachment/evidence metadata | 尚未建立每张截图与子页、核验人、核验时间的完整关联；元数据保留不等于完成证据建模。 |
| 步骤 ID、更新人员和时间 | 样例中的 `sop_step_id`、`update_user`、`update_time` 留在 `source_metadata` | 来源步骤身份可追溯；更新人员/时间不能被解释为执行人、核验人及各自操作时间，也不允许模型在新草案中生成。 |
| 异常后的人工决定与可能的多次核验 | 尚无独立模型 | 不应凭最后一个正常状态重建或掩盖先前异常；在没有历史源数据时不得制造事件序列。 |

当前每个步骤仍只生成 Procedure 或其互斥子卡。新增步骤执行记录是待设计方向，未在此次样例清理中实现。

## 顶层页面与整单结果

- 顶层“影响分析”：按人工反馈暂不纳入必需覆盖范围，其未填写不计为内容缺陷；不能因此忽略步骤内的影响分析。
- 顶层“处理记录”：当前没有已确认的申请、流转、执行、完成事件列表映射。`create_time`、`update_time` 或人员列表不能替代完整流程记录。
- `/data/change_plan/0/result`：当前独立构建 `EXECUTION_OUTCOME`，禁止进入规划 RAG。该整单结果位置不能自动证明包含所有步骤的核验记录，也不能替代处理记录。
- 页面 `--`：需要核验源数据表示为缺席、null、空串还是占位文本，不能自动等同于 false、“不涉及”或“正常”。当前覆盖报告不能用于确认这些页面语义。

## 后续核对与实现顺序

1. 由内网人工核对已有 JSON 的字段名、路径、类型、枚举含义和填写阶段；仅反馈允许的脱敏结构信息，不传真实 JSON、SchemaProfile、人员、图片、URL 或结果。
2. 优先核对步骤中三个子页各自的“涉及/不涉及”“结果正常/异常”、验证说明和图片绑定。若来自独立详情接口，明确标为本次输入未包含，而非解析器丢失。
3. 核对变更范围详情和处理记录的来源；再补齐其余案例级字段。未确认含义的“4号提权方式”保持待确认。
4. 在字段对应明确后，用合成值编写 fixture，再调整 Adapter/Builder，并验证新草案不会携带历史人工核验结果。不要靠猜新键、LLM 推断或增加字段总数来宣称补齐覆盖。

本次只清理过时样例并记录对照，不改变 Adapter、Card Builder、真实草案装配或执行能力。
