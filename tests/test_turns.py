import json

import pytest

from agentgpt.store import Store


async def setup(tmp_path, memory_enabled=None):
    store = Store(tmp_path / "turns.sqlite3", memory_enabled=memory_enabled)
    await store.initialize()
    thread = await store.create_thread("alice", "原始目标", {"memory_scope": "ZQ"})
    return store, thread


@pytest.mark.asyncio
async def test_acceptance_freezes_input_options_and_replay(tmp_path):
    store, thread = await setup(tmp_path)
    row, _ = await store.enqueue_run(thread, "start", "", None, "request-1")
    again, new = await store.enqueue_run(thread, "start", "", None, "request-1")
    assert not new and again["id"] == row["id"]
    thread["goal"] = "changed"
    thread["settings"]["memory_scope"] = "OTHER"
    turn = await store.turn_for_run(row["id"])
    assert turn["user_text"] == "原始目标"
    assert turn["scope_snapshot"]["memory_scope"] == "ZQ"
    assert json.loads(row["context"])["options"]["memory_scope"] == "ZQ"
    snapshot = await store.context_snapshot("alice", thread["id"], 1)
    assert len(snapshot["inputs"]) == len(snapshot["turns"]) == 1
    with pytest.raises(ValueError):
        await store.enqueue_run(thread, "followup", "  ", None)
    with pytest.raises(ValueError):
        await store.context_snapshot("bob", thread["id"], 1)


@pytest.mark.asyncio
async def test_finish_is_atomic_and_idempotent(tmp_path):
    store, thread = await setup(tmp_path)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    # Fault after updating turn/run but before event projection: all writes must roll back.
    async with store.connect() as db:
        await db.execute(
            "CREATE TRIGGER fail_terminal BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT,'injected crash'); END"
        )
    with pytest.raises(Exception, match="injected crash"):
        await store.finish_turn(
            row["id"], outcome="completed", assistant_text="答复", memory_enabled=True
        )
    assert (await store.run(row["id"]))["status"] == "queued"
    assert (await store.turn_for_run(row["id"]))["phase"] == "queued"
    async with store.connect() as db:
        await db.execute("DROP TRIGGER fail_terminal")
    await store.finish_turn(
        row["id"], outcome="completed", assistant_text="答复", memory_enabled=True
    )
    await store.finish_turn(
        row["id"], outcome="completed", assistant_text="重复", memory_enabled=True
    )
    view = await store.event_view(thread["id"])
    assert view["state"]["final_answer"] == "答复"
    assert view["state"]["run_id"] == row["id"]
    async with store.connect() as db:
        counts = [
            await (await db.execute(f"SELECT count(*) FROM {table}")).fetchone()
            for table in ("memory_jobs", "context_jobs", "turn_artifacts", "events")
        ]
    assert [c[0] for c in counts] == [1, 3, 1, 2]


