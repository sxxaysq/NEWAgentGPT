"""Snapshot isolation, intact corrections and request guards at the model boundary."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agentgpt.a2a.auth import RunScope
from agentgpt.config import Settings
from agentgpt.host.context import ContextAssembler, ContextBudgetError, TokenBudget
from agentgpt.model import BudgetedModel


class SnapshotStore:
    async def context_snapshot(self, user_id, thread_id, before_input_seq):
        assert (user_id, thread_id, before_input_seq) == ("alice", "thread", 3)
        return {
            "session": {
                "version": 1,
                "covered_until_input_seq": 0,
                "constraints": [{"value": "23万", "quote": "预算23万"}],
            },
            "inputs": [
                {"id": "i1", "input_seq": 1, "turn_id": "t1", "text": "预算改成18万，其他不变"},
                {"id": "i2", "input_seq": 2, "turn_id": "t2", "text": "仅内网部署"},
                {"id": "i3", "input_seq": 3, "text": "current"},
                {"id": "i4", "input_seq": 4, "text": "future secret"},
            ],
            "turns": [
                {"id": "t2", "assistant_text": "|编号|内容|\n" + "|42|保留此完整行|\n" * 300}
            ],
            "summary": {"content": "旧预算23万", "summary_version": 1},
        }


@pytest.mark.asyncio
async def test_pending_corrections_and_entire_table_survive_soft_budget_without_memory():
    assembler = ContextAssembler(SnapshotStore(), Settings(context_history_tokens=256))
    result = await assembler.assemble("alice", "thread", "改刚才的表", before_input_seq=3)
    assert [item["text"] for item in result["pending_user_updates"]] == [
        "预算改成18万，其他不变",
        "仅内网部署",
    ]
    assert result["previous_answer"]["text"].endswith("|42|保留此完整行|\n")
    assert result["previous_answer"]["text"].count("|42|") == 300
    assert result["manifest"]["history_soft_limit_exceeded"]
    assert "future secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_future_state_is_not_used_at_older_snapshot():
    class FutureStore(SnapshotStore):
        async def context_snapshot(self, *args):
            value = await super().context_snapshot(*args)
            value["session"]["covered_until_input_seq"] = 4
            value["session"]["constraints"] = [{"value": "future secret"}]
            return value

    result = await ContextAssembler(FutureStore(), Settings()).assemble(
        "alice", "thread", "继续", before_input_seq=3
    )
    assert result["session"] == {}
    assert "future secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_memory_source_ids_follow_selected_context_and_pin_version():
    memory = [{"id": "m1", "version": 2, "content": "代号XZ-73", "source_quote": "我的代号XZ-73"}]
    result = await ContextAssembler(
        SnapshotStore(), Settings(context_history_tokens=50000)
    ).assemble("alice", "thread", "代号", before_input_seq=3, memory_context=memory)
    from agentgpt.host.context import authorized_context_sources

    allowed = authorized_context_sources(result, "alice", "thread")
    assert allowed["memory:m1:v2"]["quote"] == "我的代号XZ-73"
    assert "memory:m1:v1" not in allowed
    assert authorized_context_sources(result, "bob", "thread") == {}
    assert authorized_context_sources(result, "alice", "another_thread") == {}
    assert "context_id" not in memory[0]  # never mutate memory-service cached results
    trimmed = await ContextAssembler(
        SnapshotStore(), Settings(context_history_tokens=256)
    ).assemble("alice", "thread", "代号", before_input_seq=3, memory_context=memory)
    assert "memory:m1:v2" not in authorized_context_sources(trimmed, "alice", "thread")


class CountingModel:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return "ok"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        return self


@pytest.mark.asyncio
async def test_oversized_request_never_reaches_model_and_reserves_output():
    raw = CountingModel()
    budget = TokenBudget(Settings(model_context_tokens=1000, model_token_safety_margin=100), 200)
    model = BudgetedModel(raw, budget)
    assert await model.ainvoke([HumanMessage(content="small")]) == "ok"
    with pytest.raises(ContextBudgetError, match="原始输入已保留"):
        await model.ainvoke([HumanMessage(content="x" * 700)])
    assert raw.calls == 1


@pytest.mark.asyncio
async def test_tool_schemas_and_structured_bindings_keep_guard():
    raw = CountingModel()
    budget = TokenBudget(Settings(model_context_tokens=1200, model_token_safety_margin=100), 200)
    model = BudgetedModel(raw, budget)
    bound = model.bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "huge",
                    "description": "x" * 2000,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )
    with pytest.raises(ContextBudgetError):
        await bound.ainvoke([HumanMessage(content="small")])
    with pytest.raises(ContextBudgetError):
        await model.with_structured_output(dict).ainvoke([HumanMessage(content="x" * 2000)])
    assert raw.calls == 0


def test_unknown_tokenizer_is_never_labelled_exact():
    budget = TokenBudget(Settings())
    budget.check([HumanMessage(content="中文、编号 ZQ-8E4C91、否定条件")])
    assert budget.mode == "estimated_utf8_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_service_tokenizer_checks_actual_schema_and_fails_closed(monkeypatch, failure):
    import httpx

    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer private-test-key"
        return httpx.Response(503 if failure else 200, json={"count": 25, "max_model_len": 1000})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    budget = TokenBudget(
        Settings(
            model_context_tokens=1000,
            model_token_safety_margin=100,
            model_tokenizer_url="http://model/tokenize",
            model_api_key="private-test-key",
        ),
        200,
    )
    raw = CountingModel()
    model = BudgetedModel(raw, budget, [{"type": "function", "function": {"name": "search"}}])
    if failure:
        with pytest.raises(ContextBudgetError, match="计数服务暂时不可用"):
            await model.ainvoke([HumanMessage(content="hello")])
        assert raw.calls == 0
    else:
        assert await model.ainvoke([HumanMessage(content="hello")]) == "ok"
        assert budget.mode == "server_tokenize"
    assert requests[0]["tools"][0]["function"]["name"] == "search"
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["planner", "worker", "critic", "synthesizer"])
async def test_handlers_deliver_context_to_actual_model_messages(role, monkeypatch):
    from importlib import import_module

    module = import_module("agentgpt.agents." + role)
    captured = []

    async def structured(model, schema, messages):
        captured.append(json.loads(messages[-1].content))
        values = {
            "planner": {"tasks": [{"id": "t1", "title": "翻译表格", "goal": "翻译表格"}]},
            "worker": {"task_id": "t1", "summary": "完成"},
            "critic": {"verdict": "pass"},
            "synthesizer": {"answer_md": "complete"},
        }
        return schema.model_validate(values[role])

    async def noop(*args):
        return None

    monkeypatch.setattr(module, "structured_response", structured)
    monkeypatch.setattr(module, "agent_model", lambda *a, **k: CountingModel())
    context = {
        "current_request": "用英文表格保留预算、部署限制和比较对象A/B",
        "session": {"constraints": [{"property": "budget", "value": "23万"}]},
        "pending_user_updates": [{"text": "改18万，其他不变"}],
        "previous_answer": {"text": "|tail|完整表格末行|"},
        "memory_context": "仅供参考",
    }
    data = {
        **context,
        "goal": "翻译表格",
        "task": {"id": "t1", "title": "翻译", "goal": "翻译"},
        "findings": [{"summary": "完成"}],
    }
    ctx = SimpleNamespace(
        data=data,
        text="",
        progress=noop,
        emit=noop,
        load=noop,
        save=noop,
        scope=RunScope("alice", "thread", "run"),
    )
    if role == "worker":

        class Store:
            async def documents(self, user_id):
                return []

        handler = module.make_handler(
            Settings(), SimpleNamespace(tools_for=lambda *args: []), Store()
        )
    else:
        handler = module.make_handler(Settings())
    await handler(ctx)
    assert captured
    for payload in captured:
        for key, value in context.items():
            assert payload[key] == value
