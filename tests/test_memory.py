"""Memory provenance, isolation, versioning, deletion and restart semantics."""

import json

import pytest

from agentgpt.config import Settings
from agentgpt.memory import Fact, Facts, MemoryService, Merge
from agentgpt.store import Store


class Index:
    def __init__(self):
        self.rows = {}
        self.stale = []

    async def upsert(self, row):
        self.rows[row["id"]] = dict(row)

    async def search(self, user_id, scope, query, limit=20):
        return (
            self.stale
            or [
                {"id": r["id"], "version": r["version"], "score": 0.95}
                for r in self.rows.values()
                if r["user_id"] == user_id and r["scope"] in {"", scope} and not r["deleted"]
            ][:limit]
        )

    async def close(self):
        pass


class Extractor:
    def __init__(self, fact, quote, action="ADD"):
        self.fact, self.quote, self.action = fact, quote, action
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        owner = self

        class Structured:
            async def ainvoke(self, messages):
                owner.calls += 1
                if schema is Facts:
                    return Facts(
                        facts=[
                            Fact(
                                key="answer_language",
                                fact=owner.fact,
                                quote=owner.quote,
                                kind="preference",
                            )
                        ],
                        summary="使用中文回答",
                    )
                old = json.loads(messages[-1].content)["old"]
                return Merge(action=owner.action, target_id=old[0]["id"])

        return Structured()


async def fixture(tmp_path, fact="用户偏好中文回答", quote="请记住，我偏好中文回答"):
    settings = Settings(_env_file=None, data_dir=tmp_path, memory_enabled=True)
    store = Store(settings.database, memory_enabled=True)
    await store.initialize()
    index = Index()
    model = Extractor(fact, quote)
    service = MemoryService(store, settings, index=index, model=model)
    return store, service, index, model


async def job(store, query="请记住，我偏好中文回答", message="u1", user="alice", scope=""):
    thread = await store.create_thread(user, query, {"memory_scope": scope})
    run, _ = await store.enqueue_run(thread, "start", "", "", message)
    await store.finish_turn(
        run["id"], outcome="completed", assistant_text="好的", memory_enabled=True
    )
    return await store.next_memory_job()


@pytest.mark.asyncio
async def test_extraction_is_idempotent_and_resumes_saved_proposals(tmp_path):
    store, service, index, model = await fixture(tmp_path)
    task = await job(store)
    await service.extract(task)
    await service.sync_index()
    rows = await store.memories("alice")
    assert len(rows) == 1 and rows[0]["source_quote"] == "请记住，我偏好中文回答"
    calls = model.calls
    restarted = MemoryService(store, service.settings, index=index, model=model)
    await restarted.extract(await store.next_memory_job())
    assert model.calls == calls and len(await store.memories("alice")) == 1
    assert (await restarted.recall("alice", "", "回答语言"))[0]["content"] == rows[0]["content"]


@pytest.mark.asyncio
async def test_unquoted_model_claims_do_not_become_memories(tmp_path):
    store, service, _, _ = await fixture(tmp_path, quote="不存在的用户原话")
    await service.extract(await job(store))
    assert await store.memories("alice") == []


@pytest.mark.asyncio
async def test_update_versions_and_stale_index_cannot_return_old_fact(tmp_path):
    store, service, index, model = await fixture(tmp_path)
    first = await job(store)
    await service.extract(first)
    await store.update_memory_job(first["id"], status="done")
    await service.sync_index()
    old = (await store.memories("alice"))[0]
    model.fact, model.quote, model.action = "用户偏好英文回答", "以后使用英文回答", "UPDATE"
    await service.extract(await job(store, model.quote, "u2"))
    new = (await store.memories("alice"))[0]
    assert new["id"] == old["id"] and new["version"] == 2
    # SQLite lexical recall may already return the fresh row while vectors lag;
    # the stale vector must never inject version 1.
    assert all(r["version"] == 2 for r in await service.recall("alice", "", "语言"))
    await service.sync_index()
    assert (await service.recall("alice", "", "语言"))[0]["content"] == model.fact


