"""Authoritative user turns and transactional, independently retryable projections."""

import hashlib
import json
import time
from uuid import uuid4

from agentgpt.host.session_state import constraint_scope

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 turn_seq INTEGER NOT NULL, origin_run_id TEXT NOT NULL UNIQUE,
 user_text TEXT NOT NULL, assistant_text TEXT NOT NULL DEFAULT '',
 phase TEXT NOT NULL DEFAULT 'queued', outcome TEXT NOT NULL DEFAULT '',
 turn_revision INTEGER NOT NULL DEFAULT 0, scope_snapshot TEXT NOT NULL,
 preference_revision INTEGER NOT NULL DEFAULT 0,
 memory_eligibility TEXT NOT NULL DEFAULT 'pending',
 created REAL NOT NULL, finished REAL, UNIQUE(thread_id,turn_seq));
CREATE TABLE IF NOT EXISTS turn_runs (
 run_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, relation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_inputs (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 turn_id TEXT NOT NULL, input_seq INTEGER NOT NULL, request_id TEXT NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(thread_id,input_seq), UNIQUE(user_id,thread_id,request_id));
CREATE TABLE IF NOT EXISTS turn_artifacts (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 turn_id TEXT NOT NULL, run_id TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
 evidence_refs TEXT NOT NULL DEFAULT '[]', UNIQUE(turn_id,version,kind));
CREATE TABLE IF NOT EXISTS context_jobs (
 id TEXT PRIMARY KEY, job_kind TEXT NOT NULL, user_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 target_seq INTEGER NOT NULL, base_version INTEGER NOT NULL DEFAULT 0,
 source_digest TEXT NOT NULL DEFAULT '', proposal TEXT,
 status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(job_kind,source_id,source_revision));
CREATE INDEX IF NOT EXISTS context_jobs_queue ON context_jobs(status,next_at,created);
CREATE TABLE IF NOT EXISTS session_state (
 thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 covered_until_input_seq INTEGER NOT NULL DEFAULT -1, objects TEXT NOT NULL DEFAULT '[]',
 constraints TEXT NOT NULL DEFAULT '[]', open_questions TEXT NOT NULL DEFAULT '[]',
 artifact_refs TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_state_versions (
 thread_id TEXT NOT NULL, version INTEGER NOT NULL, record TEXT NOT NULL,
 PRIMARY KEY(thread_id,version));
CREATE TABLE IF NOT EXISTS turn_summaries (
 turn_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 source_turn_revision INTEGER NOT NULL, summary_version INTEGER NOT NULL,
 content TEXT NOT NULL, source_refs TEXT NOT NULL, quality_flags TEXT NOT NULL,
 updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_summary_versions (
 thread_id TEXT NOT NULL, version INTEGER NOT NULL, record TEXT NOT NULL,
 PRIMARY KEY(thread_id,version));
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


async def one(db, sql, args=()):
    row = await (await db.execute(sql, args)).fetchone()
    return dict(row) if row else None


async def many(db, sql, args=()):
    return [dict(row) for row in await (await db.execute(sql, args)).fetchall()]


async def enqueue_projection(db, kind, source, revision, seq, user, thread):
    now = time.time()
    await db.execute(
        "INSERT OR IGNORE INTO context_jobs(id,job_kind,user_id,thread_id,source_id,"
        "source_revision,target_seq,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (uuid4().hex, kind, user, thread, source, revision, seq, now, now),
    )


class TurnStore:
    async def initialize_turn_schema(self, db):
        await db.executescript(SCHEMA)
        columns = {r["name"] for r in await many(db, "PRAGMA table_info(conversation_summaries)")}
        for name, declaration in {
            "user_id": "TEXT NOT NULL DEFAULT ''",
            "summary_version": "INTEGER NOT NULL DEFAULT 0",
            "covered_until_turn_seq": "INTEGER NOT NULL DEFAULT -1",
            "source_digest": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                await db.execute(
                    f"ALTER TABLE conversation_summaries ADD COLUMN {name} {declaration}"
                )
        # initialize() also runs in worker processes. Never steal a live Host lease;
        # abandoned jobs become claimable through their persisted lease expiry.

    async def enqueue_run(self, thread, operation, text, question_id, request_id=None):
        request_id = request_id or uuid4().hex
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            owner = await one(db, "SELECT user_id FROM conversations WHERE id=?", (thread["id"],))
            if not owner or owner["user_id"] != thread["user_id"]:
                raise ValueError("会话归属不匹配。")
            old = await one(
                db,
                "SELECT * FROM runs WHERE user_id=? AND thread_id=? AND request_id=?",
                (thread["user_id"], thread["id"], request_id),
            )
            if old:
                if (old["operation"], old["text"], old["question_id"]) != (
                    operation,
                    text,
                    question_id,
                ):
                    raise ValueError("同一请求 ID 不能用于不同命令。")
                if operation == "continue":
                    resumed = await one(
                        db,
                        "SELECT r.* FROM runs r JOIN turns t ON t.origin_run_id=r.id "
                        "JOIN turn_runs tr ON tr.turn_id=t.id WHERE tr.run_id=?",
                        (old["id"],),
                    )
                    return resumed or old, False
                return old, False
            if operation == "followup" and not text.strip():
                raise ValueError("请填写追加提问。")
            if operation == "provide_input":
                raise ValueError("补充输入接口尚未启用。")
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
            if operation == "continue":
                previous = await one(
                    db,
                    "SELECT * FROM turns WHERE thread_id=? AND phase='paused' "
                    "ORDER BY turn_seq DESC LIMIT 1",
                    (thread["id"],),
                )
                if not previous:
                    raise ValueError("没有可继续的暂停轮次，请重新发起请求。")
                await db.execute(
                    "INSERT INTO turn_runs VALUES(?,?,'resume')",
                    (run_id, previous["id"]),
                )
                await db.execute("UPDATE turns SET phase='queued' WHERE id=?", (previous["id"],))
                # Resume the exact RPC/checkpoint identity. The command row is only a receipt.
                await db.execute(
                    "UPDATE runs SET status='done',updated=? WHERE id=?", (now, run_id)
                )
                run_id = previous["origin_run_id"]
                await db.execute(
                    "UPDATE runs SET status='queued',control='',error='',updated=? WHERE id=?",
                    (now, run_id),
                )
                resumed = await one(db, "SELECT context FROM runs WHERE id=?", (run_id,))
                checkpoint = json.loads(resumed["context"] or "{}")
                checkpoint.pop("recovery", None)
                if isinstance(checkpoint.get("orchestrator"), dict):
                    checkpoint["orchestrator"]["deadline_at"] = None
                await db.execute(
                    "UPDATE runs SET context=? WHERE id=?", (encode(checkpoint), run_id)
                )
            elif operation == "stop":
                paused = await one(
                    db,
                    "SELECT * FROM turns WHERE thread_id=? AND user_id=? AND phase='paused' "
                    "AND NOT EXISTS(SELECT 1 FROM runs WHERE thread_id=? AND id!=? "
                    "AND status IN ('queued','running')) ORDER BY turn_seq DESC LIMIT 1",
                    (thread["id"], thread["user_id"], thread["id"], run_id),
                )
                if paused:
                    await self._finish_turn_in_transaction(
                        db,
                        paused["origin_run_id"],
                        outcome="stopped",
                        assistant_text="本轮已停止。",
                        memory_enabled=False,
                    )
                    await db.execute(
                        "UPDATE runs SET status='done',updated=? WHERE id=?", (now, run_id)
                    )
            elif operation != "pause":
                user_text = text if operation == "followup" else (text or thread["goal"])
                await self._create_turn(db, thread, run_id, request_id, operation, user_text)
                await db.execute(
                    "UPDATE runs SET context=? WHERE id=?",
                    (
                        encode({"version": 1, "goal": user_text, "options": thread["settings"]}),
                        run_id,
                    ),
                )
            if operation in {"pause", "stop"}:
                target = await one(
                    db,
                    "SELECT id FROM runs WHERE thread_id=? AND id!=? "
                    "AND operation NOT IN ('pause','stop') AND status IN ('queued','running') "
                    "ORDER BY created,id LIMIT 1",
                    (thread["id"], run_id),
                )
                if target:
                    await db.execute(
                        "UPDATE runs SET control=?,updated=? WHERE id=?",
                        (operation, now, target["id"]),
                    )
                    await db.execute(
                        "UPDATE conversations SET status=?,updated=? WHERE id=?",
                        ("pausing" if operation == "pause" else "stopping", now, thread["id"]),
                    )
                await db.execute(
                    "UPDATE runs SET status='done',updated=? WHERE id=?", (now, run_id)
                )
        return await self.run(run_id), True

    async def _create_turn(self, db, thread, run_id, request_id, kind, user_text, legacy=False):
        now, turn_id, input_id = time.time(), uuid4().hex, uuid4().hex
        seq = (
            await one(
                db,
                "SELECT COALESCE(MAX(turn_seq),-1)+1 n FROM turns WHERE thread_id=?",
                (thread["id"],),
            )
        )["n"]
        input_seq = (
            await one(
                db,
                "SELECT COALESCE(MAX(input_seq),-1)+1 n FROM turn_inputs WHERE thread_id=?",
                (thread["id"],),
            )
        )["n"]
        pref = await one(
            db, "SELECT * FROM memory_preferences WHERE user_id=?", (thread["user_id"],)
        )
        scope = {k: thread["settings"].get(k, "") for k in ("memory_scope", "knowledge_base")}
        eligibility = (
            "legacy_unmapped"
            if legacy
            else (
                "disabled"
                if self.memory_enabled is False or (pref and not pref["enabled"])
                else "pending"
            )
        )
        await db.execute(
            "INSERT INTO turns(id,user_id,thread_id,turn_seq,origin_run_id,user_text,"
            "scope_snapshot,preference_revision,memory_eligibility,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                turn_id,
                thread["user_id"],
                thread["id"],
                seq,
                run_id,
                user_text,
                encode(scope),
                pref["revision"] if pref else 0,
                eligibility,
                now,
            ),
        )
        await db.execute("INSERT INTO turn_runs VALUES(?,?,'origin')", (run_id, turn_id))
        await db.execute(
            "INSERT INTO turn_inputs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                input_id,
                thread["user_id"],
                thread["id"],
                turn_id,
                input_seq,
                request_id,
                kind,
                user_text,
                now,
            ),
        )
        if not getattr(self, "memory_lite", False):
            await enqueue_projection(
                db, "state", input_id, 0, input_seq, thread["user_id"], thread["id"]
            )
        return turn_id

    async def ensure_turn_for_run(self, run_id):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await one(db, "SELECT * FROM runs WHERE id=?", (run_id,))
            if not row or row["operation"] in {"pause", "stop"}:
                return None
            mapped = await one(db, "SELECT turn_id FROM turn_runs WHERE run_id=?", (run_id,))
            if not mapped:
                thread = await one(
                    db, "SELECT * FROM conversations WHERE id=?", (row["thread_id"],)
                )
                thread["settings"] = json.loads(thread["settings"])
                text = (
                    row["text"] if row["operation"] == "followup" else row["text"] or thread["goal"]
                )
                await self._create_turn(
                    db, thread, run_id, row["request_id"], row["operation"], text, legacy=True
                )
        return await self.turn_for_run(run_id)

    async def turn_for_run(self, run_id, user_id=None):
        async with self.connect() as db:
            row = await one(
                db,
                "SELECT t.*,MIN(i.input_seq) input_seq FROM turns t "
                "JOIN turn_runs r ON r.turn_id=t.id JOIN turn_inputs i ON i.turn_id=t.id "
                "WHERE r.run_id=?" + (" AND t.user_id=?" if user_id else "") + " GROUP BY t.id",
                (run_id, user_id) if user_id else (run_id,),
            )
        if row:
            row["scope_snapshot"] = json.loads(row["scope_snapshot"])
        return row

    async def update_run(self, run_id, **fields):
        allowed = {"status", "context", "input", "control", "attempts", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid run fields")
        fields["updated"] = time.time()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE runs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                (*fields.values(), run_id),
            )
            if fields.get("status") in {"queued", "running"}:
                await db.execute(
                    "UPDATE turns SET phase=? WHERE id="
                    "(SELECT turn_id FROM turn_runs WHERE run_id=?) "
                    "AND phase!='terminal'",
                    (fields["status"], run_id),
                )

    async def finish_turn(
        self, run_id, *, outcome, assistant_text, state=None, memory_enabled=False, error=""
    ):
        if outcome not in {"completed", "failed", "stopped", "cancelled", "paused"}:
            raise ValueError("Invalid turn outcome")
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._finish_turn_in_transaction(
                db,
                run_id,
                outcome=outcome,
                assistant_text=assistant_text,
                state=state,
                memory_enabled=memory_enabled,
                error=error,
            )
        return await self.turn_for_run(run_id)

    async def _finish_turn_in_transaction(
        self,
        db,
        run_id,
        *,
        outcome,
        assistant_text,
        state=None,
        memory_enabled=False,
        error="",
    ):
        run = await one(db, "SELECT * FROM runs WHERE id=?", (run_id,))
        turn = await one(
            db,
            "SELECT t.* FROM turns t JOIN turn_runs r ON t.id=r.turn_id WHERE r.run_id=?",
            (run_id,),
        )
        if not run or not turn or run["user_id"] != turn["user_id"]:
            raise ValueError("运行没有有效的用户轮次。")
        if turn["phase"] == "terminal" or (
            turn["phase"] == "paused" and run["status"] == "done" and outcome == "paused"
        ):
            return turn
        now = time.time()
        revision = turn["turn_revision"] + 1
        pref = await one(db, "SELECT * FROM memory_preferences WHERE user_id=?", (turn["user_id"],))
        enabled = memory_enabled and (not pref or pref["enabled"])
        current_revision = pref["revision"] if pref else 0
        eligible = turn["memory_eligibility"]
        if eligible != "legacy_unmapped":
            eligible = (
                "not_completed"
                if outcome != "completed"
                else (
                    "eligible"
                    if enabled
                    and eligible != "disabled"
                    and current_revision == turn["preference_revision"]
                    else "disabled"
                )
            )
        await db.execute(
            "UPDATE turns SET assistant_text=?,phase=?,outcome=?,turn_revision=?,"
            "memory_eligibility=?,finished=? WHERE id=?",
            (
                assistant_text,
                "paused" if outcome == "paused" else "terminal",
                "" if outcome == "paused" else outcome,
                revision,
                eligible,
                now,
                turn["id"],
            ),
        )
        await db.execute(
            "UPDATE runs SET status=?,error=?,updated=? WHERE id=?",
            ("failed" if outcome == "failed" else "done", error, now, run_id),
        )
        projection = dict(state or {})
        projection.update(
            status=outcome,
            final_answer=assistant_text,
            run_id=run_id,
            turn_id=turn["id"],
            user_id=turn["user_id"],
        )
        projection.setdefault("goal", turn["user_text"])
        projection.setdefault("tasks", [])
        projection.setdefault("messages", [])
        for kind, payload in (
            ("state", projection),
            (
                "settled",
                {"run_id": run_id, "turn_id": turn["id"], "interrupted": outcome == "paused"},
            ),
        ):
            cursor = await db.execute(
                "INSERT INTO events(thread_id,kind,payload,created) VALUES(?,?,?,?)",
                (turn["thread_id"], kind, encode(payload), now),
            )
        await db.execute(
            "INSERT INTO projections(thread_id,state,live,cursor) VALUES(?,?,'{}',?) "
            "ON CONFLICT(thread_id) DO UPDATE SET state=excluded.state,"
            "live='{}',cursor=excluded.cursor",
            (turn["thread_id"], encode(projection), cursor.lastrowid),
        )
        await db.execute(
            "UPDATE conversations SET status=?,updated=? WHERE id=?",
            (outcome, now, turn["thread_id"]),
        )
        if outcome != "paused" and not getattr(self, "memory_lite", False):
            for kind in ("turn_summary", "thread_summary"):
                await enqueue_projection(
                    db,
                    kind,
                    turn["id"],
                    revision,
                    turn["turn_seq"],
                    turn["user_id"],
                    turn["thread_id"],
                )
            turn.update(assistant_text=assistant_text, turn_revision=revision)
            if eligible == "eligible":
                await self._enqueue_turn_memory(db, turn)
        if assistant_text:
            await db.execute(
                "INSERT INTO turn_artifacts(id,user_id,thread_id,turn_id,run_id,version,kind,"
                "content,content_hash) VALUES(?,?,?,?,?,?,'answer',?,?)",
                (
                    uuid4().hex,
                    turn["user_id"],
                    turn["thread_id"],
                    turn["id"],
                    run_id,
                    revision,
                    assistant_text,
                    hashlib.sha256(assistant_text.encode()).hexdigest(),
                ),
            )

    async def _enqueue_turn_memory(self, db, turn):
        inputs = await many(
            db, "SELECT * FROM turn_inputs WHERE turn_id=? ORDER BY input_seq", (turn["id"],)
        )
        scope = json.loads(turn["scope_snapshot"])
        now = time.time()
        for item in inputs:
            payload = dict(
                query=item["text"],
                answer=turn["assistant_text"],
                scope=scope["memory_scope"],
                turn_id=turn["id"],
                source_input_id=item["id"],
                source_revision=turn["turn_revision"],
            )
            await db.execute(
                "INSERT OR IGNORE INTO memory_jobs(id,user_id,thread_id,source_message,payload,"
                "revision,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    turn["user_id"],
                    turn["thread_id"],
                    item["id"],
                    encode(payload),
                    turn["preference_revision"],
                    now,
                    now,
                ),
            )

    async def reconcile_turns(self, memory_enabled=False):
        if getattr(self, "memory_lite", False):
            return
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            for item in await many(db, "SELECT * FROM turn_inputs"):
                await enqueue_projection(
                    db,
                    "state",
                    item["id"],
                    0,
                    item["input_seq"],
                    item["user_id"],
                    item["thread_id"],
                )
            for turn in await many(db, "SELECT * FROM turns WHERE phase='terminal'"):
                for kind in ("turn_summary", "thread_summary"):
                    await enqueue_projection(
                        db,
                        kind,
                        turn["id"],
                        turn["turn_revision"],
                        turn["turn_seq"],
                        turn["user_id"],
                        turn["thread_id"],
                    )
                if memory_enabled and turn["memory_eligibility"] == "eligible":
                    pref = await one(
                        db, "SELECT * FROM memory_preferences WHERE user_id=?", (turn["user_id"],)
                    )
                    if (not pref and turn["preference_revision"] == 0) or (
                        pref and pref["enabled"] and pref["revision"] == turn["preference_revision"]
                    ):
                        await self._enqueue_turn_memory(db, turn)

    async def context_snapshot(self, user_id, thread_id, before_input_seq):
        async with self.connect() as db:
            await db.execute("BEGIN")
            owner = await one(db, "SELECT user_id FROM conversations WHERE id=?", (thread_id,))
            if not owner or owner["user_id"] != user_id:
                raise ValueError("会话不存在。")
            inputs = await many(
                db,
                "SELECT * FROM turn_inputs WHERE user_id=? AND thread_id=? "
                "AND input_seq<? ORDER BY input_seq",
                (user_id, thread_id, before_input_seq),
            )
            turns = await many(
                db,
                "SELECT t.* FROM turns t WHERE t.user_id=? AND t.thread_id=? AND EXISTS "
                "(SELECT 1 FROM turn_inputs i WHERE i.turn_id=t.id AND i.input_seq<?) "
                "ORDER BY turn_seq",
                (user_id, thread_id, before_input_seq),
            )
            session = await one(
                db,
                "SELECT * FROM session_state WHERE thread_id=? AND user_id=?",
                (thread_id, user_id),
            )
            if session and session["covered_until_input_seq"] >= before_input_seq:
                versions = await many(
                    db,
                    "SELECT record FROM session_state_versions WHERE thread_id=? "
                    "ORDER BY version DESC",
                    (thread_id,),
                )
                session = next(
                    (
                        v
                        for row in versions
                        if (v := json.loads(row["record"]))["covered_until_input_seq"]
                        < before_input_seq
                    ),
                    None,
                )
            if session:
                for key in ("objects", "constraints", "open_questions", "artifact_refs"):
                    session[key] = json.loads(session[key])
                current_input = await one(
                    db,
                    "SELECT * FROM turn_inputs WHERE user_id=? AND thread_id=? AND input_seq=?",
                    (user_id, thread_id, before_input_seq),
                )
                current_turn = current_input["turn_id"] if current_input else None
                previous_turn = inputs[-1]["turn_id"] if inputs else None
                carry_previous = bool(
                    current_input
                    and any(phrase in current_input["text"] for phrase in ("其他不变", "其余不变"))
                )
                input_turns = {item["id"]: item["turn_id"] for item in inputs}
                for constraint in session["constraints"]:
                    if (
                        constraint.get("status") != "active"
                        or constraint_scope(constraint.get("scope", "")) != "turn"
                    ):
                        continue
                    source_turn = constraint.get("applies_to_turn_id") or input_turns.get(
                        constraint.get("source_input_id")
                    )
                    if source_turn == current_turn or (
                        carry_previous and source_turn == previous_turn
                    ):
                        constraint["applies_to_turn_id"] = current_turn
                    else:
                        constraint["status"] = "expired_for_request"
            max_turn = max((t["turn_seq"] for t in turns), default=-1)
            summary = await one(
                db,
                "SELECT * FROM conversation_summaries WHERE thread_id=? AND user_id=? "
                "AND summary_version>0 AND covered_until_turn_seq>=0",
                (thread_id, user_id),
            )
            if summary and summary["covered_until_turn_seq"] > max_turn:
                versions = await many(
                    db,
                    "SELECT record FROM conversation_summary_versions "
                    "WHERE thread_id=? ORDER BY version DESC",
                    (thread_id,),
                )
                summary = next(
                    (
                        v
                        for row in versions
                        if (v := json.loads(row["record"]))["covered_until_turn_seq"] <= max_turn
                        and v.get("user_id") == user_id
                        and v.get("summary_version", 0) > 0
                        and v["covered_until_turn_seq"] >= 0
                    ),
                    None,
                )
            if summary:
                summary["text"] = summary["content"]
            summaries = await many(
                db,
                "SELECT s.*,t.turn_seq FROM turn_summaries s JOIN turns t ON s.turn_id=t.id "
                "WHERE s.user_id=? AND s.thread_id=? AND t.turn_seq<=? ORDER BY t.turn_seq",
                (user_id, thread_id, max_turn),
            )
            artifacts = await many(
                db,
                "SELECT a.* FROM turn_artifacts a JOIN turns t ON t.id=a.turn_id "
                "WHERE a.user_id=? AND a.thread_id=? AND t.turn_seq<=? "
                "ORDER BY t.turn_seq,a.version",
                (user_id, thread_id, max_turn),
            )
        return dict(
            session=session or {},
            inputs=inputs,
            turns=turns,
            summary=summary or {},
            turn_summaries=summaries,
            artifacts=artifacts,
        )

    async def read_turn_artifact(self, user_id, thread_id, artifact_id):
        async with self.connect() as db:
            return await one(
                db,
                "SELECT * FROM turn_artifacts WHERE id=? AND user_id=? AND thread_id=?",
                (artifact_id, user_id, thread_id),
            )

    async def context_job_status(self, user_id):
        async with self.connect() as db:
            rows = await many(
                db,
                "SELECT job_kind,status,count(*) n FROM context_jobs WHERE user_id=? "
                "GROUP BY job_kind,status",
                (user_id,),
            )
        return rows

    async def retry_context_jobs(self, user_id):
        """Explicit retry unblocks a failed source without advancing its coverage."""
        async with self.connect() as db:
            result = await db.execute(
                "UPDATE context_jobs SET status='queued',attempts=0,next_at=0,lease_until=0,"
                "error='',proposal=NULL WHERE user_id=? AND status='failed'",
                (user_id,),
            )
        return result.rowcount
