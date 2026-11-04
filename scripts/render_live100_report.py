# ruff: noqa: E501
"""Render compact, auditable action traces from real benchmark records."""

import argparse
import collections
import json
import re
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
    return re.sub(r"\\*\|", lambda _: "\\|", text.replace("\n", " "))


def brief_answer(text, web=False):
    if not web:
        return text
    # Report the agent's conclusion, not a second copy of the fetched article.
    text = re.sub(r"(?m)^>.*$", "", text)
    text = re.sub(r'["“][^"”\n]{70,}["”]', "〔长引文省略〕", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= 650:
        return text
    excerpt = text[:650]
    boundary = max(excerpt.rfind("\n\n"), excerpt.rfind("。") + 1)
    if boundary > 0:
        excerpt = excerpt[:boundary]
    return excerpt.rstrip() + "\n\n〔后续展开省略〕"


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


def tool_summary(tool):
    raw = tool.get("result", "")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # Captured results can be truncated JSON. Avoid reproducing webpage prose.
        import re

        code = re.search(r'"code"\s*:\s*"([A-Z_]+)"', raw)
        if "private_" in tool["name"]:
            evidence = re.findall(r"doc_[0-9a-f]+", raw)
            return (f"返回码 {code.group(1)}；" if code else "") + (
                "私有片段 " + ", ".join(dict.fromkeys(evidence))[:200]
                if evidence
                else f"私有资料返回摘要 {len(raw)} 字符，状态 {tool['status']}"
            )
        urls = re.findall(r'https?://[^\s"<>]+', raw)
        return (f"返回码 {code.group(1)}；" if code else "") + (
            "来源 " + ", ".join(dict.fromkeys(urls))[:300]
            if urls
            else f"已保存返回摘要 {len(raw)} 字符；状态 {tool['status']}"
        )
    if not isinstance(data, dict):
        return f"返回 {len(data) if isinstance(data, list) else 1} 项数据"
    meta = {
        k: data[k]
        for k in (
            "code",
            "status",
            "evidence_id",
            "url",
            "final_url",
            "next_offset",
            "content_truncated",
            "degraded",
            "error",
        )
        if k in data
    }
    for key in ("sources", "results", "items"):
        if isinstance(data.get(key), list):
            meta[key + "_count"] = len(data[key])
    if isinstance(data.get("text"), str):
        meta["text_chars"] = len(data["text"])
    return compact(meta or {"fields": list(data), "status": tool["status"]}, 500)


def representative(group, rows):
    # Deterministic published selection rule, including failures in the summary.
    if group == "boundary":
        return next((r for r in rows if r.get("rejection")), rows[0])
    preferred = [r for r in rows if r["passed"]] or rows

    def richness(r):
        actions = [a for run in r["runs"] for a in run["actions"]]
        tools = sum(len(a["tools"]) for a in actions)
        tool_types = {t["name"] for a in actions for t in a["tools"]}
        workers = sum(a["role"] == "worker" for a in actions)
        return (
            len(tool_types)
            if group in {"web_retrieval", "hybrid_retrieval", "private_retrieval", "typos"}
            else workers,
            len(r["runs"]),
            -tools,
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
    false_positives = [
        r
        for r in rows
        if r.get("manual_review", {}).get("classification") == "assertion_false_positive"
    ]
    lines = [
        "# 100 个真实案例：结果与各类代表执行过程",
        "",
        "测试日期：2026-11-03（北京时间）。当前发布版本在同一五进程部署上运行，数据均为测试构造。",
        "",
        f"共 **{len(rows)} 个场景**，包含 **{len(runs)} 个真实用户运行**、**{len(actions)} 个 A2A 任务**和 **{len(tools)} 次记录到回执的工具调用**。",
        f"场景断言通过 **{passed}/{len(rows)}**；限时内 completed **{complete}/{len(runs)}**。API 边界拒绝不产生 Agent 调用，也不伪装成模型回答。",
        f"人工复核标记 **{len(false_positives)} 个断言误报**，原自动分数保持不变；其余未通过场景为实际运行失败或超时。",
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
        "- 沿用既有契约测试的 12 类及各类数量，构造适合真实 API 执行的输入；多轮步骤属于同一个场景，场景数、用户运行数、A2A 任务数分别统计。",
        "- 类内主要使用相同场景脚本的随机编号、金额和轮数变体；100 个执行样本不等于 100 种独立业务流程，本轮不覆盖 OCR 或所有真实文档类型。",
        "- 顺序来自 A2A TaskStore 的真实创建/更新时间。表格按开始时间排序；时间区间重叠表示并发，不表示后一项必须等前一项结束。",
        "- Worker 内部工具调用按持久 journal 的顺序列出；journal 没有单次工具精确时间戳，因此不编造工具耗时。输出只保留摘要。",
        "- Host 负责接收、鉴权、轮次落库、上下文装配、调度和最终提交；状态/摘要/长期事实抽取是 Host 后台任务，不计作额外 A2A 子 Agent。",
        "- 恢复类别是真实 pause/continue；没有在用户正在使用的服务上强制杀进程。进程崩溃能力另由恢复单测验证。",
        "- 每轮评测期限为360秒。到期即记录timeout并请求停止；之后即使得到迟到答复也不追认通过，迟到产物仅供问题分析。",
        "- 每类优先选择通过且包含较完整执行链的案例；所有失败保留在下一节和机器可读汇总中，不能从代表例推断整类全部通过。",
        "- 答案中的 `/api/evidence/…` 是测试实例内的引用地址，公开仓库不提供该服务的私有证据数据库；工具返回只展示状态、数量、来源地址等摘要。",
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
        if row.get("manual_review"):
            lines.append("  - **人工复核**：" + row["manual_review"]["reason"])
        for run in row["runs"]:
            if run["status"] != "completed":
                lines.append(f"  - {run['status']}：{compact(run['answer'], 300)}")
                if run.get("status_after_stop") == "completed":
                    lines.append("  - 该运行随后形成了迟到答复；本次仍按360秒评测超时计数。")
        if row["runs"] and all(r["status"] == "completed" for r in row["runs"]):
            lines.append(
                "  - 虽正常结束，但答案未满足该场景断言：" + compact(row["runs"][-1]["answer"], 350)
            )
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
        if group == "recovery":
            lines += [
                "",
                "**Host 控制动作**：启动运行 → 请求 pause → 观察到 paused → 请求 continue → 检查返回的 run_id 与原运行一致 → 等待同一运行最终终态。下表是该运行合并后的实际 A2A 任务记录。",
            ]
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
                    f"| {interval} | {action['role']} · {compact(purpose, 100)} | {compact(brief_answer(output_summary(action), group in {'web_retrieval', 'hybrid_retrieval'}), 250 if group in {'web_retrieval', 'hybrid_retrieval'} else 650)} |"
                )
                for i, tool in enumerate(action["tools"], 1):
                    lines.append(
                        f"| ↳ 工具 {i} | `{tool['name']}` · {tool['status']} | 参数：{compact(tool['arguments'], 350)}；返回：{tool_summary(tool)} |"
                    )
            lines += [
                "",
                f"**Host 收尾**：{run['status']}；整轮 {run['elapsed_seconds']} 秒；Worker 工具调用 {sum(len(a['tools']) for a in run['actions'])} 次。",
                "",
                "**最终答案（网页场景展示摘要，省略长引文）**："
                if group in {"web_retrieval", "hybrid_retrieval"}
                else "**最终答案**：",
                "",
                brief_answer(run["answer"], group in {"web_retrieval", "hybrid_retrieval"})
                or "（无最终答案）",
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
        "assertion_false_positives": len(false_positives),
        "a2a_tasks": len(actions),
        "tool_calls": len(tools),
        "representatives": selected,
        "cases": [
            {
                "case_id": r["case_id"],
                "passed": r["passed"],
                "checks": r["checks"],
                "error": r.get("error"),
                "manual_review": r.get("manual_review"),
                "runs": [
                    {
                        k: x[k]
                        for k in ("run_id", "status", "elapsed_seconds", "status_after_stop")
                        if k in x
                    }
                    for x in r["runs"]
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
