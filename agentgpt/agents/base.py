"""Shared agent bootstrap: AgentCard, model factory, process entry."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from a2a.types import HTTPAuthSecurityScheme, SecurityRequirement, SecurityScheme, StringList

from agentgpt.a2a import types as t
from agentgpt.a2a.tasks import TaskStore
from agentgpt.a2a.transport import AgentRuntime
from agentgpt.config import ModelOptions, Settings
from agentgpt.model import create_model

log = logging.getLogger(__name__)

ROLES = ("planner", "worker", "critic", "synthesizer")

SKILLS: dict[str, list[t.AgentSkill]] = {
    "planner": [
        t.AgentSkill(
            id="decompose",
            name="拆解目标",
            description="把用户目标拆成可独立验收的子任务，并标注依赖关系",
            tags=["planning", "decomposition"],
            examples=["PINE 项目的预算和部署限制是什么", "对比 2024 和 2025 的岗位要求"],
        ),
        t.AgentSkill(
            id="revise_plan",
            name="按验收意见修订计划",
            description="收到评审意见后改出一版新计划",
            tags=["planning", "revision"],
            examples=["把两个取证任务拆开，并给对比任务加上依赖"],
        ),
    ],
    "worker": [
        t.AgentSkill(
            id="execute_task",
            name="执行单个子任务",
            description="自主选择工具检索与阅读资料，产出带证据的结论",
            tags=["retrieval", "rag", "web", "research"],
            examples=["核实 PINE 项目预算", "整理 2025 年岗位硬性要求"],
        )
    ],
    "critic": [
        t.AgentSkill(
            id="verify_findings",
            name="验收执行产物",
            description="判断产物是否满足验收标准、结论是否被原文支持",
            tags=["verification", "review"],
            examples=["这条结论没有来源，需要补出处"],
        ),
        t.AgentSkill(
            id="judge_plan",
            name="复核任务拆分",
            description="检查任务粒度与依赖关系是否合理",
            tags=["planning", "review"],
            examples=["两个取证任务应合并", "对比任务缺少依赖"],
        ),
    ],
    "synthesizer": [
        t.AgentSkill(
            id="merge",
            name="合并成稿",
            description="把多个子任务产物合并为一份带引用的答复",
            tags=["writing", "summarisation"],
            examples=["把三份 findings 合成一段结论加差异明细"],
        )
    ],
}

DESCRIPTIONS = {
    "planner": "计划编排代理：只拆解目标与依赖关系，不检索、不作答。",
    "worker": "执行代理：自主调用检索与阅读工具，基于正文证据产出结论。",
    "critic": "验收代理：核对产物与原文的支持关系，并复核任务粒度。",
    "synthesizer": "成文代理：合并多子任务产物为最终答复，保留引用与冲突说明。",
}


def build_card(role: str, settings: Settings) -> t.AgentCard:
    url = settings.agent_urls()[role]
    return t.AgentCard(
        name=f"agentgpt-{role}",
        description=DESCRIPTIONS[role],
        version="1.0.0",
        supported_interfaces=[
            t.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList()})],
        capabilities=t.AgentCapabilities(streaming=True),
        skills=SKILLS[role],
    )


def agent_model(settings: Settings, temperature: float = 0.2, max_tokens: int = 2400):
    options = ModelOptions(temperature=temperature, max_tokens=max_tokens)
    return create_model(settings, options)


def task_store(settings: Settings, role: str) -> TaskStore:
    return TaskStore(settings.a2a_state_dir / f"{role}.sqlite3")


async def serve(role: str, app, port: int) -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
    log.info("%s agent listening on 127.0.0.1:%s", role, port)
    await uvicorn.Server(config).serve()


def runtime_app(role: str, handler, settings: Settings, store=None, lifespan=None):
    """Build the agent's FastAPI app. The caller is responsible for store.initialize()."""
    runtime = AgentRuntime(
        card=build_card(role, settings),
        handler=handler,
        store=store or task_store(settings, role),
        secret=settings.a2a_secret,
    )
    return runtime, runtime.build(lifespan=lifespan)


async def prepare_store(settings: Settings, role: str) -> TaskStore:
    store = task_store(settings, role)
    await store.initialize()
    return store


def run(role: str, factory) -> None:
    """Blocking entry point for ``python -m agentgpt --role <role>``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(factory())


def port_of(settings: Settings, role: str) -> int:
    return int(settings.agent_urls()[role].rsplit(":", 1)[-1])


async def launch(role: str, handler_factory, settings: Settings) -> None:
    """Standard agent process: task store, runtime app, uvicorn."""
    store = await prepare_store(settings, role)
    _runtime, app = runtime_app(role, handler_factory(settings), settings, store=store)
    await serve(role, app, port_of(settings, role))
