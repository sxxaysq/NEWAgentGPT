"""Interoperability and ownership tests using the unmodified official SDK client."""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.helpers import new_data_message
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
)
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import ParseDict

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import RunScope, TokenError, issue_token, verify_token
from agentgpt.a2a.client import A2AClient, Registry, task_payload
from agentgpt.a2a.tasks import TaskStore
from agentgpt.a2a.transport import AgentRuntime, InputRequired
from agentgpt.agents.base import build_card
from agentgpt.config import Settings

SECRET = "test-only-secret"
ALICE = RunScope("alice", "same-thread", "run-1")
OTHER_RUN = RunScope("alice", "same-thread", "run-2")
OTHER_USER = RunScope("bob", "same-thread", "run-1")


@asynccontextmanager
async def runtime(tmp_path, handler=None):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    await store.initialize()

    async def echo(ctx):
        if ctx.data.get("ask"):
            raise InputRequired("please provide more information")
        if ctx.data.get("slow"):
            await asyncio.sleep(30)
        await ctx.progress("working")
        await ctx.emit(t.Artifact(name="result", parts=[t.DataPart(data=ctx.data)]))

    app_runtime = AgentRuntime(build_card("worker", Settings()), handler or echo, store, SECRET)
    app = app_runtime.build()
    try:
        yield app, app_runtime, store
    finally:
        await app_runtime.sdk_handler.aclose()


@asynccontextmanager
async def peer(app, scope=ALICE):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://agent",
        headers={"Authorization": "Bearer " + issue_token(SECRET, scope), "A2A-Version": "1.0"},
    ) as http:
        card = ParseDict((await http.get("/.well-known/agent-card.json")).json(), t.AgentCard())
        yield JsonRpcTransport(http, card, "http://agent/", is_shared_httpx=True)


def request(data, scope=ALICE, task_id=None):
    msg = new_data_message(data, role=Role.ROLE_USER, context_id=scope.run_id)
    if task_id:
        msg.task_id = task_id
    return SendMessageRequest(message=msg)


async def test_official_sdk_card_and_synchronous_task(tmp_path):
    async with runtime(tmp_path) as (app, _, store), peer(app) as client:
        response = await client.send_message(request({"x": 42}))
        assert response.task.context_id == ALICE.run_id
        assert response.task.status.state == t.COMPLETED
        assert task_payload(response.task) == {"x": 42}


async def test_official_sdk_streaming_events(tmp_path):
    async with runtime(tmp_path) as (app, _, store), peer(app) as client:
        events = [event async for event in client.send_message_streaming(request({"x": 42}))]
        states = [e.status_update.status.state for e in events if e.HasField("status_update")]
        assert t.WORKING in states
        assert states[-1] == t.COMPLETED
        assert any(e.HasField("artifact_update") for e in events)


@pytest.mark.parametrize("scope", [OTHER_RUN, OTHER_USER])
async def test_get_cancel_list_subscribe_and_continue_cannot_cross_scope(tmp_path, scope):
    async with runtime(tmp_path) as (app, _, store), peer(app) as owner:
        result = await owner.send_message(request({"x": 42}))
        task_id = result.task.id
        async with peer(app, scope) as attacker:
            with pytest.raises(TaskNotFoundError):
                await attacker.get_task(GetTaskRequest(id=task_id))
            with pytest.raises(TaskNotFoundError):
                await attacker.cancel_task(CancelTaskRequest(id=task_id))
            assert not (await attacker.list_tasks(ListTasksRequest())).tasks
            with pytest.raises(TaskNotFoundError):
                async for _ in attacker.subscribe(SubscribeToTaskRequest(id=task_id)):
                    pass
            with pytest.raises(TaskNotFoundError):
                await attacker.send_message(request({"x": 99}, scope, task_id))


async def test_forged_context_rejected(tmp_path):
    async with runtime(tmp_path) as (app, _, store), peer(app) as client:
        with pytest.raises(InvalidParamsError):
            await client.send_message(request({}, OTHER_RUN))


async def test_input_required_can_resume_same_task(tmp_path):
    async with runtime(tmp_path) as (app, _, store), peer(app) as client:
        result = await client.send_message(request({"ask": True}))
        assert result.task.status.state == t.INPUT_REQUIRED
        resumed = await client.send_message(request({"answer": "yes"}, task_id=result.task.id))
        assert resumed.task.id == result.task.id
        assert resumed.task.status.state == t.COMPLETED


async def test_cancel_running_task(tmp_path):
    async with runtime(tmp_path) as (app, _, store), peer(app) as client:
        sending = asyncio.create_task(client.send_message(request({"slow": True})))
        task = None
        for _ in range(100):
            page = await client.list_tasks(ListTasksRequest())
            if page.tasks:
                task = page.tasks[0]
                break
            await asyncio.sleep(0.01)
        assert task
        canceled = await client.cancel_task(CancelTaskRequest(id=task.id))
        assert canceled.status.state == t.CANCELED
        await sending


async def test_completed_replay_is_idempotent(tmp_path):
    calls = []

    async def handle(ctx):
        calls.append(1)
        await ctx.emit(t.Artifact(parts=[t.DataPart(data={"ok": True})]))

    async with runtime(tmp_path, handle) as (app, _, store):
        client = A2AClient(
            Registry.from_urls({"worker": "http://agent"}),
            SECRET,
            http_transport=httpx.ASGITransport(app=app),
        )
        one = await client.send("worker", ALICE, {"task": "same"})
        two = await client.send("worker", ALICE, {"task": "same"})
        assert one.id == two.id
        assert calls == [1]
        three = await client.send("worker", OTHER_RUN, {"task": "same"})
        assert three.id != one.id
        assert calls == [1, 1]


@pytest.mark.parametrize("token", ["", "forged"])
async def test_missing_and_invalid_token(tmp_path, token):
    async with runtime(tmp_path) as (app, _, store):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as http:
            response = await http.post("/", json={}, headers={"Authorization": "Bearer " + token})
            assert response.status_code == 401


def test_token_round_trip_and_expiry():
    assert verify_token(SECRET, issue_token(SECRET, ALICE)) == ALICE
    with pytest.raises(TokenError):
        verify_token(SECRET, issue_token(SECRET, ALICE, ttl=-1))
