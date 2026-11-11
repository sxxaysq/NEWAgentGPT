"""Lightweight memory lifecycle with real SQLite and controlled model proposals."""

import asyncio
import json
import sqlite3

import pytest

from agentgpt.config import Settings
from agentgpt.host.context import ContextAssembler
from agentgpt.host.runner import Control, RunManager
from agentgpt.memory_lite import LiteMemory, Operation, Request
from agentgpt.store import Store


async def setup(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, memory_enabled=True)
    store = Store(settings.database)
    await store.initialize()
    store.memory_lite = True
    return store, settings


async def source(store, text, user="alice", scope=""):
    thread = await store.create_thread(user, text, {"memory_scope": scope})
    run, _ = await store.enqueue_run(thread, "start", "", "", "request")
    turn = await store.turn_for_run(run["id"])
    return thread, run, turn


def service(
    store,
    settings,
    action="ADD",
    target="",
    quote="我偏好中文回答",
    kind="preference",
    remaining="",
    intent="explicit",
):
    async def parse(text, rows):
        return Request(
            intent=intent,
            operations=[
                Operation(action=action, target_id=target, quote=quote, key="language", kind=kind)
            ],
            remaining_request=remaining,
        )

    return LiteMemory(store, settings, parser=parse)


async def execute(memory, item, user="alice", scope=""):
    thread, _, turn = item
    return await memory.handle(user, thread["id"], turn["input_seq"], scope)


@pytest.mark.asyncio
async def test_commit_replay_restart_and_failed_business(tmp_path):
    store, settings = await setup(tmp_path)
    item = await source(store, "记住，我偏好中文回答")
    memory = service(store, settings)
    receipt = await execute(memory, item)
    assert receipt["status"] == "committed"
    await store.finish_turn(
        item[1]["id"], outcome="failed", assistant_text="研究失败", memory_enabled=True
    )
    restarted = Store(settings.database)
    await restarted.initialize()
    memory = service(restarted, settings)
    assert await execute(memory, item) == receipt
    assert len(await memory.recall("alice", "", "语言")) == 1
    async with store.connect() as db:
        for table in ("memory_jobs", "context_jobs"):
            assert (await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone())[0] == 0


@pytest.mark.asyncio
async def test_update_delete_stale_input_and_replay(tmp_path):
    store, settings = await setup(tmp_path)
    first = await source(store, "记住，我偏好中文回答")
    receipt = await execute(service(store, settings), first)
    mid = receipt["operations"][0]["id"]
    older = await source(store, "把记忆改为中文")
    newer = await source(store, "把记忆改为英文")
    receipt = await execute(service(store, settings, "UPDATE", mid, "英文"), newer)
    stale = await execute(service(store, settings, "UPDATE", mid, "中文"), older)
    assert stale["status"] == "failed" and "较旧" in stale["message"]
    deletion = await source(store, "忘记回答语言")
    await execute(service(store, settings, "DELETE", mid, "回答语言"), deletion)
    assert await execute(service(store, settings, "UPDATE", mid, "英文"), newer) == receipt
    assert not await service(store, settings).recall("alice", "", "")
    rows = await store.memories("alice", include_deleted=True)
    assert rows[0]["deleted"] == 1 and rows[0]["version"] == 3


@pytest.mark.asyncio
async def test_isolation_and_project_scope(tmp_path):
    store, settings = await setup(tmp_path)
    item = await source(store, "记住，预算十万", scope="pine")
    receipt = await execute(
        service(store, settings, quote="预算十万", kind="project"), item, scope="pine"
    )
    memory = service(store, settings)
    assert not await memory.recall("bob", "pine", "")
    assert not await memory.recall("alice", "other", "")
    assert len(await memory.recall("alice", "pine", "")) == 1
    bob = await source(store, "忘记预算", user="bob", scope="pine")
    denied = await execute(
        service(store, settings, "DELETE", receipt["operations"][0]["id"], "预算"),
        bob,
        user="bob",
        scope="pine",
    )
    assert denied["status"] == "failed" and "授权" in denied["message"]


@pytest.mark.asyncio
async def test_invalid_quote_rolls_back_and_disabled_does_not_parse(tmp_path):
    store, settings = await setup(tmp_path)
    item = await source(store, "记住，我偏好中文回答")
    failed = await execute(service(store, settings, quote="用户偏好英文"), item)
    assert failed["status"] == "failed" and "原文" in failed["message"]
    assert not await store.memories("alice")
    settings.memory_enabled = False
    disabled_input = await source(store, "记住，我偏好中文回答")
    assert (await execute(service(store, settings), disabled_input))["status"] == "disabled"
    assert not await store.memories("alice")


