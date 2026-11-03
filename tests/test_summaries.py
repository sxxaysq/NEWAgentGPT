import asyncio
import json

import pytest

from agentgpt.config import Settings
from agentgpt.host.session_state import apply_proposal
from agentgpt.host.summaries import SummaryService
from agentgpt.store import Store


async def fixture(tmp_path, proposer=None):
    store = Store(tmp_path / "state.sqlite3", memory_enabled=False)
    await store.initialize()
    thread = await store.create_thread("alice", "预算23万，仅内网部署", {})
    service = SummaryService(store, Settings(), proposer=proposer or propose)
    return store, thread, service


async def propose(kind, snapshot):
    source = snapshot["source"]
    if kind == "state":
        text = source["text"]
        if "23万" in text:
            return {
                "constraints": [
                    dict(entity="项目", property="预算", value="23万", quote="预算23万"),
                    dict(entity="项目", property="部署", value="内网", quote="仅内网部署"),
                ]
            }
        if "18万" in text:
            old = next(c for c in snapshot["previous"]["constraints"] if c["property"] == "预算")
            return {
                "constraints": [
                    dict(
                        entity="项目",
                        property="预算",
                        value="18万",
                        quote="预算改为18万",
                        supersedes=old["constraint_id"],
                    )
                ]
            }
        return {}
    return {"content": source["user_text"], "quotes": [source["user_text"]]}


async def drain(service):
    for _ in range(30):
        if not await service.process_one():
            return
    raise AssertionError("jobs did not drain")


@pytest.mark.asyncio
async def test_memory_off_still_updates_state_summaries_and_boundaries(tmp_path):
    store, thread, service = await fixture(tmp_path)
    first, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(first["id"], outcome="completed", assistant_text="安排好了")
    second, _ = await store.enqueue_run(thread, "followup", "预算改为18万，其他不变", None)
    await store.finish_turn(second["id"], outcome="completed", assistant_text="已更新")
    # Immediate follow-up while no projection worker has run sees raw updates.
    pending = await store.context_snapshot("alice", thread["id"], 2)
    assert pending["session"] == {}
    assert pending["inputs"][-1]["text"] == "预算改为18万，其他不变"
    await drain(service)
    current = await store.context_snapshot("alice", thread["id"], 2)
    active = [c for c in current["session"]["constraints"] if c["status"] == "active"]
    assert {c["value"] for c in active} == {"18万", "内网"}
    assert current["summary"]["covered_until_turn_seq"] == 1
    assert current["session"]["covered_until_input_seq"] == 1
    past = await store.context_snapshot("alice", thread["id"], 1)
    assert past["session"]["covered_until_input_seq"] == 0
    assert "18万" not in json.dumps(past, ensure_ascii=False)
    assert await store.next_memory_job() is None


@pytest.mark.asyncio
async def test_failed_state_job_does_not_block_summaries_or_skip_coverage(tmp_path):
    async def failing(kind, snapshot):
        if kind == "state":
            raise RuntimeError("state model unavailable")
        return await propose(kind, snapshot)

    store, thread, service = await fixture(tmp_path, failing)
    first, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(first["id"], outcome="failed", assistant_text="执行失败")
    await store.enqueue_run(thread, "followup", "下一问", None)
    await drain(service)
    result = await store.context_snapshot("alice", thread["id"], 2)
    assert result["session"] == {}
    assert result["summary"]["covered_until_turn_seq"] == 0
    async with store.connect() as db:
        state_jobs = await (
            await db.execute(
                "SELECT attempts FROM context_jobs WHERE job_kind='state' ORDER BY target_seq"
            )
        ).fetchall()
    assert [r[0] for r in state_jobs] == [1, 0]


@pytest.mark.asyncio
async def test_stale_generated_summary_cannot_replace_newer_view(tmp_path):
    store, thread, service = await fixture(tmp_path)
    first, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(first["id"], outcome="completed", assistant_text="安排好了")
    await drain(service)
    original = await store.summary(thread["id"])
    # An old retry has no ownership of a done job and cannot overwrite the view.
    async with store.connect() as db:
        job = dict(
            await (
                await db.execute("SELECT * FROM context_jobs WHERE job_kind='thread_summary'")
            ).fetchone()
        )
    with pytest.raises(ValueError, match="contiguous"):
        await service._snapshot(job)
    assert await store.summary(thread["id"]) == original


