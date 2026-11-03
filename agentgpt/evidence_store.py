"""User-scoped, immutable extracted source snapshots; no vector index ownership."""

import hashlib
import json
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_evidence (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS web_evidence_user ON web_evidence(user_id);
CREATE TABLE IF NOT EXISTS task_evidence (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL, task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
    call_id TEXT NOT NULL, evidence_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS task_evidence_task ON task_evidence(thread_id,task_id,attempt);
CREATE TABLE IF NOT EXISTS document_read_budget (
    thread_id TEXT, request_id TEXT, call_id TEXT,
    PRIMARY KEY(thread_id,request_id,call_id));
"""


class EvidenceStore:
    async def reserve_document_read(self, thread_id, request_id, call_id):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT call_id FROM document_read_budget WHERE thread_id=? AND request_id=?",
                    (thread_id, request_id),
                )
            ).fetchall()
            calls = {r[0] for r in rows}
            if call_id in calls:
                return True
            if len(calls) >= 2:
                return False
            await db.execute(
                "INSERT INTO document_read_budget VALUES(?,?,?)", (thread_id, request_id, call_id)
            )
            return True

    async def evidence(self, user_id, evidence_id):
        if evidence_id.startswith("doc_"):
            return await self.read_chunk(user_id, evidence_id[4:])
        if evidence_id.startswith("web_"):
            record = await self.web_evidence(user_id, evidence_id)
            return {**record, "source_type": "web"}
        raise ValueError("证据不存在")

    async def associate_evidence(self, user_id, thread_id, run_id, task_id, attempt, message):
        if message.status != "success" or message.name not in {
            "builtin__web_fetch",
            "builtin__private_search",
            "builtin__private_read",
        }:
            return []
        try:
            data = json.loads(message.content)
        except (ValueError, TypeError):
            return []
        if not isinstance(data, dict) or data.get("code") != "HIT":
            return []
        sources = (
            data.get("sources", [])
            if message.name in {"builtin__private_search", "builtin__private_read"}
            else [data]
        )
        ids = []
        for source in sources:
            eid = source.get("evidence_id", "")
            if not eid or not source.get("text"):
                continue
            authoritative = await self.evidence(user_id, eid)
            if authoritative["source_type"] == "document":
                if source["text"] != authoritative["text"]:
                    raise ValueError("检索片段与保存的文档证据不一致")
                record = authoritative
            else:
                # Validate exact returned slices, not the unread remainder of a snapshot.
                paragraphs = {p["paragraph"]: p for p in authoritative["paragraphs"]}
                if not source.get("paragraphs") or any(
                    p["text"] not in paragraphs.get(p["paragraph"], {}).get("text", "")
                    for p in source["paragraphs"]
                ):
                    raise ValueError("网页片段与快照不一致")
                record = {**source, "source_type": "web"}
            record["citation"] = source.get("citation", "[来源](/api/evidence/" + eid + ")")
            identity = json.dumps([user_id, thread_id, task_id, attempt, message.tool_call_id, eid])
            relation_id = hashlib.sha256(identity.encode()).hexdigest()
            async with self.connect() as db:
                await db.execute(
                    "INSERT OR IGNORE INTO task_evidence VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        relation_id,
                        user_id,
                        thread_id,
                        run_id,
                        task_id,
                        attempt,
                        message.tool_call_id,
                        eid,
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
            ids.append(eid)
        return ids

    async def task_sources(self, user_id, thread_id, task_id, attempt, *, run_id=None):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT payload FROM task_evidence WHERE user_id=? AND thread_id=? "
                    "AND task_id=? AND attempt=? "
                    + ("AND run_id=? " if run_id is not None else "")
                    + "ORDER BY rowid",
                    (user_id, thread_id, task_id, attempt)
                    + ((run_id,) if run_id is not None else ()),
                )
            ).fetchall()
        sources = []
        for row in rows:
            source = json.loads(row["payload"])
            try:
                await self.evidence(user_id, source["evidence_id"])
            except ValueError:
                continue
            sources.append(source)
        return sources

    async def validate_citations(self, user_id, text, sources, required=False):
        cited = set(re.findall(r"/api/evidence/((?:doc_|web_)[a-f0-9]+)", text))
        allowed = {s["evidence_id"] for s in sources}
        if cited - allowed:
            raise ValueError("引用了本轮未读取或未关联的证据")
        links = re.findall(r"\]\((/api/evidence/[^\s)]+)\)", text)
        allowed_links = {s["citation"].split("](", 1)[1][:-1] for s in sources}
        if set(links) - allowed_links:
            raise ValueError("引用位置与实际读取的证据范围不一致")
        for eid in cited:
            await self.evidence(user_id, eid)
        if required and not links:
            raise ValueError("关键结论缺少可定位证据引用，请使用返回的citation链接")

    async def save_web_evidence(self, user_id, snapshot):
        canonical = json.dumps(snapshot["paragraphs"], ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        identity = json.dumps([user_id, snapshot["url"], content_hash])
        evidence_id = "web_" + hashlib.sha256(identity.encode()).hexdigest()
        record = {**snapshot, "evidence_id": evidence_id, "content_hash": content_hash}
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO web_evidence VALUES(?,?,?)",
                (evidence_id, user_id, json.dumps(record, ensure_ascii=False)),
            )
        return await self.web_evidence(user_id, evidence_id)

    async def web_evidence(self, user_id, evidence_id):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT payload FROM web_evidence WHERE id=? AND user_id=?",
                    (evidence_id, user_id),
                )
            ).fetchone()
        if row is None:
            raise ValueError("Source snapshot not found")
        return json.loads(row["payload"])
