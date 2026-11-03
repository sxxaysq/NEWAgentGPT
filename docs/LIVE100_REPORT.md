# 100 个真实案例：结果与各类代表执行过程

测试日期：2026-11-03（北京时间）。当前发布版本在同一五进程部署上运行，数据均为测试构造。

共 **100 个场景**，包含 **173 个真实用户运行**、**910 个 A2A 任务**和 **443 次记录到回执的工具调用**。
场景断言通过 **96/100**；限时内 completed **170/173**。API 边界拒绝不产生 Agent 调用，也不伪装成模型回答。
人工复核标记 **1 个断言误报**，原自动分数保持不变；其余未通过场景为实际运行失败或超时。

这些通过率来自明确的字符串、来源、状态和隔离断言，不是独立模型语义评审的准确率。没有将之前的模拟契约测试计入本报告。

## 类别结果

| 类别 | 场景 | 断言通过 | 真实运行数 |
|---|---:|---:|---:|
| 单点事实 | 10 | 10 | 10 |
| 多步骤任务 | 10 | 10 | 10 |
| 长文本 | 6 | 6 | 6 |
| 私有知识库检索 | 10 | 10 | 10 |
| 联网检索 | 6 | 5 | 6 |
| 私有＋联网混合检索 | 6 | 4 | 6 |
| 暂停与继续 | 8 | 8 | 8 |
| 错别字输入 | 6 | 6 | 6 |
| 用户及项目隔离 | 10 | 10 | 30 |
| 同会话多轮纠正 | 14 | 13 | 49 |
| 跨会话长期记忆 | 10 | 10 | 30 |
| 边界与拒绝 | 4 | 4 | 2 |

## 记录口径

- 沿用既有契约测试的 12 类及各类数量，构造适合真实 API 执行的输入；多轮步骤属于同一个场景，场景数、用户运行数、A2A 任务数分别统计。
- 类内主要使用相同场景脚本的随机编号、金额和轮数变体；100 个执行样本不等于 100 种独立业务流程，本轮不覆盖 OCR 或所有真实文档类型。
- 顺序来自 A2A TaskStore 的真实创建/更新时间。表格按开始时间排序；时间区间重叠表示并发，不表示后一项必须等前一项结束。
- Worker 内部工具调用按持久 journal 的顺序列出；journal 没有单次工具精确时间戳，因此不编造工具耗时。输出只保留摘要。
- Host 负责接收、鉴权、轮次落库、上下文装配、调度和最终提交；状态/摘要/长期事实抽取是 Host 后台任务，不计作额外 A2A 子 Agent。
- 恢复类别是真实 pause/continue；没有在用户正在使用的服务上强制杀进程。进程崩溃能力另由恢复单测验证。
- 每轮评测期限为360秒。到期即记录timeout并请求停止；之后即使得到迟到答复也不追认通过，迟到产物仅供问题分析。
- 每类优先选择通过且包含较完整执行链的案例；所有失败保留在下一节和机器可读汇总中，不能从代表例推断整类全部通过。
- 答案中的 `/api/evidence/…` 是测试实例内的引用地址，公开仓库不提供该服务的私有证据数据库；工具返回只展示状态、数量、来源地址等摘要。

## 失败与限制

- **hybrid_retrieval-00**：TimeoutError: run a12293391dd748d78eddbab6e9e85d2a exceeded 360s
  - timeout：### 私有来源  根据私有知识库文档 `synthetic-fixture.md` 的“测试项目”章节记载：  *   **项目预算**：180,000 CNY [来源](/api/evidence/doc_e367f12ad7ab544eb451407d307f7fb6) *   **审批编号* …〔中间省略〕… 查询中不会匹配任何行；若未设置 `remove_diacritics`，还支持索引化的 GLOB 和 LIKE 模式匹配 [来源](/api/evidence/web_c220c42bc720bb4ce65f9354a5a0a8781f5f0d574fcad28d33e73d48d1decb99)。
- **hybrid_retrieval-04**：TimeoutError: run d40a9b2b87414082b860567497667ca5 exceeded 360s
  - timeout：运行已停止。
- **in_session-13**：correction_and_constraints
  - **人工复核**：英文表格当前预算为CNY 180,000；说明文字中的23仅出现在预算由23万元更新为18万元的历史描述中，并未恢复旧约束。保留自动断言passed=false，不改写原始分数。
  - 虽正常结束，但答案未满足该场景断言：\| Field \| Value \| \|---\|---\| \| Budget \| CNY 180,000 \| \| Deployment \| Intranet only \| \| Comparison \| Option A vs Option B \|  以上三项均直接来自你此前提供的约束（预算由 23 万元更新为 18 万元、仅内网部署、比较 A 和 B），未引入新事实。
- **web_retrieval-03**：run_completed、web_evidence_answer
  - failed：本次没有产生任何可用的子任务结论： - 定位并读取 asyncio.wait_for 官方文档：[host] ContextBudgetError: 本次请求的必需正文、约束或工具结果超过当前上下文预算；原始输入已保留，请拆分材料或缩小处理范围。（计数模式 server_tokenize，输入 13 …〔中间省略〕… 始输入已保留，请拆分材料或缩小处理范围。（计数模式 server_tokenize，输入 135046，窗口 131072） - 定位并读取 asyncio.wait_for 官方文档：[host] TimeoutError:  - 基于官方正文回答超时后是否等待被取消任务结束：上游任务未完成：t1

## 单点事实：single_hop-00

场景结果：预设断言通过。

### 第 1 轮

**输入**：用户提供的项目代号是BENCH-B805EEAC74。请原样返回代号，不需要检索。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→1.75 | planner · planner | 计划：t1 返回项目代号（依赖 无） |
| 1.80→3.57 | critic · judge_plan | 审核=pass；用户要求原样返回特定字符串，属于单点事实交付，无需拆分。；任务目标明确，验收标准清晰，无依赖关系缺失。；用户问题中的分句（提供代号、原样返回、不需检索）均被单一任务覆盖。 |
| 3.71→5.54 | worker · 返回项目代号 | 结果：BENCH-B805EEAC74；来源：上下文 1 条，文档/网页 0 条 |
| 5.62→7.63 | critic · verify_findings | 审核=pass；产物 summary 字段仅包含字符串 BENCH-B805EEAC74，符合验收标准。；未包含任何解释、检索结果或额外文本。；Claim 正确引用了用户请求作为上下文来源，且未虚构外部证据。 |
| 7.74→8.48 | synthesizer · synthesizer | 合成：BENCH-B805EEAC74 |

