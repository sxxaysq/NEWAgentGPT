"""Durable command inbox: idempotent request ids and a replayable event cursor.

The LangGraph checkpoint replay tests that used to live here went away with the
checkpoint itself — run state is now rebuilt from the Host's SQLite queue and event
log (agentgpt/host/runner.py). What remains worth pinning down is the inbox contract
that survived the refactor, plus the cursor consistency the UI depends on.
"""

import pytest

from agentgpt.store import Store


@pytest.mark.asyncio
async def test_request_id_is_idempotent_and_rejects_a_changed_payload(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    await store.initialize()
    thread = await store.create_thread("alice", "初始目标", {})

    first, created = await store.enqueue_run(thread, "start", "", None, "fixed-request-01")
    assert created is True
    replay, created_again = await store.enqueue_run(thread, "start", "", None, "fixed-request-01")
    assert created_again is False
    assert replay["id"] == first["id"]

    # Reusing the id for a different command is a client bug, not a retry.
    with pytest.raises(ValueError, match="同一请求 ID"):
        await store.enqueue_run(thread, "followup", "其他内容", None, "fixed-request-01")


@pytest.mark.asyncio
async def test_snapshot_cursor_and_stream_prefix_are_consistent(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    await store.initialize()
    thread = await store.create_thread("alice", "目标", {})
    tid = thread["id"]
    await store.append_event(tid, "state", {"status": "running"})
    await store.append_event(tid, "token", {"message_id": "m1", "text": "第一段"})
    view = await store.event_view(tid)
    await store.append_event(tid, "token", {"message_id": "m1", "text": "第二段"})
    remaining = await store.events(tid, view["event_cursor"])
    assert view["live"]["text"] == "第一段"
    assert [e["data"]["text"] for e in remaining] == ["第二段"]
    await store.append_event(tid, "state", {"status": "completed"})
    assert (await store.event_view(tid))["live"] == {}
