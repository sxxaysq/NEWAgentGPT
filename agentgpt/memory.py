"""Recall before a request; durable fact extraction after its verified answer."""

import asyncio
import hashlib
import json
import logging
import math
import time
from typing import Literal

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from agentgpt.config import ModelOptions
from agentgpt.model import StructuredModel, StructuredOutputError, create_model, structured_response

log = logging.getLogger(__name__)


class Fact(StructuredModel):
    key: str = Field(min_length=1, max_length=100)
    fact: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=500)
    kind: Literal["preference", "profile", "project"]
    entity: str = Field(default="", max_length=150)
    property: str = Field(default="", max_length=100)
    value: str = Field(default="", max_length=300)
    unit: str = Field(default="", max_length=40)
    applicability: str = Field(default="", max_length=200)


class Facts(StructuredModel):
    facts: list[Fact] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=3500)


class Merge(StructuredModel):
    action: Literal["ADD", "UPDATE", "NOOP"]
    target_id: str = ""


class RemoteMemoryEmbedding:
    """Bounded HTTP embeddings; provider identity never shares a local-model collection."""

    def __init__(self, settings):
        self.settings = settings

    def embed(self, texts):
        # UTF-8 bytes conservatively cap a byte-level tokenizer's input. This is
        # an operational guard, not a claim of deployment-tokenizer calibration.
        limit = self.settings.memory_embedding_max_tokens
        if any(len(text.encode("utf-8")) > limit for text in texts):
            raise ValueError("Memory embedding input exceeds conservative byte budget")
        with httpx.Client(
            timeout=self.settings.memory_embedding_timeout_s, trust_env=False
        ) as client:
            response = client.post(
                self.settings.memory_embedding_url,
                json={
                    "model": self.settings.memory_embedding_model,
                    "input": texts,
                    "max_length": limit,
                },
            )
            response.raise_for_status()
            data = response.json()["data"]
        if len(data) != len(texts) or {item["index"] for item in data} != set(range(len(texts))):
            raise ValueError("Incomplete memory embedding response")
        vectors = [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]
        for vector in vectors:
            if (
                len(vector) != self.settings.memory_embedding_dimensions
                or not all(
                    isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
                    for n in vector
                )
                or not any(vector)
            ):
                raise ValueError("Invalid memory embedding vector")
        return vectors

    query_embed = embed


