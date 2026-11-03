"""One API-owned Qdrant handle serves memory and document retrieval."""

import asyncio
import json
import logging
import time
from contextlib import suppress
from uuid import uuid4

import httpx

from agentgpt.document_embedding import DocumentEmbedding
from agentgpt.document_store import lexical_tokens
from agentgpt.ingestion import chunk_parts, parse_version
from agentgpt.memory import MemoryIndex
from agentgpt.retrieval_pipeline import expand_query, rerank_sources

log = logging.getLogger(__name__)


class DocumentIndex:
    def __init__(self, shared):
        self.shared = shared
        self.remote = (
            DocumentEmbedding(shared.settings)
            if shared.settings.rag_enabled and shared.settings.document_embedding_url
            else None
        )
        self.collection = (
            self.remote.collection
            if self.remote
            else shared.collection.replace("memories_", "documents_")
        )

    def _initialize(self):
        from qdrant_client import models

        if self.remote:
            self.shared._initialize_client()
        else:
            self.shared._initialize()
        if not self.shared.client.collection_exists(self.collection):
            size = (
                self.shared.settings.document_embedding_dimensions
                if self.remote
                else len(next(self.shared.embedder.embed(["初始化"])))
            )
            self.shared.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
            )
        info = self.shared.client.get_collection(self.collection)
        if (
            self.remote
            and info.config.params.vectors.size
            != self.shared.settings.document_embedding_dimensions
        ):
            raise ValueError("Document vector collection dimension mismatch")

    async def chunks(self, version_id, parts):
        def work():
            from tokenizers import Tokenizer

            self._initialize()
            if self.remote:
                return chunk_parts(version_id, parts, self.remote.tokenizer, 384)
            original = self.shared.embedder.model.tokenizer
            tokenizer = Tokenizer.from_str(original.to_str())
            limit = (tokenizer.truncation or {}).get("max_length", 512)
            tokenizer.no_truncation()
            tokenizer.no_padding()
            return chunk_parts(version_id, parts, tokenizer, min(384, limit - 16))

        return await self.shared._call(work)

    async def upsert(self, version, chunks):
        from qdrant_client import models

        def work():
            self._initialize()
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_str(
                (
                    self.remote.tokenizer if self.remote else self.shared.embedder.model.tokenizer
                ).to_str()
            )
            input_limit = (
                self.shared.settings.document_embedding_max_tokens
                if self.remote
                else (tokenizer.truncation or {}).get("max_length", 512)
            )
            tokenizer.no_truncation()
            tokenizer.no_padding()
            if any(len(tokenizer.encode(c["text"]).ids) > input_limit for c in chunks):
                raise ValueError("已有分块超过Embedding输入上限，请创建新版本重新分块")
            for begin in range(0, len(chunks), 32):
                batch = chunks[begin : begin + 32]
                vectors = (
                    self.remote.embed([c["text"] for c in batch])
                    if self.remote
                    else self.shared.embedder.embed([c["text"] for c in batch])
                )
                self.shared.client.upsert(
                    self.collection,
                    [
                        models.PointStruct(
                            id=c["id"],
                            vector=v if self.remote else v.tolist(),
                            payload={
                                "user_id": version["user_id"],
                                "kb": version["kb"],
                                "document_id": version["document_id"],
                                "version_id": version["id"],
                            },
                        )
                        for c, v in zip(batch, vectors, strict=True)
                    ],
                    wait=True,
                )

        await self.shared._call(work)

    async def delete(self, document_id):
        from qdrant_client import models

        def work():
            self._initialize()
            self.shared.client.delete(
                self.collection,
                models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="document_id", match=models.MatchValue(value=document_id)
                            )
                        ]
                    )
                ),
                wait=True,
            )

        await self.shared._call(work)

    async def search(self, user_id, kb, query, versions):
        from qdrant_client import models

        def work():
            self._initialize()
            vector = (
                self.remote.embed([query])[0]
                if self.remote
                else next(self.shared.embedder.query_embed(query)).tolist()
            )
            result = self.shared.client.query_points(
                self.collection,
                query=vector,
                limit=self.shared.settings.retrieval_dense_k,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_id", match=models.MatchValue(value=user_id)
                        ),
                        models.FieldCondition(key="kb", match=models.MatchValue(value=kb)),
                        models.FieldCondition(
                            key="version_id", match=models.MatchAny(any=versions)
                        ),
                    ]
                ),
                with_payload=False,
            ).points
            return [{"chunk_id": str(p.id).replace("-", ""), "score": p.score} for p in result]

        return await self.shared._call(work)


