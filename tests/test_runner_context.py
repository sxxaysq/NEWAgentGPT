"""The actual Host lifecycle uses persisted inputs, not prior UI projections."""

import json

import pytest

from agentgpt.config import Settings
from agentgpt.host.runner import RunManager
from agentgpt.store import Store


class StubOrchestrator:
    seen = []

    def __init__(self, *args, **kwargs):
        self.tasks, self.messages, self.plan_revisions = {}, [], 0
        self.completion_status = "completed"

    async def run(self, scope, goal, options, background):
        self.seen.append((scope, goal, options, background))
        if goal == "crash":
            raise RuntimeError("forced failure")
        return "answer: " + goal


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setattr("agentgpt.host.runner.Orchestrator", StubOrchestrator)
    StubOrchestrator.seen = []
    store = Store(tmp_path / "host.sqlite3")
    await store.initialize()
    manager = RunManager(store, None, Settings(_env_file=None, memory_enabled=False))
    yield store, manager
    await manager.close()


async def complete(manager, thread, operation="start", text="", request_id=None):
    run_id = await manager.command(thread, operation, text, request_id=request_id)
    await manager.running[thread["id"]]
    return run_id


async def test_off_memory_pending_corrections_and_artifact_reach_next_run(host):
    store, manager = host
    thread = await store.create_thread("alice", "预算23万，仅内网，中文表格", {})
    first = await complete(manager, thread)
    await complete(manager, thread, "followup", "预算改成18万，其他不变")
    last = await complete(manager, thread, "followup", "把刚才的表改成英文")
    background = StubOrchestrator.seen[-1][3]
    raw = json.dumps(background, ensure_ascii=False)
    assert "18万" in raw and "仅内网" in raw
    assert background["previous_answer"]["text"] == "answer: 预算改成18万，其他不变"
    assert "把刚才的表改成英文" not in json.dumps(
        background["pending_user_updates"], ensure_ascii=False
    )
    assert (await store.turn_for_run(first))["outcome"] == "completed"
    assert (await store.turn_for_run(last))["outcome"] == "completed"
    async with store.connect() as db:
        assert (await (await db.execute("SELECT count(*) FROM memory_jobs")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT count(*) FROM context_jobs")).fetchone())[0] == 9


async def test_failure_atomic_projection_and_input_survive(host):
    store, manager = host
    thread = await store.create_thread("alice", "crash", {})
    run_id = await complete(manager, thread)
    assert (await store.run(run_id))["status"] == "failed"
    turn = await store.turn_for_run(run_id)
    assert turn["user_text"] == "crash" and turn["outcome"] == "failed"
    state = (await store.event_view(thread["id"]))["state"]
    assert state["run_id"] == run_id and state["turn_id"] == turn["id"]
    assert "forced failure" in state["final_answer"]


async def test_acceptance_freezes_goal_settings_and_future_inputs(host):
    store, manager = host
    thread = await store.create_thread("alice", "original", {"memory_scope": "A"})
    first, _ = await store.enqueue_run(thread, "start", "", None, "one")
    await store.enqueue_run(thread, "followup", "FUTURE-SECRET", None, "two")
    await store.update_thread(thread["id"], settings={"memory_scope": "B"})
    await manager.recover()
    await manager.running[thread["id"]]
    scope, goal, options, background = StubOrchestrator.seen[0]
    assert goal == "original" and options.memory_scope == "A"
    assert "FUTURE-SECRET" not in json.dumps(background)
    assert (await store.turn_for_run(first["id"]))["user_text"] == "original"


async def test_deleted_memory_prevents_old_checkpoint_replay(host):
    store, manager = host
    thread = await store.create_thread("alice", "goal", {})
    row, _ = await store.enqueue_run(thread, "start", "", None, "one")
    await store.update_run(
        row["id"],
        context=json.dumps(
            {
                "version": 1,
                "memory_revision": 0,
                "background": {"memory_context": "OLD"},
                "orchestrator": {"plan": {}},
            }
        ),
    )
    await store.set_memory_preference("alice", False)
    await manager.recover()
    await manager.running[thread["id"]]
    assert not StubOrchestrator.seen
    assert (await store.turn_for_run(row["id"]))["outcome"] == "failed"


async def test_duplicate_request_does_not_create_second_turn(host):
    store, manager = host
    thread = await store.create_thread("alice", "goal", {})
    first = await complete(manager, thread, request_id="request-0001")
    again = await manager.command(thread, "start", request_id="request-0001")
    assert first == again and len(StubOrchestrator.seen) == 1


async def test_global_off_invalidates_persisted_memory_context(host):
    store, manager = host
    thread = await store.create_thread("alice", "goal", {})
    row, _ = await store.enqueue_run(thread, "start", "", None, "one")
    await store.update_run(
        row["id"],
        context=json.dumps(
            {
                "version": 1,
                "memory_revision": 0,
                "memory_enabled": True,
                "background": {"memory_context": [{"id": "old", "version": 1}]},
            }
        ),
    )
    await manager.recover()
    await manager.running[thread["id"]]
    assert not StubOrchestrator.seen
    assert (await store.turn_for_run(row["id"]))["outcome"] == "failed"


async def test_dispatch_checks_automatic_fact_versions_without_preference_bump(host):
    from agentgpt.a2a.auth import RunScope

    store, manager = host
    manager.settings.memory_enabled = True
    memory_id = await store.edit_memory("alice", "old fact")
    pref = await store.memory_preference("alice")
    background = {
        "manifest": {"memory_revision": pref["revision"]},
        "memory_context": [{"id": memory_id, "version": 1}],
    }
    async with store.connect() as db:
        await db.execute(
            "UPDATE memories SET version=2,content='new fact' WHERE id=?", (memory_id,)
        )
    with pytest.raises(ValueError, match="版本或权限"):
        await manager._validate_memory_snapshot(RunScope("alice", "t", "r"), "", background)