class MemoryIndex:
    def __init__(self, settings, *, path=None):
        self.settings = settings
        self.path = path or settings.data_dir / "qdrant"
        self.client = self.embedder = None
        self.lock = asyncio.Lock()
        if getattr(settings, "memory_embedding_url", ""):
            identity = "|".join(
                str(item)
                for item in (
                    "http",
                    settings.memory_embedding_url,
                    settings.memory_embedding_model,
                    settings.memory_embedding_dimensions,
                    settings.memory_embedding_max_tokens,
                    "v1",
                )
            )
            self.generation = hashlib.sha256(identity.encode()).hexdigest()
            self.collection = "memories_remote_" + self.generation[:16]
        else:
            self.collection = (
                "memories_" + hashlib.sha256(settings.embedding_model.encode()).hexdigest()[:12]
            )
            self.generation = hashlib.sha256(
                ("fastembed|" + settings.embedding_model + "|query_embed|v1").encode()
            ).hexdigest()

    def _initialize_client(self):
        if self.client is not None:
            return
        from qdrant_client import QdrantClient

        self.client = (
            QdrantClient(url=self.settings.qdrant_url, timeout=10)
            if self.settings.qdrant_url
            else QdrantClient(path=str(self.path))
        )

    def _initialize(self):
        if self.embedder is not None:
            return
        from qdrant_client import models

        if getattr(self.settings, "memory_embedding_url", ""):
            embedder = RemoteMemoryEmbedding(self.settings)
            dimensions = self.settings.memory_embedding_dimensions
        else:
            from fastembed import TextEmbedding

            embedder = TextEmbedding(
                self.settings.embedding_model,
                cache_dir=str(self.settings.embedding_cache),
                threads=2,
            )
            dimensions = len(list(embedder.embed(["初始化"]))[0])
        self._initialize_client()
        client = self.client
        if not client.collection_exists(self.collection):
            client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=dimensions, distance=models.Distance.COSINE
                ),
            )
        self.client, self.embedder = client, embedder

    async def _call(self, function):
        async with self.lock:
            pending = asyncio.create_task(asyncio.to_thread(function))
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop its native thread; keep the handle locked.
                await pending
                raise

    async def upsert(self, row):
        from qdrant_client import models

        def work():
            self._initialize()
            if row["deleted"] or row.get("archived", 0):
                self.client.delete(
                    self.collection, models.PointIdsList(points=[row["id"]]), wait=True
                )
                return
            vector = list(self.embedder.embed([row["content"]]))[0]
            vector = vector.tolist() if hasattr(vector, "tolist") else vector
            self.client.upsert(
                self.collection,
                [
                    models.PointStruct(
                        id=row["id"],
                        vector=vector,
                        payload={k: row[k] for k in ("user_id", "scope", "version")},
                    )
                ],
                wait=True,
            )

        await self._call(work)

    async def search(self, user_id, scope, query, limit=20):
        from qdrant_client import models

        def work():
            self._initialize()
            embedded_query = [query] if isinstance(self.embedder, RemoteMemoryEmbedding) else query
            vector = list(self.embedder.query_embed(embedded_query))[0]
            vector = vector.tolist() if hasattr(vector, "tolist") else vector
            results = self.client.query_points(
                self.collection,
                query=vector,
                limit=limit,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_id", match=models.MatchValue(value=user_id)
                        ),
                        models.FieldCondition(
                            key="scope", match=models.MatchAny(any=list({"", scope}))
                        ),
                    ]
                ),
                with_payload=True,
            ).points
            return [
                {
                    "id": str(p.id).replace("-", ""),
                    "score": p.score,
                    "version": p.payload["version"],
                }
                for p in results
            ]

        return await self._call(work)

    async def close(self):
        async with self.lock:
            if self.client:
                await asyncio.to_thread(self.client.close)


