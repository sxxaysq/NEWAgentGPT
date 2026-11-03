"""Malformed structured output is corrected a bounded number of times.

These tests cover the model client only: one initial request plus at most three
correction requests, and no retry at all for transport errors or cancellation.
"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from openai import LengthFinishReasonError

from agentgpt.a2a.types import PlanArtifact
from agentgpt.model import StructuredOutputError, structured_response


class Responses:
    def __init__(self, outputs):
        self.outputs, self.requests = outputs, []

    def with_structured_output(self, schema, **kwargs):
        owner = self

        class Structured:
            async def ainvoke(self, messages):
                position = len(owner.requests)
                owner.requests.append(list(messages))
                result = owner.outputs[min(position, len(owner.outputs) - 1)]
                if isinstance(result, BaseException):
                    raise result
                return copy.deepcopy(result)

        return Structured()


VALID = {
    "tasks": [{"id": "t1", "title": "回答编辑器名称", "goal": "查出编辑器名称"}],
    "rationale": "",
}
MALFORMED = {"tasks": [{"id": "t1", "title": "回答编辑器名称", "goal": "查出编辑器名称"}, "extra"]}


@pytest.mark.asyncio
async def test_parser_schema_and_extra_fields_corrected_on_third_retry():
    model = Responses(
        [
            OutputParserException("Invalid JSON", llm_output="{bad"),
            MALFORMED,
            {**VALID, "unexpected": "不允许额外字段"},
            VALID,
        ]
    )
    original = [SystemMessage(content="你是Planner"), HumanMessage(content="只回答编辑器名称")]
    result = await structured_response(model, PlanArtifact, original)
    assert isinstance(result, PlanArtifact) and len(result.tasks) == 1
    assert len(model.requests) == 4 and len(original) == 2
    for i, request in enumerate(model.requests):
        assert request[0].type == "system"
        assert sum(m.type == "system" for m in request) == 1
        assert request[-1] == original[-1]
        assert '"additionalProperties": false' in request[-2].content
        if i:
            assert "上一次输出未通过Pydantic校验" in request[-2].content


@pytest.mark.asyncio
async def test_exhaustion_is_exactly_four_calls_and_not_a_value_error():
    model = Responses([MALFORMED])
    with pytest.raises(StructuredOutputError, match="已重试3次（共4次调用）"):
        await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])
    assert len(model.requests) == 4
    assert not issubclass(StructuredOutputError, ValueError)


@pytest.mark.asyncio
async def test_valid_json_string_is_validated_without_retry():
    model = Responses([json.dumps(VALID)])
    assert (await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])).tasks
    assert len(model.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("service unavailable"), asyncio.CancelledError()])
async def test_unrelated_errors_and_cancellation_are_not_format_retries(error):
    model = Responses([error])
    with pytest.raises(type(error)):
        await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_unknown_task_field_is_rejected_before_any_side_effect():
    model = Responses([{**VALID, "tasks": [{**VALID["tasks"][0], "required_tools": ["x"]}]}])
    with pytest.raises(StructuredOutputError):
        await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])


def test_memory_schemas_reject_unknown_fields():
    from pydantic import ValidationError

    from agentgpt.memory import Facts

    with pytest.raises(ValidationError):
        Facts.model_validate({"facts": [], "untrusted_user_id": "someone-else"})


def length_error(content):
    return LengthFinishReasonError(
        completion=SimpleNamespace(
            usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
    )


@pytest.mark.asyncio
async def test_truncated_json_is_retried():
    model = Responses([length_error('{"tasks":['), VALID])
    assert (await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])).tasks
    assert len(model.requests) == 2
    assert "输出达到长度上限" in model.requests[1][0].content


@pytest.mark.asyncio
async def test_valid_complete_json_at_token_limit_is_still_validated():
    model = Responses([length_error(json.dumps(VALID) + " " * 100)])
    assert (await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])).tasks
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_truncation_exhaustion_is_bounded():
    model = Responses([length_error('{"tasks":[')])
    with pytest.raises(StructuredOutputError):
        await structured_response(model, PlanArtifact, [HumanMessage(content="目标")])
    assert len(model.requests) == 4
