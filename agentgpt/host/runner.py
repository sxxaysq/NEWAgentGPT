"""Durable command inbox for the Host.

Keeps what was worth keeping from the LangGraph version - the SQLite-backed queue,
one worker per conversation, an event stream the browser can replay - and drops the
checkpoint machinery, because the unit of work is now a remote A2A task rather than
a graph node.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass

from agentgpt.a2a.auth import RunScope
from agentgpt.a2a.client import A2AClient, RecoveryPending
from agentgpt.config import ModelOptions, Settings
from agentgpt.host.context import ContextAssembler
from agentgpt.host.orchestrator import Orchestrator
from agentgpt.store import Store

log = logging.getLogger(__name__)

OPERATIONS = {
    "start",
    "pause",
    "continue",
    "stop",
    "followup",
    "chat",
    "append",
    "summarize",
    "provide_input",
}


@dataclass
class Control:
    stop: bool = False
    reason: str = ""


class NullMemory:
    """Explicit disabled adapter for isolated tests and service-off operation."""

    def __init__(self) -> None:
        self.wake = asyncio.Event()
        self.index = None

    def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def recall(self, *_args, **_kwargs):
        return []

    async def enqueue(self, *_args, **_kwargs):
        return None


class RunManager:
    def __init__(
        self, store: Store, client: A2AClient, settings: Settings, memory=None, summaries=None
    ):
        self.store = store
        self.client = client
        self.settings = settings
        self.memory = memory or NullMemory()
        self.summaries = summaries
        self.store.memory_enabled = settings.memory_enabled
        self.assembler = ContextAssembler(store, settings)
        self.running: dict[str, asyncio.Task] = {}
        self.controls: dict[str, Control] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- lifecycle

    async def recover(self) -> None:
        """Resume durable inbox entries, keeping run ids and committed checkpoints."""
        await self.store.reconcile_turns(memory_enabled=self.settings.memory_enabled)
        for row in await self.store.queued_runs():
            await self.store.ensure_turn_for_run(row["id"])
            saved = json.loads(row["context"] or "{}")
            if row["attempts"] and saved.get("version") != 1:
                # Old runs have no RPC identity/receipts; replaying them could duplicate writes.
                await self.store.finish_turn(
                    row["id"],
                    outcome="failed",
                    assistant_text="旧版运行缺少恢复检查点，未自动重复执行；请核查后重新发起。",
                    error="missing legacy recovery checkpoint",
                )
                continue
            await self.store.append_event(row["thread_id"], "recovering", {"run_id": row["id"]})
            self._ensure_worker(row["thread_id"])

    async def close(self) -> None:
        active = [task for task in self.running.values() if not task.done()]
        for task in active:
            task.cancel()
        for task in active:
            with suppress(asyncio.CancelledError):
                await task

    # --------------------------------------------------------------- commands

    async def command(self, thread, operation, text="", question_id=None, request_id=None):
        operation = "followup" if operation in {"chat", "append"} else operation
        if operation not in OPERATIONS:
            raise ValueError("不支持的操作")
        thread_id = thread["id"]
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            if request_id:
                async with self.store.connect() as db:
                    prior = await (
                        await db.execute(
                            "SELECT id FROM runs WHERE user_id=? AND thread_id=? AND request_id=?",
                            (thread["user_id"], thread_id, request_id),
                        )
                    ).fetchone()
                if prior:
                    row, _ = await self.store.enqueue_run(
                        thread, operation, text, question_id, request_id
                    )
                    return row["id"]
            queue = await self.store.queued_runs(thread_id)
            active = bool(queue)
            if active and operation not in {"pause", "stop", "followup", "summarize"}:
                raise ValueError("会话仍在执行，请先暂停或等待完成。")
            if operation in {"start", "continue"} and not thread.get("goal"):
                raise ValueError("请先设定目标。")
            if operation == "followup" and not text.strip():
                raise ValueError("请填写追加提问。")
            if operation == "provide_input":
                raise ValueError("当前流程不会中途索要补充信息，可直接追加提问。")

            row, created = await self.store.enqueue_run(
                thread, operation, text, question_id, request_id
            )
            if not created:
                return row["id"]
            if self.summaries:
                self.summaries.wake.set()
            if active and operation in {"pause", "stop"}:
                control = self.controls.get(thread_id)
                if control:
                    control.stop = True
                    control.reason = "stop" if operation == "stop" else "pause"
                return row["id"]
            self._ensure_worker(thread_id)
            return row["id"]

    def _ensure_worker(self, thread_id: str) -> None:
        active = self.running.get(thread_id)
        if active is None or active.done():
            self.running[thread_id] = asyncio.create_task(
                self._pump(thread_id), name="host:" + thread_id
            )

    async def state(self, thread_id: str) -> dict:
        view = await self.store.event_view(thread_id)
        if view["state"]:
            return view["state"]
        thread = await self.store.thread(thread_id)
        return {
            "goal": thread["goal"] if thread else "",
            "tasks": [],
            "current_task_id": None,
            "replan_count": 0,
            "final_answer": "",
            "status": thread["status"] if thread else "ready",
            "messages": [],
        }

    # ------------------------------------------------------------------ pump

    async def _pump(self, thread_id: str) -> None:
        while True:
            async with self.locks.setdefault(thread_id, asyncio.Lock()):
                queue = await self.store.queued_runs(thread_id)
                if not queue:
                    return
                row = queue[0]
                control = Control(stop=row["control"] in {"stop", "pause"}, reason=row["control"])
                self.controls[thread_id] = control
            try:
                thread = await self.store.thread(thread_id)
                if row["operation"] in {"pause", "stop"}:
                    await self.store.update_run(row["id"], status="done")
                    continue
                await self._execute(row, thread, control)
            except asyncio.CancelledError:
                # Shutdown is not task failure. The original run and checkpoint survive.
                await self.store.update_run(row["id"], status="queued")
                raise
            except RecoveryPending as exc:
                await self.store.update_run(row["id"], status="queued", error=str(exc)[:1000])
                await self.store.append_event(thread_id, "recovering", {"run_id": row["id"]})
                await asyncio.sleep(3)
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI
                log.exception("run failed")
                await self.store.ensure_turn_for_run(row["id"])
                await self.store.finish_turn(
                    row["id"],
                    outcome="failed",
                    assistant_text=f"本轮执行中断：{type(exc).__name__}: {exc}",
                    error=str(exc)[:1000],
                )
                if self.summaries:
                    self.summaries.wake.set()

    async def _close_view(self, thread_id: str, goal: str, status: str, answer: str) -> None:
        """Project the terminal status into the state view the UI reads.

        An "error" event alone is not enough: /api/threads returns the last state
        projection, so a run that dies without one looks like it is still working.
        """
        await self.store.append_event(
            thread_id,
            "state",
            {
                "goal": goal,
                "tasks": [],
                "current_task_id": None,
                "replan_count": 0,
                "final_answer": answer[:2000],
                "status": status,
                "messages": [],
            },
        )

    async def _execute(self, row, thread, control: Control) -> None:
        thread_id = thread["id"]
        saved = json.loads(row["context"] or "{}")
        turn = await self.store.ensure_turn_for_run(row["id"])
        options = ModelOptions.model_validate(saved.get("options", thread["settings"]))
        goal = turn["user_text"]
        if not goal.strip():
            raise ValueError("未提供有效问题")
        await self.store.update_run(row["id"], status="running", attempts=row["attempts"] + 1)
        await self.store.update_thread(thread_id, status="running")
        await self.store.append_event(
            thread_id,
            "state",
            await self.state(thread_id) | {"status": "running", "goal": goal},
        )

        scope = RunScope(
            user_id=thread["user_id"],
            thread_id=thread_id,
            run_id=row["id"],
            knowledge_base=options.knowledge_base,
        )
        preference = await self.store.memory_preference(scope.user_id)
        revision = preference["revision"]
        background = saved.get("background")
        if (
            saved.get("memory_enabled", False) != self.settings.memory_enabled
            and isinstance(background, dict)
            and background.get("memory_context")
        ):
            raise ValueError("长期记忆服务设置已变更，旧运行已停止；请重新发起请求。")
        if saved.get("memory_revision", revision) != revision and saved.get("orchestrator"):
            # A checkpoint can contain derived old-memory text in plans and remote requests.
            # Replaying it would bypass deletion; restarting could repeat tool side effects.
            raise ValueError("记忆设置或内容已变更，本次恢复已停止；请重新发起请求。")
        if not isinstance(background, dict) or saved.get("memory_revision") != revision:
            background = await self._background(thread, scope, row, turn, options)
        saved.update(
            {
                "version": 1,
                "goal": goal,
                "options": options.model_dump(mode="json"),
                "background": background,
                "memory_revision": revision,
                "memory_enabled": self.settings.memory_enabled,
            }
        )
        await self.store.update_run(row["id"], context=json.dumps(saved, ensure_ascii=False))

        async def checkpoint(state):
            saved["orchestrator"] = state
            await self.store.update_run(row["id"], context=json.dumps(saved, ensure_ascii=False))

        async def emit(kind: str, payload: dict) -> None:
            await self.store.append_event(thread_id, kind, payload)

        async def context_guard(*_args):
            await self._validate_memory_snapshot(scope, options.memory_scope, background)

        await context_guard()
        orchestrator = Orchestrator(
            self.settings,
            self.store,
            self.client,
            emit,
            control=control,
            checkpoint=checkpoint,
            resume=saved.get("orchestrator"),
            context_guard=context_guard,
        )
        answer = await orchestrator.run(scope, goal, options, background)

        status = getattr(orchestrator, "completion_status", "completed")
        if control.stop:
            status = "paused" if control.reason == "pause" else "stopped"
        await self.store.finish_turn(
            row["id"],
            outcome=status,
            assistant_text=answer,
            state={
                "goal": goal,
                "tasks": [state.as_public() for state in orchestrator.tasks.values()],
                "current_task_id": None,
                "replan_count": orchestrator.plan_revisions,
                "messages": orchestrator.messages,
            },
            memory_enabled=self.settings.memory_enabled,
        )
        self.memory.wake.set()
        if self.summaries:
            self.summaries.wake.set()

    async def _background(self, thread, scope, row, turn, options) -> dict:
        memories = []
        # Expand pronouns using only the authorized, boundary-correct session snapshot.
        snapshot = await self.store.context_snapshot(
            scope.user_id, scope.thread_id, turn["input_seq"]
        )
        objects = snapshot.get("session", {}).get("objects", [])
        query = turn["user_text"]
        if objects:
            query = json.dumps(objects, ensure_ascii=False) + "\n" + query
        preference = await self.store.memory_preference(scope.user_id)
        try:
            memories = await self.memory.recall(scope.user_id, options.memory_scope, query)
        except Exception:
            log.warning("memory recall failed", exc_info=True)
        if not preference["enabled"] or not self.settings.memory_enabled:
            memories = []
        return await self.assembler.assemble(
            scope.user_id,
            scope.thread_id,
            turn["user_text"],
            before_input_seq=turn["input_seq"],
            memory_context=memories,
            memory_revision=preference["revision"],
            scope=options.memory_scope,
        )

    async def _validate_memory_snapshot(self, scope, memory_scope, background):
        manifest = background.get("manifest", {})
        expected_revision = manifest.get("memory_revision", 0)
        memories = background.get("memory_context") or []
        async with self.store.connect() as db:
            await db.execute("BEGIN")
            pref = await (
                await db.execute(
                    "SELECT enabled,revision FROM memory_preferences WHERE user_id=?",
                    (scope.user_id,),
                )
            ).fetchone()
            revision = pref["revision"] if pref else 0
            enabled = bool(pref["enabled"]) if pref else True
            if revision != expected_revision:
                raise ValueError("记忆已变更，本轮已停止；请重新发起请求。")
            if memories and (not enabled or not self.settings.memory_enabled):
                raise ValueError("长期记忆已关闭，本轮已停止；请重新发起请求。")
            for item in memories:
                row = await (
                    await db.execute(
                        "SELECT version FROM memories WHERE id=? AND user_id=? "
                        "AND scope IN ('',?) AND deleted=0 AND archived=0",
                        (item["id"], scope.user_id, memory_scope),
                    )
                ).fetchone()
                if not row or row["version"] != item["version"]:
                    raise ValueError("已召回记忆的版本或权限已变更，本轮已停止。")
