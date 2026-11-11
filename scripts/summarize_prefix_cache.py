"""Summarize sequential cache trials, excluding each group's first request."""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def delta(row, metric):
    keys = [k for k in row["after"] if k.startswith("vllm:" + metric + "{")]
    if not keys:
        return None
    return sum(row["after"][k] - row["before"].get(k, row["after"][k]) for k in keys)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines()]
    groups = defaultdict(list)
    for row in rows:
        if row["index"] > 0:
            groups[(row["workload"], row["layout"], row["cache"])].append(row)
    result = []
    for (workload, layout, cache), group in sorted(groups.items()):
        tokens = sum(r["input_tokens"] for r in group)
        queries = sum(delta(r, "prefix_cache_queries_total") for r in group)
        ttft = [delta(r, "time_to_first_token_seconds_sum") for r in group]
        counts = [delta(r, "time_to_first_token_seconds_count") for r in group]
        engine_apc = group[0].get("engine_apc", "unknown")
        result.append({
            "workload": workload, "layout": layout, "cache": cache, "n": len(group),
            "engine_apc": engine_apc,
            "elapsed_median_ms": 1000 * statistics.median(r["elapsed_s"] for r in group),
            "prefill_mean_ms": 1000 * statistics.mean(
                delta(r, "request_prefill_time_seconds_sum") for r in group),
            "cached_token_ratio": sum(delta(r, "prefix_cache_hits_total") for r in group) / tokens,
            "lcp_mean_tokens": statistics.mean(r["lcp_tokens"] for r in group),
            "server_ttft_mean_ms": 1000 * sum(ttft) / sum(counts)
            if all(v is not None for v in ttft + counts) and sum(counts) else None,
            "metrics_queries_equal_input": queries == tokens,
            "metrics_counter_consistent_with_apc": queries == (
                0 if engine_apc == "off" else tokens
            ),
        })
    args.output.write_text(json.dumps({"requests": len(rows), "groups": result}, indent=2),
                           encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
