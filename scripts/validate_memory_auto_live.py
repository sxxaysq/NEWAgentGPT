"""Validate automatic memory with a real model and an isolated local SQLite store.

This is a memory/context acceptance suite, not the full multi-agent business benchmark.
Every model input is synthetic; no deployed business database or API is modified.
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
import traceback
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage

from agentgpt.agents.prompts import CONTEXT_RULES
from agentgpt.config import ModelOptions, Settings
from agentgpt.host.context import ContextAssembler
from agentgpt.memory_lite import LiteMemory
from agentgpt.model import create_model
from agentgpt.store import Store

ROOT = Path(__file__).resolve().parent.parent
ORIGINAL_CASES = ROOT / "artifacts/memory-failed-retest-20261110/corpus.json"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(base_url, output, corpus, selected):
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        data_dir=output,
        memory_enabled=True,
        model_base_url=base_url,
    )
    store = Store(settings.database, memory_enabled=True)
    await store.initialize()
    store.memory_lite = True
    memory = LiteMemory(store, settings)
    records, trace = [], []
    source = {case["case_id"]: case for case in json.loads(corpus.read_text(encoding="utf-8"))}
    write_json(output / "manifest.json", {
        "model": settings.model_name,
        "model_base_url": settings.model_base_url,
        "memory_timeout_s": settings.memory_lite_timeout_s,
        "scope": "isolated memory/context integration; no deployed business API",
        "selected": selected,
        "sources": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [ROOT / "agentgpt/memory_lite.py", ROOT / "agentgpt/agents/prompts.py"]
        },
    })

    async def request(text, user, scope=""):
        thread = await store.create_thread(user, text, {"memory_scope": scope})
        run, _ = await store.enqueue_run(thread, "start", "", None, uuid4().hex)
        turn = await store.turn_for_run(run["id"])
        receipt = await memory.handle(user, thread["id"], turn["input_seq"], scope)
        trace.append({"user": user, "text": text, "scope": scope, "receipt": receipt})
        write_json(output / "trace.json", trace)
        return thread, run, turn, receipt

    async def context(text, user, scope=""):
        thread, run, turn, receipt = await request(text, user, scope)
        rows = await memory.recall(user, scope, text)
        pref = await store.memory_preference(user)
        background = await ContextAssembler(store, settings).assemble(
            user, thread["id"], text, before_input_seq=turn["input_seq"],
            memory_context=rows, memory_revision=pref["revision"], scope=scope,
        )
        background.update(current_request=text, memory_receipt=receipt)
        return run, background

    async def answer(text, user, scope=""):
        run, background = await context(text, user, scope)
        response = await create_model(
            settings, ModelOptions(temperature=0, max_tokens=500)
        ).ainvoke([
            SystemMessage(content="简洁回答用户问题。\n" + CONTEXT_RULES),
            HumanMessage(content=json.dumps(background, ensure_ascii=False)),
        ])
        assert isinstance(response.content, str) and response.content.strip(), response
        await store.finish_turn(run["id"], outcome="completed", assistant_text=response.content)
        trace.append({"user": user, "question": text, "answer": response.content,
                      "memory_context": background["memory_context"]})
        write_json(output / "trace.json", trace)
        return response.content, background

    async def restart():
        nonlocal store, memory
        store = Store(settings.database, memory_enabled=True)
        store.memory_lite = True
        await store.initialize()
        memory = LiteMemory(store, settings)

    async def automatic():
        user = "automatic-user"
        text = "我平时习惯看中文解释，代码例子一般用 Python。"
        thread, _, turn, receipt = await request(text, user)
        assert receipt["status"] == "committed", receipt
        assert receipt["intent"] == "automatic" and receipt["remaining"] == text, receipt
        rows = await memory.recall(user, "", "")
        assert rows and all(row["kind"] == "preference" for row in rows), rows
        raw = json.dumps(rows, ensure_ascii=False)
        assert "中文" in raw and "Python" in raw, rows
        await restart()

        async def unexpected_call(*_args):
            raise AssertionError("A persisted receipt should skip the parser on replay")

        replay = await LiteMemory(store, settings, parser=unexpected_call).handle(
            user, thread["id"], turn["input_seq"], ""
        )
        assert replay == receipt, replay
        result, background = await answer("我习惯用什么语言看解释和写代码？", user)
        assert "中文" in result and "Python" in result, result
        expected = {(row["id"], row["version"], row["content"]) for row in rows}
        injected = {(row["id"], row["version"], row["content"])
                    for row in background["memory_context"]}
        assert injected == expected, background["memory_context"]
        return {"receipt": receipt, "answer": result, "injected": len(rows)}

    async def temporary_override():
        user = "temporary-user"
        _, _, _, saved = await request("我平时习惯看中文解释。", user)
        assert saved["status"] == "committed", saved
        before = await store.memories(user)
        text = "这次只用英文回答：用一句话解释 SQLite 是什么。"
        result, background = await answer(text, user)
        assert background["memory_receipt"]["status"] == "noop", background["memory_receipt"]
        assert await store.memories(user) == before
        assert "SQLite" in result and not re.search(r"[\u4e00-\u9fff]", result), result
        return {"answer": result, "receipt": background["memory_receipt"]}

    async def quote_and_opt_out():
        user = "no-write-user"
        receipts = []
        for text in [
            "这只是举例，不要记住：我喜欢喝咖啡。",
            "帮我分析小说里的这句话：‘我一向喜欢用英文写作。’这不是我的个人偏好。",
            "PINE 项目预算是八万元，目前正在比较两个部署方案。",
        ]:
            _, _, _, receipt = await request(text, user, "pine")
            assert receipt["status"] == "noop" and receipt["remaining"] == text, receipt
            receipts.append(receipt)
        assert not await store.memories(user, "pine")
        return receipts

    async def dedup_and_update():
        user = "update-user"
        text = "我平时习惯看中文解释。"
        _, _, _, saved = await request(text, user)
        assert saved["status"] == "committed", saved
        before = await store.memories(user)
        assert len(before) == 1, before
        _, _, _, duplicate = await request(text, user)
        assert duplicate["status"] == "noop", duplicate
        assert await store.memories(user) == before
        update_text = "我现在习惯看英文解释，以后都用英文。"
        _, _, _, updated = await request(update_text, user)
        assert updated["status"] == "committed", updated
        assert updated["intent"] == "automatic" and updated["remaining"] == update_text, updated
        rows = await store.memories(user)
        assert len(rows) == 1 and rows[0]["id"] == before[0]["id"], rows
        assert rows[0]["version"] > before[0]["version"] and "英文" in rows[0]["content"], rows
        return {"duplicate": duplicate, "updated": updated, "rows": rows}

    async def automatic_with_business():
        text = "我平时习惯看中文解释。请用一句话解释 SQLite 是什么。"
        _, _, _, receipt = await request(text, "automatic-business-user")
        assert receipt["status"] == "committed" and receipt["intent"] == "automatic", receipt
        assert receipt["remaining"] == text, receipt
        rows = await store.memories("automatic-business-user")
        assert rows and any("中文" in row["content"] for row in rows), rows
        assert not any("SQLite" in row["content"] for row in rows), rows
        return receipt

    async def delete_and_no_resurrection():
        user = "forget-user"
        text = "我平时习惯看中文解释。"
        _, _, _, saved = await request(text, user)
        assert saved["status"] == "committed", saved
        _, _, _, deleted = await request("忘记我的回答语言偏好。", user)
        assert deleted["status"] == "committed", deleted
        assert any(op["action"] == "DELETE" for op in deleted["operations"]), deleted
        await restart()
        assert not await memory.recall(user, "", "")
        _, _, _, repeated = await request(text, user)
        assert repeated["status"] in {"noop", "failed"}, repeated
        assert not await memory.recall(user, "", "")
        tombstones = await store.memories(user, include_deleted=True)
        assert tombstones and all(row["deleted"] for row in tombstones), tombstones
        return {"deleted": deleted, "automatic_repeat": repeated}

    async def project_isolation():
        user = "project-user"
        _, _, _, receipt = await request("请记住，PINE 项目的预算上限是八万元。", user, "pine")
        assert receipt["status"] == "committed", receipt
        rows = await memory.recall(user, "pine", "")
        assert any(row["scope"] == "pine" and "八万" in row["content"] for row in rows), rows
        assert not await memory.recall(user, "other", "")
        assert not await memory.recall("another-user", "pine", "")
        return receipt

    async def original(case_id):
        case = source[case_id]
        user = "original-" + case_id
        first, question = case["prompts"][:2]
        expected = {"cross_session-00": "香菜", "cross_session-08": "中文",
                    "isolation-00": "香菜", "isolation-02": "周四"}[case_id]
        _, _, _, receipt = await request(first, user)
        assert receipt["status"] == "committed", receipt
        rows = await memory.recall(user, "", question)
        assert any(expected in row["content"] for row in rows), rows
        result, background = await answer(question, user)
        assert expected in result and background["memory_context"], result
        if case_id.startswith("isolation"):
            outsider, other_background = await answer(question, user + "-outsider")
            assert not other_background["memory_context"]
            assert expected not in outsider, outsider
            return {"input": first, "receipt": receipt, "answer": result,
                    "outsider_answer": outsider}
        await store.set_memory_preference(user, False)
        _, disabled = await context(question, user)
        assert not disabled["memory_context"] and disabled["memory_receipt"]["status"] == "disabled"
        return {"input": first, "receipt": receipt, "answer": result, "disabled_injected": 0}

    cases = [
        ("automatic_restart_replay_and_answer", automatic),
        ("temporary_override", temporary_override),
        ("quoted_opt_out_and_project_noop", quote_and_opt_out),
        ("dedup_and_automatic_update", dedup_and_update),
        ("automatic_preserves_business_request", automatic_with_business),
        ("delete_restart_no_resurrection", delete_and_no_resurrection),
        ("project_and_user_isolation", project_isolation),
    ]
    for case_id in ("cross_session-00", "cross_session-08", "isolation-00", "isolation-02"):
        async def run_original(identifier=case_id):
            return await original(identifier)
        cases.append((case_id, run_original))
    if selected:
        unknown = set(selected) - {name for name, _ in cases}
        if unknown:
            raise ValueError(f"Unknown case names: {sorted(unknown)}")
        cases = [(name, body) for name, body in cases if name in selected]
    for name, body in cases:
        started = time.monotonic()
        try:
            result = {"case": name, "passed": True, "detail": await body()}
        except Exception as exc:
            result = {"case": name, "passed": False,
                      "error": f"{type(exc).__name__}: {exc}",
                      "traceback": traceback.format_exc()}
        result["seconds"] = round(time.monotonic() - started, 2)
        records.append(result)
        write_json(output / "results.json", records)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    write_json(output / "summary.json", {
        "passed": sum(row["passed"] for row in records), "total": len(records),
        "failures": [row["case"] for row in records if not row["passed"]],
    })
    if not all(row["passed"] for row in records):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://192.168.30.215:8000/v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=ORIGINAL_CASES)
    parser.add_argument("--case", action="append", default=[], dest="selected")
    args = parser.parse_args()
    asyncio.run(main(args.base_url, args.output, args.corpus, args.selected))
