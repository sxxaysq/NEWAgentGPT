"""End-to-end acceptance check against a live five-process deployment.

    python scripts/e2e_check.py "九命奇冤最初发表在哪里？"

Registers (or logs in) a throwaway account, asks one question through the same HTTP
API the frontend uses, and prints the plan the Host accepted, the per-task outcome and
the final answer. Exits non-zero when no usable answer comes back.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid

import httpx

EMAIL = "e2e-" + uuid.uuid4().hex[:12] + "@local.test"
PASSWORD = uuid.uuid4().hex


def login(client: httpx.Client, email: str, password: str) -> None:
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    if response.status_code == 409:
        response = client.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code >= 400:
        raise SystemExit(f"登录失败：{response.status_code} {response.text[:200]}")


def run(client: httpx.Client, goal: str, timeout: float, kb: str = "") -> dict:
    thread = client.post("/api/threads", json={"goal": goal}).json()["thread"]
    if kb:
        settings = client.patch(
            f"/api/threads/{thread['id']}/settings", json={"knowledge_base": kb}
        )
        settings.raise_for_status()
    started = client.post(
        f"/api/threads/{thread['id']}/commands",
        json={"operation": "start", "request_id": f"e2e-{int(time.time())}"},
    )
    started.raise_for_status()
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = client.get(f"/api/threads/{thread['id']}").json()["state"]
        if state.get("status") in {"completed", "failed", "cancelled"}:
            break
        time.sleep(3)
    state["thread_id"] = thread["id"]
    return state


def report(state: dict) -> int:
    tasks = state.get("tasks") or []
    print(f"状态：{state.get('status')}")
    print(f"子任务数：{len(tasks)}")
    for task in tasks:
        deps = task.get("depends_on") or []
        print(
            f"  - {task['id']} [{task.get('status')}] {task.get('title', '')}"
            + (f" 依赖 {deps}" if deps else "")
        )
    answer = (state.get("final_answer") or "").strip()
    print("\n最终答复：")
    print(answer or "（空）")
    if state.get("messages"):
        print(f"\n过程消息 {len(state['messages'])} 条")

    completed = [task for task in tasks if task.get("status") == "completed"]
    reasons = []
    if state.get("status") != "completed":
        reasons.append(f"状态为 {state.get('status')}")
    if not completed:
        reasons.append("没有任何子任务完成")
    if not answer:
        reasons.append("没有最终答复")
    if "本次没有产生任何可用的子任务结论" in answer:
        reasons.append("答复是「无可用结论」兜底文本")
    print("\n结论：" + ("PASS" if not reasons else "FAIL — " + "；".join(reasons)))
    return 0 if not reasons else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("goal")
    parser.add_argument("--base", default="http://127.0.0.1:18210")
    parser.add_argument("--email", default=EMAIL)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--kb", default="", help="本轮知识库，留空即默认知识库")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--dump", default="")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base, timeout=60) as client:
        login(client, args.email, args.password)
        state = run(client, args.goal, args.timeout, args.kb)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
    return report(state)


if __name__ == "__main__":
    raise SystemExit(main())
