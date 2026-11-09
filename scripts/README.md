# 运行与维护脚本

以下脚本均从项目根目录运行。服务安装和配置见[项目首页](../README.md)。

| 脚本 | 用途 |
| --- | --- |
| [`a2a_ctl.py`](a2a_ctl.py) | 在 Linux 下启动、停止、重启或检查五个服务进程 |
| [`e2e_check.py`](e2e_check.py) | 向已启动的服务提交目标，检查完整执行过程 |
| [`validate_memory_v3.py`](validate_memory_v3.py) | 运行不调用真实模型的记忆与上下文测试 |
| [`validate_memory_live.py`](validate_memory_live.py) | 调用真实服务，检查多轮修改和长期记忆 |
| [`design_natural_cases.py`](design_natural_cases.py) | 生成自然语言测试场景数据 |
| [`benchmark_natural100.py`](benchmark_natural100.py) | 执行 100 个自然语言场景并保存运行记录 |
| [`review_natural_results.py`](review_natural_results.py) | 调用模型评阅已保存的答复 |
| [`render_natural_design.py`](render_natural_design.py) | 将测试问题和验收要求整理为 Markdown |
| [`render_natural_report.py`](render_natural_report.py) | 根据保存的记录生成自然语言评测报告 |
| [`benchmark_live_100.py`](benchmark_live_100.py) | 旧版 100 场景评测及执行记录采集 |
| [`render_live100_report.py`](render_live100_report.py) | 生成旧版报告，并提供共用的报告整理函数 |
| [`enrich_benchmark_timeouts.py`](enrich_benchmark_timeouts.py) | 从已有日志补充超时场景的执行记录 |
| [`reconcile_benchmark_resume.py`](reconcile_benchmark_resume.py) | 根据同一次继续运行的最终记录校正暂停状态 |
| [`rebuild_memory_index.py`](rebuild_memory_index.py) | 重建长期记忆的向量索引 |
| [`rebuild_document_index.py`](rebuild_document_index.py) | 重建文档向量和全文检索索引 |
| [`verify_context_tokenizer.py`](verify_context_tokenizer.py) | 对比部署服务与本地的输入 token 计数 |

重建索引前先停止 API 服务。真实服务评测需要模型及相关检索服务可用；本地回归测试的运行方式见 [`tests/`](../tests/)。
