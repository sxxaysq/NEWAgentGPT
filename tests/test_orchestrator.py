"""Host orchestration: plan validation, ready-set scheduling, evidence admission.

The store and the A2A client are faked here on purpose - these tests are about the
Host's own decisions, not about HTTP or SQLite.
"""

import asyncio
import copy

import pytest

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import RunScope
from agentgpt.config import ModelOptions, Settings
from agentgpt.host.orchestrator import Orchestrator, PlanError, validate_plan

SCOPE = RunScope("alice", "thread", "run")


def spec(task_id, deps=()):
    return {
        "id": task_id,
        "title": f"任务{task_id}",
        "goal": f"完成{task_id}",
        "depends_on": list(deps),
    }


def plan(*tasks):
    return t.PlanArtifact.model_validate({"tasks": list(tasks), "rationale": "test"})


class FakeStore:
    """Only task_sources is used by the orchestrator."""

    def __init__(self, sources=None):
        self.sources = sources or {}
        self.reads = []

    async def task_sources(self, user_id, thread_id, task_id, attempt, *, run_id=None):
        self.reads.append((task_id, attempt))
        return self.sources.get((task_id, attempt), [])


class FakeClient:
    def __init__(self, plans, findings, reviews, finals, worker_delay=0.02):
        self.plans = list(plans)
        self.findings = findings
        self.reviews = list(reviews)
        self.finals = list(finals)
        self.worker_delay = worker_delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.fail_tasks = set()

    @staticmethod
    def _task(payload):
        # The wire only carries JSON, so a fake agent must hand back a dict too.
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump(mode="json")
        return t.Task(
            id="x",
            artifacts=[t.Artifact(parts=[t.DataPart(data=payload)])],
            status=t.TaskStatus(state=t.COMPLETED),
        )

    async def send(self, role, scope, data, text="", timeout=None):
        operation = data.get("operation", "") if isinstance(data, dict) else ""
        self.calls.append(f"{role}:{operation}" if operation else role)
        if role == "planner":
            value = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return self._task(copy.deepcopy(value))
        if role == "critic":
            value = self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]
            return self._task(copy.deepcopy(value))
        if role == "synthesizer":
            value = self.finals.pop(0) if len(self.finals) > 1 else self.finals[0]
            return self._task(copy.deepcopy(value))
        raise AssertionError(f"unexpected role {role}")

    async def send_streaming(self, role, scope, data, text="", on_progress=None, timeout=None):
        task_id = data["task"]["id"]
        self.calls.append(f"worker:{task_id}")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.worker_delay)
            if task_id in self.fail_tasks:
                from agentgpt.a2a.client import AgentError

                raise AgentError("worker", "工具全部失败")
            if on_progress:
                await on_progress(f"{task_id} 检索中")
        finally:
            self.active -= 1
        payload = self.findings(task_id, data.get("attempt", 1))
        return self._task(payload)


def build(tmp_plan, findings=None, reviews=None, finals=None, **settings_overrides):
    settings = Settings().model_copy(
        update={
            "judge_plan": False,
            "max_parallel": 3,
            "task_max_retries": 1,
            "plan_max_revisions": 1,
            **settings_overrides,
        }
    )
    client = FakeClient(
        [tmp_plan],
        findings or (lambda task_id, attempt: {"task_id": task_id, "summary": f"{task_id} 结论"}),
        reviews or [{"verdict": "pass", "reasons": [], "actions": []}],
        finals or [{"answer_md": "最终答复", "citations": []}],
    )

    async def emit(kind, payload):
        return None

    return Orchestrator(settings, FakeStore(), client, emit), client


# --------------------------------------------------------------- plan shape


def test_plan_rejects_duplicate_ids():
    with pytest.raises(PlanError, match="重复"):
        validate_plan(plan(spec("t1"), spec("t1")))


def test_plan_rejects_unknown_and_self_dependency():
    with pytest.raises(PlanError, match="不存在"):
        validate_plan(plan(spec("t1", ["t9"])))
    with pytest.raises(PlanError, match="自身"):
        validate_plan(plan(spec("t1", ["t1"])))