class RetrievalService:
    def __init__(self, store, settings, shared_index=None):
        self.store, self.settings = store, settings
        self.shared = shared_index or MemoryIndex(settings)
        self.owns_index = shared_index is None
        self.index = DocumentIndex(self.shared)
        self.worker = None
        self.wake = asyncio.Event()
        self.closing = False

    async def start(self):
        await self.store.migrate_documents()
        async with self.store.connect() as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS document_vector_jobs ("
                "collection TEXT,version_id TEXT,status TEXT,attempts INTEGER DEFAULT 0,"
                "error TEXT DEFAULT '',PRIMARY KEY(collection,version_id))"
            )
            await db.execute(
                "UPDATE document_vector_jobs SET status='queued' WHERE status='running'"
            )
            if self.index.remote:
                await db.execute(
                    "INSERT OR IGNORE INTO document_vector_jobs(collection,version_id,status) "
                    "SELECT ?,m.active_version,'queued' FROM document_meta m "
                    "JOIN document_versions v ON v.id=m.active_version "
                    "WHERE m.deleted=0 AND v.status='ready'",
                    (self.index.collection,),
                )
            await db.execute("UPDATE ingestion_jobs SET status='queued' WHERE status='running'")
            if self.settings.rag_enabled:
                rows = await (
                    await db.execute(
                        "SELECT v.* FROM document_versions v JOIN document_meta m "
                        "ON v.id=m.active_version WHERE m.deleted=0 AND v.index_kind='lexical' "
                        "AND v.number=(SELECT max(number) FROM document_versions "
                        "WHERE document_id=v.document_id)"
                    )
                ).fetchall()
                for row in rows:
                    # Changing chunking creates a version; existing evidence IDs stay immutable.
                    new_id = uuid4().hex
                    await db.execute(
                        "INSERT INTO document_versions "
                        "(id,document_id,number,title,content_hash,raw_path,"
                        "legacy_text,status,created) "
                        "VALUES(?,?,?,?,?,?,?,'processing',?)",
                        (
                            new_id,
                            row["document_id"],
                            row["number"] + 1,
                            row["title"],
                            row["content_hash"],
                            row["raw_path"],
                            row["legacy_text"],
                            time.time(),
                        ),
                    )
                    await db.execute(
                        "INSERT INTO ingestion_jobs(version_id,status) VALUES(?,'queued')",
                        (new_id,),
                    )
        # Lexical migration is cheap, keeps legacy test/tool callers immediately usable.
        if not self.settings.rag_enabled:
            while await self.process_one():
                pass
        self.worker = asyncio.create_task(self._worker(), name="document-ingestion")

    async def close(self):
        self.closing = True
        self.wake.set()
        if self.worker:
            await self.worker
        if self.owns_index:
            await self.shared.close()

    async def _worker(self):
        while not self.closing:
            try:
                if await self.process_one():
                    continue
                if self.index.remote and await self.reindex_one():
                    continue
                await self.cleanup_deleted()
            except Exception:
                log.exception("Document ingestion worker failed")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 3)
            except TimeoutError:
                pass

    async def cleanup_deleted(self):
        if not self.settings.rag_enabled:
            return
        async with self.store.connect() as db:
            rows = await (
                await db.execute("SELECT document_id FROM document_meta WHERE deleted=1")
            ).fetchall()
        for row in rows:
            await self.index.delete(row["document_id"])
            async with self.store.connect() as db:
                await db.execute(
                    "UPDATE document_meta SET deleted=2 WHERE document_id=? AND deleted=1",
                    (row["document_id"],),
                )

    async def reindex_one(self):
        """Build a separate vector generation, preserving existing chunks and citations."""
        async with self.store.connect() as db:
            await db.execute(
                "UPDATE document_vector_jobs SET status='cancelled' "
                "WHERE collection=? AND status IN ('queued','running') AND NOT EXISTS "
                "(SELECT 1 FROM document_meta m WHERE m.active_version=version_id AND m.deleted=0)",
                (self.index.collection,),
            )
            row = await (
                await db.execute(
                    "SELECT v.*,d.user_id,m.kb FROM document_vector_jobs j "
                    "JOIN document_versions v ON v.id=j.version_id "
                    "JOIN document_meta m ON m.active_version=v.id "
                    "JOIN documents d ON d.id=v.document_id WHERE j.collection=? "
                    "AND j.status='queued' AND m.deleted=0 LIMIT 1",
                    (self.index.collection,),
                )
            ).fetchone()
            if not row:
                return False
            version = dict(row)
            chunks = [
                dict(c)
                for c in await (
                    await db.execute(
                        "SELECT * FROM document_chunks WHERE version_id=? ORDER BY ordinal",
                        (version["id"],),
                    )
                ).fetchall()
            ]
            await db.execute(
                "UPDATE document_vector_jobs SET status='running',attempts=attempts+1 "
                "WHERE collection=? AND version_id=?",
                (self.index.collection, version["id"]),
            )
        try:
            await self.index.upsert(version, chunks)
            async with self.store.connect() as db:
                await db.execute(
                    "UPDATE document_vector_jobs SET status='ready',error='' "
                    "WHERE collection=? AND version_id=?",
                    (self.index.collection, version["id"]),
                )
        except Exception as exc:
            log.exception("Document reindex failed")
            async with self.store.connect() as db:
                await db.execute(
                    "UPDATE document_vector_jobs SET status=CASE WHEN attempts<3 "
                    "THEN 'queued' ELSE 'failed' END,error=? WHERE collection=? AND version_id=?",
                    (str(exc)[:400], self.index.collection, version["id"]),
                )
        return True

    async def process_one(self):
        async with self.store.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT v.*,d.user_id,m.kb FROM ingestion_jobs j JOIN document_versions v "
                    "ON v.id=j.version_id JOIN documents d ON d.id=v.document_id "
                    "JOIN document_meta m ON m.document_id=d.id "
                    "WHERE j.status='queued' AND m.deleted=0 ORDER BY v.created LIMIT 1"
                )
            ).fetchone()
            if not row:
                return False
            version = dict(row)
            await db.execute(
                "UPDATE ingestion_jobs SET status='running',attempts=attempts+1 WHERE version_id=?",
                (version["id"],),
            )
        try:
            parts = await asyncio.to_thread(parse_version, version)
            chunks = (
                await self.index.chunks(version["id"], parts)
                if self.settings.rag_enabled
                else (chunk_parts(version["id"], parts))
            )
            if self.settings.rag_enabled:
                await self.index.upsert(version, chunks)
            async with self.store.connect() as db:
                await db.execute("BEGIN IMMEDIATE")
                live = await (
                    await db.execute(
                        "SELECT deleted FROM document_meta WHERE document_id=?",
                        (version["document_id"],),
                    )
                ).fetchone()
                if live["deleted"]:
                    return True
                await db.execute(
                    "DELETE FROM document_fts WHERE chunk_id IN "
                    "(SELECT id FROM document_chunks WHERE version_id=?)",
                    (version["id"],),
                )
                await db.execute("DELETE FROM document_chunks WHERE version_id=?", (version["id"],))
                for c in chunks:
                    await db.execute(
                        "INSERT INTO document_chunks VALUES(?,?,?,?,?)",
                        (
                            c["id"],
                            c["version_id"],
                            c["ordinal"],
                            c["text"],
                            json.dumps(c["location"], ensure_ascii=False),
                        ),
                    )
                    await db.execute(
                        "INSERT INTO document_fts VALUES(?,?)",
                        (c["id"], " ".join(lexical_tokens(c["text"]))),
                    )
                await db.execute(
                    "UPDATE document_versions SET status='ready',error='',index_kind=? WHERE id=?",
                    ("hybrid" if self.settings.rag_enabled else "lexical", version["id"]),
                )
                # A retried old version cannot replace a newer ready version.
                await db.execute(
                    "UPDATE document_meta SET active_version=? WHERE document_id=? "
                    "AND coalesce((SELECT number FROM document_versions "
                    "WHERE id=active_version),0)<=?",
                    (version["id"], version["document_id"], version["number"]),
                )
                await db.execute(
                    "UPDATE documents SET title=?,content=? WHERE id=? "
                    "AND EXISTS(SELECT 1 FROM document_meta "
                    "WHERE document_id=? AND active_version=?)",
                    (
                        version["title"],
                        "\n".join(t for t, _ in parts),
                        version["document_id"],
                        version["document_id"],
                        version["id"],
                    ),
                )
                await db.execute(
                    "UPDATE ingestion_jobs SET status='done',error='' WHERE version_id=?",
                    (version["id"],),
                )
                if self.index.remote:
                    await db.execute(
                        "INSERT OR REPLACE INTO document_vector_jobs "
                        "(collection,version_id,status) VALUES(?,?,'ready')",
                        (self.index.collection, version["id"]),
                    )
        except Exception as exc:
            async with self.store.connect() as db:
                await db.execute(
                    "UPDATE ingestion_jobs SET status=CASE WHEN attempts<3 THEN 'queued' "
                    "ELSE 'failed' END,error=? WHERE version_id=? AND status!='cancelled'",
                    (str(exc)[:500], version["id"]),
                )
                await db.execute(
                    "UPDATE document_versions SET status='failed',error=? WHERE id=?",
                    (str(exc)[:500], version["id"]),
                )
        return True

    async def search(self, user_id, query, kb="", limit=6):
        started = time.monotonic()
        variants, usage, degraded = await expand_query(query, self.settings)
        timings = {"rewrite_ms": round((time.monotonic() - started) * 1000, 2)}
        async with self.store.connect() as db:
            versions = [
                r[0]
                for r in await (
                    await db.execute(
                        "SELECT m.active_version FROM document_meta m JOIN "
                        "documents d ON d.id=m.document_id "
                        "WHERE d.user_id=? AND m.kb=? AND m.deleted=0 "
                        "AND m.active_version IS NOT NULL",
                        (user_id, kb),
                    )
                ).fetchall()
            ]
            if self.index.remote:
                ready = {
                    r[0]
                    for r in await (
                        await db.execute(
                            "SELECT version_id FROM document_vector_jobs "
                            "WHERE collection=? AND status='ready'",
                            (self.index.collection,),
                        )
                    ).fetchall()
                }
                if set(versions) - ready:
                    degraded.append("dense_index_pending")
                versions = [v for v in versions if v in ready]
        scores, matches, counts = {}, {}, {"dense": 0, "lexical": 0}
        begin = time.monotonic()
        for variant in variants:
            lexical = await self.store.lexical_search(
                user_id, kb, variant, limit=self.settings.retrieval_lexical_k
            )
            dense = []
            if self.settings.rag_enabled and versions:
                try:
                    dense = await self.index.search(user_id, kb, variant, versions)
                except Exception:
                    log.exception("Dense document retrieval failed")
                    degraded.append("dense_unavailable")
            local = {}
            for channel, ranking in (("lexical", lexical), ("dense", dense)):
                counts[channel] += len(ranking)
                for rank, item in enumerate(ranking, 1):
                    cid = item["chunk_id"]
                    local[cid] = local.get(cid, 0) + 1 / (60 + rank)
                    matches.setdefault(cid, []).append(
                        {"query": variant, "channel": channel, "rank": rank}
                    )
            for cid in sorted(local, key=lambda cid: (-local[cid], cid))[
                : self.settings.retrieval_hybrid_k
            ]:
                scores[cid] = max(scores.get(cid, 0), local[cid])
        timings["recall_ms"] = round((time.monotonic() - begin) * 1000, 2)
        sources = []
        for cid in sorted(scores, key=lambda cid: (-scores[cid], cid)):
            try:
                item = await self.store.read_chunk(user_id, cid, active_only=True)
            except ValueError:
                continue
            if item["kb"] == kb:
                sources.append({**item, "retrieval_score": scores[cid], "matches": matches[cid]})
            if len(sources) >= self.settings.retrieval_candidate_k:
                break
        counts["rerank_candidates"] = len(sources)
        begin = time.monotonic()
        reranked = False
        if self.settings.rerank_url and sources:
            try:
                sources = await rerank_sources(query, sources, self.settings)
                reranked = True
            except (httpx.HTTPError, KeyError, ValueError, TypeError):
                degraded.append("reranker_unavailable")
        timings["rerank_ms"] = round((time.monotonic() - begin) * 1000, 2)
        candidate_scores = [
            {
                "evidence_id": s["evidence_id"],
                "retrieval_score": s["retrieval_score"],
                "rerank_score": s.get("rerank_score"),
                "final_score": s.get("final_score"),
            }
            for s in sources
        ]
        if reranked and self.settings.retrieval_rerank_threshold is not None:
            sources = [
                s for s in sources if s["rerank_score"] >= self.settings.retrieval_rerank_threshold
            ]
        sources = sources[:limit]
        for source in sources:
            source["citation"] = "[来源](/api/evidence/" + source["evidence_id"] + ")"
        if self.settings.retrieval_neighbors and sources:
            sources, _ = await self.expand_neighbors(user_id, kb, sources)
        return {
            "code": "HIT" if sources else ("ERROR" if degraded else "MISS"),
            "sources": sources,
            "degraded": list(dict.fromkeys(degraded)),
            "mode": "hybrid" if self.settings.rag_enabled else "lexical",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "candidate_counts": counts,
            "queries": variants,
            "rewrite_usage": usage,
            "timings": timings,
            "reranked": reranked,
            "candidate_scores": candidate_scores,
            "threshold": self.settings.retrieval_rerank_threshold if reranked else None,
        }

    async def expand_neighbors(self, user_id, kb, sources):
        expanded, seen, tokens = [], set(), 0
        tokenizer = self.index.remote.tokenizer if self.index.remote else None
        for anchor in sources:
            neighbors = await self.store.adjacent_chunks(user_id, kb, anchor["evidence_id"])
            for source in [anchor] + neighbors:
                eid = source["evidence_id"]
                size = (
                    len(tokenizer.encode(source["text"]).ids) if tokenizer else len(source["text"])
                )
                if eid in seen or tokens + size > self.settings.retrieval_context_tokens:
                    continue
                seen.add(eid)
                tokens += size
                expanded.append(dict(source, citation=f"[来源](/api/evidence/{eid})"))
        return expanded, tokens

    async def read(self, user_id, evidence_id, kb=""):
        anchor = await self.store.read_chunk(user_id, evidence_id.removeprefix("doc_"))
        if not evidence_id.startswith("doc_") or anchor["kb"] != kb:
            raise ValueError("Evidence outside the selected knowledge base")
        sources, tokens = await self.expand_neighbors(user_id, kb, [anchor])
        return {
            "code": "HIT" if sources else "MISS",
            "sources": sources,
            "degraded": [],
            "context_tokens": tokens,
        }


class RetrievalBridge:
    """Loopback RPC keeps MCP a true subprocess without opening Qdrant twice."""

    def __init__(self, service, token):
        self.service, self.token = service, token
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=16384)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def handle(self, reader, writer):
        try:
            async with asyncio.timeout(120):
                data = json.loads(await reader.readline())
                import secrets

                if not secrets.compare_digest(data.pop("token", ""), self.token):
                    raise ValueError("Retrieval authentication failed")
                operation = data.pop("operation", "search")
                if operation not in {"search", "read"}:
                    raise ValueError("Unknown retrieval operation")
                result = await getattr(self.service, operation)(**data)
                writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except Exception:
            writer.write(b'{"code":"ERROR","sources":[],"error":"retrieval service failed"}\n')
            with suppress(ConnectionError):
                await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
