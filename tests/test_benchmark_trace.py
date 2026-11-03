"""A continuation acknowledgement is not a new terminal UI projection."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx


async def test_benchmark_waits_past_stale_paused_projection(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_live_100.py"
    spec = importlib.util.spec_from_file_location("benchmark_live", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    states = iter(["running", "paused", "paused", "completed"])
    commands = []

    def respond(request):
        import json

        if request.url.path == "/api/threads":
            return httpx.Response(200, json={"thread": {"id": "thread"}})
        if request.method == "POST":
            commands.append(json.loads(request.content)["operation"])
            return httpx.Response(200, json={"run_id": "run"})
        return httpx.Response(
            200,
            json={"state": {"run_id": "run", "status": next(states), "final_answer": "finished"}},
        )

    ticks = iter(range(100, 200, 2))
    monkeypatch.setattr(module.time, "time", lambda: next(ticks))

    async def no_wait(*args):
        pass

    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(module, "read_trace", lambda *args: {"context": {}, "actions": []})
    args = SimpleNamespace(root=tmp_path, base="http://test")
    case = module.Case(args, "recovery", 0)
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(respond)
    ) as client:
        _, result = await case.ask(client, "owner", "calculate", pause=True)
    assert result["status"] == "completed"
    assert commands == ["start", "pause", "continue"]
    assert all(check["passed"] for check in case.checks)
