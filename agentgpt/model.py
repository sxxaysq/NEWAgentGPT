"""Shared model factory and bounded validation retries for structured responses."""

import json
import logging
from typing import TypeVar

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from openai import LengthFinishReasonError
from pydantic import BaseModel, ConfigDict, ValidationError

from agentgpt.config import ModelOptions, Settings
from agentgpt.host.context import TokenBudget

log = logging.getLogger(__name__)
STRUCTURED_RETRIES = 3
T = TypeVar("T", bound=BaseModel)


class StructuredModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class StructuredOutputError(RuntimeError):
    """Retry exhaustion must not enter a second planner or background-job retry loop."""


async def structured_response(model, schema: type[T], messages) -> T:
    """Initial request plus at most three correction requests; never retry side effects."""
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    correction = ""
    for attempt in range(STRUCTURED_RETRIES + 1):
        instruction = SystemMessage(
            content=(
                "返回一个符合以下JSON Schema的完整JSON对象，不输出Markdown或解释。"
                "用户要求的答复风格仅适用于最终答复，不改变本次结构化接口。\n"
                + schema_text
                + correction
            )
        )
        # Qwen's serving template requires exactly one initial system message.
        system = "\n".join(str(m.content) for m in messages if m.type == "system")
        request = [
            SystemMessage(content=system + "\n" + instruction.content),
            *(m for m in messages if m.type != "system"),
        ]
        try:
            result = await model.with_structured_output(schema, method="json_mode").ainvoke(request)
            if isinstance(result, BaseModel):
                result = result.model_dump()
            if isinstance(result, str):
                return schema.model_validate_json(result)
            return schema.model_validate(result)
        except (ValidationError, OutputParserException, LengthFinishReasonError) as exc:
            if isinstance(exc, LengthFinishReasonError):
                # JSON-mode providers sometimes pad a complete object until the token limit.
                choices = exc.completion.choices
                raw = choices[0].message.content if choices else ""
                try:
                    return schema.model_validate_json(raw or "")
                except ValidationError:
                    details = (
                        "输出达到长度上限且JSON未通过校验。请缩短文本，保留必填字段，"
                        "不要重复字段或展开推理，重新输出完整JSON。"
                    )
            elif isinstance(exc, ValidationError):
                details = json.dumps(
                    exc.errors(include_url=False, include_input=False),
                    ensure_ascii=False,
                    default=str,
                )[:1600]
            else:
                details = str(exc)[:1600]
            if attempt == STRUCTURED_RETRIES:
                raise StructuredOutputError(
                    f"{schema.__name__} 输出校验失败，已重试{STRUCTURED_RETRIES}次"
                    f"（共{STRUCTURED_RETRIES + 1}次调用），本次执行停止。{details}"
                ) from exc
            correction = (
                "\n上一次输出未通过Pydantic校验，请修正以下错误后重新输出完整JSON：\n" + details
            )
            log.warning(
                "%s validation failed; correction retry %s/%s: %s",
                schema.__name__,
                attempt + 1,
                STRUCTURED_RETRIES,
                details[:600],
            )


class BudgetedModel:
    """Keep preflight checks around every binding, including retries and tool loops."""

    def __init__(self, model, budget, tools=None):
        self.model, self.budget, self.tools = model, budget, tools or []

    async def ainvoke(self, messages, *args, **kwargs):
        await self.budget.check_async(messages, self.tools)
        return await self.model.ainvoke(messages, *args, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return BudgetedModel(
            self.model.with_structured_output(schema, **kwargs), self.budget, self.tools
        )

    def bind_tools(self, tools, **kwargs):
        from langchain_core.utils.function_calling import convert_to_openai_tool

        return BudgetedModel(
            self.model.bind_tools(tools, **kwargs),
            self.budget,
            [convert_to_openai_tool(tool) for tool in tools],
        )


def create_model(settings: Settings, options: ModelOptions):
    model = ChatOpenAI(
        base_url=settings.model_base_url,
        api_key=settings.model_api_key,
        model=settings.model_name,
        temperature=options.temperature,
        max_tokens=options.max_tokens,
        timeout=120,
        max_retries=1,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return BudgetedModel(model, TokenBudget(settings, options.max_tokens))
