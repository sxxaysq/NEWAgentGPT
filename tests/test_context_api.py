"""User-authenticated access to context jobs and saved answer artifacts."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agentgpt.app import app
from agentgpt.store import Store


@pytest.fixture
async def api(tmp_path):
    store = Store(tmp_path / "api.sqlite3")
    await store.initialize()
    await store.register("alice@test.local", "password-123")
    token = await store.login("alice@test.local", "password-123")
    user = await store.user_for_token(token)
    app.state.store = store
    app.state.memory = SimpleNamespace(wake=asyncio.Event())
    app.state.runs = SimpleNamespace(summaries=SimpleNamespace(wake=asyncio.Event()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"agentgpt_session": token},
    ) as client:
        yield store, client, user


async def test_project_memory_requires_scope(api):
    _, client, _ = api
    response = await client.post("/api/memories", json={"content": "budget", "kind": "project"})
    assert response.status_code == 409


async def test_artifact_ownership(api):
    store, client, user = api
    own = await store.create_thread(user["id"], "own goal", {})
    other = await store.create_thread("bob", "secret goal", {})
    ids = []
    for thread in [own, other]:
        row, _ = await store.enqueue_run(thread, "start", "", None)
        await store.finish_turn(row["id"], outcome="completed", assistant_text="secret table")
        snapshot = await store.context_snapshot(thread["user_id"], thread["id"], 1)
        ids.append(snapshot["artifacts"][0]["id"])
    assert (await client.get(f"/api/threads/{own['id']}/artifacts/{ids[0]}")).status_code == 200
    assert (await client.get(f"/api/threads/{own['id']}/artifacts/{ids[1]}")).status_code == 404
    assert (await client.get(f"/api/threads/{other['id']}/artifacts/{ids[1]}")).status_code == 404


async def test_projection_retry_is_owner_scoped(api):
    store, client, user = api
    for owner in [user["id"], "bob"]:
        thread = await store.create_thread(owner, "goal", {})
        await store.enqueue_run(thread, "start", "", None)
    async with store.connect() as db:
        await db.execute("UPDATE context_jobs SET status='failed'")
    response = await client.post("/api/context-jobs/retry")
    assert response.json()["retried"] == 1
    assert await store.context_job_status("bob") == [
        {"job_kind": "state", "status": "failed", "n": 1}
    ]
