"""Keep operational completion, rubric quality, and inference costs separate."""

import argparse
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    summaries = []
    reviews_path = args.root / "semantic-review.json"
    reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.exists() else {}
    for group in "ABCD":
        directory = args.root / group
        cases = [json.loads(p.read_text(encoding="utf-8"))
                 for p in sorted((directory / "quality").glob("*.json"))
                 if p.name != "manifest.json"]
        if not cases:
            continue
        calls_path = directory / "model-calls.jsonl"
        calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
        # Supplementary scope checks run after the frozen main suite and are
        # assessed separately; don't silently add their cost to just some groups.
        calls = [call for call in calls if min(c["started"] for c in cases)
                 <= call["started"] <= max(c["finished"] for c in cases)]
        runs = [run for case in cases for run in case["runs"]]
        usage = [c["response"].get("usage") or {} for c in calls]
        summary = {
            "group": group, "cases": len(cases), "turns": len(runs),
            "operational_cases_passed": sum(c["passed"] for c in cases),
            "completed_turns": sum(r["status"] == "completed" for r in runs),
            "all_turn_elapsed_s": sum(r["elapsed_seconds"] for r in runs),
            "turn_elapsed_median_s": statistics.median(r["elapsed_seconds"] for r in runs),
            "model_calls": len(calls), "model_http_errors": sum(c["status"] != 200 for c in calls),
            "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage),
            "completion_tokens": sum(u.get("completion_tokens", 0) for u in usage),
            "model_call_elapsed_sum_s": sum(c["elapsed_s"] for c in calls),
            "tool_calls": sum(len(a["tools"]) for r in runs for a in r["actions"]),
            "cases_detail": [],
        }
        for case in cases:
            review = reviews.get(group, {}).get(case["case_id"])
            summary["cases_detail"].append({
                "case": case["case_id"], "operational_pass": case["passed"],
                "semantic_review": review,
                "combined_pass": bool(case["passed"] and review and review["passed"]),
                "elapsed_s": sum(r["elapsed_seconds"] for r in case["runs"]),
            })
        summary["combined_passed"] = sum(c["combined_pass"] for c in summary["cases_detail"])
        summaries.append(summary)
    (args.root / "quality-summary.json").write_text(json.dumps(summaries, ensure_ascii=False,
                                                            indent=2), encoding="utf-8")
    print(json.dumps([{k: v for k, v in s.items() if k != "cases_detail"} for s in summaries],
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
