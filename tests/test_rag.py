"""Document versioning, provenance and recovery boundaries with real SQLite/FTS."""

import asyncio
import hashlib
import json

import httpx
import pytest
from langchain_core.messages import ToolMessage
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from agentgpt.config import Settings
from agentgpt.ingestion import parse_version
from agentgpt.retrieval import RetrievalService
from agentgpt.store import Store


async def setup(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, rag_enabled=False)
    store = Store(settings.database)
    await store.initialize()
    return store, RetrievalService(store, settings)


async def upload(store, service, root, text, *, user="alice", kb="", document_id=None):
    digest = hashlib.sha256(text.encode()).hexdigest()
    path = root / digest
    path.write_text(text, encoding="utf-8")
    result = await store.queue_document(
        user, "notes.md", str(path), digest, kb=kb, document_id=document_id
    )
    while await service.process_one():
        pass
    return result


def create_pdf(path):
    writer = PdfWriter()
    for text in (
        "Introduction to the internal project.",
        "Deployment budget: 230000 CNY. Code PINE-82849.",
    ):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


@pytest.mark.asyncio
async def test_versions_switch_atomically_and_old_citations_survive(tmp_path):
    store, service = await setup(tmp_path)
    first = await upload(store, service, tmp_path, "# 项目预算\n预算23万元，代号PINE-82849。")
    result = await service.search("alice", "预算")
    old = result["sources"][0]
    assert result["code"] == "HIT" and old["location"]["section"] == "项目预算"
    queued = await store.queue_document(
        "alice",
        "notes.md",
        None,
        "newhash",
        document_id=first["document_id"],
        legacy_text="预算42万元，代号OAK-67890。",
    )
    assert (await service.search("alice", "预算"))["sources"][0]["version"] == 1
    assert await service.process_one()
    latest = await service.search("alice", "预算")
    assert latest["sources"][0]["version"] == 2
    assert "42万元" in latest["sources"][0]["text"]
    assert "23万元" in (await store.evidence("alice", old["evidence_id"]))["text"]
    assert old["evidence_id"] != latest["sources"][0]["evidence_id"]
    await store.remove_document("alice", first["document_id"])
    assert (await service.search("alice", "预算"))["code"] == "MISS"
    with pytest.raises(ValueError):
        await store.evidence("alice", old["evidence_id"])
    assert queued["id"] != first["id"]


@pytest.mark.asyncio
async def test_user_and_knowledge_base_filters_and_dedup(tmp_path):
    store, service = await setup(tmp_path)
    first = await upload(store, service, tmp_path, "预算ALPHA", kb="项目甲")
    same = await upload(store, service, tmp_path, "预算ALPHA", kb="项目甲")
    assert first["id"] == same["id"]
    await upload(store, service, tmp_path, "预算BETA", kb="项目乙")
    await upload(store, service, tmp_path, "预算BOB", user="bob", kb="项目甲")
    assert (await service.search("alice", "预算"))["sources"] == []
    result = await service.search("alice", "预算", "项目甲")
    assert len(result["sources"]) == 1 and "ALPHA" in result["sources"][0]["text"]
    with pytest.raises(ValueError):
        await store.evidence("bob", result["sources"][0]["evidence_id"])
    with pytest.raises(ValueError):
        await store.remove_document("bob", first["document_id"])


@pytest.mark.asyncio
async def test_pdf_keeps_page_and_legacy_migration_marks_unknown_location(tmp_path):
    store, service = await setup(tmp_path)
    path = tmp_path / "fixture.pdf"
    create_pdf(path)
    await store.queue_document("alice", "fixture.pdf", str(path), "pdfhash")
    await service.process_one()
    source = (await service.search("alice", "PINE-82849"))["sources"][0]
    assert source["location"]["page"] == 2 and "230000" in source["text"]
    legacy_id = await store.add_document("alice", "legacy.pdf", "旧文件预算三万元")
    await store.migrate_documents()
    await service.process_one()
    source = (await service.search("alice", "旧文件"))["sources"][0]
    assert source["document_id"] == legacy_id
    assert source["location"]["location_missing"] is True
    assert "page" not in source["location"]


@pytest.mark.asyncio
async def test_interrupted_job_recovers_and_parse_failure_retries_are_bounded(tmp_path):
    store, service = await setup(tmp_path)
    await store.queue_document(
        "alice", "notes.md", None, "recovery", legacy_text="可恢复预算23万元"
    )
    async with store.connect() as db:
        await db.execute("UPDATE ingestion_jobs SET status='running'")
    await service.start()
    try:
        assert (await service.search("alice", "可恢复"))["code"] == "HIT"
    finally:
        await service.close()
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a PDF")
    queued = await store.queue_document("alice", "bad.pdf", str(bad), "bad")
    for _ in range(3):
        assert await service.process_one()
    assert not await service.process_one()
    async with store.connect() as db:
        row = await (
            await db.execute("SELECT * FROM ingestion_jobs WHERE version_id=?", (queued["id"],))
        ).fetchone()
    assert row["attempts"] == 3 and row["status"] == "failed"


@pytest.mark.asyncio
async def test_evidence_association_replay_citations_and_deletion(tmp_path):
    store, service = await setup(tmp_path)
    doc = await upload(store, service, tmp_path, "项目代号ALPHA，预算23万元。")
    result = await service.search("alice", "预算")
    message = ToolMessage(
        name="builtin__private_search",
        tool_call_id="stable-call",
        content=json.dumps(result, ensure_ascii=False),
    )
    for _ in range(2):
        await store.associate_evidence("alice", "thread", "run", "task", 1, message)
    sources = await store.task_sources("alice", "thread", "task", 1)
    assert len(sources) == 1
    source = sources[0]
    assert "23万元" in source["text"]
    await store.validate_citations(
        "alice", "预算23万元。" + source["citation"], sources, required=True
    )
    with pytest.raises(ValueError, match="缺少"):
        await store.validate_citations("alice", "随意结论", sources, required=True)
    with pytest.raises(ValueError, match="未读取"):
        await store.validate_citations("alice", "[来源](/api/evidence/doc_deadbeef)", sources)
    await store.remove_document("alice", doc["document_id"])
    assert await store.task_sources("alice", "thread", "task", 1) == []


