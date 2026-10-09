# 标准化 SOP 工作台与爬虫对接

当前流程面向某个需求/场景，收集已上线 SOP，评审书写质量，再编写带来源引用的标准模板。它与实例化 ChangeOrder BETA 独立，不要求 SchemaProfile，不接执行工具。当前验证使用合成数据及测试模型；真实冷补丁 SOP 的适配、质量排序和生成效果须留在内网验证。

## 内网先使用已下载的 121 份 JSON

进入“标准化 SOP”，建立场景批次，场景应说明产品、版本、适用/排除条件，填写采集关键词。多选 JSON 文件，确认它们已在上游上线后导入。单批最多 500 份；页面将文件合成小包上传，减少逐文件请求。导入无需模型，也不会自动进入已批准知识卡库。

也可在内网仓库根目录执行：

```text
python run.py sop-import --directory "D:/sop-downloads" --scenario "指定产品与版本范围的冷补丁标准操作" --keywords "冷补丁" --published
```

输出 `batch_id`。追加来源使用 `--batch-id <batch_id>`；已有批次的场景保持原值。目录只读取第一层 `.json` 文件。标题/结构无法识别时会明确失败，需要内网适配；一次导入请求或 CLI 导入在同一事务内提交，失败不留下该次请求的部分来源，先前成功提交的包保留。

在批准的模型环境设置并重启：

```dotenv
STANDARD_SOP_GENERATION_ENABLED=true
STANDARD_SOP_TIMEOUT_SECONDS=240
```

模型配置复用 `DEEPSEEK_*`。真实来源、Prompt、输出与评测只留在批准的内网环境及模型路由，不得上传外网或公开仓库。默认不开启模型分析/生成，导入和结构检查始终可用。

点击“分析未完成来源”：逐份处理已声明上线的来源，每份一次模型调用，后台可查询进度和取消。121 份首次完整分析最多 121 次调用，生成一次再增加一次；评分耗时取决于模型。超时/协议失败停止任务，不隐藏重试，也不以规则填充模型报告。此前成功分析已保存，再点击分析会跳过它们。取消不能强制终止已经发出的模型推理，需等待返回或超时。

查看六维模型书写评分、原文优点和兼容性说明，手动勾选 1–12 份参考来源，或按评分选前 8 份后调整。推荐仅按分数排序，不保证来源兼容；生成时模型须报告不兼容之处。上下文超过预算会明确拒绝，需拆分场景/来源范围，不能静默截断。

生成后查看七节标准模板、逐条来源引用、实例参数占位符、缺口和冲突。可编辑 JSON 后保存，再填写审核说明并批准当前版本。只有已批准、结构及引用校验仍通过的当前版本可导出 Markdown 与包含来源的 JSON。编辑替换当前稿、增加 revision 并撤销审批；当前没有完整历史 revision 浏览功能。

## 已知结构适配

支持 `data.sop_change_step` 四阶段数组：`check_before_change`、`change_implement`、`change_verified`、`change_rollback`。保留步骤源顺序和字段 Pointer，抽取 `operate_description`、`operate_verified`、`operate_rollback`、`impact_analysis`、`operate_commond`、`command_list`；不将 `result_confirm`、`result_confirm_notes`、更新人员/时间或整单历史执行结果作为指导内容。富文本归一化复用现有能力。

`change_scene`、`change_notes` 暂作为场景背景，不能直接当成前置条件；只有来源明确提供的 `prerequisites` 才进入前置条件节。步骤里的验证/回退/影响字段沿用源 Pointer 和步骤名；它们的指导/回填语义仍需内网人工核对。自动识别不代表最终业务映射已完整。

未知结构可在爬虫侧适配为下面的显式章节格式，不需要把真实文件带回外网。章节 kind：`SCOPE`、`PRECONDITIONS`、`RISKS`、`PRECHECK`、`IMPLEMENTATION`、`VALIDATION`、`ROLLBACK`。来源允许部分章节缺失；生成结果必须包含七节，缺证据的空节会阻断批准。

## 爬虫接口：推送，不抓取

认证、搜索、下载、分页和上游权限由已有内网爬虫负责。工作台不保存爬虫凭据，也不访问 `source_ref` 地址。关键词在批次中记录，供适配器读取后交给现有爬虫；系统不会把“接口预留”显示成“已抓取”。

所有接口沿用平台 Bearer Token、Host、Origin 和限流。爬虫 Python 客户端可使用现有共享令牌；不从不可信内容读取令牌。以下对象均为合成示意。

