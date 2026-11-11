"""Worker agent: one sub-task, autonomous tool use, evidence-backed findings.

This is where the old fixed pipeline was: source_scope pre-filtering, required_tools
gating, a 3-round cap, a 2-read budget and an abstention exit that needed a receipt
id. All of that is gone. The worker gets the full tool set, a wall clock and a call
budget, and decides the rest itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import TokenError, bearer_from_header, verify_token
from agentgpt.a2a.transport import Context, TaskRejected
from agentgpt.agents import prompts
from agentgpt.agents.base import agent_model, prepare_store, runtime_app, serve
from agentgpt.agents.tool_journal import journaled_tool_call
from agentgpt.config import ModelOptions, Settings
from agentgpt.host.context import ContextBudgetError, authorized_context_sources, role_context
from agentgpt.mcp_manager import ToolManager
from agentgpt.model import structured_response
from agentgpt.prefix_layout import payload_json
from agentgpt.store import Store

log = logging.getLogger(__name__)


def text_content(message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "\n".join(
        part.get("text", "") if isinstance(part, dict) else str(part) for part in message.content
    )


def _trim(messages: list) -> None:
    """Compatibility hook: token preflight replaces destructive character slicing."""


def _digest(transcripts: list[dict]) -> str:
    return "\n\n".join(
        f"[{item['tool']} / {item['status']}]\n{item['content']}" for item in transcripts
    )


async def _library_hint(store: Store, user_id: str, kb: str = "", limit: int = 20) -> str:
    """Tell the model what the private knowledge base it can actually reach holds.

    Scoped to the same ``kb`` the search tools use: advertising documents from other
    knowledge bases would invite searches that are guaranteed to come back empty.
    This is context about the environment, not an instruction about which tool to use.
    """
    try:
        documents = [doc for doc in await store.documents(user_id) if (doc.get("kb") or "") == kb]
    except Exception:  # noqa: BLE001 - a hint must never break a task
        return "未知"
    scope = f"知识库「{kb}」" if kb else "默认知识库"
    if not documents:
        return f"{scope}为空"
    titles = [str(doc.get("title") or "")[:60] for doc in documents[:limit]]
    more = "" if len(documents) <= limit else f" 等 {len(documents)} 篇"
    return f"{scope}有 {len(documents)} 篇已索引文档：{'、'.join(titles)}{more}"


async def _call_tool(tools_by_name: dict, call: dict) -> ToolMessage:
    """Run one tool call and shape the result the way the model and evidence store expect.

    Done by hand rather than through ``ToolNode``: that node is runtime-aware in
    LangGraph 1.x and refuses to run outside a compiled graph, while this agent is a
    plain A2A handler. Failures become ``status="error"`` messages so a bad call is
    visible to the model instead of killing the task.
    """
    name = call.get("name", "")
    call_id = call.get("id", "")
    tool = tools_by_name.get(name)
    if tool is None:
        return ToolMessage(
            content=f"Error: 不存在名为 {name} 的工具",
            tool_call_id=call_id,
            name=name,
            status="error",
        )
    try:
        output = await tool.ainvoke(call.get("args") or {})
        content = (
            output
            if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False, default=str)
        )
        return ToolMessage(content=content, tool_call_id=call_id, name=name, status="success")
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        log.warning("tool %s failed: %s", name, exc)
        detail = f"{type(exc).__name__}: {exc}"
        return ToolMessage(
            content=f"Error: {detail}"[:4000], tool_call_id=call_id, name=name, status="error"
        )


async def _write_findings(
    model, payload: dict, note: str, transcripts: list[dict], remaining: float
) -> t.FindingsArtifact:
    """Ask the model for the findings artifact; never come back empty-handed.

    A timed-out or unparsable write-up still returns an artifact carrying whatever the
    tools produced. Losing the write-up should cost a review, not the whole sub-task.
    """
    try:
        return await asyncio.wait_for(
            structured_response(
                model,
                t.FindingsArtifact,
                [
                    SystemMessage(content=prompts.WORKER),
                    HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                ],
            ),
            timeout=remaining,
        )
    except ContextBudgetError:
        raise  # Partial prefixes cannot stand in for a complete transformation.
    except Exception as exc:  # noqa: BLE001 - a degraded artifact beats no artifact
        log.warning("write-up failed: %s", exc)
        excerpt = _digest(transcripts)[:2000]
        gaps = [f"写稿阶段失败：{type(exc).__name__}: {exc}"[:300]]
        if note:
            gaps.append(note)
        return t.FindingsArtifact(
            summary="本轮工具调用已完成，但整理结论时未能成文；以下为取到的原文摘要。"
            + (f"\n\n{excerpt}" if excerpt else ""),
            gaps=gaps,
        )


def make_handler(settings: Settings, manager: ToolManager, store: Store):
    async def handler(ctx: Context) -> None:
        data = ctx.data
        try:
            spec = t.TaskSpec.model_validate(data.get("task") or {})
        except Exception as exc:  # noqa: BLE001 - reported as REJECTED
            raise TaskRejected(f"任务定义无效：{exc}") from None
        dependencies = data.get("dependencies") or {}
        attempt = int(data.get("attempt") or 1)
        options = ModelOptions.model_validate(data.get("options") or {})
        scope = ctx.scope

        tools = manager.tools_for(scope.user_id, options.enabled_tools, options.knowledge_base)
        if settings.prefix_cache_layout:
            tools = sorted(tools, key=lambda tool: tool.name)
        budget = max(options.max_tool_rounds, settings.task_max_tool_calls)
        started = time.monotonic()
        total = float(settings.task_wall_clock_s)
        # The write-up call is what turns a transcript into an artifact. Give it a
        # reserved slice instead of letting the tool loop spend the whole clock.
        write_up = min(60.0, max(30.0, total * 0.35))
        deadline = started + total - write_up
        model = agent_model(settings, options.temperature, options.max_tokens)

        payload = {
            **role_context(data),
            "任务": spec.model_dump(mode="json"),
            "上游产物": dependencies,
            "语言": options.language,
            "本轮反馈": data.get("feedback") or "",
            "私有知识库": await _library_hint(store, scope.user_id, options.knowledge_base),
        }
        messages: list = [
            SystemMessage(content=prompts.WORKER),
            HumanMessage(content=payload_json(payload, settings.prefix_cache_layout)),
        ]

        tools_by_name = {tool.name: tool for tool in tools}
        bound = (
            model.bind_tools(tools, tool_choice="auto", parallel_tool_calls=False)
            if tools
            else model
        )

        calls = 0
        traces: list[dict] = []
        transcripts: list[dict] = []
        note = ""
        pending_calls = []
        saved = await ctx.load("worker")
        if saved:
            messages = messages_from_dict(saved["messages"])
            transcripts = saved["transcripts"]
            traces = saved["traces"]
            calls = saved["calls"]
            pending_calls = saved["pending_calls"]
            note = saved["note"]

        async def checkpoint():
            await ctx.save(
                "worker",
                {
                    "messages": messages_to_dict(messages),
                    "transcripts": transcripts,
                    "traces": traces,
                    "calls": calls,
                    "pending_calls": pending_calls,
                    "note": note,
                },
            )

        while tools and calls < budget:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                note = "到达单任务时间上限，改用已有资料产出。"
                break
            if not pending_calls:
                response = await asyncio.wait_for(bound.ainvoke(messages), timeout=remaining)
                messages.append(response)
                if not response.tool_calls:
                    break
                pending_calls = list(response.tool_calls)
                await checkpoint()
            for call in list(pending_calls):
                if calls >= budget:
                    break
                calls += 1
                try:
                    message = await asyncio.wait_for(
                        journaled_tool_call(ctx, call, lambda: _call_tool(tools_by_name, call)),
                        timeout=max(5.0, deadline - time.monotonic()),
                    )
                except asyncio.TimeoutError:
                    # One slow tool must not cost the whole sub-task: report it to the
                    # model and let it answer from what it already has.
                    message = ToolMessage(
                        content="Error: 该工具调用超过本轮剩余时间，已中止",
                        tool_call_id=call.get("id", ""),
                        name=call.get("name", ""),
                        status="error",
                    )
                    note = note or "有工具调用超时，改用已有资料产出。"
                messages.append(message)
                traces.append(
                    {"name": message.name, "call_id": call["id"], "status": message.status}
                )
                transcripts.append(
                    {
                        "tool": message.name,
                        "status": message.status,
                        "content": text_content(message),
                    }
                )
                await store.associate_evidence(
                    scope.user_id, scope.thread_id, scope.run_id, spec.id, attempt, message
                )
                await ctx.progress(
                    f"{spec.title}：{message.name} → {message.status}"
                    + ("" if not note else f"（{note}）")
                )
                pending_calls.pop(0)
                _trim(messages)
                await checkpoint()

        if calls >= budget and tools:
            note = note or "到达工具调用预算，改用已有资料产出。"

        final_payload = {
            **role_context(data),
            "任务": spec.model_dump(mode="json"),
            "语言": options.language,
            "工具返回": _digest(transcripts),
            "上游产物": dependencies,
            "本轮反馈": data.get("feedback") or "",
            "工具调用次数": calls,
            "说明": note,
        }
        findings = await _write_findings(
            model=model,
            payload=final_payload,
            note=note,
            transcripts=transcripts,
            remaining=max(30.0, started + total - time.monotonic()),
        )
        findings.task_id = spec.id
        findings.tool_calls = calls

        # Hard check (agent side): a claim may only cite evidence this run can resolve
        # for this user. Unknown or foreign ids are stripped, never silently kept.
        context_sources = authorized_context_sources(data, scope.user_id, scope.thread_id)
        for claim in findings.claims:
            kept = []
            for evidence_id in claim.evidence_ids:
                try:
                    await store.evidence(scope.user_id, evidence_id)
                except ValueError:
                    findings.gaps.append(f"引用的证据 {evidence_id} 不存在或不属于当前账号，已剔除")
                    continue
                kept.append(evidence_id)
            claim.evidence_ids = kept
            kept_context = []
            for context_id in claim.context_ids:
                if context_id not in context_sources:
                    findings.gaps.append(f"会话来源 {context_id} 未获本轮授权，已剔除")
                    continue
                kept_context.append(context_id)
            claim.context_ids = kept_context

        if note:
            findings.gaps.append(note)
        await ctx.emit(
            t.Artifact(
                name="findings",
                parts=[t.DataPart(data=findings.model_dump(mode="json"))],
                metadata={"role": "findings", "task_id": spec.id},
            )
        )

    return handler


async def main(settings: Settings) -> None:
    store = Store(settings.database)
    manager = ToolManager(settings, store)
    tasks = await prepare_store(settings, "worker")

    @asynccontextmanager
    async def lifespan(_app):
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    _runtime, app = runtime_app(
        "worker", make_handler(settings, manager, store), settings, store=tasks, lifespan=lifespan
    )

    async def tool_catalog_endpoint(request: Request):
        try:
            verify_token(
                settings.a2a_secret, bearer_from_header(request.headers.get("authorization"))
            )
        except TokenError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        return {"tools": manager.catalog(), "servers": manager.server_status}

    app.add_api_route("/tools", tool_catalog_endpoint, methods=["GET"])
    await serve("worker", app, int(settings.worker_url.rsplit(":", 1)[-1]))
