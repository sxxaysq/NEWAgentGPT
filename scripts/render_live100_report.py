# ruff: noqa: E501
"""Render compact, auditable action traces from real benchmark records."""

import argparse
import collections
import json
from pathlib import Path

LABELS = {
    "single_hop": "单点事实",
    "multi_step": "多步骤任务",
    "long_text": "长文本",
    "private_retrieval": "私有知识库检索",
    "web_retrieval": "联网检索",
    "hybrid_retrieval": "私有＋联网混合检索",
    "recovery": "暂停与继续",
    "typos": "错别字输入",
    "isolation": "用户及项目隔离",
    "in_session": "同会话多轮纠正",
    "cross_session": "跨会话长期记忆",
    "boundary": "边界与拒绝",
}


def compact(text, limit=400):
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    if len(text) > limit:
        text = text[: limit // 2] + " …〔中间省略〕… " + text[-limit // 2 :]
    return text.replace("\n", " ").replace("|", "\\|")


def output_summary(action):
    outputs = action.get("output") or []
    parts = []
    for output in outputs:
        if "tasks" in output:
            parts.append(
                "计划："
                + "；".join(
                    f"{t['id']} {t.get('title', '')}（依赖 {','.join(t.get('depends_on', [])) or '无'}）"
                    for t in output["tasks"]
                )
            )
        elif "verdict" in output:
            parts.append(
                "审核="
                + output["verdict"]
                + "；"
                + "；".join(output.get("reasons", []))
                + ("；修改建议：" + "；".join(output["actions"]) if output.get("actions") else "")
            )
        elif "summary" in output:
            parts.append(
                "结果："
                + output["summary"]
                + ("；缺口：" + "；".join(output["gaps"]) if output.get("gaps") else "")
            )
            claims = output.get("claims") or []
            contexts = sorted({c for claim in claims for c in claim.get("context_ids", [])})
            evidence = sorted({c for claim in claims for c in claim.get("evidence_ids", [])})
            if contexts or evidence:
                parts.append(f"来源：上下文 {len(contexts)} 条，文档/网页 {len(evidence)} 条")
        elif "answer_md" in output:
            parts.append("合成：" + output["answer_md"])
        else:
            parts.append(compact(output))
    return compact("；".join(parts) or action.get("status", "无产物"), 650)


def representative(group, rows):
    # Deterministic published selection rule, including failures in the summary.
    if group == "boundary":
        return next((r for r in rows if r.get("rejection")), rows[0])
    preferred = [r for r in rows if r["passed"]] or rows

    def richness(r):
        actions = [a for run in r["runs"] for a in run["actions"]]
        tools = sum(len(a["tools"]) for a in actions)
        workers = sum(a["role"] == "worker" for a in actions)
        return (
            tools
            if group in {"web_retrieval", "hybrid_retrieval", "private_retrieval", "typos"}
            else workers,
            len(r["runs"]),
            -r["index"],
        )

    return max(preferred, key=richness)


def render(source, dest):
    rows = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(source.glob("*.json"))
        if p.name != "manifest.json"
    ]
    groups = collections.defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
    runs = [run for row in rows for run in row["runs"]]
    passed = sum(r["passed"] for r in rows)
    complete = sum(r["status"] == "completed" for r in runs)
    actions = [a for r in runs for a in r["actions"]]
    tools = [t for a in actions for t in a["tools"]]
    lines = [
        "# 100 个真实案例：结果与各类代表执行过程",
        "",
        "测试日期：2026-11-03（北京时间）。当前发布版本在同一五进程部署上运行，数据均为测试构造。",
        "",
        f"共 **{len(rows)} 个场景**，包含 **{len(runs)} 个真实用户运行**、**{len(actions)} 个 A2A 任务**和 **{len(tools)} 次记录到回执的工具调用**。",
        f"场景断言通过 **{passed}/{len(rows)}**；运行终态 completed **{complete}/{len(runs)}**。API 边界拒绝不产生 Agent 调用，也不伪装成模型回答。",
        "",
        "这些通过率来自明确的字符串、来源、状态和隔离断言，不是独立模型语义评审的准确率。没有将之前的模拟契约测试计入本报告。",
        "",
        "## 类别结果",
        "",
        "| 类别 | 场景 | 断言通过 | 真实运行数 |",
        "|---|---:|---:|---:|",
    ]
    for group, label in LABELS.items():
        subset = groups[group]
        lines.append(
            f"| {label} | {len(subset)} | {sum(r['passed'] for r in subset)} | {sum(len(r['runs']) for r in subset)} |"
        )
    lines += [
        "",
        "## 记录口径",
        "",
        "- 顺序来自 A2A TaskStore 的真实创建/更新时间。表格按开始时间排序；时间区间重叠表示并发，不表示后一项必须等前一项结束。",
        "- Worker 内部工具调用按持久 journal 的顺序列出；journal 没有单次工具精确时间戳，因此不编造工具耗时。输出只保留摘要。",
        "- Host 负责接收、鉴权、轮次落库、上下文装配、调度和最终提交；状态/摘要/长期事实抽取是 Host 后台任务，不计作额外 A2A 子 Agent。",
        "- 恢复类别是真实 pause/continue；没有在用户正在使用的服务上强制杀进程。进程崩溃能力另由恢复单测验证。",
        "- 每类优先选择通过且包含较完整执行链的案例；所有失败保留在下一节和机器可读汇总中，不能从代表例推断整类全部通过。",
        "",
        "## 失败与限制",
        "",
    ]
    failed = [r for r in rows if not r["passed"]]
    if not failed:
        lines.append(
            "本次所有场景的预设断言通过；这不意味着任意输入、网络环境或语义要求均可正确处理。"
        )
    for row in failed:
        bad = [x["name"] for x in row["checks"] if not x["passed"]]
        lines.append(f"- **{row['case_id']}**：{compact(row.get('error') or '、'.join(bad), 500)}")
        for run in row["runs"]:
            if run["status"] != "completed":
                lines.append(f"  - {run['status']}：{compact(run['answer'], 300)}")
    selected = {}
    for group, label in LABELS.items():
        row = representative(group, groups[group])
        selected[group] = row["case_id"]
        lines += [
            "",
            f"## {label}：{row['case_id']}",
            "",
            f"场景结果：{'预设断言通过' if row['passed'] else '存在失败，见上方说明'}。",
        ]
        for note in row.get("notes", []):
            lines.append(note)
        if row.get("rejection"):
            reject = row["rejection"]
            lines += [
                "",
                f"输入：{reject['input_chars']} 个字符，超过接口上限。",
                f"处理顺序：HTTP 输入校验 → 返回 {reject['http_status']} → 不创建运行、不调用 Planner/Worker/Critic/Synthesizer。",
                "输出：明确拒绝过长请求。模型未生成答案。",
            ]
        for number, run in enumerate(row["runs"], 1):
            lines += [
                "",
                f"### 第 {number} 轮",
                "",
                "**输入**：" + compact(run["input"], 1000),
                "",
                f"**Host 装配**：召回记忆 {run['context']['memory_count']} 条，待处理用户更新 {run['context']['pending_updates']} 条；状态版本 {run['context']['state_version']}，摘要版本 {run['context']['summary_version']}。",
                "",
                "| 开始→结束（相对秒） | Agent / 动作 | 输入与结果摘要 |",
                "|---|---|---|",
            ]
            start = min((a["started"] for a in run["actions"]), default=0)
            for action in run["actions"]:
                spec = action.get("input", {}).get("task", {})
                purpose = spec.get("title") or action.get("operation") or action["role"]
                interval = f"{action['started'] - start:.2f}→{action['finished'] - start:.2f}"
                lines.append(
                    f"| {interval} | {action['role']} · {compact(purpose, 100)} | {output_summary(action)} |"
                )
                for i, tool in enumerate(action["tools"], 1):
                    lines.append(
                        f"| ↳ 工具 {i} | `{tool['name']}` · {tool['status']} | 参数：{compact(tool['arguments'], 350)}；返回：{compact(tool['result'], 500)} |"
                    )
            lines += [
                "",
                f"**Host 收尾**：{run['status']}；整轮 {run['elapsed_seconds']} 秒。",
                "",
                "**最终答案**：",
                "",
                run["answer"] or "（无最终答案）",
                "",
                f"追溯 run_id：`{run['run_id']}`。",
            ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = {
        "scenario_count": len(rows),
        "passed": passed,
        "run_count": len(runs),
        "completed_runs": complete,
        "a2a_tasks": len(actions),
        "tool_calls": len(tools),
        "representatives": selected,
        "cases": [
            {
                "case_id": r["case_id"],
                "passed": r["passed"],
                "checks": r["checks"],
                "error": r.get("error"),
                "runs": [
                    {k: x[k] for k in ("run_id", "status", "elapsed_seconds")} for x in r["runs"]
                ],
            }
            for r in rows
        ],
    }
    dest.with_suffix(".json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in {"cases", "representatives"}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    render(args.source, args.destination)
