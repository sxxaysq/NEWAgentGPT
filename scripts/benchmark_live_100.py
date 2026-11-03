"""100 real API scenarios; scoped read-only traces from the deployed A2A journals.

Creates synthetic test accounts/documents. Never kills production processes.
Recovery scenarios exercise pause/continue, not a process-crash claim.
Resume by passing the same --output directory (completed cases are never rerun).
"""

import argparse
import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

COUNTS = {
    "single_hop": 10,
    "multi_step": 10,
    "long_text": 6,
    "private_retrieval": 10,
    "web_retrieval": 6,
    "hybrid_retrieval": 6,
    "recovery": 8,
    "typos": 6,
    "isolation": 10,
    "in_session": 14,
    "cross_session": 10,
    "boundary": 4,
}
PRIVATE = ["builtin__private_search", "builtin__private_read"]
WEB = ["builtin__web_search", "builtin__web_fetch"]
TERMINAL = {"completed", "failed", "stopped", "paused", "cancelled"}


def short(value, limit=1200):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …[摘要截取]"


def data_parts(parts):
    return next((p["data"] for p in parts if "data" in p), {})


def read_trace(root, run_id, user_id):
    """Only inspect rows owned by this benchmark account and exact run."""
    actions = []
    for role in ("planner", "worker", "critic", "synthesizer"):
        with sqlite3.connect(
            f"file:{root / 'a2a_state' / (role + '.sqlite3')}?mode=ro", uri=True
        ) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM a2a_tasks WHERE run_id=? AND user_id=? ORDER BY created,id",
                (run_id, user_id),
            ).fetchall()
            for row in rows:
                task = json.loads(row["payload"])
                incoming = data_parts((task.get("history") or [{}])[0].get("parts", []))
                outputs = [data_parts(a.get("parts", [])) for a in task.get("artifacts", [])]
                action = {
                    "role": role,
                    "a2a_task_id": row["id"],
                    "started": row["created"],
                    "finished": row["updated"],
                    "status": row["state"],
                    "operation": incoming.get("operation", ""),
                    "input": {
                        k: incoming[k]
                        for k in ("goal", "task", "attempt", "feedback", "operation")
                        if k in incoming
                    },
                    "output": outputs,
                    "tools": [],
                }
                if role == "worker":
                    journal = db.execute(
                        "SELECT payload FROM execution_journal "
                        "WHERE task_id=? AND entry_key='worker'",
                        (row["id"],),
                    ).fetchone()
                    if journal:
                        state = json.loads(journal[0])
                        calls = {}
                        for message in state.get("messages", []):
                            msg = message.get("data", {})
                            for call in msg.get("tool_calls", []):
                                calls[call["id"]] = call
                        for trace, transcript in zip(
                            state.get("traces", []), state.get("transcripts", [])
                        ):
                            call = calls.get(trace.get("call_id"), {})
                            action["tools"].append(
                                {
                                    "name": trace["name"],
                                    "arguments": call.get("args", {}),
                                    "status": trace["status"],
                                    "result": short(transcript.get("content", "")),
                                }
                            )
                actions.append(action)
    with sqlite3.connect(f"file:{root / 'ui_data/agentgpt.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT context FROM runs WHERE id=? AND user_id=?", (run_id, user_id)
        ).fetchone()
        saved = json.loads(row[0]) if row else {}
        background = saved.get("background") or {}
        context = {
            "memory_count": len(background.get("memory_context") or []),
            "memories": background.get("memory_context") or [],
            "pending_updates": len(background.get("pending_user_updates") or []),
            "state_version": background.get("manifest", {}).get("state_version"),
            "summary_version": background.get("manifest", {}).get("summary_version"),
        }
    return {
        "context": context,
        "actions": sorted(actions, key=lambda x: (x["started"], x["a2a_task_id"])),
    }


