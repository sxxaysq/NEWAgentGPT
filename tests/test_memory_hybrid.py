"""Hybrid recall must preserve provenance, authorization and bounded work."""

import asyncio
import json
import time

import pytest
from test_memory import fixture, job

from agentgpt.memory_store import identifiers


async def test_fts_unique_candidate_reaches_reranker_without_other_users(tmp_path):
    store, service, index, _ = await fixture(tmp_path)
    dense = [await store.edit_memory("alice", f"普通偏好 {i}") for i in range(20)]
    target = await store.edit_memory(
        "alice", "项目 KX-9701 的随机口令是桔梗", scope="A", kind="project"
    )
    await store.edit_memory("bob", "项目 KX-9701 的口令 Bob SECRET", scope="A", kind="project")
    await store.edit_memory("alice", "项目 KX-9701 的口令 B SECRET", scope="B", kind="project")
    await service.sync_index()
    index.stale = [{"id": i, "version": 1, "score": 0.9} for i in dense]
    service.settings.rerank_url = "http://unused"
    seen = []

    async def rerank(query, candidates):
        seen.extend(candidates)
        return {i: 0.99 if c["id"] == target else 0.01 for i, c in enumerate(candidates)}

    service._rerank = rerank
    result = await service.recall("alice", "A", "KX-9701")
    assert [r["id"] for r in result] == [target]
    assert len(seen) == 21  # The FTS-only result survives twenty dense candidates.
    assert all("SECRET" not in c["content"] for c in seen)


async def test_exact_id_cannot_override_reranker_relevance_threshold(tmp_path):
    store, service, _, _ = await fixture(tmp_path)
    await store.edit_memory("alice", "代码 ZX-77 是旧测试样例")
    service.settings.rerank_url = "http://unused"

    async def rerank(query, candidates):
        return dict.fromkeys(range(len(candidates)), 0.01)

    service._rerank = rerank
    assert await service.recall("alice", "", "ZX-77") == []


@pytest.mark.parametrize("mutation", ["delete", "disable", "edit"])
async def test_inflight_rerank_revalidates_preferences_and_versions(tmp_path, mutation):
    store, service, _, _ = await fixture(tmp_path)
    memory_id = await store.edit_memory("alice", "代码 ZX-77 随机值青桔")
    await service.sync_index()
    service.settings.rerank_url = "http://unused"

    async def rerank(query, candidates):
        if mutation == "disable":
            await store.set_memory_preference("alice", False)
        else:
            await store.edit_memory(
                "alice",
                "新值蓝莓",
                memory_id=memory_id,
                expected_version=1,
                delete=mutation == "delete",
            )
        return dict.fromkeys(range(len(candidates)), 0.99)

    service._rerank = rerank
    assert await service.recall("alice", "", "ZX-77") == []


async def test_slow_recall_has_deadline_single_flight_and_capacity(tmp_path):
    store, service, index, _ = await fixture(tmp_path)
    await store.edit_memory("alice", "随机用户偏好")
    service.settings.memory_recall_timeout_s = 0.02
    service.settings.memory_recall_max_pending = 1
    gate = asyncio.Event()
    calls = 0

    async def search(*args):
        nonlocal calls
        calls += 1
        await gate.wait()
        return []

    index.search = search
    start = time.monotonic()
    assert await service.recall("alice", "", "一") == []
    assert time.monotonic() - start < 0.5
    assert await service.recall("alice", "", "一") == []
    assert calls == 1
    assert await service.recall("alice", "", "二") == []
    assert service.last_error == "memory_recall_capacity"
    assert len(service._recalls) == 1
    pending = list(service._recalls.values())
    gate.set()
    await asyncio.gather(*pending)
    await asyncio.sleep(0)
    assert not service._recalls
    await service.close()


async def test_same_thread_third_fact_is_not_lost_and_hits_are_recorded(tmp_path):
    store, service, _, _ = await fixture(tmp_path)
    for fact in ("预算十八万元", "部署只在内网", "截止日期十一月三日"):
        await store.edit_memory("alice", fact)
    async with store.connect() as db:
        await db.execute("UPDATE memories SET source_thread='one-session'")
    await service.sync_index()
    assert len(await service.recall("alice", "", "约束")) == 3
    assert all(row["hits"] == 1 for row in await store.memories("alice"))


