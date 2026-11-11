# 测试数据

[`natural100_v2.json`](natural100_v2.json) 收录 100 个自然语言场景，包含日常问答、多步骤任务、长文本、资料检索、多轮对话及长期记忆等 12 类测试。

每条记录包含以下字段：

| 字段 | 含义 |
| --- | --- |
| `case_id` | 场景编号 |
| `category` | 测试类别 |
| `scene` | 场景名称 |
| `prompts` | 按顺序发送的用户输入 |
| `rubric` | 回答的验收要求 |
| `document` | 测试前上传的资料，未使用时为空 |
| `memory` | 是否为该场景启用长期记忆 |

运行入口是 [`scripts/benchmark_natural100.py`](../scripts/benchmark_natural100.py)。题目和验收要求可阅读[场景设计文档](../docs/NATURAL100_DESIGN.md)，运行结果见[执行报告](../docs/LIVE100_REPORT.md)。