@pytest.mark.asyncio
async def test_automatic_preference_keeps_business_and_is_injected_across_sessions(tmp_path):
    store, settings = await setup(tmp_path)
    text = "我偏好中文回答；解释一下数据库事务"
    item = await source(store, text)
    memory = service(store, settings, intent="automatic", remaining="解释一下数据库事务")
    result = await execute(memory, item)
    assert result["status"] == "committed" and result["intent"] == "automatic"
    assert result["remaining"] == text
    other, _, turn = await source(store, "请解释索引")
    recalled = await memory.recall("alice", "", "索引")
    context = await ContextAssembler(store, settings).assemble(
        "alice", other["id"], "请解释索引", before_input_seq=turn["input_seq"],
        memory_context=recalled,
    )
    assert context["memory_context"][0]["content"] == "我偏好中文回答"
    assert context["context_sources"][0]["kind"] == "user_memory"


@pytest.mark.asyncio
async def test_pure_memory_bypasses_business_agents(tmp_path):
    store, settings = await setup(tmp_path)
    item = await source(store, "记住，我偏好中文回答")
    manager = RunManager(store, object(), settings, memory=service(store, settings))
    await manager._execute(item[1], item[0], Control())
    turn = await store.turn_for_run(item[1]["id"])
    assert turn["outcome"] == "completed" and "已提交" in turn["assistant_text"]


@pytest.mark.asyncio
async def test_recent_raw_corrections_ignore_legacy_state(tmp_path):
    store, settings = await setup(tmp_path)
    thread, run, _ = await source(store, "周六值班")
    await store.finish_turn(run["id"], outcome="completed", assistant_text="收到")
    run, _ = await store.enqueue_run(thread, "followup", "改周五晚上", "", "second")
    await store.finish_turn(run["id"], outcome="completed", assistant_text="收到")
    run, _ = await store.enqueue_run(thread, "followup", "我哪天值班？", "", "third")
    turn = await store.turn_for_run(run["id"])
    context = await ContextAssembler(store, settings).assemble(
        "alice", thread["id"], "我哪天值班？", before_input_seq=turn["input_seq"]
    )
    assert [i["text"] for i in context["pending_user_updates"]] == ["周六值班", "改周五晚上"]
    assert context["turn_summaries"] == []
    assert len(context["recent_turns"]) == 2
    assert "周五晚上有空" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.asyncio
async def test_summary_failure_preserves_raw_and_success_is_durable(tmp_path, monkeypatch):
    from agentgpt.host import lite_summary

    store, settings = await setup(tmp_path)
    settings.context_history_tokens = 800
    thread, run, _ = await source(store, "只考虑本地部署。" * 12)
    await store.finish_turn(run["id"], outcome="completed", assistant_text="收到")
    for n in range(7):
        run, _ = await store.enqueue_run(thread, "followup", f"继续任务{n}", "", str(n))
        await store.finish_turn(run["id"], outcome="completed", assistant_text="收到")
    run, _ = await store.enqueue_run(thread, "followup", "总结", "", "last")
    turn = await store.turn_for_run(run["id"])

    async def fail(*args):
        raise OSError("model unavailable")

    monkeypatch.setattr(lite_summary, "structured_response", fail)
    monkeypatch.setattr(lite_summary, "create_model", lambda *args: object())
    assembler = ContextAssembler(store, settings)
    context = await assembler.assemble(
        "alice", thread["id"], "总结", before_input_seq=turn["input_seq"]
    )
    assert len(context["pending_user_updates"]) == 8
    async with store.connect() as db:
        assert not await (await db.execute("SELECT * FROM lite_summaries")).fetchone()

    async def success(*args):
        return lite_summary.Excerpts(quotes=["只考虑本地部署。"])

    monkeypatch.setattr(lite_summary, "structured_response", success)
    context = await assembler.assemble(
        "alice", thread["id"], "总结", before_input_seq=turn["input_seq"]
    )
    assert context["thread_summary"]["content"] == "只考虑本地部署。"
    assert len(context["pending_user_updates"]) == 6
    assert any("只考虑本地部署。" in s["text"] for s in context["context_sources"])


@pytest.mark.asyncio
async def test_mixed_request_and_capacity_rollback(tmp_path):
    store, settings = await setup(tmp_path)
    item = await source(store, "记住，我偏好中文回答；调研数据库")
    receipt = await execute(service(store, settings, remaining="调研数据库"), item)
    assert receipt["remaining"] == "调研数据库"
    settings.memory_lite_limit = 1
    other = await source(store, "记住，项目预算十万", scope="pine")
    result = await execute(
        service(store, settings, quote="项目预算十万", kind="project"), other, scope="pine"
    )
    assert result["status"] == "failed" and "容量" in result["message"]
    assert result["remaining"] == "记住，项目预算十万"
    assert len(await store.memories("alice")) == 1