class MemoryService:
    def __init__(self, store, settings, *, index=None, model=None):
        self.store, self.settings = store, settings
        self.index = index or MemoryIndex(
            settings,
            path=getattr(settings, "memory_index_dir", None) or settings.data_dir / "memory_qdrant",
        )
        self.model = model
        self.worker = None
        self.wake = asyncio.Event()
        self.closing = False
        self.last_error = ""
        self._recalls = {}
        self._dense_tasks = {}
        self._close_task = None

    def start(self):
        if self.settings.memory_enabled:
            self.worker = asyncio.create_task(self._worker(), name="memory-worker")

    async def close(self):
        self.closing = True
        self.wake.set()
        pending = list(self._recalls.values())
        pending.extend(self._dense_tasks.values())
        if self.worker:
            pending.append(self.worker)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=8)
        if pending:
            # Native operations retain their handle until drained; shutdown never closes
            # Qdrant underneath a still-running thread.
            async def drain():
                await asyncio.gather(*pending, return_exceptions=True)
                await self.index.close()

            self._close_task = asyncio.create_task(drain(), name="memory-index-drain")
            log.warning("Memory shutdown deferred for %d native operations", len(pending))
        else:
            await self.index.close()

    async def enqueue(self, user_id, thread_id, state, scope):
        """Completion jobs are inserted atomically by Store.finish_turn, never from messages."""
        self.wake.set()

    async def sync_index(self):
        generation = getattr(self.index, "generation", None)
        for row in await self.store.pending_memory_index(generation):
            await self.index.upsert(row)
            await self.store.mark_memory_indexed(row["id"], row["version"], generation)

    async def recall(self, user_id, scope, query):
        if not self.settings.memory_enabled or self.closing:
            return []
        pref = await self.store.memory_preference(user_id)
        if not pref["enabled"]:
            return []
        key = (
            user_id,
            scope,
            hashlib.sha256(query.encode()).hexdigest(),
            pref["revision"],
            getattr(self.index, "generation", "legacy"),
        )
        pending = self._recalls.get(key)
        if pending is None:
            if len(self._recalls) >= getattr(self.settings, "memory_recall_max_pending", 2):
                self.last_error = "memory_recall_capacity"
                return []
            pending = asyncio.create_task(
                self._recall(user_id, scope, query, pref["revision"]), name="memory-recall"
            )
            self._recalls[key] = pending

            def cleanup(task):
                self._recalls.pop(key, None)
                if not task.cancelled():
                    task.exception()  # Observe errors even when callers have timed out.

            pending.add_done_callback(cleanup)
        done, _ = await asyncio.wait(
            {pending}, timeout=getattr(self.settings, "memory_recall_timeout_s", 2.0)
        )
        if not done:
            self.last_error = "memory_recall_timeout"
            return []
        result = pending.result()
        current = await self.store.memory_preference(user_id)
        if not current["enabled"] or current["revision"] != pref["revision"]:
            return []
        return result

    async def _rerank(self, query, candidates):
        async with httpx.AsyncClient(
            timeout=getattr(self.settings, "memory_rerank_timeout_s", 8.0), trust_env=False
        ) as client:
            response = await client.post(
                self.settings.rerank_url.rstrip("/") + "/rerank",
                headers={"Authorization": "Bearer " + self.settings.rerank_api_key}
                if self.settings.rerank_api_key
                else {},
                json={
                    "model": self.settings.rerank_model,
                    "query": query,
                    "documents": [c["content"] for c in candidates],
                },
            )
            response.raise_for_status()
            results = response.json()["results"]
        scores = {r["index"]: r["relevance_score"] for r in results}
        if set(scores) != set(range(len(candidates))) or not all(
            isinstance(s, (int, float))
            and not isinstance(s, bool)
            and math.isfinite(s)
            and 0 <= s <= 1
            for s in scores.values()
        ):
            raise ValueError("Incomplete or invalid reranker scores")
        return scores

    async def _search_dense(self, user_id, scope, query, revision, limit=20):
        key = (
            user_id,
            scope,
            hashlib.sha256(query.encode()).hexdigest(),
            revision,
            getattr(self.index, "generation", "legacy"),
            limit,
        )
        task = self._dense_tasks.get(key)
        if task is None:
            if len(self._dense_tasks) >= self.settings.memory_recall_max_pending:
                self.last_error = "memory_dense_capacity"
                return []
            task = asyncio.create_task(
                self.index.search(user_id, scope, query, limit), name="memory-dense"
            )
            self._dense_tasks[key] = task

            def cleanup(done):
                self._dense_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(cleanup)
        done, _ = await asyncio.wait(
            {task}, timeout=getattr(self.settings, "memory_dense_timeout_s", 0.5)
        )
        if not done:
            self.last_error = "memory_dense_timeout"
            return []
        return task.result()

    async def _recall(self, user_id, scope, query, revision):
        rows = {r["id"]: r for r in await self.store.memories(user_id, scope)}
        if not rows:
            return []
        try:
            lexical, exact = await self.store.lexical_memories(user_id, scope, query)
            try:
                hits = await self._search_dense(user_id, scope, query, revision, 20)
            except Exception:
                hits = []
                self.last_error = "memory_dense_unavailable"
            # Union with per-channel quotas (20 + 20 + 24); no early dense-biased cutoff.
            candidates_by_id = {}
            for hit in hits[:20]:
                row = rows.get(hit["id"])
                if row and hit["version"] == row["version"]:
                    candidates_by_id[row["id"]] = {**row, "score": hit["score"], "dense": True}
            exact_ids = {r["id"] for r in exact}
            for row in lexical + exact:
                current = rows.get(row["id"])
                if current and current["version"] == row["version"]:
                    candidate = candidates_by_id.setdefault(row["id"], {**row, "score": 0})
                    candidate["lexical"] = True
                    candidate["exact"] = row["id"] in exact_ids
            candidates = list(candidates_by_id.values())
            fresh_pref = await self.store.memory_preference(user_id)
            if not fresh_pref["enabled"] or fresh_pref["revision"] != revision:
                return []
            fresh_rows = {r["id"]: r for r in await self.store.memories(user_id, scope)}
            candidates = [
                c
                for c in candidates
                if c["id"] in fresh_rows and c["version"] == fresh_rows[c["id"]]["version"]
            ]
            reranked = False
            if candidates and self.settings.rerank_url:
                try:
                    scores = await self._rerank(query, candidates)
                    candidates = [{**c, "score": scores[i]} for i, c in enumerate(candidates)]
                    reranked = True
                except (httpx.HTTPError, KeyError, ValueError):
                    self.last_error = "memory_rerank_unavailable"
                    log.warning("Reranker unavailable; using authorized conservative fallback")
            # Mutations can occur while embedding/reranking is in flight. Recheck authority
            # before returning any content, including pref revision and active version.
            current_pref = await self.store.memory_preference(user_id)
            if not current_pref["enabled"] or current_pref["revision"] != revision:
                return []
            latest = {r["id"]: r for r in await self.store.memories(user_id, scope)}
            candidates = [
                c
                for c in candidates
                if c["id"] in latest and c["version"] == latest[c["id"]]["version"]
            ]
            selected, sessions, chars = [], {}, 0
            for c in candidates:
                if reranked:
                    c["eligible"] = c["score"] >= getattr(
                        self.settings, "memory_rerank_threshold", 0.3
                    )
                    c["rank"] = c["score"]
                else:
                    # Lexical matches have no calibrated semantic score. Only a full
                    # literal query or exact ID can qualify without dense evidence.
                    literal = query.casefold().strip() in c["content"].casefold()
                    c["eligible"] = (
                        c.get("dense") and c["score"] >= self.settings.memory_score_threshold
                    ) or (c.get("lexical") and (literal or c.get("exact")))
                    c["rank"] = max(c["score"], 0.6 if c["eligible"] else 0)
                if c["kind"] == "project":
                    c["rank"] *= math.pow(2, -(time.time() - c["updated"]) / (90 * 86400))
            remaining = [c for c in candidates if c["eligible"]]
            while remaining:
                # Soft diversity, so a third required fact from one input can still win.
                c = max(
                    remaining,
                    key=lambda c: c["rank"] / (1 + 0.1 * sessions.get(c["source_thread"], 0)),
                )
                remaining.remove(c)
                session = c["source_thread"]
                if chars + len(c["content"]) > getattr(
                    self.settings, "memory_recall_char_limit", 6000
                ):
                    continue
                chars += len(c["content"])
                selected.append(
                    {
                        k: c[k]
                        for k in (
                            "id",
                            "version",
                            "content",
                            "kind",
                            "scope",
                            "source_thread",
                            "source_message",
                            "source_quote",
                        )
                    }
                )
                sessions[session] = sessions.get(session, 0) + 1
                if len(selected) >= self.settings.memory_recall_limit:
                    break
            if not await self.store.record_memory_use(user_id, selected, revision):
                return []
            return selected
        except Exception as exc:
            self.last_error = str(exc)[:300]
            log.warning("Memory recall unavailable: %s", exc)
            return []

    async def extract(self, job):
        if not self.settings.memory_enabled or not await self.store.valid_memory_job(job):
            return
        payload = json.loads(job["payload"])
        model = self.model or create_model(
            self.settings, ModelOptions(max_tokens=2000, temperature=0)
        )
        if job["proposals"]:
            result = json.loads(job["proposals"])
        else:
            facts = await structured_response(
                model,
                Facts,
                [
                    SystemMessage(
                        content=(
                            "你是长期记忆提取器。只从用户原话提取明确陈述且可长期复用的身份、偏好、项目约束。"
                            "不把问题、假设、临时指令、助手推测记为事实。每条quote必须逐字引用用户原话。"
                            "key使用稳定主题名；kind为preference/profile/project。项目约束没有scope则不提取。"
                            "保留entity/property/value/unit/applicability，无法明确的字段留空。"
                            "引用文档、示例、条件句和否定陈述不能作为用户自己的身份或事实。"
                            '输出JSON:{"facts":[{"key":"主题","fact":"完整事实","quote":"用户原话",'
                            '"kind":"preference"}]}。无事实时facts=[]。'
                        )
                    ),
                    HumanMessage(
                        content=json.dumps(
                            {"query": payload["query"], "scope": payload.get("scope", "")},
                            ensure_ascii=False,
                        )
                    ),
                ],
            )
            facts = Facts.model_validate(facts.model_dump())
            proposals = []
            existing = await self.store.memories(job["user_id"], payload.get("scope", ""), True)
            for fact in facts.facts:
                if fact.quote not in payload["query"]:
                    continue
                scope = payload.get("scope", "") if fact.kind == "project" else ""
                if fact.kind == "project" and not scope:
                    continue
                relevant = [r for r in existing if r["scope"] == scope]
                # Exact topic lookup includes tombstones before semantic conflict detection.
                exact = next((r for r in relevant if r["fact_key"] == fact.key), None)
                if exact and exact["deleted"]:
                    continue
                try:
                    hits = (
                        await self._search_dense(
                            job["user_id"], scope, fact.fact, job["revision"], 8
                        )
                        if relevant
                        else []
                    )
                except Exception:
                    hits = []  # Exact-topic conflict handling survives vector failure.
                hit_ids = {h["id"] for h in hits}
                candidates = [r for r in relevant if r["id"] in hit_ids or r == exact][:8]
                merge = Merge(action="ADD")
                if candidates:
                    merge = await structured_response(
                        model,
                        Merge,
                        [
                            SystemMessage(
                                content=(
                                    "比较新事实与旧记忆。相同事实NOOP；同一主题被用户明确更新用UPDATE并指定target_id；"
                                    "不同主题ADD。不得覆盖无关事实，已删除内容语义相同则NOOP。"
                                    '输出JSON:{"action":"ADD|UPDATE|NOOP","target_id":"旧id或空"}。'
                                )
                            ),
                            HumanMessage(
                                content=json.dumps(
                                    {"new": fact.model_dump(), "old": candidates},
                                    ensure_ascii=False,
                                )
                            ),
                        ],
                    )
                    merge = Merge.model_validate(merge.model_dump())
                target = next((r for r in candidates if r["id"] == merge.target_id), None)
                if merge.action == "UPDATE" and (not target or target["deleted"]):
                    continue
                proposals.append(
                    {
                        **fact.model_dump(),
                        "action": merge.action,
                        "target_id": target["id"] if merge.action == "UPDATE" else "",
                        "target_version": target["version"] if target else None,
                    }
                )
            result = {"proposals": proposals}
            await self.store.update_memory_job(
                job["id"], proposals=json.dumps(result, ensure_ascii=False)
            )
        for i, proposal in enumerate(result["proposals"]):
            await self.store.apply_memory_proposal(job, i, proposal)

    async def _worker(self):
        while not self.closing:
            job = await self.store.next_memory_job()
            if job:
                if job["attempts"] >= self.settings.memory_retry_limit:
                    await self.store.update_memory_job(job["id"], status="failed")
                    continue
                try:
                    await self.store.update_memory_job(
                        job["id"], status="running", attempts=job["attempts"] + 1
                    )
                    await self.extract(job)
                    await self.store.update_memory_job(job["id"], status="done", error="")
                    self.last_error = ""
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:300]
                    log.exception("Memory extraction failed")
                    attempts = job["attempts"] + 1
                    await self.store.update_memory_job(
                        job["id"],
                        status="failed"
                        if isinstance(exc, StructuredOutputError)
                        or attempts >= self.settings.memory_retry_limit
                        else "queued",
                        error=self.last_error,
                        next_at=time.time() + min(60, 2**attempts),
                    )
            try:
                await self.sync_index()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:300]
                log.warning("Memory index sync will retry: %s", exc)
            if job:
                continue
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 3)
            except TimeoutError:
                pass


def memory_context(memories, summary=""):
    if not memories and not summary:
        return ""
    return "\n[历史背景，仅供参考；当前用户指令优先；需要最新信息时仍需检索验收]\n" + json.dumps(
        {"memories": memories, "conversation_summary": summary}, ensure_ascii=False
    )