def test_invalid_supersede_is_conflict_not_second_active_constraint():
    previous = {
        "constraints": [
            dict(constraint_id="c1", entity="项目", property="预算", value="23万", status="active")
        ]
    }
    result = apply_proposal(
        previous,
        {
            "constraints": [
                dict(
                    entity="项目", property="预算", value="18万", quote="18万", supersedes="missing"
                )
            ]
        },
        {"id": "input2", "text": "改为18万"},
    )
    assert len(result["constraints"]) == 1
    assert result["open_questions"]
    assert previous["constraints"][0]["value"] == "23万"
    with pytest.raises(ValueError, match="exact user source"):
        apply_proposal(
            previous,
            {"constraints": [dict(entity="项目", property="预算", value="18万", quote="伪造来源")]},
            {"id": "input2", "text": "改为18万"},
        )


@pytest.mark.asyncio
async def test_temporary_scope_expires_but_explicit_followup_can_carry_once(tmp_path):
    async def scoped(kind, snapshot):
        if kind == "state":
            return {
                "constraints": [
                    dict(
                        entity="输出",
                        property="语言",
                        value="英文",
                        scope="turn",
                        quote="这次用英文",
                    ),
                    dict(
                        entity="项目",
                        property="部署",
                        value="内网",
                        scope="project",
                        quote="仅内网部署",
                    ),
                ]
            }
        return await propose(kind, snapshot)

    store, _, service = await fixture(tmp_path, scoped)
    thread = await store.create_thread("alice", "仅内网部署，这次用英文", {})
    row, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(row["id"], outcome="completed", assistant_text="English answer")
    await drain(service)
    current, _ = await store.enqueue_run(thread, "followup", "预算改成18万，其他不变", None)
    snapshot = await store.context_snapshot("alice", thread["id"], 1)
    active = [c for c in snapshot["session"]["constraints"] if c["status"] == "active"]
    assert {c["value"] for c in active} == {"英文", "内网"}
    assert (
        next(c for c in active if c["value"] == "英文")["applies_to_turn_id"]
        == (await store.turn_for_run(current["id"]))["id"]
    )
    await store.enqueue_run(thread, "followup", "请说明新的设计", None)
    next_snapshot = await store.context_snapshot("alice", thread["id"], 2)
    active = [c for c in next_snapshot["session"]["constraints"] if c["status"] == "active"]
    assert [c["value"] for c in active] == ["内网"]


@pytest.mark.asyncio
async def test_legacy_summary_is_not_injected_even_from_version_history(tmp_path):
    store, thread, service = await fixture(tmp_path)
    # This old schema row has no authenticated ownership/coverage.
    async with store.connect() as db:
        await db.execute(
            "INSERT INTO conversation_summaries(thread_id,source_message,content,updated) "
            "VALUES(?,'legacy','UNOWNED HISTORY',0)",
            (thread["id"],),
        )
    first, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(first["id"], outcome="completed", assistant_text="答复")
    await drain(service)
    await store.enqueue_run(thread, "followup", "下一问", None)
    past = await store.context_snapshot("alice", thread["id"], 0)
    assert past["summary"] == {}
    assert "UNOWNED HISTORY" not in json.dumps(past)


@pytest.mark.asyncio
async def test_shutdown_after_commit_does_not_requeue_completed_projection(tmp_path):
    store, thread, service = await fixture(tmp_path)
    await store.enqueue_run(thread, "start", "", None)
    original_commit = service._commit

    async def commit_then_cancel(*args):
        await original_commit(*args)
        raise asyncio.CancelledError()

    service._commit = commit_then_cancel
    with pytest.raises(asyncio.CancelledError):
        await service.process_one()
    async with store.connect() as db:
        job = await (await db.execute("SELECT * FROM context_jobs")).fetchone()
    assert job["status"] == "done"
    assert (await store.context_snapshot("alice", thread["id"], 1))["session"]["version"] == 1


@pytest.mark.asyncio
async def test_paused_gap_waits_without_exhausting_summary_retries(tmp_path):
    store, thread, service = await fixture(tmp_path)
    first, _ = await store.enqueue_run(thread, "start", "", None)
    await store.finish_turn(first["id"], outcome="paused", assistant_text="暂停")
    second, _ = await store.enqueue_run(thread, "followup", "新的问题", None)
    await store.finish_turn(second["id"], outcome="completed", assistant_text="新回答")
    await drain(service)
    async with store.connect() as db:
        pending = await (
            await db.execute("SELECT * FROM context_jobs WHERE job_kind='thread_summary'")
        ).fetchone()
    assert pending["status"] == "queued" and pending["attempts"] == 0
    await store.enqueue_run(thread, "stop", "", None)
    await drain(service)
    snapshot = await store.context_snapshot("alice", thread["id"], 2)
    assert snapshot["summary"]["covered_until_turn_seq"] == 1
