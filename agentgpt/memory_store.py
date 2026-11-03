"""Authoritative memories, tombstones and restartable extraction jobs."""

import json
import re
import time
import unicodedata
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_preferences (
 user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
 revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS memories (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, scope TEXT NOT NULL,
 fact_key TEXT NOT NULL, content TEXT NOT NULL, kind TEXT NOT NULL,
 source_thread TEXT NOT NULL, source_message TEXT NOT NULL, source_quote TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 indexed_version INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(user_id,scope,fact_key));
CREATE TABLE IF NOT EXISTS memory_versions (
 memory_id TEXT NOT NULL, version INTEGER NOT NULL, record TEXT NOT NULL,
 PRIMARY KEY(memory_id,version));
CREATE TABLE IF NOT EXISTS memory_jobs (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 source_message TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', proposals TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(user_id,thread_id,source_message));
CREATE TABLE IF NOT EXISTS memory_operations (
 job_id TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(job_id,position));
CREATE TABLE IF NOT EXISTS conversation_summaries (
 thread_id TEXT PRIMARY KEY, source_message TEXT NOT NULL,
 content TEXT NOT NULL, updated REAL NOT NULL);
"""


def identifiers(text):
    """Preserve separators: AB-12 and AB12 must never collapse to one identifier."""
    return {
        (unicodedata.normalize("NFKC", value).casefold(), value)
        for value in re.findall(
            r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*", text
        )
        if any(c.isdigit() for c in value) or "-" in value or "_" in value
    }


class MemoryStore:
    async def initialize_memory_schema(self, db):
        columns = {r[1] for r in await (await db.execute("PRAGMA table_info(memories)")).fetchall()}
        for name, definition in {
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "archive_reason": "TEXT NOT NULL DEFAULT ''",
            "last_used": "REAL",
            "hits": "INTEGER NOT NULL DEFAULT 0",
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        await db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, content='memories', content_rowid='rowid', tokenize='trigram');
            CREATE TABLE IF NOT EXISTS memory_identifiers (
                memory_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'code',
                normalized TEXT NOT NULL, original TEXT NOT NULL,
                PRIMARY KEY(memory_id,kind,normalized,original));
            CREATE INDEX IF NOT EXISTS memory_identifiers_lookup
                ON memory_identifiers(normalized,memory_id);
            CREATE TABLE IF NOT EXISTS memory_index_versions (
                memory_id TEXT NOT NULL, generation TEXT NOT NULL, version INTEGER NOT NULL,
                PRIMARY KEY(memory_id,generation));
            CREATE TABLE IF NOT EXISTS memory_migrations (
                name TEXT PRIMARY KEY, applied REAL NOT NULL);
            CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid,content) VALUES(new.rowid,new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts,rowid,content)
                    VALUES('delete',old.rowid,old.content);
                DELETE FROM memory_identifiers WHERE memory_id=old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_update
                AFTER UPDATE OF content ON memories BEGIN
                INSERT INTO memories_fts(memories_fts,rowid,content)
                    VALUES('delete',old.rowid,old.content);
                INSERT INTO memories_fts(rowid,content) VALUES(new.rowid,new.content);
            END;
        """)
        migrated = await (
            await db.execute("SELECT 1 FROM memory_migrations WHERE name='hybrid_v3'")
        ).fetchone()
        if not migrated:
            await db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            rows = await (await db.execute("SELECT id,content FROM memories")).fetchall()
            for row in rows:
                await self._sync_identifiers(db, row["id"], row["content"])
            await db.execute("INSERT INTO memory_migrations VALUES('hybrid_v3',?)", (time.time(),))

    async def _sync_identifiers(self, db, memory_id, content):
        await db.execute("DELETE FROM memory_identifiers WHERE memory_id=?", (memory_id,))
        await db.executemany(
            "INSERT INTO memory_identifiers(memory_id,normalized,original) VALUES(?,?,?)",
            [(memory_id, normalized, original) for normalized, original in identifiers(content)],
        )

    async def lexical_memories(self, user_id, scope, query, limit=20):
        """Every branch filters authorization and active state before LIMIT."""
        tokens = re.findall(r"[\w-]+", unicodedata.normalize("NFKC", query))
        terms = [t for t in tokens if len(t) >= 3][:24]
        async with self.connect() as db:
            if terms:
                expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
                rows = await (
                    await db.execute(
                        "SELECT m.*,bm25(memories_fts) lexical_rank FROM memories_fts "
                        "JOIN memories m ON m.rowid=memories_fts.rowid "
                        "WHERE memories_fts MATCH ? AND m.user_id=? AND m.scope IN ('',?) "
                        "AND m.deleted=0 AND m.archived=0 ORDER BY lexical_rank LIMIT ?",
                        (expression, user_id, scope, min(limit, 20)),
                    )
                ).fetchall()
            else:
                # Short tokens cannot use trigram. Bound the authorized pool before LIKE.
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = (
                    await (
                        await db.execute(
                            "WITH authorized AS (SELECT * FROM memories WHERE user_id=? "
                            "AND scope IN ('',?) AND deleted=0 AND archived=0 "
                            "ORDER BY updated DESC LIMIT 200) SELECT * FROM authorized "
                            "WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                            (user_id, scope, "%" + escaped + "%", min(limit, 20)),
                        )
                    ).fetchall()
                    if query.strip()
                    else []
                )
            exact = []
            for normalized, _ in sorted(identifiers(query))[:24]:
                exact.extend(
                    await (
                        await db.execute(
                            "SELECT m.* FROM memory_identifiers i "
                            "JOIN memories m ON m.id=i.memory_id "
                            "WHERE i.normalized=? AND m.user_id=? AND m.scope IN ('',?) "
                            "AND m.deleted=0 AND m.archived=0 ORDER BY m.updated DESC LIMIT 24",
                            (normalized, user_id, scope),
                        )
                    ).fetchall()
                )
        return [dict(r) for r in rows], list({r["id"]: dict(r) for r in exact}.values())[:24]

    async def memory_preference(self, user_id):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM memory_preferences WHERE user_id=?",
                    (user_id,),
                )
            ).fetchone()
        return dict(row) if row else {"user_id": user_id, "enabled": 1, "revision": 0}

    async def set_memory_preference(self, user_id, enabled):
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO memory_preferences(user_id,enabled,revision) VALUES(?,?,1) "
                "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,revision=revision+1",
                (user_id, int(enabled)),
            )

    async def memories(self, user_id, scope=None, include_deleted=False):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM memories WHERE user_id=?"
                    + (" AND scope IN ('',?)" if scope is not None else "")
                    + ("" if include_deleted else " AND deleted=0 AND archived=0")
                    + " ORDER BY updated DESC",
                    (user_id, scope) if scope is not None else (user_id,),
                )
            ).fetchall()
        return [dict(r) for r in rows]

    async def edit_memory(
        self,
        user_id,
        content,
        *,
        memory_id=None,
        scope="",
        kind="preference",
        expected_version=None,
        delete=False,
    ):
        if not memory_id and kind == "project" and not scope.strip():
            raise ValueError("项目记忆必须指定非空项目范围。")
        now = time.time()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            old = None
            if memory_id:
                old = await (
                    await db.execute(
                        "SELECT * FROM memories WHERE id=? AND user_id=?",
                        (memory_id, user_id),
                    )
                ).fetchone()
                if not old:
                    raise ValueError("记忆不存在。")
                if expected_version != old["version"]:
                    raise ValueError("记忆已更新，请刷新后重试。")
                await db.execute(
                    "INSERT INTO memory_versions VALUES(?,?,?)",
                    (memory_id, old["version"], json.dumps(dict(old))),
                )
                await db.execute(
                    "UPDATE memories SET content=?,version=version+1,deleted=?,"
                    "archived=0,updated=? "
                    "WHERE id=?",
                    (content or old["content"], int(delete), now, memory_id),
                )
                if not delete:
                    await db.execute(
                        "UPDATE memories SET source_thread='',source_message='',source_quote=? "
                        "WHERE id=?",
                        (content, memory_id),
                    )
            else:
                memory_id = uuid4().hex
                await db.execute(
                    "INSERT INTO memories(id,user_id,scope,fact_key,content,kind,source_thread,"
                    "source_message,source_quote,created,updated) VALUES(?,?,?,?,?,?,'','',?,?,?)",
                    (
                        memory_id,
                        user_id,
                        scope,
                        "manual:" + memory_id,
                        content,
                        kind,
                        content,
                        now,
                        now,
                    ),
                )
            # A user's edit invalidates in-flight automatic proposals based on an older view.
            if content:
                await self._sync_identifiers(db, memory_id, content)
            await db.execute(
                "INSERT INTO memory_preferences(user_id,revision) VALUES(?,1) "
                "ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
                (user_id,),
            )
        return memory_id

    async def enqueue_memory(self, user_id, thread_id, source_message, payload):
        now = time.time()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            pref = await (
                await db.execute(
                    "SELECT * FROM memory_preferences WHERE user_id=?",
                    (user_id,),
                )
            ).fetchone()
            if pref and not pref["enabled"]:
                return
            await db.execute(
                "INSERT OR IGNORE INTO memory_jobs(id,user_id,thread_id,source_message,payload,"
                "revision,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    user_id,
                    thread_id,
                    source_message,
                    json.dumps(payload, ensure_ascii=False),
                    pref["revision"] if pref else 0,
                    now,
                    now,
                ),
            )

    async def next_memory_job(self):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM memory_jobs WHERE status IN ('queued','running') AND next_at<=? "
                    "ORDER BY created LIMIT 1",
                    (time.time(),),
                )
            ).fetchone()
        return dict(row) if row else None

    async def update_memory_job(self, job_id, **fields):
        if not set(fields) <= {"status", "proposals", "attempts", "error", "next_at"}:
            raise ValueError("Invalid memory job fields")
        fields["updated"] = time.time()
        async with self.connect() as db:
            await db.execute(
                "UPDATE memory_jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
                (*fields.values(), job_id),
            )

    async def apply_memory_proposal(self, job, position, proposal):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            done = await (
                await db.execute(
                    "SELECT 1 FROM memory_operations WHERE job_id=? AND position=?",
                    (job["id"], position),
                )
            ).fetchone()
            if done:
                return
            await db.execute("INSERT INTO memory_operations VALUES(?,?)", (job["id"], position))
            pref = await (
                await db.execute(
                    "SELECT * FROM memory_preferences WHERE user_id=?",
                    (job["user_id"],),
                )
            ).fetchone()
            if pref and (not pref["enabled"] or pref["revision"] != job["revision"]):
                return
            if proposal["action"] == "NOOP":
                return
            payload = json.loads(job["payload"])
            if not await self._memory_source_valid(db, job, payload):
                return
            scope = payload.get("scope", "") if proposal["kind"] == "project" else ""
            if proposal["kind"] == "project" and not scope:
                return
            source = payload["query"]
            if not proposal["quote"].strip() or proposal["quote"] not in source:
                return
            old = None
            if proposal.get("target_id"):
                old = await (
                    await db.execute(
                        "SELECT * FROM memories WHERE id=? AND user_id=? AND scope=?",
                        (proposal["target_id"], job["user_id"], scope),
                    )
                ).fetchone()
                if not old or old["deleted"] or old["version"] != proposal.get("target_version"):
                    return
            else:
                old = await (
                    await db.execute(
                        "SELECT * FROM memories WHERE user_id=? AND scope=? AND fact_key=?",
                        (job["user_id"], scope, proposal["key"]),
                    )
                ).fetchone()
                if old:
                    # Includes deleted keys: retries must never resurrect a user's deletion.
                    return
            now = time.time()
            if old:
                if old["content"] == proposal["fact"] and not old["archived"]:
                    return
                await db.execute(
                    "INSERT INTO memory_versions VALUES(?,?,?)",
                    (old["id"], old["version"], json.dumps(dict(old))),
                )
                await db.execute(
                    "UPDATE memories SET content=?,source_thread=?,source_message=?,source_quote=?,"
                    "version=version+1,archived=0,archive_reason='',updated=? WHERE id=?",
                    (
                        proposal["fact"],
                        job["thread_id"],
                        job["source_message"],
                        proposal["quote"],
                        now,
                        old["id"],
                    ),
                )
            else:
                await db.execute(
                    "INSERT INTO memories(id,user_id,scope,fact_key,content,kind,source_thread,"
                    "source_message,source_quote,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        job["user_id"],
                        scope,
                        proposal["key"],
                        proposal["fact"],
                        proposal["kind"],
                        job["thread_id"],
                        job["source_message"],
                        proposal["quote"],
                        now,
                        now,
                    ),
                )

            row = await (
                await db.execute(
                    "SELECT id FROM memories WHERE user_id=? AND scope=? AND fact_key=?",
                    (job["user_id"], scope, old["fact_key"] if old else proposal["key"]),
                )
            ).fetchone()
            if row:
                await self._sync_identifiers(db, row["id"], proposal["fact"])
                await db.execute(
                    "UPDATE memories SET metadata=? WHERE id=?",
                    (
                        json.dumps(
                            {
                                k: proposal.get(k, "")
                                for k in ("entity", "property", "value", "unit", "applicability")
                            },
                            ensure_ascii=False,
                        ),
                        row["id"],
                    ),
                )

    async def _memory_source_valid(self, db, job, payload):
        if not payload.get("turn_id"):
            # Legacy payloads cannot prove raw-input provenance or eligibility.
            # Manual facts use edit_memory; queued extraction never bypasses turns.
            return False
        if payload.get("source_input_id") != job["source_message"]:
            return False
        row = await (
            await db.execute(
                "SELECT i.text,t.turn_revision,t.phase,t.outcome,t.scope_snapshot,"
                "t.memory_eligibility,t.preference_revision "
                "FROM turn_inputs i JOIN turns t ON t.id=i.turn_id "
                "WHERE i.id=? AND i.turn_id=? AND i.user_id=? AND i.thread_id=?",
                (job["source_message"], payload["turn_id"], job["user_id"], job["thread_id"]),
            )
        ).fetchone()
        return bool(
            row
            and row["text"] == payload.get("query")
            and row["phase"] == "terminal"
            and row["outcome"] == "completed"
            and row["memory_eligibility"] == "eligible"
            and row["preference_revision"] == job["revision"]
            and row["turn_revision"] == payload.get("source_revision")
            and json.loads(row["scope_snapshot"]).get("memory_scope", "")
            == payload.get("scope", "")
        )

    async def valid_memory_job(self, job):
        async with self.connect() as db:
            pref = await (
                await db.execute(
                    "SELECT * FROM memory_preferences WHERE user_id=?", (job["user_id"],)
                )
            ).fetchone()
            if pref and (not pref["enabled"] or pref["revision"] != job["revision"]):
                return False
            return await self._memory_source_valid(db, job, json.loads(job["payload"]))

    async def record_memory_use(self, user_id, memories, revision):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            pref = await (
                await db.execute("SELECT * FROM memory_preferences WHERE user_id=?", (user_id,))
            ).fetchone()
            if pref and (not pref["enabled"] or pref["revision"] != revision):
                return False
            for memory in memories:
                result = await db.execute(
                    "UPDATE memories SET hits=hits+1,last_used=? WHERE id=? AND user_id=? "
                    "AND version=? AND deleted=0 AND archived=0",
                    (time.time(), memory["id"], user_id, memory["version"]),
                )
                if result.rowcount != 1:
                    return False
        return True

    async def pending_memory_index(self, generation=None):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT m.* FROM memories m LEFT JOIN memory_index_versions i "
                    "ON i.memory_id=m.id AND i.generation=? "
                    "WHERE COALESCE(i.version,0)!=m.version ORDER BY m.updated LIMIT 100"
                    if generation
                    else "SELECT * FROM memories WHERE indexed_version!=version "
                    "ORDER BY updated LIMIT 100",
                    (generation,) if generation else (),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def mark_memory_indexed(self, memory_id, version, generation=None):
        async with self.connect() as db:
            await db.execute(
                "UPDATE memories SET indexed_version=? WHERE id=? AND version=?",
                (version, memory_id, version),
            )
            if generation:
                await db.execute(
                    "INSERT INTO memory_index_versions VALUES(?,?,?) "
                    "ON CONFLICT(memory_id,generation) DO UPDATE SET version=excluded.version",
                    (memory_id, generation, version),
                )

    async def summary(self, thread_id):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM conversation_summaries WHERE thread_id=?",
                    (thread_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def save_summary(self, thread_id, source_message, content):
        async with self.connect() as db:
            columns = {
                r[1]
                for r in await (
                    await db.execute("PRAGMA table_info(conversation_summaries)")
                ).fetchall()
            }
            if "summary_version" in columns:
                current = await (
                    await db.execute(
                        "SELECT summary_version FROM conversation_summaries WHERE thread_id=?",
                        (thread_id,),
                    )
                ).fetchone()
                if current and current[0] > 0:
                    raise ValueError("Legacy writer cannot replace a versioned summary")
            await db.execute(
                "INSERT INTO conversation_summaries(thread_id,source_message,content,updated) "
                "VALUES(?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
                "source_message=excluded.source_message,content=excluded.content,updated=excluded.updated",
                (thread_id, source_message, content, time.time()),
            )

    async def memory_job_status(self, user_id):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT status,count(*) n FROM memory_jobs WHERE user_id=? GROUP BY status",
                    (user_id,),
                )
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    async def retry_memory_jobs(self, user_id):
        async with self.connect() as db:
            result = await db.execute(
                "UPDATE memory_jobs SET status='queued',attempts=0,next_at=0,error='' "
                "WHERE user_id=? AND status='failed'",
                (user_id,),
            )
        return result.rowcount