async def test_index_generation_change_rebuilds_current_versions(tmp_path):
    store, service, index, _ = await fixture(tmp_path)
    memory_id = await store.edit_memory("alice", "偏好中文")
    index.generation = "generation-a"
    await service.sync_index()
    assert await store.pending_memory_index("generation-a") == []
    assert [r["id"] for r in await store.pending_memory_index("generation-b")] == [memory_id]
    index.generation = "generation-b"
    index.rows.clear()
    await service.sync_index()
    assert memory_id in index.rows
    assert await store.pending_memory_index("generation-b") == []


async def test_memory_extraction_never_writes_conversation_summary(tmp_path):
    store, service, _, _ = await fixture(tmp_path)

    async def forbidden(*args):
        raise AssertionError("Long-term extraction must not mutate conversation summaries")

    store.save_summary = forbidden
    await service.extract(await job(store))
    assert len(await store.memories("alice")) == 1


async def test_disabled_pending_job_skips_model_before_submission(tmp_path):
    store, service, _, model = await fixture(tmp_path)
    pending = await job(store)
    await store.set_memory_preference("alice", False)
    await service.extract(pending)
    assert model.calls == 0
    assert await store.memories("alice") == []


async def test_fts_punctuation_and_short_words_are_literal_and_scoped(tmp_path):
    store, _, _, _ = await fixture(tmp_path)
    target = await store.edit_memory("alice", "中文偏好，项目AB-12、AB12及xy_99")
    await store.edit_memory("bob", "中文偏好，项目AB-12")
    rows, _ = await store.lexical_memories("alice", "", "中文")
    assert [r["id"] for r in rows] == [target]
    assert (await store.lexical_memories("alice", "", '%_" OR *')) == ([], [])
    assert {x[0] for x in identifiers("项目AB-12、AB12及xy_99")} == {"ab-12", "ab12", "xy_99"}


async def test_schema_reentry_preserves_existing_rows_and_identifier_lookup(tmp_path):
    store, _, _, _ = await fixture(tmp_path)
    memory_id = await store.edit_memory("alice", "随机编号 QW-93")
    await store.initialize()
    await store.initialize()
    _, exact = await store.lexical_memories("alice", "", "QW-93")
    assert [r["id"] for r in exact] == [memory_id]
    assert (await store.memories("alice"))[0]["version"] == 1


async def test_completed_turn_contract_extracts_only_original_user_input(tmp_path):
    store, service, _, _ = await fixture(tmp_path)
    store.memory_enabled = True
    thread = await store.create_thread("alice", "请记住，我偏好中文回答", {})
    run, _ = await store.enqueue_run(thread, "start", "", "", "request-1")
    turn = await store.finish_turn(
        run["id"], outcome="completed", assistant_text="助手杜撰内容", memory_enabled=True
    )
    pending = await store.next_memory_job()
    payload = json.loads(pending["payload"])
    assert payload["turn_id"] == turn["id"]
    assert payload["query"] == thread["goal"]
    await service.extract(pending)
    memory = (await store.memories("alice"))[0]
    assert memory["source_message"] == payload["source_input_id"]
    assert memory["source_quote"] == thread["goal"]


@pytest.mark.parametrize("change", ["query", "scope", "source_revision", "source_input_id"])
async def test_corrupted_turn_job_is_rejected_before_model(tmp_path, change):
    store, service, _, model = await fixture(tmp_path)
    store.memory_enabled = True
    thread = await store.create_thread("alice", "请记住，我偏好中文回答", {})
    run, _ = await store.enqueue_run(thread, "start", "", "", "request-1")
    await store.finish_turn(
        run["id"], outcome="completed", assistant_text="好的", memory_enabled=True
    )
    pending = await store.next_memory_job()
    payload = json.loads(pending["payload"])
    payload[change] = "tampered"
    pending["payload"] = json.dumps(payload)
    await service.extract(pending)
    assert model.calls == 0
    assert await store.memories("alice") == []