def test_plan_rejects_cycles():
    with pytest.raises(PlanError, match="成环"):
        validate_plan(plan(spec("t1", ["t2"]), spec("t2", ["t1"])))


def test_valid_graph_passes():
    validate_plan(plan(spec("t1"), spec("t2"), spec("t3", ["t1", "t2"])))


# -------------------------------------------------------------- scheduling


@pytest.mark.asyncio
async def test_dependencies_gate_dispatch_and_independent_tasks_run_in_parallel():
    orchestrator, client = build(plan(spec("t1"), spec("t2"), spec("t3", ["t1", "t2"])))
    await orchestrator.run(SCOPE, "对比两年要求", ModelOptions())

    order = [call for call in client.calls if call.startswith("worker:")]
    assert order[:2] in (["worker:t1", "worker:t2"], ["worker:t2", "worker:t1"])
    assert order[2] == "worker:t3"
    assert client.max_active == 2


@pytest.mark.asyncio
async def test_parallel_width_is_bounded():
    orchestrator, client = build(
        plan(spec("t1"), spec("t2"), spec("t3"), spec("t4")), max_parallel=2
    )
    await orchestrator.run(SCOPE, "四个并列问题", ModelOptions())
    assert client.max_active <= 2


@pytest.mark.asyncio
async def test_failed_dependency_cancels_dependents_but_still_answers():
    orchestrator, client = build(plan(spec("t1"), spec("t2", ["t1"])))
    client.fail_tasks = {"t1"}
    answer = await orchestrator.run(SCOPE, "链式目标", ModelOptions())

    state = orchestrator.tasks
    assert state["t1"].status == "failed"
    assert state["t2"].status == "cancelled"
    assert "t1" not in [call for call in client.calls if call.startswith("worker:")][1:]
    assert answer


@pytest.mark.asyncio
async def test_unschedulable_plan_falls_back_to_one_task_instead_of_stalling():
    orchestrator, client = build(plan(spec("t1", ["t2"])))
    await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert len(orchestrator.tasks) == 1
    assert list(orchestrator.tasks)[0] == "t1"


# --------------------------------------------------------- evidence admission


@pytest.mark.asyncio
async def test_claim_citing_unread_evidence_is_stripped_and_flagged():
    def findings(task_id, attempt):
        return {
            "task_id": task_id,
            "summary": "结论",
            "claims": [
                {"text": "真实结论", "evidence_ids": ["doc_real"]},
                {"text": "编造结论", "evidence_ids": ["doc_fake"]},
            ],
            "gaps": [],
        }

    orchestrator, client = build(plan(spec("t1")), findings=findings)
    orchestrator.store.sources = {("t1", 1): [{"evidence_id": "doc_real", "text": "原文"}]}
    await orchestrator.run(SCOPE, "目标", ModelOptions())

    claims = orchestrator.tasks["t1"].findings["claims"]
    assert claims[0]["evidence_ids"] == ["doc_real"]
    assert claims[1]["evidence_ids"] == []
    assert any("doc_fake" in error for error in orchestrator.tasks["t1"].errors)


@pytest.mark.asyncio
async def test_a_task_with_unreadable_citations_never_reaches_completed():
    def findings(task_id, attempt):
        return {
            "task_id": task_id,
            "summary": "结论",
            "claims": [{"text": "只有编造来源", "evidence_ids": ["doc_fake"]}],
            "gaps": [],
        }

    orchestrator, client = build(plan(spec("t1")), findings=findings)
    orchestrator.store.sources = {}
    await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert orchestrator.tasks["t1"].status == "failed"
    assert orchestrator.unresolved


# ------------------------------------------------------------- final answer


