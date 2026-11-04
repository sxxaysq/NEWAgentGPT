"""Publish all input/rubric definitions, separately from execution results."""

import json
from pathlib import Path

from render_live100_report import LABELS

root = Path(__file__).resolve().parents[1]
cases = json.loads((root / "datasets/natural100_v2.json").read_text(encoding="utf-8"))
lines = [
    "# 自然提问版：100个测试场景设计",
    "",
    "用例版本：v2，2026-11-03。本文为预先定义的设计，不是运行结果。",
    "",
    "保留原12类及数量；类内覆盖不同生活、学术与工作主题。用户输入不包含工具名、网页URL、知识库名或准确文档名。",
    "6个长文本用例使用完整原创材料，不重复句子凑长度；要求交付可直接使用的完整回答。",
    "含糊提问允许适度澄清，不能以需求未完整列出为由一律判失败。运行状态、工具调用与语义评阅分开记录。",
    "语义评阅为另一次同模型调用，并非独立人工真值；不确定结果需单独标记。",
    "",
    "隔离类前5例检查跨用户记忆；后5例检查私有资料的跨用户和跨知识库隔离，避免把全局饮食偏好错误地要求按项目隔离。",
    "",
]
for group, label in LABELS.items():
    subset = [c for c in cases if c["category"] == group]
    lines += [f"## {label}（{len(subset)}例）", ""]
    for case in subset:
        lines += [f"### {case['case_id']} · {case['scene']}", ""]
        for i, prompt in enumerate(case["prompts"], 1):
            lines += [f"用户第{i}次输入：", "", prompt, ""]
        if case["document"]:
            lines += ["预置资料内容（用户提问不包含文件名）：", "", case["document"], ""]
        lines += ["预先定义的验收重点：" + case["rubric"], ""]
(root / "NATURAL100_DESIGN.md").write_text("\n".join(lines), encoding="utf-8")
print("Wrote NATURAL100_DESIGN.md with", len(cases), "cases")
