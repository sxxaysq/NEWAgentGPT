"""Exercise the real model against an isolated, disposable local memory database."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from agentgpt.config import Settings
from agentgpt.host.context import ContextAssembler
from agentgpt.memory_lite import LiteMemory
from agentgpt.store import Store


async def main(base_url, output):
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        data_dir=output,
        memory_enabled=True,
        model_base_url=base_url,
        memory_lite_timeout_s=90,
    )
    store = Store(settings.database)
    await store.initialize()
    store.memory_lite = True
    memory = LiteMemory(store, settings)
    records = []

    async def request(text, scope="", user="live-alice"):
        thread = await store.create_thread(user, text, {"memory_scope": scope})
        run, _ = await store.enqueue_run(thread, "start", "", "", uuid4().hex)
        turn = await store.turn_for_run(run["id"])
        receipt = await memory.handle(user, thread["id"], turn["input_seq"], scope)
        return receipt

    async def check(name, body):
        try:
            detail = await body()
            row = {"case": name, "passed": True, "detail": detail}
        except Exception as exc:
            row = {"case": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
        records.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        (output / "results.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def remember():
        receipt = await request("请记住，我偏好使用中文回答。")
        assert receipt and receipt["status"] == "committed", receipt
        assert not receipt["remaining"], receipt
        rows = await memory.recall("live-alice", "", "我喜欢用什么语言？")
        assert len(rows) == 1 and "中文" in rows[0]["content"], rows
        return receipt

    async def update():
        receipt = await request("把我的回答语言偏好改为英文，以后都使用英文回答。")
        assert receipt and receipt["status"] == "committed", receipt
        rows = await memory.recall("live-alice", "", "回答语言")
        assert len(rows) == 1 and "英文" in rows[0]["content"], rows
        assert receipt["operations"][0]["action"] == "UPDATE", receipt
        return receipt

    async def mixed():
        receipt = await request("请记住，我不吃香菜。另外，请解释什么是向量数据库。")
        assert receipt and receipt["status"] == "committed", receipt
        assert "向量数据库" in receipt["remaining"], receipt
        rows = await memory.recall("live-alice", "", "")
        assert any("不吃香菜" in row["content"] for row in rows), rows
        return receipt

    async def no_write():
        before = await store.memories("live-alice")
        receipt = await request("这只是举例，不要记住：我喜欢喝咖啡。")
        assert not receipt or receipt["status"] != "committed", receipt
        assert await store.memories("live-alice") == before
        return receipt

    async def project():
        receipt = await request("请记住，PINE 项目的预算上限是八万元。", "pine")
        assert receipt and receipt["status"] == "committed", receipt
        rows = await store.memories("live-alice", "pine")
        assert any(r["scope"] == "pine" and "八万" in r["content"] for r in rows), rows
        assert not any(
            "八万" in r["content"] for r in await memory.recall("live-alice", "other", "")
        )
        assert not await memory.recall("live-bob", "pine", "")
        return receipt

    async def delete():
        receipt = await request("请忘记我的回答语言偏好。")
        assert receipt and receipt["status"] == "committed", receipt
        assert receipt["operations"][0]["action"] == "DELETE", receipt
        restarted = LiteMemory(Store(settings.database), settings)
        rows = await restarted.recall("live-alice", "", "回答语言")
        assert not any("英文" in r["content"] for r in rows), rows
        return receipt

    async def summary():
        settings.context_history_tokens = 1800
        text = "当前研究只考虑本地部署，预算上限十万元。" * 15
        thread = await store.create_thread("summary-user", text, {})
        run, _ = await store.enqueue_run(thread, "start", "", "", "summary-start")
        await store.finish_turn(run["id"], outcome="completed", assistant_text="已确认研究约束。")
        for n in range(8):
            text = "预算改为八万元，其他条件不变。" if n == 0 else f"继续比较方案第{n}项。"
            run, _ = await store.enqueue_run(thread, "followup", text, "", f"summary-{n}")
            await store.finish_turn(run["id"], outcome="completed", assistant_text="继续。")
        run, _ = await store.enqueue_run(thread, "followup", "现在有什么约束？", "", "summary-last")
        turn = await store.turn_for_run(run["id"])
        context = await ContextAssembler(store, settings).assemble(
            "summary-user", thread["id"], "现在有什么约束？", before_input_seq=turn["input_seq"]
        )
        result = context["thread_summary"].get("content", "")
        assert result and "本地部署" in result and "八万" in result, context["thread_summary"]
        return context["thread_summary"]

    for name, body in [
        ("remember_cross_session", remember),
        ("update", update),
        ("mixed_negation", mixed),
        ("no_write", no_write),
        ("project_isolation", project),
        ("delete_restart", delete),
        ("rolling_summary", summary),
    ]:
        await check(name, body)
    if not all(r["passed"] for r in records):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://192.168.30.215:8000/v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.base_url, args.output))
