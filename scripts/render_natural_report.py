# ruff: noqa: E501
"""Natural v2 report with separate operational and same-model semantic checks."""

import argparse
import json
from pathlib import Path

import render_live100_report as base
from render_live100_report import render


def main(source, dest):
    chosen = dict(
        single_hop="single_hop-00",
        multi_step="multi_step-02",
        long_text="long_text-02",
        private_retrieval="private_retrieval-00",
        web_retrieval="web_retrieval-00",
        hybrid_retrieval="hybrid_retrieval-02",
        recovery="recovery-02",
        typos="typos-00",
        isolation="isolation-00",
        in_session="in_session-07",
        cross_session="cross_session-05",
        boundary="boundary-00",
    )
    original_pick, original_summary = base.representative, base.output_summary
    base.representative = lambda group, rows: next(r for r in rows if r["case_id"] == chosen[group])
    base.output_summary = lambda action: base.compact(original_summary(action), 240)
    render(source, dest)
    base.representative, base.output_summary = original_pick, original_summary
    rows = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(source.glob("*.json"))
        if p.name != "manifest.json"
    ]
    text = dest.read_text(encoding="utf-8")
    text = text.replace(
        "每类优先选择通过且包含较完整执行链的案例",
        "每类选择有代表性的自然表达与任务链，包含失败示例",
    )
    text = text.replace(
        "## 用户及项目隔离：isolation-00",
        "## 用户及项目隔离：isolation-00\n\n轮次身份：本人陈述偏好 → 本人新会话正向召回 → 另一账号询问。",
    )
    text = text.replace(
        "## 跨会话长期记忆：cross_session-05",
        "## 跨会话长期记忆：cross_session-05\n\n轮次条件：本人陈述 → 同用户新会话且记忆开启 → 同用户再开新会话且记忆关闭。",
    )
    text = text.replace("测试日期：2026-11-03（北京时间）", "测试日期：2026-11-03至04（北京时间）")
    text = text.replace("场景断言通过", "综合条件通过").replace("| 断言通过 |", "| 综合通过 |")
    import re

    text = re.sub(r"人工复核标记[^\n]*\n", "本轮未逐题人工复核，不报告人工通过率。\n", text)
    text = text.replace(
        "# 100 个真实案例：结果与各类代表执行过程", "# 自然提问版100例：重测结果与代表执行过程"
    )
    text = text.replace(
        "这些通过率来自明确的字符串、来源、状态和隔离断言，不是独立模型语义评审的准确率。没有将之前的模拟契约测试计入本报告。",
        "本轮是重新设计的100个自然语言场景。通过标准同时包含运行/工具断言和一次独立调用的同模型语义评阅；评阅模型与被测模型相同，不是独立人工真值，也不应将通过率解读为通用准确率。",
    )
    text = text.replace(
        "- 类内主要使用相同场景脚本的随机编号、金额和轮数变体；100 个执行样本不等于 100 种独立业务流程，本轮不覆盖 OCR 或所有真实文档类型。",
        "- 类内采用不同日常、学术和业务场景，具体输入与预先定义的标准见 NATURAL100_DESIGN.md；6份长文本是完整原创材料，没有重复句或占位字符凑长度。",
    )
    text = text.replace(
        "其余未通过场景为实际运行失败或超时。",
        "其他未通过场景可能是运行失败、超时、工具路径缺失或语义评阅未通过，须按下表分别解读。",
    )
    text += "\n\n评测环境说明：延用上一轮按类别控制工具权限的方式，简单问答、多步规划、长文本改写等不开放检索；私有、联网与混合类别分别开放对应工具。该受控设置不是默认全工具模式，回答失败不能直接推广为所有部署配置的表现。\n"
    text += "\n新旧两轮的题目内容和评阅方法都发生了变化，因此分数差异不能单独归因于提问语气；旧报告作为历史记录保留，不作为同题对照实验。\n"
    text += "\n私有检索场景按每例单主题预置资料运行，能检查不提供准确文件名时的语义查找，但不代表已验证大型知识库中大量相似文档的消歧能力。\n"
    details = [
        "",
        "## 本轮评阅分项",
        "",
        "| 用例 | 场景 | 操作断言 | 语义评阅 | 评阅存疑 | 依据 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        review = r.get("review", {})
        operational = all(
            c["passed"] for c in r["checks"] if c["name"] != "model_rubric_review"
        ) and not r.get("error")
        reason = (
            str(review.get("reason", r.get("error", "未评阅"))).replace("|", "/").replace("\n", " ")
        )
        details.append(
            f"| {r['case_id']} | {r.get('design', {}).get('scene', '')} | {operational} | {review.get('passed', '未评阅')} | {review.get('uncertain', True)} | {reason} |"
        )
    text += "\n".join(details) + "\n"
    text += "\n## 多会话场景的角色与条件\n\n跨会话记忆类：第一轮本人陈述，第二轮同用户新会话召回，第三轮关闭长期记忆后再次开新会话。隔离类前五例：本人陈述→本人正向召回→另一用户询问；后五例：本人查询私有资料→另一用户查询→同用户切换到其他知识库查询。每个查询均使用自然问题，不把文档准确名称写入问题。\n"
    text += "\n## 环境与记录保留\n\n11月3日内网断连及过期代理影响了首轮联网执行，4份已落盘失败记录保存在network_outage。11月4日恢复服务器回环SSH代理后，以相同问题补测受影响案例；本报告主表使用恢复后的记录。应用源码未因测试结果而修改。初次语义评阅的模块导入错误仅补做评阅，未重跑已完成Agent任务，初始错误在记录中保留。\n"

    text += "\n隔离类的综合失败不等同于数据泄漏：若本人正向召回失败或前置记忆没有形成，不能仅凭另一用户答不知道判定隔离已经验证。该类应结合每轮身份、记忆注入数量及回答判断。\n"
    text += "\n## 审核失败的可追溯反馈\n\n| 用例 | Critic 实际反馈摘要 |\n|---|---|\n"
    for row in rows:
        if all(r["status"] == "completed" for r in row["runs"]):
            continue
        reasons = [
            reason
            for r in row["runs"]
            for a in r["actions"]
            for out in a.get("output", [])
            if out.get("verdict") in {"reject", "revise"}
            for reason in out.get("reasons", [])
        ]
        snippet = "；".join(reasons[-2:]).replace("|", "/").replace("\n", " ")[:350]
        text += f"| {row['case_id']} | {snippet or row.get('error', '未取得审核产物')} |\n"
    text += "\n人工抽查发现：部分长文写作任务的Worker事实摘录与Critic完整成稿验收要求不匹配；Synthesizer之后虽能形成正文，运行仍失败。另有计划自行增加例子数量等用户未明确要求的验收条件。以上是具体记录支持的观察，不代表所有失败均由同一原因造成。\n"

    dest.write_text(text, encoding="utf-8")
    summary = json.loads(dest.with_suffix(".json").read_text(encoding="utf-8"))
    summary["mode"] = "natural_language_v2"
    summary["operational_passed"] = sum(
        not r.get("error")
        and all(c["passed"] for c in r["checks"] if c["name"] != "model_rubric_review")
        for r in rows
    )
    summary["semantic_review_passed"] = sum(r.get("review", {}).get("passed", False) for r in rows)
    summary["semantic_review_count"] = sum("review" in r for r in rows)
    summary["not_reviewed"] = sum("review" not in r for r in rows)
    summary["uncertain_reviews"] = sum(
        r["review"].get("uncertain", True) for r in rows if "review" in r
    )
    text = text.replace(
        "本轮未逐题人工复核，不报告人工通过率。",
        f"本轮未逐题人工复核，不报告人工通过率。操作条件通过 **{summary['operational_passed']}/100**；同模型内容评阅通过 **{summary['semantic_review_passed']}/{summary['semantic_review_count']}**，另有 **{summary['not_reviewed']} 例未进入内容评阅**。",
    )
    text += "\n附件：[全部问题与验收重点](NATURAL100_DESIGN.md) · [六篇长文本完整答复](LONG_TEXT_ANSWERS_V2.md) · [机器可读统计](LIVE100_REPORT.json)。\n"
    dest.write_text(text, encoding="utf-8")
    for entry, r in zip(summary["cases"], rows):
        entry["review"] = r.get("review")
    dest.with_suffix(".json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("destination", type=Path)
    a = p.parse_args()
    main(a.source, a.destination)
