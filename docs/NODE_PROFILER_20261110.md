# 节点级 APC profiling 结果

2026-11-10。结论：明确受益的是 Worker 工具循环；Planner 和 Worker 整理产物的
Prefill 也受益。未测得 Critic、Synthesizer、Host 记忆抽取的缓存收益，不能宣称
Host 调度代码被 APC 加速。当前候选重排开关仍关闭，测量使用原布局。

## 部署和实验范围

- 215 独立 vLLM 0.23.0，2×H100，原 Qwen3.5 架构权重、BF16、TP=2、32K 上下文、
  0.40 显存预算。正式 8000 推理服务和业务服务保持原配置。
- 实际重启测试引擎，分别使用 enable-prefix-caching/no-enable-prefix-caching。
- PyTorch profiler 采集前端 CPU 和两个 GPU rank 的 trace；with_stack=false。
  每种开关对 8 类请求分别采样，共 32 份 GPU trace、16 份前端 trace。
- Host 部署 cProfile，保留 .pstats、函数 self/cumulative 时间及整个进程的 CPU/墙钟时间。
- 性能计时期间 profiler 不处于采样状态；采样和导出另做，不把导出开销算作服务延迟。
- 从上轮真实 A 组完整模型请求中取样：7 类各 6 个请求，Worker 工具循环 12 个。
  每类保持原顺序、组内共享 salt，重复 3 次；APC 开关各 162 次，共 **324 次完整生成回放**。
  salt 用于隔离试验，开关由引擎参数实际控制。
- 162 对请求经服务端模板得到的 Token hash 全部一致。114 对输出 Token 数相同，
  其余不同，因此不能直接用总耗时证明缓存使输出变快。
- 分角色回放用于隔离节点贡献，不代表生产混合并发下每个节点必有同等命中率；
  不在此声称对相同输入的生成结果完全一致或完成新一轮答案质量非劣效检验。

## 正式计时结果

下表均为每组排除第一条初始请求后的平均值。通常每节点 15 个样本，Worker 工具循环
33 个。TTFT 是服务端首 Token 延迟，不是前端 UI 的可见首字延迟；当前客户端使用 ainvoke。

| 节点/模型调用 | TTFT：关→开 | Prefill：关→开 | 开启后的命中 Token 比例 | 判断 |
|---|---:|---:|---:|---|
| Planner | 239.3→156.0 ms | 219.3→136.4 ms | 55.7% | 输入阶段受益，TTFT 下降34.8% |
| Worker 工具循环 | 453.9→153.6 ms | 420.7→120.3 ms | 85.0% | 最明显；TTFT下降66.2% |
| Worker 整理产物 | 392.4→235.9 ms | 365.7→208.5 ms | 51.5% | 输入阶段受益，TTFT下降39.9% |
| Critic 计划评审 | 206.8→245.5 ms | 189.7→228.5 ms | 0% | 未发现缓存收益 |
| Critic 结果验收 | 306.2→323.7 ms | 283.9→302.5 ms | 0% | 未发现缓存收益 |
| Synthesizer | 273.6→296.4 ms | 256.0→275.7 ms | 0% | 未发现缓存收益 |
| Host 长期记忆解析 | 113.1→116.3 ms | 100.1→104.2 ms | 0% | 未发现缓存收益 |

没有命中的节点在本轮反而略慢；这不构成稳定减速的统计结论。未锁 GPU 时钟，
测试引擎与驻留的正式模型共享 GPU；本报告不把十几毫秒的波动宣传成优化。

### 为什么不能说“Critic 输出变快了”

Critic 验收请求总耗时从平均 2.132 s 降为 1.872 s，看起来快约12%。但输出长度
从144.4降到123.4 Token；缓存命中是0，TTFT还增加了。
在同输入、同输出长度的9个配对样本中，总耗时 **1.796→1.802 s**，基本没有变化。
每个后续输出 Token 时间约 **12.69→12.61 ms**，不支持“解码明显加速”的说法。