**Host 收尾**：completed；整轮 9.31 秒；Worker 工具调用 0 次。

**最终答案**：

BENCH-B805EEAC74

追溯 run_id：`a98cd9b71b534313954abd251c11fb66`。

## 多步骤任务：multi_step-05

场景结果：预设断言通过。

### 第 1 轮

**输入**：请分别计算方案A：125元乘3台；方案B：85元乘4台；最后比较总价，给出两项总价、差额及便宜的方案。所有数据均在本题，不用检索。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→5.91 | planner · planner | 计划：t1 计算方案A总价（依赖 无）；t2 计算方案B总价（依赖 无）；t3 比较总价并给出结论（依赖 t1,t2） |
| 5.96→8.14 | critic · judge_plan | 审核=pass；用户要求分别计算两个方案，再比较，任务图正确拆分为两个独立计算任务和一个依赖前两者的比较任务。；所有数据均在题目中，无需检索，任务目标明确且可单独验收。；依赖关系正确：t3 依赖 t1 和 t2，符合逻辑。；用户问题中的分句（计算A、计算B、比较总价、给出差额及便宜方案）均有对应子任务覆盖。 |
| 8.33→10.39 | worker · 计算方案A总价 | 结果：方案A总价：125元/台 × 3台 = 375元。；来源：上下文 1 条，文档/网页 0 条 |
| 8.43→10.49 | worker · 计算方案B总价 | 结果：方案B总价为 85元/台 × 4台 = 340元。；来源：上下文 1 条，文档/网页 0 条 |
| 10.48→12.23 | critic · verify_findings | 审核=pass；产物明确给出了方案A的总价数值375元；计算过程（125元/台 × 3台）与用户请求中的数据一致；引用了正确的上下文来源 request:5d80992d079e41cdb4f0c79db4ad4d34 |
| 10.59→11.95 | critic · verify_findings | 审核=pass；产物明确给出了方案B的总价数值340元；计算过程（85元/台 × 4台）与用户请求中的数据一致；验收标准仅要求给出方案B总价，产物已覆盖 |
| 12.41→15.07 | worker · 比较总价并给出结论 | 结果：方案A总价375元，方案B总价340元，差额35元，方案B更便宜。；来源：上下文 1 条，文档/网页 0 条 |
| 15.15→16.65 | critic · verify_findings | 审核=pass；差额数值35元与验收标准一致；明确指出方案B更便宜，符合验收标准；计算过程基于用户提供的数据（125*3=375, 85*4=340），逻辑正确 |
| 16.76→19.19 | synthesizer · synthesizer | 合成：## 计算结果  \| 项目 \| 计算 \| 总价 \| \|------\|------\|------\| \| 方案A \| 125元 × 3台 \| **375元** \| \| 方案B \| 85元 × 4台 \| **340元** \|  ## 比较  - **差额**：375 − 340 = **35元** - **更便宜的方案**：**方案B**（340元 < 375元） |

**Host 收尾**：completed；整轮 19.91 秒；Worker 工具调用 0 次。

**最终答案**：

## 计算结果

| 项目 | 计算 | 总价 |
|------|------|------|
| 方案A | 125元 × 3台 | **375元** |
| 方案B | 85元 × 4台 | **340元** |

## 比较

- **差额**：375 − 340 = **35元**
- **更便宜的方案**：**方案B**（340元 < 375元）

追溯 run_id：`5d80992d079e41cdb4f0c79db4ad4d34`。

## 长文本：long_text-00

场景结果：预设断言通过。

### 第 1 轮

**输入**：请从下面完整材料提取首尾两个编号，并保留不得联网的约束。 开头编号：BENCH-0967335977 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 …〔中间省略〕… 不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 背景：此项目仅供测试，会议内容不改变已确认条件。 末尾编号：TAIL-0967335977。约束：不得联网。只输出两个编号和约束。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→3.40 | planner · planner | 计划：t1 提取首尾编号与约束（依赖 无） |
| 3.47→5.56 | critic · judge_plan | 审核=pass；用户要求提取两个明确编号并保留单一约束，属于单点事实提取，无需拆分。；任务目标清晰，验收标准覆盖了所有用户要求（两个编号和约束）。；没有依赖关系遗漏，因为只有一个任务。；用户问题中的分句（提取编号、保留约束）均在单个任务中覆盖。 |
| 5.85→10.60 | worker · 提取首尾编号与约束 | 结果：开头编号：BENCH-0967335977 末尾编号：TAIL-0967335977 约束：不得联网；来源：上下文 1 条，文档/网页 0 条 |
| 10.87→13.26 | critic · verify_findings | 审核=pass；产物包含开头编号 BENCH-0967335977；产物包含末尾编号 TAIL-0967335977；产物包含约束：不得联网；产物仅包含要求的编号和约束，无多余内容 |
| 13.33→14.60 | synthesizer · synthesizer | 合成：开头编号：BENCH-0967335977 末尾编号：TAIL-0967335977 约束：不得联网 |

**Host 收尾**：completed；整轮 15.1 秒；Worker 工具调用 0 次。

**最终答案**：

开头编号：BENCH-0967335977
末尾编号：TAIL-0967335977
约束：不得联网

追溯 run_id：`656637339d044ca7b8f99b31eb7b6576`。

## 私有知识库检索：private_retrieval-01

场景结果：预设断言通过。

### 第 1 轮

