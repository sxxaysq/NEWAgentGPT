# 评测文档

| 文件 | 内容 |
| --- | --- |
| [`LIVE100_REPORT.md`](LIVE100_REPORT.md) | 100 个自然语言场景的结果、统计方法和代表执行过程 |
| [`LIVE100_REPORT.json`](LIVE100_REPORT.json) | 对应报告的结构化统计与逐项记录 |
| [`LIVE100_ENVIRONMENT.json`](LIVE100_ENVIRONMENT.json) | 本轮评测环境、部署配置及源码校验值 |
| [`NATURAL100_DESIGN.md`](NATURAL100_DESIGN.md) | 全部测试问题、预置资料和验收要求 |
| [`LONG_TEXT_ANSWERS_V2.md`](LONG_TEXT_ANSWERS_V2.md) | 六个长文本场景的完整实际答复 |

旧版报告及配套记录保存在 [`archive/`](archive/) 中。

## 最新记忆与推理性能记录

| 文件 | 内容 |
| --- | --- |
| [`MEMORY_LITE.md`](MEMORY_LITE.md) | 自动长期偏好、事务回执和断点恢复的配置与实现 |
| [`MEMORY_AUTO_RETEST_20261110.md`](MEMORY_AUTO_RETEST_20261110.md) | 新版部署及14个历史案例复测，保留未通过的案例 |
| [`APC_ABLATION_QUALITY_20261110.md`](APC_ABLATION_QUALITY_20261110.md) | 真正 APC 开／关消融及端到端质量评测 |
| [`NODE_PROFILER_20261110.md`](NODE_PROFILER_20261110.md) | 节点级 CPU/GPU profiling、缓存命中和耗时归因 |
| [`PREFIX_CACHE_20261110.md`](PREFIX_CACHE_20261110.md) | 前缀缓存首轮实验及候选布局的负结果 |

候选布局 `PREFIX_CACHE_LAYOUT` 默认关闭。报告中的 `artifacts/` 路径指实验机器
保留的原始证据；数据库、请求快照和大体积 profiler trace 不随源码发布。
复跑需要自行配置模型服务并提供或生成输入，单独克隆仓库不包含这些运行数据。
