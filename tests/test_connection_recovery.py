"""Bounded reconnection preserves real SQLite checkpoints and dispatch identity."""

import json

import pytest

from agentgpt.a2a import types as t
from agentgpt.a2a.client import RecoveryPending
from agentgpt.config import Settings
from agentgpt.host.runner import RunManager
from agentgpt.store import Store


def artifact(data):
    return t.Task(
        id="fixture",
        status=t.TaskStatus(state=t.COMPLETED),
        artifacts=[t.Artifact(parts=[t.DataPart(data=data)])],
    )


class UnavailableClient:
    def __init__(self, store, failures):
        self.store, self.failures = store, failures
        self.calls = []
        self.deliveries = []

    async def send(self, role, scope, data, **kwargs):
        self.calls.append(role)
        if role == "planner":
            return artifact(
                {
                    "tasks": [
                        {"id": "t1", "title": "first", "goal": "first"},
                        {"id": "t2", "title": "second", "goal": "second", "depends_on": ["t1"]},
                    ]
                }
            )
        if role == "critic":
            return artifact({"verdict": "pass"})
        return artifact({"answer_md": "recovered answer"})

    async def send_streaming(self, role, scope, data, **kwargs):
        task_id = data["task"]["id"]
        self.calls.append(task_id)
        self.deliveries.append((scope.run_id, task_id, json.dumps(data, sort_keys=True)))
        if task_id == "t2" and self.failures:
            self.failures -= 1
            row = await self.store.run(scope.run_id)
            checkpoint = json.loads(row["context"])
            # Simulate an outage that outlasted the original wall-clock deadline.
            checkpoint["orchestrator"]["deadline_at"] = 1
            await self.store.update_run(scope.run_id, context=json.dumps(checkpoint))
            raise RecoveryPending(role, "test connection unavailable")
        return artifact({"task_id": task_id, "summary": task_id + " result"})


@pytest.mark.parametrize("failures", [1, 2])
async def test_short_retry_or_pause_then_continue_preserves_completed_steps(tmp_path, failures):
    store = Store(tmp_path / "host.sqlite3")
    await store.initialize()
    client = UnavailableClient(store, failures)
    settings = Settings(
        _env_file=None,
        memory_enabled=False,
        judge_plan=False,
        recovery_max_retries=1,
        recovery_retry_delay_s=0,
    )
    manager = RunManager(store, client, settings)
    thread = await store.create_thread("alice", "two steps", {})
    try:
        run_id = await manager.command(thread, "start", request_id="request")
        await manager.running[thread["id"]]
        if failures == 2:
            turn = await store.turn_for_run(run_id)
            assert turn["phase"] == "paused"
            saved = json.loads((await store.run(run_id))["context"])
            assert saved["recovery"]["attempts"] == 2
            assert [x["status"] for x in saved["orchestrator"]["tasks"]] == ["completed", "running"]
            view = await store.event_view(thread["id"])
            assert view["state"]["tasks"][0]["status"] == "completed"
            resumed = await manager.command(thread, "continue", request_id="continue-request")
            assert resumed == run_id
            await manager.running[thread["id"]]
            assert "recovery" not in json.loads((await store.run(run_id))["context"])
        turn = await store.turn_for_run(run_id)
        assert turn["outcome"] == "completed"
        assert turn["assistant_text"] == "recovered answer"
        assert client.calls.count("planner") == client.calls.count("t1") == 1
        deliveries = [item for item in client.deliveries if item[1] == "t2"]
        assert len(deliveries) == failures + 1
        assert len(set(deliveries)) == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("used_attempts,expected_worker_calls", [(1, 1), (2, 0)])
async def test_restart_preserves_automatic_recovery_budget(
    tmp_path, used_attempts, expected_worker_calls
):
    store = Store(tmp_path / "host.sqlite3")
    await store.initialize()
    thread = await store.create_thread("alice", "two steps", {})
    row, _ = await store.enqueue_run(thread, "start", "", None, "request")
    saved = json.loads(row["context"])
    saved["recovery"] = {"attempts": used_attempts}
    await store.update_run(row["id"], context=json.dumps(saved))
    client = UnavailableClient(store, failures=1)
    manager = RunManager(
        store,
        client,
        Settings(
            _env_file=None,
            memory_enabled=False,
            judge_plan=False,
            recovery_max_retries=1,
            recovery_retry_delay_s=0,
        ),
    )
    try:
        await manager.recover()
        if thread["id"] in manager.running:
            await manager.running[thread["id"]]
        assert (await store.turn_for_run(row["id"]))["phase"] == "paused"
        assert client.calls.count("t2") == expected_worker_calls
    finally:
        await manager.close()