@pytest.mark.asyncio
async def test_queued_old_input_cannot_overwrite_later_manual_edit(tmp_path):
    store, settings = await setup(tmp_path)
    mid = await store.edit_memory("alice", "中文")
    item = await source(store, "修改记忆为英文")
    await store.edit_memory("alice", "日文", memory_id=mid, expected_version=1)
    result = await execute(service(store, settings, "UPDATE", mid, "英文"), item)
    assert result["status"] == "failed" and "较旧" in result["message"]
    assert (await store.memories("alice"))[0]["content"] == "日文"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["noop", "failed", "disabled", "committed"])
async def test_every_outcome_is_durable_and_replay_never_reparses(tmp_path, status):
    store, settings = await setup(tmp_path)
    item = await source(store, "我偏好中文回答")
    calls = []

    async def parse(*args):
        calls.append(args)
        if status == "failed":
            raise TimeoutError("offline")
        if status == "noop":
            return Request()
        return Request(intent="automatic", operations=[
            Operation(action="ADD", key="language", quote="我偏好中文回答")
        ])

    settings.memory_enabled = status != "disabled"
    result = await execute(LiteMemory(store, settings, parser=parse), item)
    assert result["status"] == status and result["remaining"] == "我偏好中文回答"
    restarted = Store(settings.database)
    await restarted.initialize()
    settings.memory_enabled = True

    async def unexpected(*args):
        raise AssertionError("replayed input must reuse its durable outcome")

    assert await execute(LiteMemory(restarted, settings, parser=unexpected), item) == result
    assert len(calls) == (0 if status == "disabled" else 1)


@pytest.mark.asyncio
async def test_unchanged_preference_is_noop_but_advances_input_order(tmp_path):
    store, settings = await setup(tmp_path)
    first = await source(store, "我偏好中文回答")
    receipt = await execute(service(store, settings, intent="automatic"), first)
    mid = receipt["operations"][0]["id"]
    older = await source(store, "我偏好英文回答")
    newest = await source(store, "我偏好中文回答")
    result = await execute(service(store, settings, intent="automatic"), newest)
    assert result["status"] == "noop" and result["remaining"] == "我偏好中文回答"
    assert (await store.memory_preference("alice"))["revision"] == 1
    result = await execute(
        service(store, settings, "UPDATE", mid, "我偏好英文回答", intent="automatic"), older
    )
    assert result["status"] == "failed" and "较旧" in result["message"]
    assert (await store.memories("alice"))[0]["version"] == 1


@pytest.mark.asyncio
async def test_tombstone_blocks_automatic_resurrection_but_allows_new_explicit_request(tmp_path):
    store, settings = await setup(tmp_path)
    first = await source(store, "我偏好中文回答")
    result = await execute(service(store, settings, intent="automatic"), first)
    mid = result["operations"][0]["id"]
    old_request = await source(store, "记住，我偏好中文回答")
    await store.edit_memory("alice", "", memory_id=mid, expected_version=1, delete=True)
    stale = await execute(service(store, settings), old_request)
    assert stale["status"] == "failed"
    automatic = await source(store, "我偏好中文回答")
    blocked = await execute(service(store, settings, intent="automatic"), automatic)
    assert blocked["status"] == "failed" and "不会自动恢复" in blocked["message"]
    assert not await store.memories("alice")
    explicit = await source(store, "重新记住，我偏好中文回答")
    restored = await execute(service(store, settings), explicit)
    assert restored["status"] == "committed"
    assert (await store.memories("alice"))[0]["id"] == mid