class Case:
    def __init__(self, args, group, number):
        self.args, self.group, self.number = args, group, number
        self.id = f"{group}-{number:02}"
        self.nonce = uuid.uuid4().hex[:10]
        self.code = "BENCH-" + self.nonce.upper()
        self.scope = "bench-" + self.nonce
        self.runs, self.checks, self.notes = [], [], []

    async def register(self, suffix="owner", memory=False):
        client = httpx.AsyncClient(base_url=self.args.base, timeout=60)
        response = await client.post(
            "/api/auth/register",
            json={"email": f"bench-{self.nonce}-{suffix}@local.test", "password": uuid.uuid4().hex},
        )
        response.raise_for_status()
        owner = response.json()["id"]
        (await client.patch("/api/memory-preference", json={"enabled": memory})).raise_for_status()
        return client, owner

    async def ask(self, client, owner, text, *, thread=None, tools=None, pause=False, scope=None):
        if thread is None:
            response = await client.post(
                "/api/threads",
                json={
                    "goal": text,
                    "settings": {
                        "enabled_tools": tools or [],
                        "judge_plan": True,
                        "memory_scope": self.scope if scope is None else scope,
                        "knowledge_base": self.scope,
                    },
                },
            )
            response.raise_for_status()
            thread = response.json()["thread"]["id"]
            operation = "start"
        else:
            operation = "followup"
        request_id = uuid.uuid4().hex
        response = await client.post(
            f"/api/threads/{thread}/commands",
            json={
                "operation": operation,
                "text": text if operation == "followup" else "",
                "request_id": request_id,
            },
        )
        response.raise_for_status()
        run = response.json()["run_id"]
        started = time.time()
        pause_sent = False
        resumed = False
        while time.time() - started < 360:
            state = (await client.get(f"/api/threads/{thread}")).json()["state"]
            if pause and not pause_sent and time.time() - started >= 1:
                (
                    await client.post(
                        f"/api/threads/{thread}/commands",
                        json={"operation": "pause", "request_id": uuid.uuid4().hex},
                    )
                ).raise_for_status()
                pause_sent = True
            if state.get("run_id") == run and state.get("status") in TERMINAL:
                if pause and state["status"] == "paused" and not resumed:
                    continued = await client.post(
                        f"/api/threads/{thread}/commands",
                        json={"operation": "continue", "request_id": uuid.uuid4().hex},
                    )
                    continued.raise_for_status()
                    self.checks.append(
                        {"name": "resume_same_run", "passed": continued.json()["run_id"] == run}
                    )
                    resumed = True
                    continue
                break
            await asyncio.sleep(0.5)
        else:
            await client.post(
                f"/api/threads/{thread}/commands",
                json={"operation": "stop", "request_id": uuid.uuid4().hex},
            )
            raise TimeoutError(f"run {run} exceeded 360s")
        if pause:
            self.checks.append({"name": "pause_resume_observed", "passed": resumed})
        trace = await asyncio.to_thread(read_trace, self.args.root, run, owner)
        record = {
            "run_id": run,
            "thread_id": thread,
            "input": text,
            "status": state["status"],
            "answer": state.get("final_answer", ""),
            "elapsed_seconds": round(time.time() - started, 2),
            **trace,
        }
        self.runs.append(record)
        self.checks.append({"name": "run_completed", "passed": state["status"] == "completed"})
        return thread, record

    async def document(self, client, text):
        response = await client.post(
            "/api/documents",
            data={"kb": self.scope},
            files={"file": ("synthetic-fixture.md", text.encode(), "text/markdown")},
        )
        response.raise_for_status()
        doc = response.json()["id"]
        for _ in range(90):
            rows = (await client.get("/api/documents")).json()
            row = next(x for x in rows if x["id"] == doc)
            if row["status"] == "ready":
                return
            if row["status"] == "failed":
                raise RuntimeError("fixture ingestion failed: " + str(row.get("error")))
            await asyncio.sleep(1)
        raise TimeoutError("fixture document not ready")

    def check(self, name, condition):
        self.checks.append({"name": name, "passed": bool(condition)})

    async def execute(self):
        group, n = self.group, self.number
        memory = group in {"cross_session", "isolation"}
        client, owner = await self.register(memory=memory)
        clients = [client]
        try:
            if group == "single_hop":
                _, r = await self.ask(
                    client, owner, f"用户提供的项目代号是{self.code}。请原样返回代号，不需要检索。"
                )
                self.check("exact_input_fact", self.code in r["answer"])
            elif group == "multi_step":
                a, b = 120 + n, 80 + n
                _, r = await self.ask(
                    client,
                    owner,
                    f"请分别计算方案A：{a}元乘3台；方案B：{b}元乘4台；最后比较总价，给出两项总价、差额及便宜的方案。所有数据均在本题，不用检索。",
                )
                self.check(
                    "arithmetic_results",
                    all(
                        str(x) in r["answer"].replace(",", "")
                        for x in (a * 3, b * 4, a * 3 - b * 4)
                    ),
                )
            elif group == "long_text":
                text = (
                    "请从下面完整材料提取首尾两个编号，并保留不得联网的约束。\n开头编号："
                    + self.code
                    + "\n"
                )
                text += "背景：此项目仅供测试，会议内容不改变已确认条件。\n" * (70 + n * 5)
                text += "末尾编号：TAIL-" + self.nonce + "。约束：不得联网。只输出两个编号和约束。"
                _, r = await self.ask(client, owner, text)
                self.check(
                    "both_ends_and_negation",
                    self.code in r["answer"]
                    and "TAIL-" + self.nonce in r["answer"]
                    and ("不得联网" in r["answer"] or "禁止联网" in r["answer"]),
                )
            elif group in {"private_retrieval", "hybrid_retrieval", "typos"}:
                amount = 180000 + n * 137
                await self.document(
                    client,
                    f"# 测试项目\n项目代号：{self.code}\n批准预算：{amount} CNY\n"
                    f"审批编号：APP-{self.nonce}\n部署限制：只能内网部署。\n"
                    "公开技术依据：SQLite FTS5 官方说明：https://www.sqlite.org/fts5.html\n",
                )
                if group == "hybrid_retrieval":
                    text = (
                        "请从当前私有知识库查出项目预算和审批编号，再读取 "
                        "https://www.sqlite.org/fts5.html ，确认trigram tokenizer"
                        "是否支持一般子串匹配。"
                        "分别给出私有和公开来源。"
                    )
                    tools = PRIVATE + WEB
                else:
                    text = (
                        "请查当前私有知识库的项目预算、审批编号和布署限止。"
                        if group == "typos"
                        else "请查当前私有知识库的批准预算、审批编号和部署限制，并引用来源。"
                    )
                    tools = PRIVATE
                _, r = await self.ask(client, owner, text, tools=tools)
                self.check(
                    "private_answer_and_citation",
                    str(amount) in r["answer"].replace(",", "")
                    and "APP-" + self.nonce in r["answer"]
                    and "/api/evidence/" in r["answer"],
                )
                names = [t["name"] for a in r["actions"] for t in a["tools"]]
                self.check("private_tool_used", any("private_" in x for x in names))
                if group == "hybrid_retrieval":
                    self.check("web_tool_used", any("web_" in x for x in names))
            elif group == "web_retrieval":
                questions = [
                    (
                        "读取 https://www.sqlite.org/fts5.html ，说明FTS5的trigram tokenizer"
                        "有什么作用，并引用网页原文。",
                        "trigram",
                    ),
                    (
                        "先搜索Python asyncio wait_for官方文档，再读取官方正文，"
                        "说明超时后是否会等待被取消任务结束，并引用来源。",
                        "取消",
                    ),
                ]
                text, keyword = questions[n % 2]
                _, r = await self.ask(client, owner, text, tools=WEB)
                tools = [t for a in r["actions"] for t in a["tools"]]
                self.check(
                    "web_evidence_answer",
                    keyword.lower() in r["answer"].lower() and "/api/evidence/" in r["answer"],
                )
                self.check(
                    "actual_fetch",
                    any(
                        t["name"] == "builtin__web_fetch" and t["status"] == "success"
                        for t in tools
                    ),
                )
            elif group == "recovery":
                _, r = await self.ask(
                    client,
                    owner,
                    f"根据本题数据分两步计算：甲方案{100 + n}元乘7台，乙方案{90 + n}元乘8台，"
                    "比较总价。请给出计算结果，不使用检索。",
                    pause=True,
                )
                self.check("usable_resumed_answer", bool(r["answer"].strip()))
                self.notes.append("真实暂停/继续；不代表进程SIGKILL故障注入。")
            elif group == "in_session":
                thread, _ = await self.ask(
                    client,
                    owner,
                    f"项目{self.code}预算23万元，仅内网部署，比较A和B。请整理中文表格。",
                )
                await self.ask(
                    client, owner, "预算改成18万元，其他不变。请更新表格。", thread=thread
                )
                if n >= 7:
                    await self.ask(
                        client, owner, "请概括之前明确的部署边界，不引入新事实。", thread=thread
                    )
                _, r = await self.ask(
                    client,
                    owner,
                    "把刚才的约束改成英文表格，保留当前预算、部署边界和A/B比较对象。",
                    thread=thread,
                )
                self.check(
                    "correction_and_constraints",
                    "180" in r["answer"].replace(",", "")
                    and "23" not in r["answer"]
                    and "A" in r["answer"]
                    and "B" in r["answer"]
                    and any(x in r["answer"].lower() for x in ("intranet", "internal network")),
                )
            elif group in {"cross_session", "isolation"}:
                await self.ask(
                    client,
                    owner,
                    f"请记住我的长期项目事实：项目内部代号是{self.code}，以后提到项目代号就使用它。只需确认。",
                )
                for _ in range(90):
                    memories = (await client.get("/api/memories")).json()["items"]
                    if any(self.code in m["content"] for m in memories):
                        break
                    await asyncio.sleep(1)
                else:
                    raise TimeoutError("memory extraction not committed")
                if group == "isolation":
                    other, bob = await self.register("bob", memory=True)
                    clients.append(other)
                    _, r = await self.ask(
                        other, bob, "我的项目内部代号是什么？无资料则说明不知道，不得猜测。"
                    )
                    self.check(
                        "no_cross_user_fact",
                        self.code not in r["answer"] and r["context"]["memory_count"] == 0,
                    )
                    _, r = await self.ask(
                        client,
                        owner,
                        "我的项目内部代号是什么？无资料则说明不知道，不得猜测。",
                        scope="different-" + self.nonce,
                    )
                    self.check(
                        "no_cross_scope_fact",
                        self.code not in r["answer"] and r["context"]["memory_count"] == 0,
                    )
                else:
                    _, r = await self.ask(
                        client, owner, "我的项目内部代号是什么？仅输出代号，资料不足则说明不知道。"
                    )
                    self.check(
                        "cross_thread_recall",
                        self.code in r["answer"] and r["context"]["memory_count"] > 0,
                    )
                    (
                        await client.patch("/api/memory-preference", json={"enabled": False})
                    ).raise_for_status()
                    _, r = await self.ask(
                        client, owner, "我的项目内部代号是什么？无资料则说明不知道，不得猜测。"
                    )
                    self.check(
                        "disabled_control",
                        self.code not in r["answer"] and r["context"]["memory_count"] == 0,
                    )
            elif group == "boundary":
                if n < 2:
                    text = "超长材料" * (2001 + n)
                    response = await client.post("/api/threads", json={"goal": text})
                    self.check("oversized_input_422", response.status_code == 422)
                    self.notes.append(
                        "输入超过API的8000字符上限，HTTP 422，未触发任何Agent或模型调用。"
                    )
                    self.rejection = {
                        "input_chars": len(text),
                        "http_status": response.status_code,
                        "response": short(response.json(), 500),
                    }
                else:
                    _, r = await self.ask(
                        client,
                        owner,
                        "请给出我的私人项目审批编号。当前没有给过相关资料；不知道就明确说明，不得编造。",
                    )
                    self.check(
                        "insufficient_data",
                        any(k in r["answer"] for k in ("不知道", "无法", "未提供", "不足")),
                    )
        finally:
            for item in clients:
                await item.aclose()

    async def run(self):
        result = {
            "case_id": self.id,
            "category": self.group,
            "index": self.number,
            "synthetic_code": self.code,
            "started": time.time(),
        }
        try:
            await self.execute()
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result.update(
            runs=self.runs,
            checks=self.checks,
            notes=self.notes,
            finished=time.time(),
            rejection=getattr(self, "rejection", None),
        )
        result["passed"] = (
            "error" not in result and bool(self.checks) and all(x["passed"] for x in self.checks)
        )
        return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.parallel)

    async def run(group, number):
        path = args.output / f"{group}-{number:02}.json"
        if path.exists():
            return
        async with semaphore:
            result = await Case(args, group, number).run()
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "case": result["case_id"],
                        "passed": result["passed"],
                        "runs": len(result["runs"]),
                        "error": result.get("error"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    manifest = {
        "mode": "real_api_100",
        "counts": COUNTS,
        "base": args.base,
        "source_sha256": {
            str(p.relative_to(args.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (args.root / "agentgpt").rglob("*.py")
        },
        "judging": "Deterministic assertions, not an independent semantic LLM judge.",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    await asyncio.gather(*(run(group, n) for group, count in COUNTS.items() for n in range(count)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18210")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parallel", type=int, default=3)
    asyncio.run(main(parser.parse_args()))
