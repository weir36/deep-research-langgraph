# Deep Research：基于 LangGraph 的多 Agent 深度研究助手

把用户的复杂研究问题拆解为可执行的研究计划，由多个 Agent 协作（规划 → 调研 → 计算 → 写作）生成带引用的研究报告。
在 [deer-flow](https://github.com/bytedance/deer-flow) 的流程基础上重新实现，重点解决**状态可靠性、执行效率、引用可信度和效果可度量**四个问题。

## 架构

```
START → coordinator ─┬→ END（闲聊 / 简单问题）
                     └→ planner ─┬→ human_feedback ─┐   （仅首轮计划需要用户审核）
                                 ├→ reporter → END   │
                                 └→ research_team ←──┘
                                      │  ▲   按依赖关系并行分发（Send）
                                      ▼  │
                       research_step 子图：researcher → researcher_output
                       coder（Python 沙箱）
                                      │ 本轮步骤全部完成
                                      └→ planner（评估信息是否充足，必要时生成补充计划）
```

| Agent | 职责 | 模型档位 |
|---|---|---|
| coordinator | 意图识别：闲聊直接回复，研究问题调用 `handoff_to_planner` | basic |
| planner | 生成结构化计划（`Plan`），判断信息是否充足，声明步骤依赖 | reasoning |
| human_feedback | `interrupt` 挂起，等待 `ACCEPT` / `EDIT PLAN {...}` | - |
| research_team | 按依赖计算可执行步骤，`Send` 并行分发 | - |
| researcher | 只调用 `web_search` / `knowledge_search` 收集引用 | basic |
| researcher_output | 基于本步骤引用写结论，带 `[[n]]` 行内引用 | basic |
| coder | 在隔离子进程中执行 Python 完成计算 | basic |
| reporter | 合并所有结论，全局重编号引用，生成最终报告 | reasoning |

外部依赖（时序图中的知识库平台、临时知识库、资源存储、用户记忆）均做了本地 mock：
`data/kb/` 下的文件即知识库，`-f` 传入的文件即临时知识库，报告和记忆写入 `data/runtime/`。

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填入 OPENAI_API_KEY、TAVILY_API_KEY（不填 Tavily 则使用 mock 搜索）

python main.py "固态电池的产业化进展到了什么阶段？"           # 交互式审核计划
python main.py "..." --auto-accept -f data/kb/内部报告.md    # 自动接受计划，并附带文件
python main.py --list                                        # 查看未结束的任务
python main.py --resume <thread_id>                          # 从断点继续

pytest                                                       # 测试（不需要 API Key）
python -m eval.run_eval --limit 5                            # 评测：baseline vs optimized
```

## 设计要点

### 1. 状态不可变 + reducer 合并：真正做到可中断、可恢复

节点不修改传入的状态对象，只返回增量更新；步骤结果存在 `step_results: dict[step_id, StepResult]`，由 reducer 合并。
- 并行分支各自写自己的 key，合并没有竞争；
- 每个 checkpoint 都是不可变快照，用 SQLite 持久化后，进程被杀掉也能按 `thread_id` 继续；
- research 步骤是独立子图，有自己的 checkpoint 命名空间：researcher_output 失败重试时**不会重新搜索**；
- **真杀进程验证**（`tests/test_kill_resume.py`）：子进程跑到一半被 `SIGKILL`，父进程只靠 SQLite 恢复，已完成的搜索不重做。
  这个测试第一次跑是失败的：LangGraph 默认 `durability="async"`，checkpoint 边跑边写，进程被杀时最后一批写入还没落盘，
  恢复后搜索被重跑了一遍。改成 `durability="sync"`（每个超步落盘后才进下一步）后连续 10 次通过。
  抛异常模拟的崩溃（`test_graph.py`）发现不了这个问题，因为异常路径上异步写入有机会完成。

### 2. 依赖感知的步骤并行

Planner 为每个步骤输出 `depends_on`，research_team 每次分发所有依赖已满足的步骤：
互相独立的 research 步骤同时执行，processing 步骤等待它依赖的数据。未声明依赖的 processing 步骤默认依赖之前所有步骤；依赖成环时退化为顺序执行。
researcher 在同一轮内的多个工具调用也用 `asyncio.gather` 并发执行。

### 3. 引用治理

- **局部编号 → 全局编号**：每个步骤内用 `[[1..k]]` 编号，互不干扰（这也是能并行的前提）；reporter 阶段按 URL 去重并重编号，改写各步骤结论后再交给 LLM。
- **参考文献由代码生成**：LLM 只写 `[[n]]`，不写 URL，杜绝编造链接；只列出正文实际引用的来源。
- **越界编号清理**：步骤结论和最终报告中不存在的编号被删除。
- **引用落地校验**（`grounding.py`，默认关闭，见评测结果第 3 条）：researcher_output 写完结论后，每句话连同它引用的全部来源交给 basic 模型判断，
  删掉不提供依据的编号（只删编号、不删句子）。一句一次调用，不额外搜索；判定失败时保留原引用，宁可漏删不误删。
  一句和它的全部来源一起判，而不是逐个来源单独判：「规模约 954 亿至 962 亿美元[[1]][[12]]」两个来源各给一个数，
  单独判两个引用都会被误删 —— 第一版就是这么做的，误删率接近一半。
- **提示词约束**：带引用的句子只写来源里有的事实；事实和推断分句写，推断不挂引用；不堆叠引用；数字照抄原文。
- **正文分块重排**：网页全文切块，按与查询的相关度在字符预算内选块，代替"取前 500 字"，避免关键数据落在正文后半部分时丢失。
- 传给下游的已完成步骤结论会去掉 `[[n]]`，避免把别的步骤的编号带进当前步骤造成错位。

### 4. 评测体系（`eval/`）

- **五维度打分**：事实准确性、覆盖度与深度、结构与行文、引用质量、风险与偏见控制。每个维度有分档标准，要求先写扣分理由再打分，降低"一律给高分"的倾向。
- **引用核验**：抽取带 `[[n]]` 的句子，逐条让模型判断第 n 个来源是否支持该论断，得到**引用准确率**；同时统计越界引用数和**没有引用的数字断言**。这部分不依赖打分模型的整体印象，更客观。
  核验用 reasoning 模型，线上剪枝用 basic 模型，两者不是同一个模型，避免"自己出题自己改"。
  切句前先把被硬换行折断的句子拼回去 —— 早先按行切，只拿到半句（"导供应链利润[[31]]"），引用准确率被系统性低估（0.28 → 0.44）。
- **分层诊断**（`eval/diagnose_citations.py`）：同一核验器分别核验步骤结论和最终报告，定位引用是在哪一层挂错的。
- **消融实验**：`baseline` 关闭全部优化（串行、截断、不校验、全用强模型），另有 `no_parallel`、`no_rerank`、`no_validation`、`no_tiering` 每次只关一项，把指标变化归因到具体改动。
- 同一题集、同一打分模型、逐题胜负对比；支持断点续跑和只重新打分。

### 5. 成本与安全

- **搜索缓存与预算**：评测时搜索结果落盘缓存（`search_cache.jsonl`），同一查询跨变体只搜一次；`--search-budget` 限制真实搜索次数，
  每个在跑的任务预留一次研究的用量，剩余额度不够就不开始，不会跑到一半额度耗尽。额度用完抛 `SearchBudgetExhausted` 让整次研究失败
  —— 早先这类错误被当成普通工具失败交给 LLM 重试，额度耗尽后研究在 0 个来源上照样写出报告，被评测当成有效结果打分。
- **模型分级**：调工具、单步整合用 basic 模型，规划和写报告用 reasoning 模型（`config.py` 中 `DEFAULT_NODE_TIERS`）。
- **按节点统计** token 与成本（`UsageTracker` 回调），每次运行结束输出。
- **Coder 沙箱**：独立子进程、超时强杀、CPU 时间与写文件大小限制、临时工作目录、清空环境变量（不泄露 API Key）、默认禁用网络。断网是在子进程里替换 `socket` 的连接函数实现的，能挡住常规库的网络请求，但直接用 `_socket` 或 `ctypes` 可以绕过；这是进程级隔离，生产环境应放进容器并在网络层禁网。

## 相对参考实现修复的问题

| 问题 | 后果 | 处理 |
|---|---|---|
| 工具调用循环中每个 tool_call 都 `append(response)` | 一次返回多个调用时消息历史重复，OpenAI 接口报错 | 每轮只追加一次 AI 消息，每个 tool_call 都有对应 ToolMessage |
| 引用起始编号从从未写入的 `state["plans"]` 计算 | 第二轮计划编号从 1 重来，报告中同一编号指向不同来源 | 步骤内局部编号，报告阶段全局去重重编号 |
| 原地修改 `current_plan.steps[i].execution_res`、`observations.append` | checkpoint 语义被破坏，无法并行、无法可靠恢复 | 只返回增量，reducer 合并 |
| `_next_node` 不在 State 中 | 返回值被静默丢弃 | 用 `Command(goto=...)` 和条件边路由 |
| `plan_iterations` 从未递增 | 最大迭代保护不生效，可能无限循环 | planner 每次生成计划时递增，达到上限直接写报告 |
| Coordinator 用 `len(input) > 10` 判断意图 | "你好，请介绍一下你自己" 也会进入研究流程 | 使用 `handoff_to_planner` 工具调用 |
| 引用内容 `[:500]` 截断 | 正文后半部分的数据丢失 | 分块重排 |
| 编辑后的计划缺 `step_type` / `need_search` | 前端只能改标题和描述，新增步骤校验失败 | Pydantic `model_validator` 宽松解析并推断默认值 |

## 评测结果

5 道研究题，打分和引用核验都用 `gemini-4-argon`；basic 档 `gemini-3.8-flash`，reasoning 档 `gemini-4-argon`。
完整表格：`eval/results/final-v2/summary.md`。

| 变体 | 综合得分 | 引用准确率 | 成本 | 耗时 | LLM 调用 |
|---|---:|---:|---:|---:|---:|
| baseline（参考实现做法：串行、截断 500 字、不校验、全用强模型） | 78.8 ± 8.9 | 0.64 | $0.11 | 474s | 24 |
| v1（上一版：并行 + 重排 + 越界清理 + 模型分档） | 76.4 ± 8.2 | 0.66 | $0.06 | 332s | 20 |
| v1 关并行 | 82.2 ± 14.8 | 0.66 | $0.07 | 480s | 19 |
| **optimized**（v1 + 引用提示词约束，**默认**） | **93.2 ± 1.5** | **0.78** | **$0.08** | 389s | 20 |
| strict（optimized + 逐句引用剪枝） | 89.7 ± 3.2 | 0.82 | $0.12 | 430s | 72 |

引用准确率按全部抽查的「句子 × 引用」合并计算：v1 82/125 = 0.656 → optimized 84/107 = 0.785 → strict 102/125 = 0.816。

**结论**

1. **工程优化把成本降了 45%、耗时降了 30%，质量持平**（baseline → v1：$0.11 → $0.06，474s → 332s，得分差 2.4 分，在 9 分的标准差以内）。
   其中并行单独贡献了 31% 的耗时下降（480s → 332s）。
2. **引用准确率 0.66 → 0.78，靠的是提示词约束**，成本只多 $0.02。分层诊断（`eval/diagnose_citations.py`）显示问题主要出在
   步骤结论本身（步骤层 0.36），而不是 reporter 改写：模型把多个来源的信息和自己的推断揉进一句话，句尾挂一串编号。
   约束改成「事实和推断分句写、推断不挂引用、不堆叠引用、数字照抄」。
3. **逐句剪枝在此之上只多 0.03**（0.785 → 0.816，在噪声范围内），却让 LLM 调用 ×3.7、成本 +50%，所以**默认关闭**，
   留作对引用要求极严时的开关（`ground_citations=True`）。离线在 v1 的 21 个真实步骤上单测剪枝，引用准确率 0.73 → 0.87，
   说明剪枝本身有效，只是提示词约束已经拿走了大部分收益。

**需要一起说的**

- **样本只有 5 题。** optimized 的 93.2 比 v1 高 17 分，远超标准差，但 v1 和 optimized 是两轮分开跑的（搜索结果随时间变化），
  不是同一轮的对照；同一轮里的 optimized vs strict 才是干净的对照。
- **v1 关并行比 v1 高 6 分**，但两者的上下文完全相同（并行只改执行时机），标准差 15 分，应视为随机波动。
- **「无引用的数字断言」从 8 升到 13**：约束让模型不再给推断挂引用，一部分数字也跟着没了引用。
  这是把「挂错引用」变成了「没有引用」，读者至少能看出哪些数字没有出处。
- **打分模型偏向 baseline**：baseline 所有节点都用 argon，和打分模型相同。
- 搜索额度：最终一轮实发 300 次 Tavily 查询（预算 480），缓存命中跨变体复用。

## 目录结构

```
deep_research/
  graph.py            状态图
  state.py            全局状态与 reducer
  schemas.py          Plan / Step / Reference / StepResult
  references.py       分块重排、引用校验、全局重编号
  grounding.py        引用落地校验：删掉来源不支持的编号
  runner.py           SQLite checkpoint、运行与恢复
  llm.py              模型工厂、token / 成本统计
  nodes/              各 Agent 节点
  tools/              网页搜索、知识库、Python 沙箱
  services/           资源存储、用户记忆（mock）
  prompts/            Jinja2 prompt 模板
eval/                 评测题集、打分与引用核验、对比脚本、分层诊断、离线剪枝验证
tests/                脚本化假 LLM 的端到端与单元测试
```
