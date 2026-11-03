"""Targeted live five-process acceptance; random facts never come from a knowledge base."""

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18310")
    parser.add_argument("--output", default="memory-live-results.json")
    args = parser.parse_args()
    suffix = uuid.uuid4().hex[:10]
    results = []
    client = httpx.Client(base_url=args.base, timeout=60)
    response = client.post(
        "/api/auth/register",
        json={"email": f"memory-{suffix}@local.test", "password": "acceptance-" + suffix},
    )
    response.raise_for_status()

    def ask(text, thread=None):
        if thread is None:
            r = client.post(
                "/api/threads",
                json={
                    "goal": text,
                    "settings": {
                        "enabled_tools": [],
                        "judge_plan": False,
                        "memory_scope": "acceptance-" + suffix,
                    },
                },
            )
            r.raise_for_status()
            thread = r.json()["thread"]["id"]
            operation = "start"
        else:
            operation = "followup"
        r = client.post(
            f"/api/threads/{thread}/commands",
            json={
                "operation": operation,
                "text": text if operation == "followup" else "",
                "request_id": uuid.uuid4().hex,
            },
        )
        r.raise_for_status()
        run = r.json()["run_id"]
        start = time.monotonic()
        while time.monotonic() - start < 240:
            data = client.get(f"/api/threads/{thread}").json()
            state = data["state"]
            if state.get("run_id") == run and state.get("status") in {
                "completed",
                "failed",
                "stopped",
            }:
                results.append(
                    {
                        "query": text,
                        "thread": thread,
                        "run": run,
                        "state": state,
                        "elapsed": round(time.monotonic() - start, 2),
                    }
                )
                print(
                    json.dumps(
                        {
                            "query": text,
                            "status": state.get("status"),
                            "answer": state.get("final_answer"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
                return thread, state
            time.sleep(1)
        raise TimeoutError(run)

    client.patch("/api/memory-preference", json={"enabled": False}).raise_for_status()
    thread, _ = ask(
        "这个项目预算23万元，只能内网部署；比较方案A和B，用中文两行表格整理当前约束。不需要外部检索。"
    )
    ask("预算改成18万元，其他不变。请给出更新后的表格。", thread)
    _, state = ask("把刚才的表格改成英文，保留预算、部署边界和比较对象。", thread)
    answer = state.get("final_answer", "")
    results.append(
        {
            "assertion": "same_thread_memory_off_correction",
            "pass": state["status"] == "completed"
            and ("180" in answer or "18" in answer)
            and "23" not in answer
            and "A" in answer
            and "B" in answer,
        }
    )
    client.patch("/api/memory-preference", json={"enabled": True}).raise_for_status()
    fact = "ZX-" + suffix.upper()
    ask(
        "请记住我的长期项目事实：当前项目内部代号是"
        + fact
        + "，以后提到我的项目代号就使用它。只需确认。"
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        data = client.get("/api/memories").json()
        if any(fact in x["content"] for x in data["items"]):
            break
        time.sleep(2)
    _, state = ask("我的项目内部代号是什么？请仅输出代号；资料不足则说明不知道。")
    results.append(
        {
            "assertion": "cross_thread_random_fact",
            "pass": state["status"] == "completed" and fact in state.get("final_answer", ""),
        }
    )
    client.patch("/api/memory-preference", json={"enabled": False}).raise_for_status()
    _, state = ask("我的项目内部代号是什么？资料不足则说明不知道，不要猜测。")
    results.append(
        {
            "assertion": "disabled_control",
            "pass": state["status"] == "completed" and fact not in state.get("final_answer", ""),
        }
    )
    # The negative user check runs while Alice's fact still exists.
    alice_client = client
    client = httpx.Client(base_url=args.base, timeout=60)
    client.post(
        "/api/auth/register",
        json={"email": f"bob-{suffix}@local.test", "password": "acceptance-" + suffix},
    ).raise_for_status()
    _, state = ask("我的项目内部代号是什么？资料不足则说明不知道，不要猜测。")
    results.append(
        {
            "assertion": "different_user",
            "pass": state["status"] == "completed" and fact not in state.get("final_answer", ""),
        }
    )
    client.close()
    client = alice_client
    for item in client.get("/api/memories").json()["items"]:
        if fact in item["content"]:
            client.delete(
                f"/api/memories/{item['id']}", params={"version": item["version"]}
            ).raise_for_status()
    client.patch("/api/memory-preference", json={"enabled": True}).raise_for_status()
    _, state = ask("我的项目内部代号是什么？不知道就说不知道，不要猜测。")
    results.append(
        {
            "assertion": "deleted_memory",
            "pass": state["status"] == "completed" and fact not in state.get("final_answer", ""),
        }
    )
    client.close()
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    checks = [r for r in results if "assertion" in r]
    print(json.dumps(checks, ensure_ascii=False), flush=True)
    return 0 if all(c["pass"] for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