**输入**：请查当前私有知识库的批准预算、审批编号和部署限制，并引用来源。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.22 | planner · planner | 计划：t1 核实私有知识库预算、审批编号与部署限制（依赖 无） |
| 2.28→4.01 | critic · judge_plan | 审核=pass；用户询问的批准预算、审批编号和部署限制均属于同一私有知识库内的相关配置信息，通常集中在同一份文档或配置文件中，适合合并为一个任务处理。；任务目标明确，要求从私有知识库中检索并引用来源，符合用户“引用来源”的要求。；没有发现依赖关系缺失或目标脱离上下文的问题。 |
| 4.20→16.32 | worker · 核实私有知识库预算、审批编号与部署限制 | 结果：私有知识库文档 synthetic-fixture.md（章节“测试项目”）载明：批准预算 180,137 CNY；审批编号 APP-158859db38；部署限制为只能内网部署。三项信息均出自同一文档。[来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b)；来源：上下文 0 条，文档/网页 1 条 |
| ↳ 工具 1 | `builtin__private_search` · success | 参数：{"query": "批准预算 审批编号 部署限制"}；返回：返回码 HIT；私有片段 doc_43378d2cc9ed5ccb96f10042fb60310b |
| ↳ 工具 2 | `builtin__private_read` · success | 参数：{"evidence_id": "doc_43378d2cc9ed5ccb96f10042fb60310b"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| 16.42→17.95 | critic · verify_findings | 审核=pass；预算金额 180,137 CNY 与原文一致，且引用了来源；审批编号 APP-158859db38 与原文一致，且引用了来源；部署限制“只能内网部署”与原文一致，且引用了来源 |
| 18.04→21.87 | synthesizer · synthesizer | 合成：根据当前私有知识库中的文档 `synthetic-fixture.md`（章节“测试项目”），查询结果如下：  *   **批准预算**：180,137 CNY [来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b) *   **审批编号**：APP-158859db38 [来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b) *   **部署限制**：只能内网部署 [来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b) |

**Host 收尾**：completed；整轮 22.56 秒；Worker 工具调用 2 次。

**最终答案**：

根据当前私有知识库中的文档 `synthetic-fixture.md`（章节“测试项目”），查询结果如下：

*   **批准预算**：180,137 CNY [来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b)
*   **审批编号**：APP-158859db38 [来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b)
*   **部署限制**：只能内网部署 [来源](/api/evidence/doc_43378d2cc9ed5ccb96f10042fb60310b)

追溯 run_id：`2097238440e641289af6aa77cf64bb5c`。

## 联网检索：web_retrieval-01

场景结果：预设断言通过。

### 第 1 轮

**输入**：先搜索Python asyncio wait_for官方文档，再读取官方正文，说明超时后是否会等待被取消任务结束，并引用来源。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→4.33 | planner · planner | 计划：t1 检索并读取 asyncio.wait_for 官方文档（依赖 无）；t2 基于官方文档回答超时行为（依赖 t1） |
| 4.44→7.85 | critic · judge_plan | 审核=pass；用户明确要求“先搜索...再读取...说明...”，任务图拆分为“检索读取”和“基于证据作答”两步，符合用户指定的执行顺序和产物类型差异。；t2 正确依赖 t1，因为最终回答需要引用 t1 获取的官方文档 URL 和原文片段。；用户问题中的分句（搜索、读取、说明行为、引用来源）均在 t1 和 t2 的 goal 和 acceptance 中得到覆盖。；没有发现脱离上下文无法执行的子任务，t1 是独立的检索任务，t2 基于 t1 结果。 |
| 8.00→79.03 | worker · 检索并读取 asyncio.wait_for 官方文档 | 结果：官方文档（Python 3.15.2，URL: https://docs.python.org/3/library/asyncio-task.html）在 asyncio.wait_for 条目下明确说明：超时发生时，wait_for 会取消传 …〔中间省略〕… 文省略〕 因此，自 Python 3.7 起，wait_for 在超时后确实会等待被取消的任务结束（即等待取消完成），总等待时间可能超过设定的 timeout 值。；缺口：到达工具调用预算，改用已有资料产出。；来源：上下文 0 条，文档/网页 1 条 |
| ↳ 工具 1 | `builtin__web_search` · success | 参数：{"query": "Python asyncio.wait_for official documentation", "include_domains": ["docs.python.org"]}；返回：{"code": "MISS", "sources_count": 0} |
| ↳ 工具 2 | `builtin__web_fetch` · success | 参数：{"url": "https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for", "max_chars": 12000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 3 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 24000, "max_chars": 16000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 4 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 20000, "max_chars": 4500}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 5 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 19500, "max_chars": 1200}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 6 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 18500, "max_chars": 1100}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 7 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 17500, "max_chars": 1100}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 8 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 16500, "max_chars": 1100}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 9 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 14500, "max_chars": 1200}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 10 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 13500, "max_chars": 1100}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 11 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 12500, "max_chars": 1100}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 12 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 11500, "max_chars": 1100}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| 79.28→84.32 | critic · verify_findings | 审核=pass；产物给出了官方文档 URL：https://docs.python.org/3/library/asyncio-task.html，且引用片段标题为 Python 3.15.2 文档，与 claim 一致。；产物引用了文档中关于超时后 …〔中间省略〕… imeoutError immediately.' 一致。；所有 claims 均引用了同一 evidence_id，且该 evidence 片段包含支持这些 claims 的原文内容。；gaps 中说明了工具调用预算限制，但未影  〔后续展开省略〕 |
| 84.57→129.19 | worker · 基于官方文档回答超时行为 | TASK_STATE_FAILED |
| ↳ 工具 1 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 2 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 24000, "max_chars": 16000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 3 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 4 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 5 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 6 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 7 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 8 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 9 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 12000, "offset": 22000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| 129.40→173.67 | worker · 基于官方文档回答超时行为 | 结果：结论：会。根据 Python 官方文档（Python 3.15.2，https://docs.python.org/3/library/asyncio-task.html），asyncio.wait_for 在超时后会取消传入的 awaitab …〔中间省略〕…  TimeoutError." 以及 〔长引文省略〕 版本变更记录进一步确认：〔长引文省略〕 即自 Python 3.7 起，wait_for 在超时后确实会等待被取消任务结束（取消完成），而非立即抛出异常。；来源：上下文 0 条，文档/网页 1 条 |
| ↳ 工具 1 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 4000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 2 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "offset": 14000, "max_chars": 6000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| ↳ 工具 3 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94", "max_chars": 6000, "offset": 20000}；返回：返回码 HIT；来源 https://docs.python.org/3/library/asyncio-task.html |
| 173.86→177.66 | critic · verify_findings | 审核=pass；结论明确回答“会”等待被取消任务结束；引用了 t1 提供的官方文档来源（URL 及原文片段），且原文片段支持所有 claim；语言为简体中文 |
| 177.78→186.91 | synthesizer · synthesizer | 合成：## 结论  **会。** 自 Python 3.7 起，`asyncio.wait_for` 在超时后**会等待被取消的任务（awaitable）真正完成取消**，然后才抛出 `TimeoutError`，因此总等待时间可能超过设定的 `ti …〔中间省略〕…  \| 版本差异 \| Python 3.7 之前：立即抛出 `TimeoutError`，不等待取消完成 \|  [来源](/api/evidence/web_b8b2feee9dd88fb9cf68d4c8b3af19bb43  〔后续展开省略〕 |

