"""Durable command inbox and consistent, replayable UI projections."""

import json
import time
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, user_id TEXT NOT NULL,
 request_id TEXT NOT NULL, operation TEXT NOT NULL, text TEXT NOT NULL,
 question_id TEXT, status TEXT NOT NULL DEFAULT 'queued',
 context TEXT, input TEXT, control TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(user_id, thread_id, request_id));
CREATE INDEX IF NOT EXISTS runs_queue ON runs(thread_id,status,created);
CREATE TABLE IF NOT EXISTS projections (
 thread_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT '{}',
 live TEXT NOT NULL DEFAULT '{}', checkpoint_id TEXT NOT NULL DEFAULT '',
 cursor INTEGER NOT NULL DEFAULT 0);
"""


class DurableStore:
    async def enqueue_run(self, thread, operation, text, question_id, request_id=None):
        request_id = request_id or uuid4().hex
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            old = await (
                await db.execute(
                    "SELECT * FROM runs WHERE user_id=? AND thread_id=? AND request_id=?",
                    (thread["user_id"], thread["id"], request_id),
                )
            ).fetchone()
            if old:
                if (old["operation"], old["text"], old["question_id"]) != (
                    operation,
                    text,
                    question_id,
                ):
                    raise ValueError("同一请求 ID 不能用于不同命令。")
                return dict(old), False
            now, run_id = time.time(), uuid4().hex
            await db.execute(
                "INSERT INTO runs(id,thread_id,user_id,request_id,operation,text,question_id,"
                "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    thread["id"],
                    thread["user_id"],
                    request_id,
                    operation,
                    text,
                    question_id,
                    now,
                    now,
                ),
            )
        return await self.run(run_id), True

    async def run(self, run_id):
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM runs WHERE id=?", (run_id,))).fetchone()
        return dict(row) if row else None

    async def update_run(self, run_id, **fields):
        allowed = {"status", "context", "input", "control", "attempts", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid run fields")
        fields["updated"] = time.time()
        async with self.connect() as db:
            await db.execute(
                "UPDATE runs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                (*fields.values(), run_id),
            )

    async def queued_runs(self, thread_id=None):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM runs WHERE status IN ('queued','running')"
                    + (" AND thread_id=?" if thread_id else "")
                    + " ORDER BY created,id",
                    (thread_id,) if thread_id else (),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def latest_run(self, thread_id):
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM runs WHERE thread_id=? AND context IS NOT NULL "
                    "ORDER BY created DESC LIMIT 1",
                    (thread_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def append_event(self, thread_id, kind, payload):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "INSERT INTO events(thread_id,kind,payload,created) VALUES(?,?,?,?)",
                (thread_id, kind, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            event_id = cursor.lastrowid
            await db.execute("INSERT OR IGNORE INTO projections(thread_id) VALUES(?)", (thread_id,))
            if kind == "state":
                await db.execute(
                    "UPDATE projections SET state=?,live='{}' WHERE thread_id=?",
                    (json.dumps(payload, ensure_ascii=False), thread_id),
                )
                await db.execute(
                    "UPDATE conversations SET status=?,updated=? WHERE id=?",
                    (payload["status"], time.time(), thread_id),
                )
            elif kind == "token":
                row = await (
                    await db.execute(
                        "SELECT live FROM projections WHERE thread_id=?",
                        (thread_id,),
                    )
                ).fetchone()
                live = json.loads(row["live"])
                if live.get("message_id") != payload["message_id"]:
                    live = {**payload, "text": ""}
                live["text"] += payload["text"]
                await db.execute(
                    "UPDATE projections SET live=? WHERE thread_id=?",
                    (json.dumps(live, ensure_ascii=False), thread_id),
                )
            elif kind in {"settled", "error", "recovering"}:
                await db.execute("UPDATE projections SET live='{}' WHERE thread_id=?", (thread_id,))
            await db.execute(
                "UPDATE projections SET cursor=? WHERE thread_id=?", (event_id, thread_id)
            )
        return event_id

    async def event_view(self, thread_id):
        async with self.connect() as db:
            # All components must describe the same committed prefix of the event log.
            await db.execute("BEGIN")
            row = await (
                await db.execute(
                    "SELECT * FROM projections WHERE thread_id=?",
                    (thread_id,),
                )
            ).fetchone()
            tools = await (
                await db.execute(
                    "SELECT payload FROM events WHERE thread_id=? AND kind='tool' "
                    "ORDER BY id DESC LIMIT 12",
                    (thread_id,),
                )
            ).fetchall()
        return {
            "state": json.loads(row["state"]) if row else {},
            "event_cursor": row["cursor"] if row else 0,
            "live": json.loads(row["live"]) if row else {},
            "tool_activity": [json.loads(t["payload"]) for t in reversed(tools)],
        }

    async def pending_receipts(self, thread_id):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT call_key,result FROM tool_receipts WHERE call_key LIKE ?",
                    (thread_id + ":%",),
                )
            ).fetchall()
        return [
            {"call_key": r["call_key"], **json.loads(r["result"])}
            for r in rows
            if json.loads(r["result"]).get("status") == "pending"
        ]
