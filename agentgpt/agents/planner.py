"""Planner agent: goal -> task graph. No tools, no retrieval, no answers."""

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
        goal = (data.get("goal") or ctx.text or "").strip()
        if not goal:
            raise TaskRejected("缺少目标文本")
        language = data.get("language") or "简体中文"
        previous = data.get("previous_plan")
        feedback = (data.get("feedback") or "").strip()

        await ctx.progress("按评审意见修订计划" if previous else "正在拆解目标")
        model = agent_model(settings)

        payload = {
            "goal": goal,
            "language": language,
            **role_context(data),
            "available_downstream": [
                "worker：可检索私有知识库与公开网页、读取正文、按证据作答",
                "synthesizer：可把多个子任务产物合并成最终答复",
            ],
        }
        if previous:
            payload.update(previous_plan=previous, feedback=feedback or "（无具体意见）")
        human = json.dumps(payload, ensure_ascii=False)
        messages = [SystemMessage(content=prompts.PLANNER), HumanMessage(content=human)]

        plan = await asyncio.wait_for(
            structured_response(model, t.PlanArtifact, messages),
            timeout=settings.task_wall_clock_s,
        )
        await ctx.emit(
            t.Artifact(
                name="plan",
                parts=[t.DataPart(data=plan.model_dump(mode="json"))],
                metadata={"role": "plan"},
            )
        )

    return handler


async def main(settings: Settings) -> None:
    await launch("planner", make_handler, settings)