**Host 收尾**：completed；整轮 187.21 秒；Worker 工具调用 24 次。

**最终答案（网页场景展示摘要，省略长引文）**：

## 结论

**会。** 自 Python 3.7 起，`asyncio.wait_for` 在超时后**会等待被取消的任务（awaitable）真正完成取消**，然后才抛出 `TimeoutError`，因此总等待时间可能超过设定的 `timeout` 值。

## 官方文档原文

来源：Python 3.15.2 官方文档 — [asyncio-task](/api/evidence/web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94)

版本变更记录：

## 要点说明

| 要点 | 说明 |
|------|------|
| 超时行为 | 取消传入的 awaitable（aw），并等待其取消完成 |
| 异常抛出时机 | 取消完成后抛出 `TimeoutError` |
| 总耗时 | 可能超过 `timeout` 参数设定的值 |
| 取消期间异常 | 若取消过程中发生异常，会向上传播 |
| 版本差异 | Python 3.7 之前：立即抛出 `TimeoutError`，不等待取消完成 |

[来源](/api/evidence/web_b8b2feee9dd88fb9cf68d4c8b3af19bb4308baca97082ce2e59c3dd07b1b9c94)

追溯 run_id：`fc3b46b0960546c8b2fb42a01d899fe2`。

## 私有＋联网混合检索：hybrid_retrieval-01

场景结果：预设断言通过。

### 第 1 轮