其他同输出长度配对结果（这些是额外分层，未用来替代全部样本）：

| 节点 | 配对数量 | 请求总耗时：关→开 |
|---|---:|---:|
| Worker 工具循环 | 30 | 0.997→0.648 s，下降约35% |
| Worker 整理产物 | 3 | 2.698→2.430 s；样本很少 |
| Planner | 3 | 7.943→7.893 s；长输出使首 Token 收益占比很小 |
| Critic 计划评审 | 9 | 2.044→2.047 s |
| Synthesizer | 15 | 3.347→3.348 s |
| Host 记忆解析 | 12 | 0.864→0.863 s |

Planner 全部样本平均请求耗时4.851→4.828 s，也说明首 Token 快35%并不代表整次
规划快35%。长 JSON 的逐 Token 生成仍占主要时间。

## 原布局为什么产生这些差异

服务的真实 metrics 显示：`block_size=784`、`mamba_block_size=16`、
`mamba_cache_mode=align`。它需要同时满足注意力 KV 块和混合模型状态的复用条件。
只看两个提示词有一段相同文字是不够的；必须保存过可匹配位置的计算状态。

以下取第一轮试验中相邻请求的真实 Token 数据：

| 节点 | 上一请求输入 | 下一请求输入 | 连续公共前缀 | 实际命中 |
|---|---:|---:|---:|---:|
| Planner | 2215 | 2288 | 1648 | 1568 |
| Worker 整理 | 2279 | 2313 | 1607 | 1568 |
| Worker 工具循环 | 2405 | 2972 | 2405 | 2352 |
| Critic 计划评审 | 1682 | 1750 | 998 | 0 |
| Critic 验收 | 2012 | 2261 | 1178 | 0 |
| Synthesizer | 1914 | 2194 | 1098 | 0 |
| Host 记忆解析 | 764 | 1047 | 738 | 0 |

- **Planner**：固定角色说明、规则和 Plan JSON Schema 构成较长公共开头，部分请求
  到当前目标发生变化之前已经覆盖可复用检查点，因此命中。任务目标本身并没有缓存答案。
- **Worker 整理产物**：固定 Worker 规则和输出 Schema 能形成公共开头，部分整理/重试
  请求还共享业务上下文；主要收益仍在 Prefill。不同请求的工具材料并非全都复用。
- **Worker 工具循环**：System、工具集合、本任务初始上下文保持不变，AI tool call
  和工具结果不断追加，旧请求几乎完整地留在新请求开头。最容易跨过已缓存的状态位置。
- **Critic**：有固定提示词，但输入很快进入不同任务图、验收要求、产物与证据。
  例中公共前缀998/1178 Token，没能匹配可用的完整状态检查点，实际命中为0。
  例如上一份计划评审输入为1682 Token，公共部分不足1568 Token的后部对齐位置；
  在 align 策略下，不能据此假定较早的784位置也保存了可复用状态。
- **Synthesizer**：每次合并的 findings 和任务上下文变化早，公共开头不足以触发实际命中。
- **Host 记忆解析**：短输入中用户原话与已有记忆快照占较大比例；例中的公共前缀738
  Token还不到一个784 Token注意力块。它和 Host 的调度代码是两件不同的事。

上述收益来自原来的固定提示词/Schema与追加轨迹，**不是上轮候选重排的新增收益**。
仅把字段挪到前面，不保证跨过缓存检查点，更不能通过保留已失效记忆换取命中。

## Host cProfile：自身计算和等待要分开

使用一个完整预算计算任务做 CPU profiling。首次 off Host 在 SIGTERM 后没有输出
pstats，已修复 profiler 收尾信号处理，并单独重采 off Host；旧任务记录仍保留。

| 指标 | APC关（重采） | APC开 |
|---|---:|---:|
| 完整任务耗时 | 15.68 s | 13.36 s |
| agentgpt/host 模块函数 self time 合计 | 15.17 ms | 13.34 ms |
| Host进程CPU时间，含启动/导入/其他模块 | 5.28 s | 4.94 s |
| Host进程墙钟时间，含启动/退出 | 20.51 s | 18.03 s |

