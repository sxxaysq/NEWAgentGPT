"""Critic agent: judges finished work and reviews plans. No tools.

It only sees what the Host passes in: the task definition, the artifact, and the
原文 excerpts the Host pulled from the evidence store. That keeps the Host as the
gatekeeper of what may be read, and makes the Critic's verdict auditable against a
fixed evidence window.
"""

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

VERIFY = "verify_findings"
JUDGE = "judge_plan"


def make_handler(settings: Settings):
    async def handler(ctx: Context) -> None:
        data = ctx.data
        operation = data.get("operation") or VERIFY
        language = data.get("language") or "简体中文"
        model = agent_model(settings, temperature=0.1, max_tokens=1200)

        if operation == VERIFY:
            await ctx.progress("核对产物与原文的支持关系")
            payload = {
                "验收标准": data.get("acceptance") or [],
                "任务": data.get("spec") or {},
                "执行产物": data.get("findings") or {},
                "引用的原文片段": data.get("excerpts") or [],
                "语言": language,
            }
            system = prompts.CRITIC_VERIFY
        elif operation == JUDGE:
            await ctx.progress("复核任务拆分与依赖")
            payload = {
                "用户目标": data.get("goal") or "",
                "任务图": data.get("plan") or {},
                "语言": language,
            }
            system = prompts.CRITIC_PLAN
        else:
            raise TaskRejected(f"不支持的验收操作：{operation}")

        payload.update(role_context(data))
        review = await asyncio.wait_for(
            structured_response(
                model,
                t.ReviewArtifact,
                [
                    SystemMessage(content=system),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ],
            ),
            timeout=settings.task_wall_clock_s,
        )
        await ctx.emit(
            t.Artifact(
                name="review",
                parts=[t.DataPart(data=review.model_dump(mode="json"))],
                metadata={"role": "review", "operation": operation},
            )
        )

    return handler


async def main(settings: Settings) -> None:
    await launch("critic", make_handler, settings)
