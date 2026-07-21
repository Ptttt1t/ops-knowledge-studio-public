# 第一阶段可信度加固

完成日期：2026-07-17

## 完成内容

### 1. 检索门槛与严格拒答

- 中文单字停用词和常见英文停用词不再参与召回。
- 检索结果同时满足最低相关度和查询词覆盖率才可进入可信回答。
- 默认配置为 `KNOWLEDGE_MIN_SCORE=10`、`KNOWLEDGE_MIN_COVERAGE=0.15`。
- 没有强相关 APPROVED 卡片时直接返回 `no_relevant_approved_knowledge`，不调用回答模型。

### 2. 逐结论结构化引用

- 模型不再直接生成带引用的自由文本，而是输出 `claims` JSON。
- 每条结论必须包含合法 `category`、独立 `text` 和至少一个 `card_ids`。
- `card_ids` 必须来自本次实际检索到的 APPROVED 卡片。
- 正文中出现模型自行书写的 `K数字` 会被拒绝。
- Markdown 答案和 `[K编号]` 由程序统一渲染，来源列表由同一结构生成。
- 模型返回空 claims 时按证据不足拒答。

### 3. 原文证据回定位

- 先尝试逐字匹配，再处理空白、Unicode 标点和大小写规范化。
- 对 Markdown 展示指令采用保守的首尾锚点匹配。
- 最终存储的引文始终是程序从来源分片截取的连续原文。
- 证据位置缩小为真实字符区间，并记录 `exact / normalized / anchored` 匹配方式。
- 无法回定位的卡片保持 DRAFT，不能只靠质量分进入待审队列。

### 4. 分类质量评分

- `procedure` 继续检查步骤、风险、回退和验证。
- `risk` 重点检查风险事实，不再强制要求回退步骤。
- `case` 重点检查事件影响。
- `constraint` 不再被操作步骤字段误罚。
- `compatibility` 检查版本或适用范围。
- `rollback` 重点检查回退和验证字段。

## 现有知识库迁移结果

执行：

```powershell
python run.py regrade
```

结果：

- 处理卡片：73
- 成功回定位原文：53
- 当前平均质量分：82.1
- 当前状态：DRAFT 22 / PENDING_REVIEW 46 / APPROVED 5
- 全过程不调用大模型，不会改变 APPROVED、REJECTED、SUPERSEDED 的人工治理结论

## 验收结果

- 离线单元与集成测试：11/11 通过。
- 四个正向公开语料检索：4/4 目标卡 Top 1。
- 三次真实 DeepSeek 结构化回答：3/3 通过 card ID 和来源校验。
- 负向问题“HTTP 503 和 Retry-After 表示什么”：返回零命中，不调用回答模型。
- 测试脚本退出码：0；公开版不包含带本机路径和运行数据的 `artifacts/` 结果文件。

## 配置

```dotenv
KNOWLEDGE_TOP_K=6
KNOWLEDGE_MIN_SCORE=10
KNOWLEDGE_MIN_COVERAGE=0.15
```

阈值应继续通过固定评测集调优，不能把当前默认值理解为所有数据规模下的最终参数。
