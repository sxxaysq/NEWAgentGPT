# 应用代码

五个角色分别运行在独立进程中。Host 提供网页和 API，负责调度其余四个智能体。

| 目录或文件 | 用途 |
| --- | --- |
| [`__main__.py`](__main__.py) | 按 `--role` 参数启动指定角色 |
| [`app.py`](app.py) | 网页、账号、会话、资料和记忆接口 |
| [`config.py`](config.py) | 读取环境变量及 `.env` 配置 |
| [`a2a/`](a2a/) | 智能体通信、身份校验和任务持久化 |
| [`agents/`](agents/) | 规划、执行、审核和汇总四个角色 |
| [`host/`](host/) | 任务调度、轮次记录、上下文装配和历史摘要 |
| [`static/`](static/) | 网页 HTML、样式和交互脚本 |
| [`model.py`](model.py) | 模型调用与结构化输出处理 |
| [`mcp_manager.py`](mcp_manager.py)、[`mcp_server.py`](mcp_server.py) | MCP 服务连接和内置工具 |
| [`store.py`](store.py)、[`durable.py`](durable.py) | 应用数据与持久任务队列 |
| [`memory.py`](memory.py)、[`memory_store.py`](memory_store.py) | 长期记忆的提取、存储和召回 |
| [`ingestion.py`](ingestion.py)、[`document_store.py`](document_store.py) | 文档解析、分块及版本记录 |
| [`retrieval.py`](retrieval.py)、[`retrieval_pipeline.py`](retrieval_pipeline.py) | 文档检索、查询扩展和结果重排 |
| [`web_retrieval.py`](web_retrieval.py)、[`evidence_store.py`](evidence_store.py) | 网页内容获取与证据记录 |

安装和启动步骤见[项目首页](../README.md)。
