# 回归测试

安装开发依赖后，在项目根目录执行：

```bash
python -m pytest -q
python -m ruff check agentgpt tests
```

| 测试文件 | 检查内容 |
| --- | --- |
| `test_a2a_protocol.py` | A2A 协议和任务通信 |
| `test_orchestrator.py`、`test_durable.py` | 任务调度与持久命令处理 |
| `test_recovery.py` | 进程恢复和工具回执复用 |
| `test_turns.py`、`test_summaries.py` | 对话轮次与摘要记录 |
| `test_context.py`、`test_context_api.py`、`test_runner_context.py` | 上下文装配及接口行为 |
| `test_memory.py`、`test_memory_hybrid.py` | 长期记忆和混合召回 |
| `test_rag.py`、`test_web_retrieval.py` | 私有文档和网页检索 |
| `test_prompts.py`、`test_structured.py` | 提示词和结构化输出 |
| `test_benchmark_trace.py` | 评测执行记录的采集 |

`conftest.py` 提供公共测试配置，`fixture_mcp_server.py` 和 `recovery_process.py` 用于工具及进程测试。真实模型评测使用 [`scripts/`](../scripts/) 下的专用脚本。
