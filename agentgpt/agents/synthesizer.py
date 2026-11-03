"""Synthesizer agent: merge findings into the final answer, with citations kept."""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import HumanMessage, SystemMessage

from agentgpt.a2a import types as t
from agentgpt.a2a.transport import Context, TaskRejected
from agentgpt.agents import prompts
from agentgpt.agents.base import agent_model, launch
from agentgpt.config import Settings
from agentgpt.host.context import role_context
from agentgpt.model import structured_response


def make_handler(settings: Settings):
    async def handler(ctx: Context) -> None:
        data = ctx.data
        items = data.get("findings") or []
        if not isinstance(items, list) or not items:
            raise TaskRejected("没有可合并的子任务产物")
        goal = data.get("goal") or ctx.text or ""
        language = data.get("language") or "简体中文"

        await ctx.progress("合并子任务产物")
        model = agent_model(settings, temperature=0.2, max_tokens=2600)
        payload = {
            **role_context(data),
            "用户问题": goal,
            "子任务产物": items,
            "未解决项": data.get("unresolved") or [],
            "语言": language,
        }
        result = await asyncio.wait_for(
            structured_response(
                model,
                t.FinalArtifact,
                [
                    SystemMessage(content=prompts.SYNTHESIZER),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ],
            ),
            timeout=settings.task_wall_clock_s,
        )
        await ctx.emit(
            t.Artifact(
                name="final",
                parts=[t.DataPart(data=result.model_dump(mode="json"))],
                metadata={"role": "final"},
            )
        )

    return handler


async def main(settings: Settings) -> None:
    await launch("synthesizer", make_handler, settings)
