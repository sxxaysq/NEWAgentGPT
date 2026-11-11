# 前缀缓存首轮实验（2026-11-10）

## 结论与当前状态

在“设计记忆模块最小实现方案”聊天完成部署和复测后开始实验。
基线为已部署的 memory-auto-20261110：服务器回归 259 项通过，历史案例 8/14
通过；其余业务和上下文语义失败未被当作本次性能优化解决。

完成 360 次首轮回放和 192 次服务端 TTFT 补测，共 552 次请求。现有 Worker
追加消息方式已能有效使用 APC。此次稳定字段前置与工具排序没有可确认的新增收益，
因此 `PREFIX_CACHE_LAYOUT` 默认仍为 false，正式 Agent 和 vLLM 服务未切换配置。

## 环境与方法

- 192.168.30.215，2 × NVIDIA H100 80GB，vLLM 0.23.0。
- 服务名 qwen3.8-27b；权重配置为 Qwen3_5ForConditionalGeneration，BF16，TP=2。
- 131072 上下文、max_num_seqs=16、max_num_batched_tokens=8192、GPU 利用预算 0.50。
- APC 已开启。启动日志确认 Attention block size 为 **784 Token**。
- 从正式 Worker SQLite 的 execution_journal 只读提取最近 25 个 checkpoint；
  在独立 /tmp/agentgpt-prefix-20261110 目录回放，未调用任何实际工具。
- 2/4/8 轮工作负载取最长真实轨迹前若干次工具结果之后的消息，是真实工具交互
  的回放；后续请求使用原始记录，未使用本轮生成结果构造闭环。
- real_tasks 为最近 8 个任务的初始输入；controlled_request_change 为同一真实
  输入仅修改 current_request 的受控样本，不称为真实多轮追问。
- 工具定义经当前生产 MCPTool 适配器重建四个内置检索工具。checkpoint 不保存
  当时绑定的工具集合，所以这不是逐字节完整线上请求抓包。
- temperature=0、seed=42、max_tokens=1、enable_thinking=false，并发 1。
  测试组随机顺序，3 次重复；同组请求按顺序发送，无真实工具等待间隔。
- cold 每次使用独立随机 cache_salt；reuse 同组共享 salt。两者都开启 APC。
  **冷缓存不能冒充 APC 关闭，不构成原方案完整 A/B/C/D 消融。**
- `/tokenize` 使用服务端实际模板和工具定义计算公共 Token 前缀；模型请求前后
  采集 metrics。汇总组的 prefix queries 增量与本组输入 Token 总量一致。
- 以下后续请求统计排除每组首条（初始建缓存）请求；原始 JSONL 保留全部请求。

## 结果

首轮原布局、冷缓存对比连续复用：

| 工具轮次 | 后续请求数 | 冷缓存耗时中位数 | 复用耗时中位数 | 缓存 Token 比例 |
|---|---:|---:|---:|---:|
| 2 | 3 | 288.5 ms | 124.8 ms | 75.3% |
| 4 | 9 | 306.6 ms | 129.0 ms | 78.9% |
| 8 | 21 | 344.4 ms | 114.5 ms | 84.0% |

单 Token 有时被工具解析器保留，客户端未收到可见内容，因此客户端 TTFT 保持
null。没有把上述请求耗时错误命名为 TTFT。另一次 192 请求补测使用服务端
time_to_first_token_seconds 的 sum/count 增量：

| 8 轮负载 | 服务端 TTFT 均值 | Prefill 均值 | 请求耗时中位数 |
|---|---:|---:|---:|
| 原布局，冷缓存 | 339.9 ms | 308.4 ms | 343.8 ms |
| 原布局，连续复用 | 135.2 ms | 106.6 ms | 136.6 ms |
| 候选布局，冷缓存 | 331.7 ms | 303.3 ms | 342.1 ms |
| 候选布局，连续复用 | 130.9 ms | 103.1 ms | 134.0 ms |