@pytest.mark.asyncio
async def test_unverifiable_citation_link_is_removed_from_the_answer():
    orchestrator, client = build(
        plan(spec("t1")),
        reviews=[{"verdict": "pass", "reasons": [], "actions": []}],
        finals=[
            {
                "answer_md": "结论见 [来源](/api/evidence/doc_fake1234567890abcdef)",
                "citations": ["doc_fake1234567890abcdef"],
            }
        ],
    )
    orchestrator.store.sources = {("t1", 1): [{"evidence_id": "doc_real", "text": "原文"}]}
    answer = await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert "doc_fake1234567890abcdef" not in answer
    assert "未通过本轮证据校验" in answer


@pytest.mark.asyncio
async def test_verified_citation_survives():
    evidence_id = "doc_" + "a" * 32
    orchestrator, client = build(
        plan(spec("t1")),
        findings=lambda task_id, attempt: {
            "task_id": task_id,
            "summary": "结论",
            "claims": [{"text": "结论", "evidence_ids": [evidence_id]}],
            "gaps": [],
        },
        finals=[
            {"answer_md": f"结论见 [来源](/api/evidence/{evidence_id})", "citations": [evidence_id]}
        ],
    )
    orchestrator.store.sources = {("t1", 1): [{"evidence_id": evidence_id, "text": "原文"}]}
    answer = await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert f"/api/evidence/{evidence_id}" in answer


@pytest.mark.asyncio
async def test_synthesis_failure_still_returns_an_answer():
    orchestrator, client = build(plan(spec("t1")), finals=[{"answer_md": "", "citations": []}])
    orchestrator.store.sources = {("t1", 1): []}
    answer = await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert answer.strip()
    assert orchestrator.completion_status == "failed"


# ---------------------------------------------------------------- plan review


@pytest.mark.asyncio
async def test_judge_plan_revision_loop_is_bounded_and_applied():
    orchestrator, client = build(
        plan(spec("t1")),
        reviews=[
            {"verdict": "revise", "reasons": ["两年来源应拆开"], "actions": ["拆成两个任务"]},
            {"verdict": "pass", "reasons": [], "actions": []},
        ],
        judge_plan=True,
    )
    orchestrator.plans = None  # unused; keep attribute out of the way
    await orchestrator.run(SCOPE, "对比两年", ModelOptions())
    assert orchestrator.plan_revisions == 1
    assert "critic:judge_plan" in client.calls
    assert client.calls.count("planner") >= 2


@pytest.mark.asyncio
async def test_judge_plan_can_be_switched_off():
    """Switching off plan review must not switch off findings verification."""
    orchestrator, client = build(plan(spec("t1")), judge_plan=False)
    await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert "critic:judge_plan" not in client.calls
    assert "critic:verify_findings" in client.calls


@pytest.mark.asyncio
async def test_terminal_projection_is_owned_by_runner_transaction():
    orchestrator, _ = build(plan(spec("t1")))
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    orchestrator.emit = emit
    await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert orchestrator.final_answer == "最终答复"
    assert not any(
        kind == "state" and data["status"] in {"completed", "failed"} for kind, data in events
    )


@pytest.mark.asyncio
async def test_pause_preserves_pending_tasks_and_does_not_save_false_final():
    from types import SimpleNamespace

    orchestrator, client = build(plan(spec("t1")))
    orchestrator.control = SimpleNamespace(stop=True, reason="pause")
    answer = await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert "暂停" in answer
    assert orchestrator.final_answer is None
    assert orchestrator.tasks["t1"].status == "pending"
    assert "synthesizer" not in client.calls
    orchestrator.control.stop = False
    await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert "worker:t1" in client.calls


@pytest.mark.asyncio
async def test_long_answer_stream_is_complete():
    answer = "完整正文" * 2000
    orchestrator, _ = build(plan(spec("t1")), finals=[{"answer_md": answer}])
    chunks = []

    async def emit(kind, payload):
        if kind == "token":
            chunks.append(payload)

    orchestrator.emit = emit
    assert await orchestrator.run(SCOPE, "目标", ModelOptions()) == answer
    assert "".join(chunk["text"] for chunk in chunks) == answer
    assert len({chunk["message_id"] for chunk in chunks}) == 1


