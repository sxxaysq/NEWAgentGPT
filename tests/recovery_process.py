"""Subprocess fixture: deterministic agents and host, with no external model calls."""

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import ToolMessage

from agentgpt.a2a import types as t
from agentgpt.a2a.tasks import TaskStore
from agentgpt.a2a.transport import AgentRuntime
from agentgpt.agents.base import build_card
from agentgpt.agents.tool_journal import journaled_tool_call
from agentgpt.config import Settings
from agentgpt.host.runner import RunManager
from agentgpt.store import Store

mode, directory = sys.argv[1:3]
root = Path(directory)


def record(name):
    with (root / "calls.log").open("a") as f:
        f.write(name + "\n")


async def wait_release():
    while not (root / "release").exists():
        await asyncio.sleep(0.03)


def artifact(data):
    return t.Task(
        id="fixture",
        status=t.TaskStatus(state=t.COMPLETED),
        artifacts=[t.Artifact(parts=[t.DataPart(data=data)])],
    )


class Client:
    async def send(self, role, scope, data, **kwargs):
        record(role)
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
        tid = data["task"]["id"]
        record(tid)
        if tid == "t2":
            (root / "waiting").touch()
            await wait_release()
        return artifact({"task_id": tid, "summary": tid + " result"})


async def host():
    store = Store(root / "host.sqlite3")
    await store.initialize()
    settings = Settings().model_copy(update={"judge_plan": False})
    manager = RunManager(store, Client(), settings)
    await manager.recover()
    await asyncio.gather(*manager.running.values())


async def agent():
    import uvicorn

    port = int(sys.argv[3])
    store = TaskStore(root / "agent.sqlite3")
    await store.initialize()

    async def handler(ctx):
        kind = ctx.data.get("kind", "committed")
        tool = "builtin__private_search" if kind != "uncertain-write" else "write_external"
        call = {"id": "call-1", "name": tool, "args": {}}

        async def invoke():
            record(tool)
            if kind != "committed":
                (root / "waiting").touch()
                await wait_release()
            return ToolMessage(content="recorded result", tool_call_id="call-1", name=tool)

        result = await journaled_tool_call(ctx, call, invoke)
        (root / "waiting").touch()
        await wait_release()
        await ctx.emit(t.Artifact(parts=[t.DataPart(data={"result": result.content})]))

    settings = Settings().model_copy(update={"worker_url": f"http://127.0.0.1:{port}"})
    runtime = AgentRuntime(build_card("worker", settings), handler, store, "fixture-secret")
    await uvicorn.Server(
        uvicorn.Config(runtime.build(), host="127.0.0.1", port=port, log_level="warning")
    ).serve()


asyncio.run(host() if mode == "host" else agent())