原布局复用相对冷缓存的服务端 TTFT 下降约 **60.2%**，Prefill 下降约 **65.4%**。
这是现有 APC 的效果，不能记作本次新增代码收益。两次实验中候选布局相对原布局
的快慢方向不同，缓存命中比例也相同，暂不采纳为默认优化。

受控当前请求变化中，公共前缀从 2158 增加到 2208 Token，增加 50 Token；
两种布局实际缓存命中均为 0。真实任务组的命中约 12.3%，两种布局基本相同。
大缓存块和混合注意力状态的缓存边界是下一步重点核查对象。仅公共前缀变长不足以
证明引擎会命中，也不能仅用 784 的整除关系完全解释零命中。

官方混合缓存说明要求对不同层类型缓存命中取交集，并指出混合模型可能需要较大的
块：[Hybrid KV Cache Manager](https://docs.vllm.ai/en/stable/design/hybrid_kv_cache_manager/)。
本轮已核对安装版 cache.py 对 all/align/none 的定义，但尚未确认运行时最终选择，
不能宣称已验证修改 Mamba 缓存模式的收益。

## 实现与复跑

`agentgpt/prefix_layout.py` 仅重排顶层稳定字段，保留全部字段值、嵌套顺序、版本、
来源标识、记忆回执和权限 manifest，不保留被删除的记忆。四个业务角色接入同一
序列化函数，Worker 开关启用时按完整工具名排序，权限和实际执行逻辑不变。
公共 System 提示词、结构化纠错和长期偏好抽取提示词本轮尚未更改。

在项目根目录（Linux）复跑：

```bash
PYTHONPATH=. python scripts/benchmark_prefix_cache.py \
  --journals artifacts/prefix-cache-20261110/worker-journals.json \
  --out artifacts/prefix-cache-rerun --repeats 3
python scripts/summarize_prefix_cache.py \
  artifacts/prefix-cache-rerun/requests.jsonl artifacts/prefix-cache-rerun/summary.json
```

隔离评测进程可设置 `PREFIX_CACHE_LAYOUT=true`；移除该环境变量即恢复原布局。
样本含原测试会话文本，只在已有项目和同一服务器内使用。

证据在 `artifacts/prefix-cache-20261110/`：environment.txt、source-hashes.json、
worker-journals.json、results/、ttft-results/、summary.json、ttft-summary.json。
初次 360 请求的脚本版本未采集服务端 TTFT；后续版本已补齐，故其摘要对应项为 null。

## 范围限制与下一步

验证：开启候选布局时，前缀保真、结构化输出、上下文、编排和轻量记忆相关测试
共 76 项通过，Ruff 检查通过。恢复专项首次在本地代理环境下出现 6 项健康检查
超时（当次 53 项通过）；堆栈显示 localhost 请求进入 HTTP 代理。仅为测试进程
设置 `NO_PROXY=127.0.0.1,localhost` 后，开启候选布局的恢复专项 10 项全部通过，
JUnit 记录为 `artifacts/prefix-cache-20261110/recovery.xml`。
552 个模型请求输出长度均为 1 Token，所有请求采样前运行/等待计数为 0。

目前仅完成前缀缓存方向的首轮实验和可关闭候选实现，未完成整个优化路线。
不报告 TPOT、任务端到端提速或答案质量不下降：单 Token 回放无法测量这些指标。
未测并发 2/4/8、真实工具间隔与缓存驱逐、长输出、摘要更新和记忆删除后的闭环质量。
工具 Schema 的跨发现顺序与结构化重试提示词也还需独立实验。

下一阶段先确认混合模型的实际缓存模式和块边界，再以足够长的真实历史做受控消融；
之后在隔离推理实例完成真正 APC 关/开对照，保持模型、输出长度和服务参数一致。
任何候选都需完成业务、记忆纠正/删除、来源引用和工具参数质量回归后才可默认启用。