@pytest.mark.asyncio
async def test_context_guard_covers_all_dispatches_including_synthesis_retry():
    orchestrator, client = build(
        plan(spec("t1")),
        finals=[{"answer_md": "[伪造](/api/evidence/foreign)"}, {"answer_md": "修正结果"}],
    )
    guarded = []

    async def guard(scope, role, payload):
        assert scope == SCOPE
        assert payload["current_request"] == "保留预算、部署限制和比较对象"
        guarded.append(role)

    orchestrator.context_guard = guard
    await orchestrator.run(SCOPE, "保留预算、部署限制和比较对象", ModelOptions())
    assert guarded == ["planner", "worker", "critic", "synthesizer", "synthesizer"]
    assert client.calls.count("synthesizer") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at", ["worker", "critic", "synthesizer"])
async def test_context_guard_revocation_aborts_without_fallback_dispatch(revoke_at):
    from agentgpt.host.orchestrator import ContextInvalidatedError

    orchestrator, client = build(plan(spec("t1")))

    async def guard(scope, role, payload):
        if role == revoke_at:
            raise ValueError("memory deleted during run")

    orchestrator.context_guard = guard
    with pytest.raises(ContextInvalidatedError, match="停止下发"):
        await orchestrator.run(SCOPE, "目标", ModelOptions())
    assert not any(call.split(":")[0] == revoke_at for call in client.calls)
    assert orchestrator.final_answer is None


def personal_context():
    return {
        "manifest": {
            "user_id": "alice",
            "thread_id": "thread",
            "context_ids": ["memory:m1:v2", "memory:bob:v1"],
        },
        "context_sources": [
            {
                "context_id": "memory:m1:v2",
                "kind": "user_memory",
                "user_id": "alice",
                "thread_id": "thread",
                "text": "项目代号 XZ-73",
                "quote": "我的项目代号XZ-73",
            },
            {
                "context_id": "memory:bob:v1",
                "kind": "user_memory",
                "user_id": "bob",
                "thread_id": "thread",
                "text": "foreign secret",
            },
        ],
    }


@pytest.mark.asyncio
async def test_personal_memory_has_separate_provenance_without_document_receipts():
    orchestrator, _ = build(
        plan(spec("t1")),
        findings=lambda task_id, attempt: {
            "task_id": task_id,
            "summary": "你此前提供的代号是XZ-73",
            "claims": [{"text": "你此前提供的代号是XZ-73", "context_ids": ["memory:m1:v2"]}],
        },
    )
    await orchestrator.run(SCOPE, "我之前的代号是什么", ModelOptions(), personal_context())
    assert orchestrator.tasks["t1"].status == "completed"
    findings = orchestrator.tasks["t1"].findings
    assert findings["claims"][0]["context_ids"] == ["memory:m1:v2"]
    assert findings["claims"][0]["evidence_ids"] == []
    assert findings["allowed_evidence"] == []
    assert "memory:bob:v1" not in findings["allowed_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    [
        {"context_ids": ["memory:m1:v1"]},  # old version of the selected memory
        {"context_ids": ["memory:forged:v2"]},
        {"context_ids": ["memory:bob:v1"]},  # even an allowlisted foreign owner is rejected
        {"evidence_ids": ["memory:m1:v2"]},  # memory never becomes document evidence
    ],
)
async def test_forged_foreign_stale_or_document_memory_citations_cannot_complete(claim):
    orchestrator, _ = build(
        plan(spec("t1")),
        task_max_retries=0,
        findings=lambda task_id, attempt: {
            "task_id": task_id,
            "summary": "untrusted",
            "claims": [{"text": "untrusted", **claim}],
        },
    )
    await orchestrator.run(SCOPE, "代号", ModelOptions(), personal_context())
    assert orchestrator.tasks["t1"].status == "failed"
    assert orchestrator.tasks["t1"].errors
    assert orchestrator.completion_status == "failed"