@pytest.mark.asyncio
async def test_failed_and_disabled_turns_have_context_but_no_fact_jobs(tmp_path):
    store, thread = await setup(tmp_path, memory_enabled=False)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    # Turning service on later must not backfill an input accepted while disabled.
    store.memory_enabled = True
    await store.finish_turn(
        row["id"], outcome="completed", assistant_text="答复", memory_enabled=True
    )
    second, _ = await store.enqueue_run(thread, "followup", "新问题", None)
    await store.finish_turn(
        second["id"], outcome="failed", assistant_text="失败", memory_enabled=True
    )
    await store.reconcile_turns(memory_enabled=True)
    async with store.connect() as db:
        assert (await (await db.execute("SELECT count(*) FROM memory_jobs")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT count(*) FROM context_jobs")).fetchone())[0] == 6
    assert (await store.turn_for_run(second["id"]))["user_text"] == "新问题"


@pytest.mark.asyncio
async def test_preference_change_blocks_finish_and_reconcile(tmp_path):
    store, thread = await setup(tmp_path, memory_enabled=True)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    await store.set_memory_preference("alice", False)
    await store.set_memory_preference("alice", True)
    await store.finish_turn(
        row["id"], outcome="completed", assistant_text="答复", memory_enabled=True
    )
    assert (await store.turn_for_run(row["id"]))["memory_eligibility"] == "disabled"
    assert await store.next_memory_job() is None


@pytest.mark.asyncio
async def test_pause_continue_retains_turn_and_rpc_identity(tmp_path):
    store, thread = await setup(tmp_path)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    await store.update_run(row["id"], control="pause")
    await store.finish_turn(row["id"], outcome="paused", assistant_text="暂停")
    command, _ = await store.enqueue_run(thread, "continue", "", None, "resume-1")
    assert command["id"] == row["id"]
    assert command["control"] == ""
    repeated, new = await store.enqueue_run(thread, "continue", "", None, "resume-1")
    assert not new and repeated["id"] == row["id"]
    await store.finish_turn(command["id"], outcome="completed", assistant_text="完成")
    stop, _ = await store.enqueue_run(thread, "stop", "", None)
    assert await store.turn_for_run(stop["id"]) is None
    snapshot = await store.context_snapshot("alice", thread["id"], 1)
    assert len(snapshot["turns"]) == len(snapshot["inputs"]) == 1
    assert snapshot["turns"][0]["turn_revision"] == 2
    assert len(snapshot["artifacts"]) == 2


@pytest.mark.asyncio
async def test_reconcile_only_repairs_recorded_eligibility(tmp_path):
    store, thread = await setup(tmp_path, memory_enabled=True)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(
        row["id"], outcome="completed", assistant_text="答复", memory_enabled=True
    )
    async with store.connect() as db:
        await db.execute("DELETE FROM memory_jobs")
        await db.execute("DELETE FROM context_jobs")
    await store.reconcile_turns(memory_enabled=True)
    assert await store.next_memory_job()
    async with store.connect() as db:
        assert (await (await db.execute("SELECT count(*) FROM context_jobs")).fetchone())[0] == 3


@pytest.mark.asyncio
async def test_stop_paused_turn_is_terminal_with_atomic_jobs(tmp_path):
    store, thread = await setup(tmp_path)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(row["id"], outcome="paused", assistant_text="暂停")
    stop, _ = await store.enqueue_run(thread, "stop", "", None, "stop-request")
    assert stop["status"] == "done"
    turn = await store.turn_for_run(row["id"])
    assert (turn["phase"], turn["outcome"]) == ("terminal", "stopped")
    assert (await store.event_view(thread["id"]))["state"]["status"] == "stopped"
    with pytest.raises(ValueError, match="没有可继续"):
        await store.enqueue_run(thread, "continue", "", None)
    async with store.connect() as db:
        assert (await (await db.execute("SELECT count(*) FROM turns")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT count(*) FROM context_jobs")).fetchone())[0] == 3


@pytest.mark.asyncio
async def test_initialize_does_not_steal_a_live_context_job_lease(tmp_path):
    store, thread = await setup(tmp_path)
    await store.enqueue_run(thread, "start", "", None)
    async with store.connect() as db:
        await db.execute("UPDATE context_jobs SET status='running',lease_until=9999999999")
    other_worker = Store(store.path)
    await other_worker.initialize()
    async with store.connect() as db:
        job = await (await db.execute("SELECT * FROM context_jobs")).fetchone()
    assert (job["status"], job["lease_until"]) == ("running", 9999999999)


@pytest.mark.asyncio
async def test_control_is_durable_before_manager_handles_response(tmp_path):
    store, thread = await setup(tmp_path)
    row, _ = await store.enqueue_run(thread, "start", "", None)
    control, _ = await store.enqueue_run(thread, "pause", "", None, "pause-1")
    assert control["status"] == "done"
    assert (await store.run(row["id"]))["control"] == "pause"
    assert (await store.thread(thread["id"]))["status"] == "pausing"
    assert len(await store.queued_runs(thread["id"])) == 1
    await store.finish_turn(row["id"], outcome="paused", assistant_text="暂停")
    saved = json.loads((await store.run(row["id"]))["context"])
    saved["orchestrator"] = {"deadline_at": 123}
    await store.update_run(row["id"], context=json.dumps(saved))
    resumed, _ = await store.enqueue_run(thread, "continue", "", None)
    assert json.loads(resumed["context"])["orchestrator"]["deadline_at"] is None