**输入**：请从当前私有知识库查出项目预算和审批编号，再读取 https://www.sqlite.org/fts5.html ，确认trigram tokenizer是否支持一般子串匹配。分别给出私有和公开来源。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→14.84 | planner · planner | 计划：t1 检索私有知识库中的项目预算与审批编号（依赖 无）；t2 核实 SQLite FTS5 trigram tokenizer 的子串匹配能力（依赖 无） |
| 14.92→19.53 | critic · judge_plan | 审核=pass；用户请求包含两个独立的信息源（私有知识库和公开网页），且两者之间没有数据依赖关系，拆分为两个并行任务是合理的。；子任务 t1 准确覆盖了从私有知识库检索项目预算和审批编号的需求，验收标准明确。；子任务 t2 准确覆盖了读取指定 URL …〔中间省略〕… igram tokenizer 子串匹配能力的需求，验收标准包含原文引用，符合事实核查要求。；两个子任务均无 depends_on，符合并行执行逻辑。；用户问题中的分句（查私有库、读公开网页、分别给出来源）均有对应的子任务或隐含在最终整合中，无遗漏。 |
| 19.72→106.71 | worker · 核实 SQLite FTS5 trigram tokenizer 的子串匹配能力 | 结果：**私有来源（项目预算与审批编号）**  从私有知识库文档 `synthetic-fixture.md`（doc_0b68f15beeb25e1095a5dcf449f35883）中查到： - **批准预算**：180,137 CNY - ** …〔中间省略〕… 和 `GLOB` 模式匹配（如 `LIKE '%cdefg%'`）。 - 若 `case_sensitive=1`，则只能索引 GLOB 查询，不能索引 LIKE。；缺口：到达工具调用预算，改用已有资料产出。；来源：上下文 0 条  〔后续展开省略〕 |
| ↳ 工具 1 | `builtin__web_fetch` · success | 参数：{"url": "https://www.sqlite.org/fts5.html", "max_chars": 16000}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 2 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 16000, "max_chars": 16000}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 3 | `builtin__private_search` · success | 参数：{"query": "项目预算 审批编号"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| ↳ 工具 4 | `builtin__private_read` · success | 参数：{"evidence_id": "doc_0b68f15beeb25e1095a5dcf449f35883"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| ↳ 工具 5 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 29572, "max_chars": 4000}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 6 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 24135, "max_chars": 1500}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 7 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 31163, "max_chars": 800}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 8 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 30799, "max_chars": 700}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 9 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 29572, "max_chars": 1200}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 10 | `builtin__web_fetch` · error | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 24135, "max_chars": 200}；返回：已保存返回摘要 278 字符；状态 error |
| ↳ 工具 11 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 24135, "max_chars": 500}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| ↳ 工具 12 | `builtin__web_fetch` · success | 参数：{"evidence_id": "web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88", "offset": 31163, "max_chars": 500}；返回：返回码 HIT；来源 https://www.sqlite.org/fts5.html |
| 19.79→45.91 | worker · 检索私有知识库中的项目预算与审批编号 | 结果：私有知识库 synthetic-fixture.md（测试项目节）记载：批准预算 180137 CNY，审批编号 APP-e0188439ba。；来源：上下文 0 条，文档/网页 1 条 |
| ↳ 工具 1 | `builtin__private_search` · success | 参数：{"query": "项目预算 金额 审批编号"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| ↳ 工具 2 | `builtin__private_read` · success | 参数：{"evidence_id": "doc_0b68f15beeb25e1095a5dcf449f35883"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| 46.03→52.75 | critic · verify_findings | 审核=pass；预算金额 180137 CNY 与原文一致，且指明了文档名 synthetic-fixture.md 及章节「测试项目」；审批编号 APP-e0188439ba 与原文一致，且指明了文档名 synthetic-fixture.md 及章节「测试项目」；两条 claim 均引用了有效的 evidence_id，且原文片段支持了这些 claim；没有未支持的 claim 被遗漏在 gaps 中 |
| 106.97→109.32 | critic · verify_findings | 审核=pass；明确回答了 trigram tokenizer 支持一般子串匹配；引用了 SQLite FTS5 官方文档第 4.3.4 节和第 4.3 节的原文作为证据；私有来源（预算、审批编号）与公开来源（技术依据）已分别列出；所有 claim 均有对应的 evidence_ids 支持，且原文片段可核验 |
| 109.46→122.00 | synthesizer · synthesizer | 合成：## 私有来源：项目预算与审批编号  从私有知识库文档 `synthetic-fixture.md`（「测试项目」节）中查到：  - **批准预算**：180,137 CNY - **审批编号**：APP-e0188439ba  来源：[syn …〔中间省略〕… remove_diacritics`，trigram 表还支持索引化的 `LIKE` 和 `GLOB` 模式匹配（如 `LIKE '%cdefg%'`）。 - 若 `case_sensitive=1`，则只能索引 GLOB 查询，  〔后续展开省略〕 |

**Host 收尾**：completed；整轮 122.61 秒；Worker 工具调用 14 次。

**最终答案（网页场景展示摘要，省略长引文）**：

## 私有来源：项目预算与审批编号

从私有知识库文档 `synthetic-fixture.md`（「测试项目」节）中查到：

- **批准预算**：180,137 CNY
- **审批编号**：APP-e0188439ba

来源：[synthetic-fixture.md](/api/evidence/doc_0b68f15beeb25e1095a5dcf449f35883)

---

## 公开来源：SQLite FTS5 trigram tokenizer 子串匹配能力

根据 [SQLite FTS5 官方文档](/api/evidence/web_27afa743ae53eaf5e1e8e3eaba448ef070919120f6a82fe64986056b3157ff88)（https://www.sqlite.org/fts5.html）：

**第 4.3.4 节 "The Trigram Tokenizer" 原文：**

**第 4.3 节 Tokenizers 总览：**

### 结论

**trigram tokenizer 确实支持一般子串匹配（general substring matching）**，不限于前缀匹配。查询词可以匹配行内任意位置的字符序列，而不仅是完整 token 或 token 前缀。

### 限制条件（同节 Notes）

- 少于 3 个 Unicode 字符的子串在全文本查询中不会匹配任何行。
- 若未设置 `remo

〔后续展开省略〕

追溯 run_id：`dd040eebab15408b8ad011149f07b057`。

## 暂停与继续：recovery-00

场景结果：预设断言通过。
真实暂停/继续；不代表进程SIGKILL故障注入。
采集器最初读到continue响应后的旧paused投影；现按同一run持久终态校正并补采轨迹，未重新执行场景。原采集快照保留在collector_original。

**Host 控制动作**：启动运行 → 请求 pause → 观察到 paused → 请求 continue → 检查返回的 run_id 与原运行一致 → 等待同一运行最终终态。下表是该运行合并后的实际 A2A 任务记录。

### 第 1 轮

**输入**：根据本题数据分两步计算：甲方案100元乘7台，乙方案90元乘8台，比较总价。请给出计算结果，不使用检索。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→4.24 | planner · planner | 计划：t1 计算并比较甲乙两方案总价（依赖 无） |
| 4.30→6.29 | critic · judge_plan | 审核=pass；用户要求基于给定数据计算并比较，属于单点算术题，无需检索，一个子任务即可完整交付。；子任务目标明确，包含两步计算和比较，符合用户要求。；验收标准清晰，涵盖了甲方案总价、乙方案总价及比较结果。 |
| 7.14→12.02 | worker · 计算并比较甲乙两方案总价 | 结果：甲方案总价：100元 × 7台 = 700元；乙方案总价：90元 × 8台 = 720元。比较：甲方案总价（700元）低于乙方案总价（720元），乙方案比甲方案贵20元。；来源：上下文 1 条，文档/网页 0 条 |
| 12.16→14.12 | critic · verify_findings | 审核=pass；甲方案总价700元计算正确且明确写出；乙方案总价720元计算正确且明确写出；比较结果明确：甲低于乙，乙比甲贵20元；所有claim均基于用户提供的数据（context_ids引用正确），无需外部证据 |
| 14.21→16.03 | synthesizer · synthesizer | 合成：根据题目数据分两步计算如下：  1. **甲方案总价**：100元 × 7台 = **700元** 2. **乙方案总价**：90元 × 8台 = **720元**  **比较结果**：甲方案总价（700元）低于乙方案总价（720元），乙方案比甲方案贵20元。 |

**Host 收尾**：completed；整轮 16.26 秒；Worker 工具调用 0 次。

**最终答案**：

根据题目数据分两步计算如下：

1. **甲方案总价**：100元 × 7台 = **700元**
2. **乙方案总价**：90元 × 8台 = **720元**

**比较结果**：甲方案总价（700元）低于乙方案总价（720元），乙方案比甲方案贵20元。

追溯 run_id：`dd960eec988b4b449a4681502bb9d60e`。

## 错别字输入：typos-02

场景结果：预设断言通过。

### 第 1 轮

**输入**：请查当前私有知识库的项目预算、审批编号和布署限止。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.71 | planner · planner | 计划：t1 检索项目预算、审批编号及部署限制（依赖 无） |
| 2.76→4.96 | critic · judge_plan | 审核=pass；用户询问的预算、审批编号和部署限制通常位于同一份项目文档或相邻章节，属于单点事实查询，合并为一个任务符合效率原则。；任务图包含一个子任务，无依赖关系，不存在漏写 depends_on 的情况。；子任务 goal 明确指向私有知识库检索，上下文完整，可独立执行。；用户问题中的三个分句（预算、审批编号、部署限制）均在子任务的 acceptance 和 goal 中得到了覆盖。 |
| 5.07→22.84 | worker · 检索项目预算、审批编号及部署限制 | 结果：私有知识库中「测试项目」（代号 BENCH-9D51C61CE4）的关键信息如下： - 批准预算：180,274 CNY - 审批编号：APP-9d51c61ce4 - 部署限制：只能内网部署 来源文档：synthetic-fixture.md（section: 测试项目）[来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795)；来源：上下文 0 条，文档/网页 1 条 |
| ↳ 工具 1 | `builtin__private_search` · success | 参数：{"query": "项目预算 审批编号 部署限制"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| ↳ 工具 2 | `builtin__private_read` · success | 参数：{"evidence_id": "doc_7103aa4604385aa2b143872d8b34b795"}；返回：{"code": "HIT", "degraded": [], "sources_count": 1} |
| 22.96→25.38 | critic · verify_findings | 审核=pass；预算金额 180,274 CNY 与原文一致，且引用了来源文档。；审批编号 APP-9d51c61ce4 与原文一致，且引用了来源文档。；部署限制“只能内网部署”与原文一致，且引用了来源文档。；所有验收标准均已覆盖，无未支持的 claim。 |
| 25.48→31.91 | synthesizer · synthesizer | 合成：根据私有知识库检索结果，当前项目（代号 BENCH-9D51C61CE4）的关键信息如下：  - **项目预算**：180,274 CNY [来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795) - **审批编号**：APP-9d51c61ce4 [来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795) - **部署限制**：只能内网部署 [来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795) |

**Host 收尾**：completed；整轮 32.57 秒；Worker 工具调用 2 次。

**最终答案**：

根据私有知识库检索结果，当前项目（代号 BENCH-9D51C61CE4）的关键信息如下：

- **项目预算**：180,274 CNY [来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795)
- **审批编号**：APP-9d51c61ce4 [来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795)
- **部署限制**：只能内网部署 [来源](/api/evidence/doc_7103aa4604385aa2b143872d8b34b795)

追溯 run_id：`8af59654a32e4988b8d5f7486b017c23`。

## 用户及项目隔离：isolation-00

场景结果：预设断言通过。

### 第 1 轮

**输入**：请记住我的长期项目事实：项目内部代号是BENCH-36E387EECA，以后提到项目代号就使用它。只需确认。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→3.97 | planner · planner | 计划：t1 确认项目代号记忆（依赖 无） |
| 4.03→5.20 | critic · judge_plan | 审核=pass；用户仅要求确认记忆，无需拆分任务；子任务目标与用户请求完全一致；无依赖关系缺失；无脱离上下文的子任务 |
| 5.34→19.71 | worker · 确认项目代号记忆 | 结果：已记住：你的项目内部代号是 BENCH-36E387EECA，以后提到项目代号就使用它。；来源：上下文 1 条，文档/网页 0 条 |
| 19.80→21.42 | critic · verify_findings | 审核=pass；产物明确确认已记住代号 BENCH-36E387EECA；产物说明后续将使用该代号；Claim 正确引用了用户当前请求作为上下文来源 |
| 21.54→22.61 | synthesizer · synthesizer | 合成：已记住：你的项目内部代号是 **BENCH-36E387EECA**，以后提到项目代号就使用它。 |

**Host 收尾**：completed；整轮 22.88 秒；Worker 工具调用 0 次。

**最终答案**：

已记住：你的项目内部代号是 **BENCH-36E387EECA**，以后提到项目代号就使用它。

追溯 run_id：`3c256ae63bc14491a78e3c1a9f590e7a`。

### 第 2 轮

**输入**：我的项目内部代号是什么？无资料则说明不知道，不得猜测。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→3.82 | planner · planner | 计划：t1 检索项目内部代号（依赖 无） |
| 3.88→5.91 | critic · judge_plan | 审核=pass；用户问题为单一事实查询，无需拆分。；子任务目标明确，涵盖了检索与无结果时的处理逻辑。；验收标准严格对应了用户“无资料则说明不知道，不得猜测”的要求。；无依赖关系缺失，无上下文脱离问题。 |
| 6.04→7.74 | worker · 检索项目内部代号 | 结果：无资料，不知道。；缺口：未找到任何关于用户项目内部代号的资料 |
| 7.84→9.07 | critic · verify_findings | 审核=pass；产物明确表述为“无资料，不知道”，符合未找到时的验收标准；未包含任何猜测性内容；gaps 中记录了未找到资料的情况，处理得当 |
| 9.19→9.99 | synthesizer · synthesizer | 合成：未找到任何关于你项目内部代号的资料，因此不知道。 |

**Host 收尾**：completed；整轮 10.58 秒；Worker 工具调用 0 次。

**最终答案**：

未找到任何关于你项目内部代号的资料，因此不知道。

追溯 run_id：`bd1a38e15f4245b9a854a6d8ab58057f`。

### 第 3 轮

**输入**：我的项目内部代号是什么？无资料则说明不知道，不得猜测。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.02 | planner · planner | 计划：t1 检索项目内部代号（依赖 无） |
| 2.08→3.69 | critic · judge_plan | 审核=pass；用户问题为单点事实查询，无需拆分。；子任务目标明确，包含“无资料则说明不知道”的约束，符合用户要求。；无依赖关系遗漏，无脱离上下文的子任务。 |
| 3.86→5.85 | worker · 检索项目内部代号 | 结果：无资料，不知道。；缺口：未提供私有知识库清单，无法在私有资料中检索项目内部代号；未提供用户此前关于项目代号的明确记录，不能猜测 |
| 5.94→7.55 | critic · verify_findings | 审核=pass；产物明确表述“无资料，不知道”，符合未找到时的验收标准。；产物未给出任何猜测性代号，符合“不得猜测”的要求。；gaps 中合理说明了因缺乏私有知识库清单和明确记录而无法检索的原因，未编造信息。 |
| 7.66→8.38 | synthesizer · synthesizer | 合成：无资料，不知道。未取到可核实的项目内部代号记录，不做猜测。 |

**Host 收尾**：completed；整轮 8.97 秒；Worker 工具调用 0 次。

**最终答案**：

无资料，不知道。未取到可核实的项目内部代号记录，不做猜测。

追溯 run_id：`ab2fc4683b4d46f8a1fabf5a5350fe67`。

## 同会话多轮纠正：in_session-07

场景结果：预设断言通过。

### 第 1 轮

**输入**：项目BENCH-DD56A125B6预算23万元，仅内网部署，比较A和B。请整理中文表格。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.82 | planner · planner | 计划：t1 整理项目BENCH-DD56A125B6的中文对比表格（依赖 无） |
| 2.88→5.09 | critic · judge_plan | 审核=pass；用户请求中的所有信息（项目ID、预算、部署方式、比较对象）均已明确给出，无需额外检索或拆分。；单个任务即可覆盖所有要求，符合“一次能答完的问题不拆分”的原则。；子任务目标清晰，验收标准完整覆盖了用户提出的所有字段。 |
| 5.24→9.43 | worker · 整理项目BENCH-DD56A125B6的中文对比表格 | 结果：\| 项目 \| 内容 \| \|---\|---\| \| 项目名称 \| BENCH-DD56A125B6 \| \| 预算 \| 23万元 \| \| 部署方式 \| 仅内网 \| \| 比较对象 \| A 和 B \|；来源：上下文 1 条，文档/网页 0 条 |
| 9.56→10.68 | critic · verify_findings | 审核=pass；表格为中文；包含预算：23万元；包含部署方式：仅内网；包含比较对象：A和B；所有claim均有context_ids支持 |
| 10.79→12.05 | synthesizer · synthesizer | 合成：\| 项目 \| 内容 \| \|---\|---\| \| 项目名称 \| BENCH-DD56A125B6 \| \| 预算 \| 23万元 \| \| 部署方式 \| 仅内网 \| \| 比较对象 \| A 和 B \| |

**Host 收尾**：completed；整轮 12.53 秒；Worker 工具调用 0 次。

**最终答案**：

| 项目 | 内容 |
|---|---|
| 项目名称 | BENCH-DD56A125B6 |
| 预算 | 23万元 |
| 部署方式 | 仅内网 |
| 比较对象 | A 和 B |

追溯 run_id：`7690d136a6cf4173992cbb20e993e35f`。

### 第 2 轮

**输入**：预算改成18万元，其他不变。请更新表格。

**Host 装配**：召回记忆 0 条，待处理用户更新 1 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.82 | planner · planner | 计划：t1 更新项目表格预算字段（依赖 无） |
| 2.88→4.70 | critic · judge_plan | 审核=pass；用户仅要求修改预算字段，其他内容保持不变，单任务即可覆盖。；子任务目标明确，包含更新预算为18万元及保留其他字段的要求。；验收标准完整，涵盖了项目名称、预算、部署方式、比较对象及语言要求。；无依赖关系缺失，无脱离上下文的子任务。 |
| 4.90→9.79 | worker · 更新项目表格预算字段 | 结果：\| 项目 \| 内容 \| \|---\|---\| \| 项目名称 \| BENCH-DD56A125B6 \| \| 预算 \| 18万元 \| \| 部署方式 \| 仅内网 \| \| 比较对象 \| A 和 B \|；来源：上下文 2 条，文档/网页 0 条 |
| 9.92→11.65 | critic · verify_findings | 审核=pass；表格包含项目名称BENCH-DD56A125B6；预算显示为18万元；部署方式显示为仅内网；比较对象显示为A和B；表格为简体中文 |
| 11.78→13.38 | synthesizer · synthesizer | 合成：\| 项目 \| 内容 \| \|---\|---\| \| 项目名称 \| BENCH-DD56A125B6 \| \| 预算 \| 18万元 \| \| 部署方式 \| 仅内网 \| \| 比较对象 \| A 和 B \| |

**Host 收尾**：completed；整轮 14.06 秒；Worker 工具调用 0 次。

**最终答案**：

| 项目 | 内容 |
|---|---|
| 项目名称 | BENCH-DD56A125B6 |
| 预算 | 18万元 |
| 部署方式 | 仅内网 |
| 比较对象 | A 和 B |

追溯 run_id：`c14c17c69c824d00bd0aa41906493956`。

### 第 3 轮

**输入**：请概括之前明确的部署边界，不引入新事实。

**Host 装配**：召回记忆 0 条，待处理用户更新 2 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.81 | planner · planner | 计划：t1 概括已明确的部署边界（依赖 无） |
| 2.88→3.95 | critic · judge_plan | 审核=pass；用户问题仅包含一个分句，且为单点事实概括，无需拆分；任务目标明确，依赖关系为空符合逻辑；验收标准涵盖了不引入新事实的要求 |
| 4.09→6.27 | worker · 概括已明确的部署边界 | 结果：根据你此前提供的信息，项目 BENCH-DD56A125B6 的部署边界为：仅内网部署。；来源：上下文 1 条，文档/网页 0 条 |
| 6.38→7.93 | critic · verify_findings | 审核=pass；产物明确指出部署方式为'仅内网'，符合验收标准；产物未引入用户未提及的部署细节，仅概括了已知事实；产物语言为简体中文，符合要求 |
| 8.05→9.30 | synthesizer · synthesizer | 合成：根据你此前提供的信息，项目 BENCH-DD56A125B6 的部署边界为：**仅内网部署**。 |

**Host 收尾**：completed；整轮 9.96 秒；Worker 工具调用 0 次。

**最终答案**：

根据你此前提供的信息，项目 BENCH-DD56A125B6 的部署边界为：**仅内网部署**。

追溯 run_id：`6a6403163d6f4d2092ff1d8ec81ac79b`。

### 第 4 轮

**输入**：把刚才的约束改成英文表格，保留当前预算、部署边界和A/B比较对象。

**Host 装配**：召回记忆 0 条，待处理用户更新 3 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→6.13 | planner · planner | 计划：t1 生成项目约束英文表格（依赖 无） |
| 6.21→8.49 | critic · judge_plan | 审核=pass；用户要求将已知约束转换为英文表格，属于单次格式化任务，无需拆分。；子任务目标明确，涵盖了项目名称、预算、部署方式和比较对象四个字段。；验收标准正确识别了预算更新为18万元（180,000 CNY），并保留了仅内网部署和A/B比较的约束。；没有遗漏用户当前请求中的任何分句或隐含要求。 |
| 8.67→15.13 | worker · 生成项目约束英文表格 | 结果：\| Field \| Value \| \|---\|---\| \| Project Name \| BENCH-DD56A125B6 \| \| Budget \| CNY 180,000 \| \| Deployment \| Intranet only \| \| Comparison \| A vs B \|；来源：上下文 3 条，文档/网页 0 条 |
| 15.24→18.60 | critic · verify_findings | 审核=pass；输出为 Markdown 格式的英文表格，符合格式要求。；包含 Budget 行，值为 CNY 180,000，与用户最新指令（18万元）一致。；包含 Deployment 行，值为 Intranet only，与用户指令（仅内网）一致。；包含 Comparison 行，值为 A vs B，与用户指令（A/B比较）一致。；包含 Project Name 行，值为 BENCH-DD56A125B6，与上下文一致。；未引入会话历史中未提及的新约束或事实，所有字段均源自用户输入。 |
| 18.70→20.53 | synthesizer · synthesizer | 合成：\| Field \| Value \| \|---\|---\| \| Project Name \| BENCH-DD56A125B6 \| \| Budget \| CNY 180,000 \| \| Deployment \| Intranet only \| \| Comparison \| A vs B \| |

**Host 收尾**：completed；整轮 20.82 秒；Worker 工具调用 0 次。

**最终答案**：

| Field | Value |
|---|---|
| Project Name | BENCH-DD56A125B6 |
| Budget | CNY 180,000 |
| Deployment | Intranet only |
| Comparison | A vs B |

追溯 run_id：`c811ee411ff74c1798ee1ce07a790ff5`。

## 跨会话长期记忆：cross_session-00

场景结果：预设断言通过。

### 第 1 轮

**输入**：请记住我的长期项目事实：项目内部代号是BENCH-35F7B2EF8C，以后提到项目代号就使用它。只需确认。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.20 | planner · planner | 计划：t1 确认长期项目代号（依赖 无） |
| 2.28→3.51 | critic · judge_plan | 审核=pass；用户仅要求确认一条长期事实，无需拆分或检索；任务目标与用户请求完全一致，无遗漏分句；无依赖关系缺失或上下文脱离问题 |
| 3.75→6.64 | worker · 确认长期项目代号 | 结果：已记住你的长期项目事实：项目内部代号是 BENCH-35F7B2EF8C。以后提到项目代号时，我会使用 BENCH-35F7B2EF8C。；来源：上下文 1 条，文档/网页 0 条 |
| 6.74→8.39 | critic · verify_findings | 审核=pass；产物明确确认已记住该长期项目事实；产物明确说明后续提到项目代号时将使用 BENCH-35F7B2EF8C；Claim 引用的 context_id 与用户原话一致，内容准确 |
| 8.52→9.99 | synthesizer · synthesizer | 合成：已记住你的长期项目事实：项目内部代号是 **BENCH-35F7B2EF8C**。以后提到项目代号时，我会使用 BENCH-35F7B2EF8C。 |

**Host 收尾**：completed；整轮 10.61 秒；Worker 工具调用 0 次。

**最终答案**：

已记住你的长期项目事实：项目内部代号是 **BENCH-35F7B2EF8C**。以后提到项目代号时，我会使用 BENCH-35F7B2EF8C。

追溯 run_id：`b56592ab9922447cbcd0f5d32937587e`。

### 第 2 轮

**输入**：我的项目内部代号是什么？仅输出代号，资料不足则说明不知道。

**Host 装配**：召回记忆 1 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.42 | planner · planner | 计划：t1 确认项目内部代号（依赖 无） |
| 2.53→4.76 | critic · judge_plan | 审核=pass；用户问题为单点事实查询，无需拆分。；记忆上下文中已包含明确答案，单任务即可执行。；子任务目标清晰，验收标准符合用户“仅输出代号或说明不知道”的要求。；无依赖关系遗漏，无脱离上下文的子任务。 |
| 4.91→6.76 | worker · 确认项目内部代号 | 结果：BENCH-35F7B2EF8C；来源：上下文 1 条，文档/网页 0 条 |
| 6.88→9.34 | critic · verify_findings | 审核=pass；资料充足：记忆上下文 memory:d3cff93d84c148b6a88b5f582bdd88bb:v1 明确记载项目内部代号为 BENCH-35F7B2EF8C。；产物 summary 仅输出代号字符串，符合验收标准。；Claim 正确引用了支持该事实的 context_id。 |
| 9.45→10.09 | synthesizer · synthesizer | 合成：BENCH-35F7B2EF8C |

**Host 收尾**：completed；整轮 10.95 秒；Worker 工具调用 0 次。

**最终答案**：

BENCH-35F7B2EF8C

追溯 run_id：`c2f0841844c448698c567fd140db3b01`。

### 第 3 轮

**输入**：我的项目内部代号是什么？无资料则说明不知道，不得猜测。

**Host 装配**：召回记忆 0 条，待处理用户更新 0 条；状态版本 0，摘要版本 0。

| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |
|---|---|---|
| 0.00→2.25 | planner · planner | 计划：t1 检索项目内部代号（依赖 无） |
| 2.32→3.73 | critic · judge_plan | 审核=pass；用户问题为单点事实查询，无需拆分。；子任务目标明确，包含“无资料则说明不知道”的约束，符合用户要求。；无依赖关系缺失，无上下文脱离问题。 |
| 4.00→5.55 | worker · 检索项目内部代号 | 结果：未找到该用户项目内部代号的任何可靠资料，无法确认。；缺口：私有知识库及授权资料中未检索到项目内部代号的明确记载，不得猜测 |
| 5.67→6.88 | critic · verify_findings | 审核=pass；产物明确表述为未找到可靠资料，符合验收标准中“若未找到”的要求；产物未给出任何猜测性代号，符合“不得猜测”的要求；gaps 中记录了未检索到记载的情况，处理得当 |
| 7.01→7.94 | synthesizer · synthesizer | 合成：未找到你项目内部代号的任何可靠资料，无法确认。现有授权资料中未检索到明确记载，按你的要求不做猜测。 |

**Host 收尾**：completed；整轮 8.44 秒；Worker 工具调用 0 次。

**最终答案**：

未找到你项目内部代号的任何可靠资料，无法确认。现有授权资料中未检索到明确记载，按你的要求不做猜测。

追溯 run_id：`52163b0d4e47422e9e6802676209ad0a`。

## 边界与拒绝：boundary-00

场景结果：预设断言通过。
输入超过API的8000字符上限，HTTP 422，未触发任何Agent或模型调用。

输入：8004 个字符，超过接口上限。
处理顺序：HTTP 输入校验 → 返回 422 → 不创建运行、不调用 Planner/Worker/Critic/Synthesizer。
输出：明确拒绝过长请求。模型未生成答案。
