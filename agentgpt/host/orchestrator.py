"""Host orchestration: plan -> judge -> schedule -> verify -> synthesise.

The Host owns three things the agents cannot get around:

1. identity - it issues the run-scoped token, so an agent can only ever touch the
   user, thread and run that token names;
2. evidence admission - every claim in a finished task is re-checked against what
   this run actually read (``task_evidence``), so an agent cannot introduce a
   fabricated or foreign ``evidence_id`` no matter what it returns;
3. the final answer - only the Host writes it, and only after its citations resolve
   against the same evidence set.

Scheduling is a ready-set loop over ``depends_on`` with a bounded parallel width,
not a linear walk through a task list.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import RunScope
from agentgpt.a2a.client import A2AClient, AgentError, RecoveryPending, task_payload
from agentgpt.config import ModelOptions, Settings
from agentgpt.host.context import authorized_context_sources, role_context
from agentgpt.store import Store

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
# Any /api/evidence/<id> link is a citation claim, whatever shape the id has: a model
# that invents doc_fake_123 must not slip past because the id is not well-formed hex.
CITATION = re.compile(r"\[([^\]]*)\]\(/api/evidence/([^)\s]+)\)")


class PlanError(ValueError):
    """The task graph cannot be scheduled as given."""


class ContextInvalidatedError(RuntimeError):
    """A changed authorization/revision aborts dispatch, including fallback retries."""


@dataclass
class TaskState:
    spec: t.TaskSpec
    status: str = "pending"
    result: str = ""
    feedback: str = ""
    attempts: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    findings: dict | None = None
    review: dict | None = None
    dispatch_payload: dict | None = None

    def as_public(self) -> dict:
        return {
            "id": self.spec.id,
            "title": self.spec.title,
            "status": self.status,
            "result": self.result,
            "acceptance_criteria": self.spec.acceptance,
            "depends_on": self.spec.depends_on,
            "feedback": self.feedback,
            "attempts": self.attempts,
            "tool_calls": self.tool_calls,
            "errors": self.errors,
            "history": [],
            "required_tools": [],
            "min_sources": 0,
            "source_scope": "auto",
            "source_reason": "",
            "request_id": None,
        }


def validate_plan(plan: t.PlanArtifact) -> None:
    ids = [task.id for task in plan.tasks]
    if len(ids) != len(set(ids)):
        raise PlanError("任务 id 重复")
    known = set(ids)
    for task in plan.tasks:
        unknown = [d for d in task.depends_on if d not in known]
        if unknown:
            raise PlanError(f"{task.id} 依赖不存在的任务 {unknown}")
        if task.id in task.depends_on:
            raise PlanError(f"{task.id} 依赖自身")
    # depth-first cycle detection over the dependency edges
    state: dict[str, int] = {}
    graph = {task.id: list(task.depends_on) for task in plan.tasks}

    def visit(node: str) -> None:
        if state.get(node) == 1:
            raise PlanError(f"依赖成环：{node}")
        if state.get(node) == 2:
            return
        state[node] = 1
        for neighbour in graph[node]:
            visit(neighbour)
        state[node] = 2

    for node in graph:
        visit(node)


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: A2AClient,
        emit: Emit,
        control=None,
        checkpoint=None,
        resume=None,
        context_guard=None,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.emit = emit
        self.control = control
        self.tasks: dict[str, TaskState] = {}
        self.unresolved: list[str] = []
        self.plan_revisions = 0
        self.messages: list[dict] = []
        self.goal = ""
        self.checkpoint_lock = asyncio.Lock()
        self.checkpoint = checkpoint
        self.resume = resume or {}
        self.context_guard = context_guard
        self.plan = None
        self.deadline_at = None
        self.final_answer = None
        self.completion_status = "completed"
        self.context: dict = {}
        # Tokens are appended to the live view per message id, so the answer needs one.
        self.answer_message_id = uuid4().hex

    async def _guard_dispatch(self, scope: RunScope, role: str, payload: dict) -> None:
        if self.context_guard is None:
            return
        try:
            await self.context_guard(scope, role, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not let semantic-review or synthesis fallback catch a ValueError
            # and continue with findings derived from revoked memory.
            raise ContextInvalidatedError(
                "上下文权限或记忆版本已变化，本轮已停止下发；请重新发起以使用最新上下文。"
            ) from exc

    async def _send(self, role: str, scope: RunScope, payload: dict, **kwargs):
        await self._guard_dispatch(scope, role, payload)
        return await self.client.send(role, scope, payload, **kwargs)

    async def _send_streaming(self, role: str, scope: RunScope, payload: dict, **kwargs):
        await self._guard_dispatch(scope, role, payload)
        return await self.client.send_streaming(role, scope, payload, **kwargs)

    async def _say(self, text: str) -> None:
        """Stream the answer as one live message; the view dedupes on the message id."""
        for start in range(0, len(text), 2000):
            await self.emit(
                "token",
                {
                    "message_id": self.answer_message_id,
                    "text": text[start : start + 2000],
                },
            )

    # ------------------------------------------------------------------ public
    async def run(
        self,
        scope: RunScope,
        goal: str,
        options: ModelOptions,
        background: str | dict = "",
    ) -> str:
        self.goal = goal
        self.context = (
            role_context(background)
            if isinstance(background, dict)
            else {"memory_context": background}
        )
        self.context["current_request"] = goal
        manifest = self.context.setdefault("manifest", {})
        if manifest and (
            manifest.get("user_id") != scope.user_id or manifest.get("thread_id") != scope.thread_id
        ):
            raise ContextInvalidatedError("上下文不属于本轮账号或会话，已停止下发。")
        manifest.update(user_id=scope.user_id, thread_id=scope.thread_id)
        request_id = "request:" + scope.run_id
        sources = self.context.setdefault("context_sources", [])
        sources[:] = [item for item in sources if item.get("kind") != "current_user_request"]
        sources.append(
            {
                "context_id": request_id,
                "kind": "current_user_request",
                "text": goal,
                "user_id": scope.user_id,
                "thread_id": scope.thread_id,
            }
        )
        manifest["context_ids"] = list(
            dict.fromkeys([*manifest.get("context_ids", []), request_id])
        )
        self.context["context_sources"] = list(
            authorized_context_sources(self.context, scope.user_id, scope.thread_id).values()
        )
        saved = self.resume
        self.deadline_at = saved.get("deadline_at") or time.time() + self.settings.run_wall_clock_s
        if saved.get("final_answer"):
            self.completion_status = saved.get("completion_status", "completed")
            return saved["final_answer"]
        if saved.get("plan"):
            plan = t.PlanArtifact.model_validate(saved["plan"])
            self.plan_revisions = saved.get("plan_revisions", 0)
            self.unresolved = saved.get("unresolved", [])
            self.messages = saved.get("messages", [])
            self.answer_message_id = saved.get("answer_message_id", self.answer_message_id)
            self.tasks = {
                item["spec"]["id"]: TaskState(
                    **{**item, "spec": t.TaskSpec.model_validate(item["spec"])}
                )
                for item in saved["tasks"]
            }
        else:
            plan = await self._plan(scope, goal, options, background)
            if options.judge_plan and self.settings.judge_plan:
                plan = await self._judge_plan(scope, plan, options)
            self.tasks = {task.id: TaskState(task) for task in plan.tasks}
        self.plan = plan
        await self._emit_state("running")
        deadline = time.monotonic() + max(0, self.deadline_at - time.time())
        await self._schedule(scope, options, deadline)
        if self._stopped():
            self.completion_status = "paused" if self.control.reason == "pause" else "stopped"
            await self._save_checkpoint()
            return (
                "运行已暂停，可继续执行。" if self.completion_status == "paused" else "运行已停止。"
            )
        answer = await self._synthesize(scope, goal, options)
        if not any(state.status == "completed" for state in self.tasks.values()):
            self.completion_status = "failed"
        if not answer.strip():
            self.completion_status = "failed"
            answer = "本次执行未能生成有效答复，请重试或缩小任务范围。"
        self.final_answer = answer
        # The runner commits terminal state, turn and derived jobs atomically.
        await self._save_checkpoint()
        return answer

    async def _save_checkpoint(self):
        async with self.checkpoint_lock:
            if self.checkpoint is None or self.plan is None:
                return
            await self.checkpoint(
                {
                    "version": 1,
                    "plan": self.plan.model_dump(mode="json"),
                    "tasks": [
                        {**vars(state), "spec": state.spec.model_dump(mode="json")}
                        for state in self.tasks.values()
                    ],
                    "unresolved": self.unresolved,
                    "messages": self.messages,
                    "plan_revisions": self.plan_revisions,
                    "deadline_at": self.deadline_at,
                    "answer_message_id": self.answer_message_id,
                    "final_answer": self.final_answer,
                    "completion_status": self.completion_status,
                }
            )

    # ------------------------------------------------------------------- plan

    def _stopped(self) -> bool:
        return bool(self.control and self.control.stop)

    async def _plan(
        self, scope: RunScope, goal: str, options: ModelOptions, background: str | dict
    ) -> t.PlanArtifact:
        payload = {"goal": goal, "language": options.language}
        plan = await self._ask_plan(scope, payload)
        try:
            validate_plan(plan)
            return plan
        except PlanError as exc:
            self.plan_revisions += 1
            await self.emit(
                "replan",
                {
                    "kind": "replan",
                    "count": self.plan_revisions,
                    "action": "revise",
                    "feedback": str(exc),
                },
            )
            revised = await self._ask_plan(
                scope,
                {
                    "goal": goal,
                    "language": options.language,
                    "previous_plan": plan.model_dump(mode="json"),
                    "feedback": f"上一版计划无法执行：{exc}",
                },
            )
            try:
                validate_plan(revised)
                return revised
            except PlanError:
                # Never stall: fall back to a single task that covers the whole goal.
                log.warning("planner produced an unschedulable graph twice; using a single task")
                return t.PlanArtifact(
                    tasks=[t.TaskSpec(id="t1", title=goal[:120], goal=goal, acceptance=[])],
                    rationale="计划两版均不可调度，退化为单任务以保证产出。",
                )

    async def _ask_plan(self, scope: RunScope, payload: dict) -> t.PlanArtifact:
        payload = {**payload, **self.context}
        task = await self._send("planner", scope, payload, timeout=self.settings.task_wall_clock_s)
        return t.PlanArtifact.model_validate(task_payload(task))

    async def _judge_plan(
        self, scope: RunScope, plan: t.PlanArtifact, options: ModelOptions
    ) -> t.PlanArtifact:
        current = plan
        for _ in range(self.settings.plan_max_revisions + 1):
            review = await self._call_critic(
                scope,
                {
                    "operation": "judge_plan",
                    "goal": self.goal,
                    "plan": current.model_dump(mode="json"),
                    "language": options.language,
                },
            )
            if review.verdict == "pass":
                return current
            self.plan_revisions += 1
            feedback = "；".join(review.reasons + review.actions) or "请按验收意见调整"
            await self.emit(
                "replan",
                {
                    "kind": "replan",
                    "count": self.plan_revisions,
                    "action": "revise",
                    "feedback": feedback,
                },
            )
            revised = await self._ask_plan(
                scope,
                {
                    "goal": self.goal,
                    "language": options.language,
                    "previous_plan": current.model_dump(mode="json"),
                    "feedback": feedback,
                },
            )
            try:
                validate_plan(revised)
                current = revised
            except PlanError as exc:
                self.unresolved.append(f"计划修订后不可调度：{exc}")
                return current
        return current

    # --------------------------------------------------------------- schedule

    async def _schedule(self, scope: RunScope, options: ModelOptions, deadline: float) -> None:
        pending = {
            key: state
            for key, state in self.tasks.items()
            if state.status not in {"completed", "failed", "cancelled"}
        }
        running: dict[str, asyncio.Task] = {}
        try:
            while pending or running:
                if self._stopped():
                    if self.control.reason == "pause":
                        if not running:
                            break
                    else:
                        for task_id, state in pending.items():
                            state.status = "cancelled"
                            state.feedback = "用户中止，未执行。"
                        pending.clear()
                for task_id in list(pending):
                    state = self.tasks[task_id]
                    blocked = [
                        d
                        for d in state.spec.depends_on
                        if self.tasks[d].status in {"failed", "cancelled"}
                    ]
                    if blocked:
                        state.status = "cancelled"
                        state.feedback = f"上游任务未完成：{', '.join(blocked)}"
                        self.unresolved.append(f"{state.spec.title}：{state.feedback}")
                        pending.pop(task_id)
                if time.monotonic() > deadline:
                    for task_id, state in pending.items():
                        state.status = "cancelled"
                        state.feedback = "运行总时长预算耗尽，未执行。"
                        self.unresolved.append(f"{state.spec.title}：运行预算耗尽")
                    pending.clear()

                ready = [
                    task_id
                    for task_id, state in pending.items()
                    if all(self.tasks[d].status == "completed" for d in state.spec.depends_on)
                ]
                if self._stopped():
                    ready = []
                for task_id in ready:
                    if len(running) >= self.settings.max_parallel:
                        break
                    pending.pop(task_id)
                    running[task_id] = asyncio.create_task(
                        self._run_task(scope, self.tasks[task_id], options, deadline)
                    )
                if not running:
                    if pending:
                        raise PlanError(f"无法调度：{sorted(pending)}")
                    break
                finished, _ = await asyncio.wait(
                    running.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for task in finished:
                    task_id = next(key for key, value in running.items() if value is task)
                    running.pop(task_id)
                    try:
                        await task
                    except BaseException:
                        for sibling in running.values():
                            sibling.cancel()
                        await asyncio.gather(*running.values(), return_exceptions=True)
                        raise
        finally:
            for task in running.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)

    async def _run_task(
        self, scope: RunScope, state: TaskState, options: ModelOptions, deadline: float
    ) -> None:
        payload_base = {
            "task": state.spec.model_dump(mode="json"),
            "options": options.model_dump(mode="json"),
        }
        first_attempt = max(1, state.attempts + (state.status == "awaiting_review"))
        for attempt in range(first_attempt, self.settings.task_max_retries + 2):
            if self._stopped():
                state.status = "pending" if self.control.reason == "pause" else "cancelled"
                state.feedback = "用户暂停。" if self.control.reason == "pause" else "用户中止。"
                return
            if time.monotonic() > deadline:
                state.status = "cancelled"
                state.feedback = "运行总时长预算耗尽。"
                return
            resuming = state.status == "running" and state.attempts == attempt
            state.attempts = attempt
            state.status = "running"

            dependencies = {
                dep: self.tasks[dep].findings
                for dep in state.spec.depends_on
                if self.tasks[dep].findings
            }
            payload = {
                **payload_base,
                **self.context,
                "dependencies": dependencies,
                "attempt": attempt,
                "feedback": state.feedback,
            }

            if resuming and state.dispatch_payload:
                payload = state.dispatch_payload
            state.dispatch_payload = payload
            await self._emit_state("running")

            async def progress(text: str, task_id: str = state.spec.id) -> None:
                await self.emit(
                    "tool", {"kind": "tool", "event": "progress", "task_id": task_id, "name": text}
                )

            try:
                task = await self._send_streaming(
                    "worker",
                    scope,
                    payload,
                    on_progress=progress,
                    timeout=self.settings.task_wall_clock_s + 30,
                )
                findings = t.FindingsArtifact.model_validate(task_payload(task))
            except RecoveryPending:
                raise
            except (AgentError, ValueError) as exc:
                state.errors.append(str(exc)[:500])
                state.status = "failed"
                state.result = f"执行失败：{exc}"
                self.unresolved.append(f"{state.spec.title}：{exc}")
                if (
                    attempt <= self.settings.task_max_retries
                    and getattr(exc, "state", None) != t.INPUT_REQUIRED
                ):
                    state.feedback = str(exc)[:500]
                    state.status = "awaiting_review"
                    await self._emit_state("running")
                    continue
                await self._emit_state("running")
                return

            state.findings = findings.model_dump(mode="json")
            state.tool_calls = [
                {"name": state.spec.id, "status": "ok", "calls": findings.tool_calls}
            ]
            violations = await self._admit_evidence(scope, state)
            if violations:
                state.errors.extend(violations)

            review = await self._review(scope, state, options)
            state.review = review.model_dump(mode="json")
            if review.verdict == "pass" and not violations:
                state.status = "completed"
                state.result = findings.summary or _claims_summary(findings)
                self.messages.append(
                    {
                        "id": f"{state.spec.id}-{attempt}",
                        "role": "ai",
                        "text": f"### {state.spec.title}\n\n{state.result}",
                        "kind": "task_result",
                        "reply_to": None,
                    }
                )
                await self._emit_state("running")
                return

            state.feedback = (
                "；".join(violations + review.reasons + review.actions)
                or "产物未通过验收，请按验收标准补齐。"
            )
            state.status = "awaiting_review"
            await self._emit_state("running")

        state.status = "failed"
        state.result = (state.findings or {}).get("summary", "") or "未通过验收"
        self.unresolved.append(f"{state.spec.title}：{state.feedback}")
        await self._emit_state("running")

    # ------------------------------------------------------- evidence admission

    async def _allowed_evidence(self, scope: RunScope, task_id: str, attempts: int) -> set[str]:
        allowed: set[str] = set()
        for attempt in range(1, max(attempts, 1) + 1):
            for source in await self.store.task_sources(
                scope.user_id, scope.thread_id, task_id, attempt, run_id=scope.run_id
            ):
                allowed.add(source["evidence_id"])
        return allowed

    async def _admit_evidence(self, scope: RunScope, state: TaskState) -> list[str]:
        """Drop any claim citation this run cannot account for. Never a warning only."""
        if not state.findings:
            return []
        allowed = await self._allowed_evidence(scope, state.spec.id, state.attempts)
        context_sources = authorized_context_sources(self.context, scope.user_id, scope.thread_id)
        violations: list[str] = []
        for claim in state.findings.get("claims", []):
            kept = []
            for evidence_id in claim.get("evidence_ids", []):
                if evidence_id not in allowed:
                    violations.append(f"结论引用了本轮未读取的证据 {evidence_id}")
                    continue
                kept.append(evidence_id)
            claim["evidence_ids"] = kept
            kept_context = []
            for context_id in claim.get("context_ids", []):
                if context_id not in context_sources:
                    violations.append(f"结论引用了本轮未授权的会话来源 {context_id}")
                    continue
                kept_context.append(context_id)
            claim["context_ids"] = kept_context
        state.findings["allowed_evidence"] = sorted(allowed)
        state.findings["allowed_context"] = sorted(context_sources)
        return violations

    # ------------------------------------------------------------------ review

    async def _call_critic(self, scope: RunScope, payload: dict) -> t.ReviewArtifact:
        payload = {**payload, **self.context}
        task = await self._send("critic", scope, payload, timeout=self.settings.task_wall_clock_s)
        return t.ReviewArtifact.model_validate(task_payload(task))

    async def _review(
        self, scope: RunScope, state: TaskState, options: ModelOptions
    ) -> t.ReviewArtifact:
        try:
            return await self._call_critic(
                scope,
                {
                    "operation": "verify_findings",
                    "spec": state.spec.model_dump(mode="json"),
                    "acceptance": state.spec.acceptance,
                    "findings": state.findings,
                    "excerpts": await self._excerpts(scope, state),
                    "language": options.language,
                },
            )
        except RecoveryPending:
            raise
        except (AgentError, ValueError) as exc:
            # A reviewer outage must not block delivery; it becomes an unresolved item.
            self.unresolved.append(f"{state.spec.title}：验收代理不可用（{exc}）")
            return t.ReviewArtifact(verdict="pass", reasons=["验收代理不可用，未做语义核对"])

    async def _excerpts(self, scope: RunScope, state: TaskState) -> list[dict]:
        budget = self.settings.review_excerpt_chars
        used = 0
        out: list[dict] = []
        sources = await self.store.task_sources(
            scope.user_id, scope.thread_id, state.spec.id, state.attempts, run_id=scope.run_id
        )
        for source in sources:
            text = source.get("text") or ""
            if not text:
                continue
            if used + len(text) > budget:
                out.append(
                    {
                        "evidence_id": source["evidence_id"],
                        "text": "",
                        "omitted": "完整原文超过验收片段预算，未提供支持关系核验",
                    }
                )
                continue
            out.append(
                {
                    "evidence_id": source["evidence_id"],
                    "title": source.get("title") or source.get("url", ""),
                    "text": text,
                }
            )
            used += len(text)
        return out

    # -------------------------------------------------------------- synthesis

    async def _synthesize(self, scope: RunScope, goal: str, options: ModelOptions) -> str:
        findings = [
            {
                "task": state.spec.model_dump(mode="json"),
                "result": state.findings,
                "status": state.status,
            }
            for state in self.tasks.values()
            if state.findings
        ]
        if not findings:
            answer = "本次没有产生任何可用的子任务结论：\n" + "\n".join(
                f"- {item}" for item in (self.unresolved or ["所有子任务均未完成"])
            )
            await self._say(answer)
            return answer

        payload = {
            "goal": goal,
            **self.context,
            "findings": findings,
            "unresolved": self.unresolved,
            "language": options.language,
        }
        allowed = set()
        for state in self.tasks.values():
            if state.findings:
                allowed.update(state.findings.get("allowed_evidence", []))

        try:
            task = await self._send(
                "synthesizer", scope, payload, timeout=self.settings.task_wall_clock_s
            )
            final = t.FinalArtifact.model_validate(task_payload(task))
        except RecoveryPending:
            raise
        except (AgentError, ValueError) as exc:
            self.unresolved.append(f"成文代理失败：{exc}")
            self.completion_status = "failed"
            answer = _sanitize_citations(_fallback_answer(findings, self.unresolved), allowed)
            await self._say(answer)
            return answer

        bad = [m[1] for m in CITATION.findall(final.answer_md) if m[1] not in allowed]
        if bad:
            self.unresolved.append(f"成文引用了本轮未读取的证据：{', '.join(sorted(set(bad)))}")
            retry_payload = {
                **payload,
                "unresolved": self.unresolved
                + [f"以下引用未通过校验，不能出现在答复中：{', '.join(sorted(set(bad)))}"],
            }
            try:
                task = await self._send(
                    "synthesizer", scope, retry_payload, timeout=self.settings.task_wall_clock_s
                )
                final = t.FinalArtifact.model_validate(task_payload(task))
            except RecoveryPending:
                raise
            except (AgentError, ValueError):
                pass
        # Deterministic last line of defence: an unverifiable link never reaches the user.
        final.answer_md = _sanitize_citations(final.answer_md, allowed)
        if not final.answer_md.strip():
            self.completion_status = "failed"
            self.unresolved.append("成文代理返回空答复，以下为已完成子任务的回退结果")
            final.answer_md = _fallback_answer(findings, self.unresolved)
        final.answer_md = _sanitize_citations(final.answer_md, allowed)
        await self._say(final.answer_md)
        return final.answer_md

    # ------------------------------------------------------------------- state

    async def _emit_state(self, status: str, final_answer: str | None = None) -> None:
        await self._save_checkpoint()
        payload = {
            "goal": self.goal,
            "tasks": [state.as_public() for state in self.tasks.values()],
            "current_task_id": next(
                (state.spec.id for state in self.tasks.values() if state.status == "running"), None
            ),
            "replan_count": self.plan_revisions,
            "final_answer": final_answer or "",
            "status": status,
            "messages": self.messages,
        }
        await self.emit("state", payload)


def _sanitize_citations(text: str, allowed: set[str]) -> str:
    def replace(match: re.Match) -> str:
        evidence_id = match.group(2)
        if evidence_id in allowed:
            return match.group(0)
        return "（该引用未通过本轮证据校验，已移除）"

    return CITATION.sub(replace, text)


def _claims_summary(findings: t.FindingsArtifact) -> str:
    lines = [f"- {claim.text}" for claim in findings.claims] or ["（无明确结论）"]
    if findings.gaps:
        lines.append("缺口：" + "；".join(findings.gaps))
    return "\n".join(lines)


def _fallback_answer(findings: list[dict], unresolved: list[str]) -> str:
    blocks = []
    for item in findings:
        result = item.get("result") or {}
        title = (item.get("task") or {}).get("title", "任务")
        summary = result.get("summary") or ""
        claims = [claim.get("text", "") for claim in result.get("claims", [])]
        body = summary or "\n".join(f"- {text}" for text in claims) or "（无结论）"
        blocks.append(f"### {title}\n\n{body}")
    if unresolved:
        blocks.append("### 未完成项\n\n" + "\n".join(f"- {line}" for line in unresolved))
    return "\n\n".join(blocks)
