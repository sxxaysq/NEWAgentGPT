"""Replay saved Worker inputs against vLLM; cold salts are NOT APC-off.

Run from the project root with PYTHONPATH=. Input is a JSON list of Worker
journal payloads. No tools are executed and generated outputs are not replayed.
"""

import argparse
import copy
import hashlib
import json
import random
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from langchain_core.messages import convert_to_openai_messages, messages_from_dict
from langchain_core.utils.function_calling import convert_to_openai_tool

from agentgpt.config import Settings
from agentgpt.mcp_manager import MCPTool
from agentgpt.prefix_layout import payload_json
from agentgpt.tools import builtin_tools


def lcp(left, right):
    return next((i for i, pair in enumerate(zip(left, right)) if pair[0] != pair[1]),
                min(len(left), len(right)))


def tool_schemas():
    # Reconstruct current built-in schemas through the production adapter;
    # journal checkpoints do not persist their original bound tool definitions.
    result = []
    for tool in builtin_tools(Settings(_env_file=None), None):
        if tool.name == "generate_image":
            continue
        descriptor = SimpleNamespace(name=tool.name, description=tool.description,
                                     inputSchema=tool.input_schema, annotations=None)
        result.append(convert_to_openai_tool(
            MCPTool("builtin", descriptor, None).as_langchain_tool()))
    return result


def workloads(journals):
    longest = max(journals, key=lambda j: len(j["messages"]))
    messages = convert_to_openai_messages(messages_from_dict(longest["messages"]))
    ends = [i + 1 for i, m in enumerate(messages) if m["role"] == "tool"]
    result = {f"worker_{n}": [messages[:i] for i in ends[:n]] for n in (2, 4, 8)}
    result["real_tasks"] = [convert_to_openai_messages(messages_from_dict(j["messages"][:2]))
                            for j in journals[:8]]
    # Controlled request-only change isolates layout impact with identical history.
    base = result["real_tasks"][0]
    variations = []
    for i in range(8):
        item = copy.deepcopy(base)
        payload = json.loads(item[1]["content"])
        payload["current_request"] = f"实验请求 {i}: " + str(payload["current_request"])
        item[1]["content"] = json.dumps(payload, ensure_ascii=False)
        variations.append(item)
    result["controlled_request_change"] = variations
    return result


def metrics(client, base):
    response = client.get(base + "/metrics")
    response.raise_for_status()
    wanted = ("prefix_cache_hits_total", "prefix_cache_queries_total",
              "request_prefill_time_seconds_sum", "num_requests_running",
              "num_requests_waiting", "num_preemptions_total",
              "time_to_first_token_seconds_sum", "time_to_first_token_seconds_count")
    values = {}
    for line in response.text.splitlines():
        if line.startswith("vllm:") and any(line.startswith("vllm:" + w + "{")
                                             for w in wanted):
            key, value = line.rsplit(" ", 1)
            values[key] = float(value)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journals", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="qwen3.8-27b")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", default="")
    parser.add_argument("--engine-apc", choices=("on", "off", "unknown"), default="unknown")
    parser.add_argument("--cache-modes", default="cold,reuse")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    sets = workloads(json.loads(args.journals.read_text(encoding="utf-8")))
    if args.only:
        sets = {name: requests for name, requests in sets.items() if name in args.only.split(",")}
        if not sets:
            parser.error("--only did not select any workload")
    tools = tool_schemas()
    cache_modes = args.cache_modes.split(",")
    if not set(cache_modes) <= {"cold", "reuse"}:
        parser.error("cache modes must be cold and/or reuse")
    metadata = {"mode": "unique-salt cold vs shared-salt sequential reuse",
                "engine_apc": args.engine_apc,
                "output_tokens": 1, "tool_schemas": "reconstructed current builtins",
                "input_sha256": hashlib.sha256(args.journals.read_bytes()).hexdigest(),
                "quality_evaluation": False, "repeats": args.repeats}
    (args.out / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    with httpx.Client(timeout=120, trust_env=False) as client, (
        args.out / "requests.jsonl").open("w", encoding="utf-8") as output:
        jobs = [(repeat, name, layout, cache) for repeat in range(args.repeats)
                for name in sets for layout in (False, True) for cache in cache_modes]
        random.Random(42).shuffle(jobs)
        for repeat, name, layout, cache in jobs:
            salt = secrets.token_urlsafe(32)
            previous = []
            for index, original in enumerate(sets[name]):
                messages = copy.deepcopy(original)
                payload = json.loads(messages[1]["content"])
                messages[1]["content"] = payload_json(payload, layout)
                schemas = sorted(tools, key=lambda t: t["function"]["name"]) if layout else tools
                request = {"model": args.model, "messages": messages, "tools": schemas,
                           "chat_template_kwargs": {"enable_thinking": False}}
                tokenized = client.post(args.base + "/tokenize", json=request)
                tokenized.raise_for_status()
                tokens = tokenized.json()["tokens"]
                before = metrics(client, args.base)
                record = {"repeat": repeat, "workload": name, "layout": layout,
                          "engine_apc": args.engine_apc,
                          "cache": cache, "index": index, "input_tokens": len(tokens),
                          "lcp_tokens": lcp(previous, tokens), "before": before}
                request.update(max_tokens=1, temperature=0, seed=42, stream=True,
                               stream_options={"include_usage": True},
                               cache_salt=salt if cache == "reuse" else secrets.token_urlsafe(32))
                start = time.perf_counter()
                first = None
                usage = None
                with client.stream("POST", args.base + "/v1/chat/completions",
                                   json=request) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        event = json.loads(line[6:])
                        if event.get("error"):
                            raise RuntimeError(event["error"])
                        usage = event.get("usage") or usage
                        for choice in event.get("choices", []):
                            delta = choice.get("delta", {})
                            if first is None and any(delta.get(k) for k in
                                                     ("content", "tool_calls", "reasoning")):
                                first = time.perf_counter() - start
                record.update(ttft_s=first, elapsed_s=time.perf_counter() - start,
                              usage=usage, after=metrics(client, args.base))
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                previous = tokens
            print(repeat, name, layout, cache, flush=True)


if __name__ == "__main__":
    main()
