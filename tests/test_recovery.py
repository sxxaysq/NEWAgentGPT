"""Real process crashes, scoped evidence and safe tool replay regressions."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import RunScope
from agentgpt.a2a.client import A2AClient, RecoveryPending, Registry, task_payload
from agentgpt.store import Store

ROOT = Path(__file__).resolve().parents[1]


def spawn(mode, directory, *args):
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests/recovery_process.py"),
            mode,
            str(directory),
            *map(str, args),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


async def wait_for(predicate, process, timeout=15):
    async with asyncio.timeout(timeout):
        while not predicate():
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode())
            await asyncio.sleep(0.03)


def kill(process):
    if process and process.poll() is None:
        process.kill()
    if process:
        process.wait(timeout=8)
        process.stderr.close()


async def test_host_sigkill_resumes_same_run_without_replaying_completed_task(tmp_path):
    store = Store(tmp_path / "host.sqlite3")
    await store.initialize()
    thread = await store.create_thread("alice", "two steps", {})
    run, _ = await store.enqueue_run(thread, "start", "", None, "request")
    first = spawn("host", tmp_path)
    second = None
    try:
        await wait_for(lambda: (tmp_path / "waiting").exists(), first)
        saved = await store.run(run["id"])
        checkpoint = json.loads(saved["context"])["orchestrator"]
        assert checkpoint["tasks"][0]["status"] == "completed"
        kill(first)
        second = spawn("host", tmp_path)
        (tmp_path / "release").touch()
        await wait_for(lambda: second.poll() is not None, second)
        assert second.returncode == 0, second.stderr.read().decode()
        row = await store.run(run["id"])
        assert row["status"] == "done"
        assert row["attempts"] == 2
        calls = (tmp_path / "calls.log").read_text().splitlines()
        assert calls.count("planner") == 1
        assert calls.count("t1") == 1
        assert (await store.event_view(thread["id"]))["state"]["final_answer"] == "recovered answer"
    finally:
        kill(first)
        kill(second)


async def test_stop_intent_survives_host_sigkill(tmp_path):
    store = Store(tmp_path / "host.sqlite3")
    await store.initialize()
    thread = await store.create_thread("alice", "two steps", {})
    run, _ = await store.enqueue_run(thread, "start", "", None, "request")
    first = spawn("host", tmp_path)
    second = None
    try:
        await wait_for(lambda: (tmp_path / "waiting").exists(), first)
        await store.update_run(run["id"], control="stop")
        kill(first)
        second = spawn("host", tmp_path)
        await wait_for(lambda: second.poll() is not None, second)
        assert second.returncode == 0, second.stderr.read().decode()
        assert (tmp_path / "calls.log").read_text().splitlines().count("t2") == 1
        assert (await store.event_view(thread["id"]))["state"]["status"] == "stopped"
    finally:
        kill(first)
        kill(second)


@pytest.mark.parametrize(
    "kind, expected_calls", [("committed", 1), ("uncertain-read", 2), ("uncertain-write", 1)]
)
async def test_worker_sigkill_preserves_receipts_and_never_repeats_uncertain_write(
    tmp_path, kind, expected_calls
):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    client = A2AClient(Registry.from_urls({"worker": url}), "fixture-secret", timeout=10)
    scope = RunScope("alice", "thread", "run")
    first = spawn("agent", tmp_path, port)
    second = None
    pending = None

    async def healthy(process):
        async with asyncio.timeout(15):
            while True:
                assert process.poll() is None, process.stderr.read().decode()
                try:
                    async with httpx.AsyncClient() as http:
                        if (await http.get(url + "/health")).status_code == 200:
                            return
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.05)

    try:
        await healthy(first)
        pending = asyncio.create_task(client.send("worker", scope, {"kind": kind}))
        await wait_for(lambda: (tmp_path / "waiting").exists(), first)
        kill(first)
        with pytest.raises(RecoveryPending):
            await pending
        second = spawn("agent", tmp_path, port)
        await healthy(second)
        (tmp_path / "release").touch()
        result = await client.send("worker", scope, {"kind": kind})
        if kind == "uncertain-write":
            assert result.status.state == t.INPUT_REQUIRED
            again = await client.send("worker", scope, {"kind": kind})
            assert again.status.state == t.INPUT_REQUIRED
        else:
            assert result.status.state == t.COMPLETED
            assert task_payload(result)["result"] == "recorded result"
        assert len((tmp_path / "calls.log").read_text().splitlines()) == expected_calls
    finally:
        if pending and not pending.done():
            pending.cancel()
        kill(first)
        kill(second)


async def test_evidence_query_does_not_admit_another_run_same_thread_and_task(tmp_path):
    store = Store(tmp_path / "host.sqlite3")
    await store.initialize()

    # Bypass ingestion here: this regression targets the run predicate, not retrieval.
    async def evidence(user_id, eid):
        return {"text": eid}

    store.evidence = evidence
    async with store.connect() as db:
        for run in ["run-1", "run-2"]:
            await db.execute(
                "INSERT INTO task_evidence VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    "alice",
                    "thread",
                    run,
                    "t1",
                    1,
                    "call",
                    run,
                    json.dumps({"evidence_id": run}),
                ),
            )
    result = await store.task_sources("alice", "thread", "t1", 1, run_id="run-2")
    assert [r["evidence_id"] for r in result] == ["run-2"]


async def test_host_connection_loss_reattaches_without_duplicate_worker_execution(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    client = A2AClient(Registry.from_urls({"worker": url}), "fixture-secret", timeout=10)
    scope = RunScope("alice", "thread", "run")
    process = spawn("agent", tmp_path, port)
    pending = None
    try:
        async with asyncio.timeout(15):
            while not await client.refresh("worker"):
                assert process.poll() is None
                await asyncio.sleep(0.05)
        pending = asyncio.create_task(client.send_streaming("worker", scope, {"kind": "committed"}))
        await wait_for(lambda: (tmp_path / "waiting").exists(), process)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        reattached = asyncio.create_task(
            client.send_streaming("worker", scope, {"kind": "committed"})
        )
        await asyncio.sleep(0.1)
        (tmp_path / "release").touch()
        result = await reattached
        assert result.status.state == t.COMPLETED
        assert len((tmp_path / "calls.log").read_text().splitlines()) == 1
    finally:
        if pending and not pending.done():
            pending.cancel()
        kill(process)
