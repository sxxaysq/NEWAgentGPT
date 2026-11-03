"""Versioned source files, durable ingestion jobs and rebuildable lexical index."""

import hashlib
import json
import re
import time
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS document_meta (
    document_id TEXT PRIMARY KEY, kb TEXT NOT NULL DEFAULT '',
    active_version TEXT, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS document_versions (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, number INTEGER NOT NULL,
    title TEXT NOT NULL, content_hash TEXT NOT NULL, raw_path TEXT,
    legacy_text TEXT, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
    index_kind TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
    UNIQUE(document_id,number));
CREATE TABLE IF NOT EXISTS document_chunks (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    text TEXT NOT NULL, location TEXT NOT NULL,
    UNIQUE(version_id,ordinal));
CREATE INDEX IF NOT EXISTS chunks_version ON document_chunks(version_id);
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(chunk_id UNINDEXED, tokens);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    version_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
"""


def lexical_tokens(text):
    """CJK uni/bigrams retain exact names without requiring a new tokenizer package."""
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.casefold())
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class DocumentStore:
    async def queue_document(
        self, user_id, title, raw_path, content_hash, *, document_id=None, kb="", legacy_text=None
    ):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            if document_id:
                row = await (
                    await db.execute(
                        "SELECT d.id FROM documents d JOIN document_meta m ON m.document_id=d.id "
                        "WHERE d.id=? AND d.user_id=? AND m.deleted=0",
                        (document_id, user_id),
                    )
                ).fetchone()
                if row is None:
                    raise ValueError("文档不存在")
            else:
                duplicate = await (
                    await db.execute(
                        "SELECT v.document_id,v.id,v.status FROM document_versions v "
                        "JOIN documents d ON d.id=v.document_id "
                        "JOIN document_meta m ON m.document_id=d.id "
                        "WHERE d.user_id=? AND m.kb=? AND m.deleted=0 AND v.content_hash=? "
                        "AND v.number=(SELECT max(number) FROM document_versions "
                        "WHERE document_id=d.id) "
                        "ORDER BY v.created DESC LIMIT 1",
                        (user_id, kb, content_hash),
                    )
                ).fetchone()
                if duplicate:
                    return dict(duplicate)
                document_id = uuid4().hex
                await db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?)",
                    (document_id, user_id, title, legacy_text or "", time.time()),
                )
                await db.execute(
                    "INSERT INTO document_meta(document_id,kb) VALUES(?,?)", (document_id, kb)
                )
            last = await (
                await db.execute(
                    "SELECT * FROM document_versions WHERE document_id=? "
                    "ORDER BY number DESC LIMIT 1",
                    (document_id,),
                )
            ).fetchone()
            if last and last["content_hash"] == content_hash:
                return {"document_id": document_id, "id": last["id"], "status": last["status"]}
            version_id = uuid4().hex
            await db.execute(
                "INSERT INTO document_versions "
                "(id,document_id,number,title,content_hash,raw_path,legacy_text,status,created) "
                "VALUES(?,?,?,?,?,?,?,'processing',?)",
                (
                    version_id,
                    document_id,
                    (last["number"] + 1) if last else 1,
                    title,
                    content_hash,
                    raw_path,
                    legacy_text,
                    time.time(),
                ),
            )
            await db.execute(
                "INSERT INTO ingestion_jobs(version_id,status) VALUES(?,'queued')", (version_id,)
            )
        return {"document_id": document_id, "id": version_id, "status": "processing"}

    async def migrate_documents(self):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT d.* FROM documents d LEFT JOIN document_meta m ON m.document_id=d.id "
                    "WHERE m.document_id IS NULL"
                )
            ).fetchall()
            for row in rows:
                version_id = uuid4().hex
                await db.execute(
                    "INSERT OR IGNORE INTO document_meta(document_id) VALUES(?)", (row["id"],)
                )
                await db.execute(
                    "INSERT INTO document_versions "
                    "(id,document_id,number,title,content_hash,legacy_text,status,created) "
                    "VALUES(?,?,1,?,?,?,'processing',?)",
                    (
                        version_id,
                        row["id"],
                        row["title"],
                        hashlib.sha256(row["content"].encode()).hexdigest(),
                        row["content"],
                        time.time(),
                    ),
                )
                await db.execute(
                    "INSERT INTO ingestion_jobs(version_id,status) VALUES(?,'queued')",
                    (version_id,),
                )

    async def document_listing(self, user_id):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT d.id,d.title,length(d.content) AS characters,m.kb,m.active_version,"
                    "v.number AS version,v.status,v.error,v.index_kind FROM documents d "
                    "LEFT JOIN document_meta m ON m.document_id=d.id "
                    "LEFT JOIN document_versions v ON v.document_id=d.id AND v.number="
                    "(SELECT max(v2.number) FROM document_versions v2 WHERE v2.document_id=d.id) "
                    "WHERE d.user_id=? AND coalesce(m.deleted,0)=0 ORDER BY d.created DESC",
                    (user_id,),
                )
            ).fetchall()
        return [dict(r) for r in rows]

    async def remove_document(self, user_id, document_id):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT id FROM documents WHERE id=? AND user_id=?", (document_id, user_id)
                )
            ).fetchone()
            if not row:
                raise ValueError("文档不存在")
            await db.execute(
                "INSERT OR IGNORE INTO document_meta(document_id) VALUES(?)", (document_id,)
            )
            await db.execute(
                "UPDATE document_meta SET deleted=1 WHERE document_id=?", (document_id,)
            )
            await db.execute(
                "UPDATE ingestion_jobs SET status='cancelled' WHERE version_id IN "
                "(SELECT id FROM document_versions WHERE document_id=?)",
                (document_id,),
            )
            await db.execute(
                "DELETE FROM document_fts WHERE chunk_id IN "
                "(SELECT c.id FROM document_chunks c JOIN document_versions v "
                "ON c.version_id=v.id WHERE v.document_id=?)",
                (document_id,),
            )

    async def retry_document(self, user_id, document_id):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT v.id FROM document_versions v JOIN documents d ON d.id=v.document_id "
                    "JOIN document_meta m ON m.document_id=d.id "
                    "WHERE d.user_id=? AND d.id=? AND m.deleted=0 ORDER BY v.number DESC LIMIT 1",
                    (user_id, document_id),
                )
            ).fetchone()
            if not row:
                raise ValueError("文档不存在")
            await db.execute(
                "UPDATE ingestion_jobs SET status='queued',attempts=0,error='' "
                "WHERE version_id=? AND status='failed'",
                (row["id"],),
            )
            await db.execute(
                "UPDATE document_versions SET status='processing',error='' "
                "WHERE id=? AND status='failed'",
                (row["id"],),
            )

    async def read_chunk(self, user_id, chunk_id, *, active_only=False):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT c.*,v.document_id,v.number AS "
                    "version,v.title,v.content_hash,d.user_id,m.kb "
                    "FROM document_chunks c JOIN document_versions v ON c.version_id=v.id "
                    "JOIN documents d ON d.id=v.document_id JOIN document_meta m "
                    "ON m.document_id=d.id "
                    "WHERE c.id=? AND d.user_id=? AND m.deleted=0 AND v.status='ready' "
                    + ("AND m.active_version=v.id" if active_only else ""),
                    (chunk_id, user_id),
                )
            ).fetchone()
        if not row:
            raise ValueError("证据不存在或已撤销访问")
        result = dict(row)
        result["location"] = json.loads(result["location"])
        result["evidence_id"] = "doc_" + result.pop("id")
        result["source_type"] = "document"
        result.pop("user_id")
        return result

    async def adjacent_chunks(self, user_id, kb, evidence_id):
        anchor = await self.read_chunk(user_id, evidence_id.removeprefix("doc_"))
        if anchor["kb"] != kb:
            raise ValueError("Evidence outside knowledge base")
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id FROM document_chunks WHERE version_id=? "
                    "AND ordinal IN (?,?) ORDER BY ordinal",
                    (anchor["version_id"], anchor["ordinal"] - 1, anchor["ordinal"] + 1),
                )
            ).fetchall()
        return [await self.read_chunk(user_id, r[0]) for r in rows]

    async def lexical_search(self, user_id, kb, query, limit=20):
        tokens = list(dict.fromkeys(lexical_tokens(query)))[:80]
        if not tokens:
            return []
        expression = " OR ".join('"' + t + '"' for t in tokens)
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT f.chunk_id,bm25(document_fts) AS score FROM document_fts f "
                    "JOIN document_chunks c ON c.id=f.chunk_id "
                    "JOIN document_versions v ON v.id=c.version_id "
                    "JOIN document_meta m ON m.document_id=v.document_id "
                    "JOIN documents d ON d.id=m.document_id WHERE document_fts MATCH ? "
                    "AND d.user_id=? AND m.kb=? AND m.deleted=0 AND m.active_version=v.id "
                    "AND v.status='ready' ORDER BY score LIMIT ?",
                    (expression, user_id, kb, limit),
                )
            ).fetchall()
        return [dict(r) for r in rows]