Host模块self time是函数自身计时，**不包括其调用库的全部开销，也不是完整任务CPU
成本**。每组只有一个任务，输入中的运行标识不同、生成结果也可不同，不能用约2ms
差异认定调度稳定加速。更不能把整轮少等的2.32秒全部说成Host调度计算提速。

Host的队列、SQLite checkpoint、A2A分发和状态校验没有使用模型KV缓存；它只能通过
下游请求变快间接受益。本轮没有测得Host调度自身的可归因加速。

## PyTorch GPU trace 核查

原始双卡和前端trace均保留。下面只列同一rank0的代表请求，不能把两张卡有重叠的
kernel时间相加当作墙钟时间，也不能用profiling状态下的数据替代正式计时。

| 节点代表请求 | rank0 kernel累计时间：关→开 |
|---|---:|
| Planner | 7712→7566 ms |
| Worker 整理 | 3561→3448 ms |
| Worker 工具循环 | 1081→542 ms |
| Critic 计划评审 | 2539→2507 ms |
| Critic 验收 | 2133→2134 ms |
| Synthesizer | 2454→2448 ms |
| Host 记忆解析 | 1108→1135 ms |

Trace包含完整生成过程，可以看到大量nvjet线性运算kernel、混合注意力运算和TP通信。
Worker工具循环的GPU累计执行量明显减少，与实际缓存命中、Prefill下降一致；Critic
验收与Synthesizer的总GPU执行量基本未变。采样代表请求的输出长度并未另存响应，
所以这些trace只用于诊断佐证，不作为独立、严格等输出长度的加速率实验。

## 方法修正与限制

- 初次试跑的 ignore_eos 与 JSON grammar 发生冲突，保留在 failed-ignore-eos*，
  不纳入正式计时；去掉该参数后重新完整执行。
- LangChain请求使用max_completion_tokens，vLLM优先采用它。早期驱动还携带较低的
  max_tokens，但未覆盖角色原上限；开启组前清理了冗余字段。本报告按实际usage分析
  完整输出，不称“固定32 Token”。
- 检索改写直接使用max_tokens，off计时上限32与on原上限不同，但样本输出均提前结束，
  同输出长度样本总耗时0.437→0.458 s，缓存命中0。它只作为短请求无命中的观察，
  未纳入上方严格节点结论表；其GPU采样实际输出长度不同，排除GPU时间对照。
- 其余七类节点保留相同有效生成上限；三次重复、完全相同输入Token仍可能生成不同
  输出。因此同时报告TTFT、Prefill、实际输出量、单位输出Token时间和同长度分层。
- 此轮无并发扫描、无新的布局修改、无模型质量非劣效声明；正式服务未改变。

## 文件与复跑

本地证据目录：`artifacts/node-profile-20261110/`。

- summary.json：节点指标、同输出长度对照、Host统计、rank0 kernel摘要。
- nodes-off/on.jsonl、selected.json：正式计时与原始请求集。
- cache-config-on.txt：运行时align模式和784块大小的直接证据。
- traces-off/on.json：每个节点与trace文件的映射；traces/：双卡及前端原始trace。
- A/B/host.pstats、host-profile.txt、host-process.json：Host CPU profiling。
- A/quality-reprofile/、B/quality/：与有效Host profiler对应的任务结果。
- evidence.tar：上述运行证据归档，不含生产.env。
- scripts/profile_agent_nodes.py：配对节点回放及/start_profile、/stop_profile采集。
- scripts/summarize_node_profile.py：统计和trace分析。

可以在 Perfetto 中载入对应 `.pt.trace.json.gz` 查看时间线。profiling接口用法参考
[vLLM官方Profiling说明](https://docs.vllm.ai/en/stable/examples/features/profiling/)。
所有本次测试进程已停止，正式服务健康、显存恢复到测试前水平；候选布局保持关闭。