@pytest.mark.asyncio
async def test_delete_blocks_inflight_jobs_and_stale_vectors(tmp_path):
    store, service, index, _ = await fixture(tmp_path)
    first = await job(store)
    await service.extract(first)
    await store.update_memory_job(first["id"], status="done")
    await service.sync_index()
    old = (await store.memories("alice"))[0]
    pending = await job(store, message="u2")
    await store.edit_memory("alice", "", memory_id=old["id"], expected_version=1, delete=True)
    index.stale = [{"id": old["id"], "version": 1, "score": 1}]
    await service.extract(pending)
    assert await store.memories("alice") == []
    assert await service.recall("alice", "", "中文") == []


@pytest.mark.asyncio
async def test_ownership_scope_and_opt_out(tmp_path):
    store, service, index, _ = await fixture(tmp_path)
    global_id = await store.edit_memory("alice", "中文回答")
    project_id = await store.edit_memory("alice", "项目预算十万", scope="A", kind="project")
    bob_id = await store.edit_memory("bob", "Bob私有事实")
    await service.sync_index()
    index.stale = [{"id": key, "version": 1, "score": 1} for key in (global_id, project_id, bob_id)]
    # Even a faulty/stale vector index is not an authorization boundary.
    assert [r["id"] for r in await service.recall("alice", "B", "资料")] == [global_id]
    with pytest.raises(ValueError):
        await store.edit_memory("bob", "改写", memory_id=global_id, expected_version=1)
    await store.set_memory_preference("alice", False)
    assert await service.recall("alice", "A", "资料") == []
    await store.enqueue_memory("alice", "t", "u", {"query": "新偏好"})
    assert await store.next_memory_job() is None


@pytest.mark.asyncio
async def test_manual_conflict_and_sqlite_reopen(tmp_path):
    store, service, _, _ = await fixture(tmp_path)
    memory_id = await store.edit_memory("alice", "偏好中文")
    await store.edit_memory("alice", "偏好英文", memory_id=memory_id, expected_version=1)
    with pytest.raises(ValueError):
        await store.edit_memory("alice", "旧页面覆盖", memory_id=memory_id, expected_version=1)
    new_store = Store(service.settings.database)
    await new_store.initialize()
    assert (await new_store.memories("alice"))[0]["content"] == "偏好英文"
    assert (await new_store.memories("alice"))[0]["source_quote"] == "偏好英文"


@pytest.mark.asyncio
async def test_memory_management_api_and_failed_job_retry(tmp_path):
    """Drive the management routes directly: the Host no longer owns a run graph.

    The old version booted everything through ``test_followup.application``. Only two
    pieces are actually touched by these routes — the store and a memory worker that
    exposes a wake event — so those are injected instead of starting five agents.
    """
    from httpx import ASGITransport, AsyncClient

    from agentgpt.app import app as api
    from agentgpt.host.runner import NullMemory

    store = Store(Settings(data_dir=tmp_path).database)
    await store.initialize()
    api.state.store = store
    api.state.memory = NullMemory()

    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://host") as client:
        session = await client.post(
            "/api/auth/register", json={"email": "alice@example.com", "password": "secret123"}
        )
        assert session.status_code == 201
        created = await client.post("/api/memories", json={"content": "使用中文"})
        assert created.status_code == 201
        memory_id = created.json()["id"]
        edited = await client.patch(
            f"/api/memories/{memory_id}", json={"content": "使用英文", "version": 1}
        )
        assert edited.status_code == 200
        assert (await client.delete(f"/api/memories/{memory_id}?version=1")).status_code == 409
        assert (await client.delete(f"/api/memories/{memory_id}?version=2")).status_code == 200
        assert (await client.get("/api/memories")).json()["items"] == []
        user = (await client.get("/api/auth/me")).json()
        await store.enqueue_memory(user["id"], "test", "retry", {"query": "使用中文"})
        pending = await store.next_memory_job()
        await store.update_memory_job(pending["id"], status="failed", attempts=3)
        assert (await client.post("/api/memory-jobs/retry")).json() == {"retried": 1}
        assert (await store.next_memory_job())["attempts"] == 0