@pytest.mark.asyncio
@pytest.mark.parametrize("action,kind", [("DELETE", "preference"), ("ADD", "project")])
async def test_automatic_extraction_cannot_delete_or_save_project_facts(tmp_path, action, kind):
    store, settings = await setup(tmp_path)
    item = await source(store, "忘记我的项目预算十万", scope="pine")
    result = await execute(
        service(store, settings, action=action, kind=kind, quote="项目预算十万",
                intent="automatic"), item, scope="pine",
    )
    assert result["status"] == "failed" and result["remaining"] == "忘记我的项目预算十万"
    assert not await store.memories("alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError, sqlite3.OperationalError])
async def test_cancellation_and_database_errors_are_not_cached_as_model_failures(tmp_path, error):
    store, settings = await setup(tmp_path)
    item = await source(store, "我偏好中文回答")

    async def fail(*args):
        raise error()

    with pytest.raises(error):
        await execute(LiteMemory(store, settings, parser=fail), item)
    async with store.connect() as db:
        assert not await (await db.execute("SELECT * FROM explicit_memory_receipts")).fetchone()


@pytest.mark.asyncio
async def test_clarification_preserves_business_request(tmp_path):
    store, settings = await setup(tmp_path)
    item = await source(store, "忘记那个偏好；解释事务")

    async def parse(*args):
        return Request(intent="explicit", clarification="请指明要删除哪条偏好")

    result = await execute(LiteMemory(store, settings, parser=parse), item)
    assert result["status"] == "clarification"
    assert result["remaining"] == "忘记那个偏好；解释事务"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_quote", [True, False])
async def test_invalid_batch_rolls_back_all_changes_and_persists_failure(tmp_path, invalid_quote):
    store, settings = await setup(tmp_path)
    item = await source(store, "我偏好中文回答，我习惯用 Python")

    async def parse(*args):
        return Request(intent="automatic", operations=[
            Operation(action="ADD", key="language", quote="我偏好中文回答"),
            Operation(action="ADD", key="code" if invalid_quote else "language",
                      quote="我偏好英文回答" if invalid_quote else "我习惯用 Python"),
        ])

    memory = LiteMemory(store, settings, parser=parse)
    result = await execute(memory, item)
    assert result["status"] == "failed"
    assert not await store.memories("alice")
    assert (await store.memory_preference("alice"))["revision"] == 0
    assert await execute(memory, item) == result


@pytest.mark.asyncio
async def test_automatic_extraction_cannot_recreate_deleted_manual_memory_with_a_new_key(tmp_path):
    store, settings = await setup(tmp_path)
    mid = await store.edit_memory("alice", "我偏好中文回答")
    await store.edit_memory("alice", "", memory_id=mid, expected_version=1, delete=True)
    item = await source(store, "我偏好中文回答")
    result = await execute(service(store, settings, intent="automatic"), item)
    assert result["status"] == "failed" and "不会自动恢复" in result["message"]
    rows = await store.memories("alice", include_deleted=True)
    assert len(rows) == 1 and rows[0]["deleted"] == 1


@pytest.mark.asyncio
async def test_many_tombstones_do_not_consume_capacity_or_disable_delete(tmp_path):
    store, settings = await setup(tmp_path)
    old_key, old_quote = "language", "我一直偏好英文"
    async with store.connect() as db:
        await db.executemany(
            "INSERT INTO memories(id,user_id,scope,fact_key,content,kind,source_thread,"
            "source_message,source_quote,deleted,created,updated) "
            "VALUES(?,'alice','',?,?,'preference','','',?,1,0,?)",
            [
                (f"old-{index}", old_key if index == 0 else f"retired-{index}",
                 old_quote if index == 0 else f"旧偏好{index}",
                 old_quote if index == 0 else f"旧偏好{index}", index)
                for index in range(220)
            ],
        )
    active_id = await store.edit_memory("alice", "我喜欢简洁回答")
    snapshots = []

    async def parse(text, rows):
        snapshots.append(rows)
        assert len(rows) <= 22
        assert sum(len(row["content"]) for row in rows) <= settings.memory_recall_char_limit
        assert not any(row["id"] == "old-0" for row in rows)
        if "忘记" in text:
            return Request(intent="explicit", operations=[
                Operation(action="DELETE", target_id=active_id, quote="简洁回答")
            ])
        if "英文" in text:
            return Request(intent="automatic", operations=[
                Operation(action="ADD", key=old_key, quote=text)
            ])
        return Request(intent="automatic", operations=[
            Operation(action="ADD", key="code_language", quote=text)
        ])

    memory = LiteMemory(store, settings, parser=parse)
    added = await execute(memory, await source(store, "我习惯用 Python"))
    assert added["status"] == "committed"
    deleted = await execute(memory, await source(store, "忘记简洁回答"))
    assert deleted["status"] == "committed"
    # The old key was omitted from the bounded prompt, but remains authoritative in SQLite.
    blocked = await execute(memory, await source(store, "我现在偏好英文"))
    assert blocked["status"] == "failed" and "不会自动恢复" in blocked["message"]
    remaining = await memory.recall("alice", "", "")
    assert [row["content"] for row in remaining] == ["我习惯用 Python"]
    assert len(snapshots) == 3


@pytest.mark.asyncio
async def test_old_tombstone_content_is_checked_when_model_changes_the_key(tmp_path):
    store, settings = await setup(tmp_path)
    async with store.connect() as db:
        await db.executemany(
            "INSERT INTO memories(id,user_id,scope,fact_key,content,kind,source_thread,"
            "source_message,source_quote,deleted,created,updated) "
            "VALUES(?,'alice','',?,?,'preference','','',?,1,0,?)",
            [
                (f"old-{index}", f"manual-{index}",
                 "我偏好中文回答" if index == 0 else f"旧偏好{index}",
                 "我偏好中文回答" if index == 0 else f"旧偏好{index}", index)
                for index in range(205)
            ],
        )
    item = await source(store, "我偏好中文回答")
    result = await execute(service(store, settings, intent="automatic"), item)
    assert result["status"] == "failed" and "不会自动恢复" in result["message"]
    assert not await store.memories("alice")