@pytest.mark.asyncio
async def test_web_only_records_returned_excerpt_and_checks_location(tmp_path):
    from agentgpt.web_retrieval import evidence_window

    store, _ = await setup(tmp_path)
    record = await store.save_web_evidence(
        "alice",
        {
            "url": "https://example.org/a",
            "title": "Example",
            "paragraphs": [
                {"paragraph": 1, "page": None, "text": "已读证据"},
                {"paragraph": 2, "page": None, "text": "未读内容"},
            ],
        },
    )
    window = evidence_window(record, 0, 4)
    window["citation"] = f"[来源](/api/evidence/{record['evidence_id']}?offset=0&length=4)"
    message = ToolMessage(
        name="builtin__web_fetch", tool_call_id="read", content=json.dumps(window)
    )
    await store.associate_evidence("alice", "thread", "run", "task", 1, message)
    sources = await store.task_sources("alice", "thread", "task", 1)
    assert sources[0]["text"] == "已读证据"
    assert "未读内容" not in json.dumps(sources, ensure_ascii=False)
    # Reading the same slice twice must not add a second source row for this task.
    await store.associate_evidence("alice", "thread", "run", "task", 1, message)
    again = await store.task_sources("alice", "thread", "task", 1)
    assert [s["evidence_id"] for s in again] == [sources[0]["evidence_id"]]
    with pytest.raises(ValueError, match="范围"):
        await store.validate_citations(
            "alice", window["citation"].replace("offset=0", "offset=5"), sources
        )


@pytest.mark.asyncio
async def test_dense_outage_is_visible_and_lexical_remains_usable(tmp_path):
    store, service = await setup(tmp_path)
    await upload(store, service, tmp_path, "代号PINE-82849")
    service.settings.rag_enabled = True

    async def fail(*args):
        raise RuntimeError("index offline")

    service.index.search = fail
    result = await service.search("alice", "PINE-82849")
    assert result["code"] == "HIT" and result["degraded"] == ["dense_unavailable"]
    result = await service.search("alice", "zzzzz")
    assert result["code"] == "ERROR"


def test_markdown_sections_are_not_lost(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# 部署\n第一节\n## 内存\n需要8GB", encoding="utf-8")
    parts = parse_version({"title": "notes.md", "raw_path": str(path), "legacy_text": None})
    assert parts[1][1]["section"] == "部署 / 内存"


@pytest.mark.asyncio
async def test_dense_candidates_are_rechecked_against_sqlite_scope(tmp_path):
    store, service = await setup(tmp_path)
    await upload(store, service, tmp_path, "苹果预算", kb="甲")
    await upload(store, service, tmp_path, "香蕉预算", user="bob", kb="甲")
    own = (await service.search("alice", "苹果", "甲"))["sources"][0]
    other = (await service.search("bob", "香蕉", "甲"))["sources"][0]
    service.settings.rag_enabled = True

    async def candidates(*args):
        return [{"chunk_id": s["evidence_id"][4:], "score": 0.9} for s in [other, own]]

    service.index.search = candidates
    result = await service.search("alice", "no lexical match", "甲")
    assert [s["evidence_id"] for s in result["sources"]] == [own["evidence_id"]]


@pytest.mark.asyncio
async def test_reranker_reorders_and_invalid_results_explicitly_degrade(tmp_path, monkeypatch):
    store, service = await setup(tmp_path)
    await upload(store, service, tmp_path, "苹果预算")
    await upload(store, service, tmp_path, "香蕉预算")
    original = await service.search("alice", "预算")
    service.settings.rerank_url = "http://reranker.test"
    client_type = httpx.AsyncClient
    responses = [
        {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]},
        {"results": [{"index": 0, "relevance_score": 0.9}]},
    ]

    def client(**kwargs):
        return client_type(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=responses.pop(0))
            )
        )

    monkeypatch.setattr("agentgpt.retrieval.httpx.AsyncClient", client)
    reranked = await service.search("alice", "预算")
    assert reranked["sources"][0]["evidence_id"] == original["sources"][1]["evidence_id"]
    degraded = await service.search("alice", "预算")
    assert degraded["degraded"] == ["reranker_unavailable"]
    assert "rerank_score" not in degraded["sources"][0]


@pytest.mark.asyncio
async def test_enabling_dense_creates_version_without_overwriting_old_chunks(tmp_path):
    from agentgpt.ingestion import chunk_parts

    store, service = await setup(tmp_path)
    await upload(store, service, tmp_path, "项目预算23万元")
    old = (await service.search("alice", "预算"))["sources"][0]
    service.settings.rag_enabled = True

    async def chunks(version_id, parts):
        return chunk_parts(version_id, parts)

    async def upsert(*args):
        pass

    service.index.chunks, service.index.upsert = chunks, upsert
    await service.start()
    try:
        for _ in range(100):
            if (await store.document_listing("alice"))[0]["index_kind"] == "hybrid":
                break
            await asyncio.sleep(0.01)
        latest = (await store.document_listing("alice"))[0]
        assert latest["version"] == 2 and latest["status"] == "ready"
        assert (await store.evidence("alice", old["evidence_id"]))["text"] == old["text"]
    finally:
        await service.close()