async def test_hung_dense_does_not_block_authorized_fts_fallback(tmp_path):
    store, service, index, _ = await fixture(tmp_path)
    memory_id = await store.edit_memory("alice", "项目 QT-673 的凭证是随机值")
    service.settings.memory_dense_timeout_s = 0.01
    gate = asyncio.Event()

    async def blocked(*args):
        await gate.wait()
        return []

    index.search = blocked
    try:
        result = await service.recall("alice", "", "QT-673")
        assert [r["id"] for r in result] == [memory_id]
        assert len(service._dense_tasks) == 1
        # Different queries cannot create an unbounded queue behind a stuck index.
        for number in range(4):
            await service.recall("alice", "", f"QT-673 details {number}")
        assert len(service._dense_tasks) <= service.settings.memory_recall_max_pending
    finally:
        pending = list(service._dense_tasks.values())
        gate.set()
        await asyncio.gather(*pending)
        await service.close()


async def test_http_memory_embedding_uses_distinct_collection_and_validates_vectors(
    tmp_path, monkeypatch
):
    import httpx

    import agentgpt.memory as memory_module

    store, service, _, _ = await fixture(tmp_path)
    settings = service.settings
    settings.memory_embedding_url = "http://embedding.test/v1/embeddings"
    settings.memory_embedding_dimensions = 2
    requests = []
    invalid = False

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0] if invalid else [1.0, 0.0]}
                    for i, _ in enumerate(body["input"])
                ]
            },
        )

    original_client = httpx.Client

    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(
        memory_module.httpx,
        "Client",
        MockClient,
    )
    index = memory_module.MemoryIndex(settings, path=tmp_path / "http-memory-index")
    assert index.collection.startswith("memories_remote_")
    memory_id = await store.edit_memory("alice", "项目 QT-673 随机值")
    row = (await store.memories("alice"))[0]
    try:
        await index.upsert(row)
        hits = await index.search("alice", "", "QT-673")
        assert hits[0]["id"] == memory_id
        assert len(requests) == 2
        assert requests[0]["model"] == settings.memory_embedding_model
        invalid = True
        with pytest.raises(ValueError, match="Invalid memory embedding"):
            await index.search("alice", "", "QT-673")
    finally:
        await index.close()


async def test_manual_project_memory_requires_explicit_scope(tmp_path):
    store, _, _, _ = await fixture(tmp_path)
    with pytest.raises(ValueError, match="项目范围"):
        await store.edit_memory("alice", "项目预算", kind="project")


async def test_legacy_job_without_turn_provenance_never_reaches_extractor(tmp_path):
    store, service, _, model = await fixture(tmp_path)
    await store.enqueue_memory(
        "alice",
        "legacy-thread",
        "legacy-message",
        {"query": "请记住，我偏好中文回答", "answer": "好的", "scope": ""},
    )
    pending = await store.next_memory_job()
    assert not await store.valid_memory_job(pending)
    await service.extract(pending)
    assert model.calls == 0
    assert await store.memories("alice") == []


async def test_disabled_source_turn_cannot_be_requeued_after_enabling(tmp_path):
    store, service, _, model = await fixture(tmp_path)
    await store.set_memory_preference("alice", False)
    thread = await store.create_thread("alice", "请记住，我偏好中文回答", {})
    run, _ = await store.enqueue_run(thread, "start", "", "", "disabled-source")
    turn = await store.finish_turn(
        run["id"], outcome="completed", assistant_text="好的", memory_enabled=True
    )
    snapshot = await store.context_snapshot("alice", thread["id"], 999)
    source = snapshot["inputs"][0]
    await store.set_memory_preference("alice", True)
    await store.enqueue_memory(
        "alice",
        thread["id"],
        source["id"],
        {
            "query": source["text"],
            "scope": "",
            "turn_id": turn["id"],
            "source_input_id": source["id"],
            "source_revision": turn["turn_revision"],
        },
    )
    pending = await store.next_memory_job()
    assert not await store.valid_memory_job(pending)
    await service.extract(pending)
    assert model.calls == 0
    assert await store.memories("alice") == []