1. `POST /api/standard-sop/batches`：

```json
{"scenario":"某合成产品 synthetic-v1 冷补丁标准操作","keywords":["冷补丁"]}
```

返回批次对象及 `batch_id`。

2. `POST /api/standard-sop/batches/{batch_id}/sources`：

```json
{"documents":[{"external_id":"synthetic-sop-001","source_ref":"synthetic://sop/001","published":true,"content":{"data":{"title":"合成 SOP","sop_change_step":{"check_before_change":[],"change_implement":[{"check_name":"合成实施","operate_description":"这里放原始指导正文，示例不包含可执行命令。"}],"change_verified":[],"change_rollback":[]}}}}]}
```

返回 `inserted`、`duplicates`、`total`。默认 JSON 请求上限 256 KiB，超限须分包。`published` 必须是布尔值，缺省 false；它是上游/人工声明，平台不自行证明已上线。相同规范化正文、标题与上线状态在同批次幂等；仅更换文件名或来源地址不重复建源，保留首次来源信息。正文变化形成独立来源快照。

显式章节适配的 `content` 也可为：

```json
{"title":"合成标准 SOP 来源","scope":"synthetic-service","version":"synthetic-v1","sections":[{"kind":"PRECHECK","text":"确认合成测试目标与版本范围，异常时停止并交由人工核查。"}]}
```

较大批次可使用 `POST /api/standard-sop/batches/{batch_id}/upload`，multipart `file` 为 `.json`。文件内容可为上述 `documents` 数组（不带外层对象），默认上传总上限 10 MiB；单份序列化 JSON 另受 `KNOWLEDGE_MAX_CHANGE_ORDER_JSON_CHARS` 限制，默认 500,000 字符。上传单份原结构时通过 `X-SOP-Published: true` 声明上线；数组中的每项独立声明。无 ZIP 解包或任意本地路径导入接口。

3. `POST /api/standard-sop/jobs`：

```json
{"batch_id":"返回的batch_id","operation":"analyze"}
```

返回 `{ "run": ... }`，状态码 202。轮询 `GET /api/runs/{id}`，返回 **Run 对象本身**（不是 `run` 外层），包含 status、result、steps 和 latest_checkpoint。取消使用 `POST /api/runs/{id}/cancel`。分析完成后用 `GET /api/standard-sop/batches/{id}` 获取质量摘要和来源 ID；单份正文使用 `GET /api/standard-sop/batches/{id}/sources/{source_id}`。

生成任务：

```json
{"batch_id":"返回的batch_id","operation":"generate","source_ids":["人工确认的source_id"]}
```

成功 Run 的 result 是标准 SOP 草案。来源必须属于同批次、已声明上线、模型评审完成且与场景相关。每次生成最多 12 份完整来源，分析单源上下文最多 60,000 字符，生成来源上下文最多 160,000 字符；超限明确拒绝。

4. `GET /api/standard-sop/drafts/{id}` 读取草案；`POST .../{id}/edit` 提交 `{revision, payload}`；`POST .../{id}/review` 提交 `{revision, decision:"APPROVED"或"REJECTED", comment}`。review 的 comment 必填，审核主体使用平台共享操作者。`GET .../{id}/export` 返回 `{draft, sources, markdown}`，失败不输出可导出的已批准结果。

## 质量与证据边界

- 结构检查只反映章节是否有文本。模型六维评分各 0–5 等权换算到 100，rubric 为 `standard_sop_writing_v1`；未校准为企业客观质量标准。
- 优点及草案条目的引用都须落在该来源的指定章节，quote 至少 12 字符且逐字存在。跨来源冒用 ID、伪造原文、无引用条目会被拒绝。引用存在不证明改写语义、命令安全、步骤顺序、实例泛化或技术兼容，需人工审核。
- 显式 gaps/conflicts 或空章节使草案 BLOCKED，不能直接批准。参数表是标准模板占位说明，不装配实例化变更单。模型可能漏报冲突或保留实例值，人工需核对。
- 来源库和标准 SOP 稿件保存在现有 `knowledge.db` 的独立表中，不自动注册知识卡、不激活 Profile。全库清空会删除这些批次、来源和标准稿件，并受现有三库备份保护；已导出文件不随清空删除。
- 模型分析只对既有原文快照负责；若原文或模型/场景需要改变，请创建新批次或导入新来源。上游真实性验证与抓取时点由爬虫保留并在内网人工核对。
