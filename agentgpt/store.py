"""Small SQLite business store, sharing a file with LangGraph checkpoints."""

import hashlib
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import aiosqlite

from agentgpt.document_store import SCHEMA as DOCUMENT_SCHEMA
from agentgpt.document_store import DocumentStore
from agentgpt.durable import SCHEMA as DURABLE_SCHEMA
from agentgpt.durable import DurableStore
from agentgpt.evidence_store import SCHEMA as EVIDENCE_SCHEMA
from agentgpt.evidence_store import EvidenceStore
from agentgpt.host.turns import TurnStore
from agentgpt.memory_store import SCHEMA as MEMORY_SCHEMA
from agentgpt.memory_store import MemoryStore


class Store(TurnStore, DurableStore, MemoryStore, EvidenceStore, DocumentStore):
    def __init__(self, path: Path, memory_enabled=None):
        self.path = path
        self.memory_enabled = memory_enabled

    @asynccontextmanager
    async def connect(self):
        async with aiosqlite.connect(self.path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute("PRAGMA synchronous=FULL")
            yield db
            await db.commit()

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                    goal TEXT NOT NULL, status TEXT NOT NULL, settings TEXT NOT NULL,
                    share_token TEXT UNIQUE, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_thread ON events(thread_id, id);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_receipts (
                    call_key TEXT PRIMARY KEY, result TEXT NOT NULL, created REAL NOT NULL);
            """)
            await db.executescript(
                DURABLE_SCHEMA + MEMORY_SCHEMA + EVIDENCE_SCHEMA + DOCUMENT_SCHEMA
            )
            await self.initialize_turn_schema(db)
            if hasattr(self, "initialize_memory_schema"):
                await self.initialize_memory_schema(db)
            # An interrupted process leaves recoverable work, not a fictitious running job.
            await db.execute(
                "UPDATE conversations SET status='interrupted' "
                "WHERE status IN ('running','responding','pausing','stopping')"
            )

    @staticmethod
    def password_hash(password: str, salt: str | None = None) -> str:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return salt + ":" + digest.hex()

    async def register(self, email: str, password: str):
        user_id = uuid4().hex
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO users VALUES(?,?,?,?)",
                (user_id, email.casefold(), self.password_hash(password), time.time()),
            )
        return {"id": user_id, "email": email.casefold()}

    async def login(self, email: str, password: str):
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT * FROM users WHERE email=?", (email.casefold(),))
            ).fetchone()
            if not row:
                return None
            expected = self.password_hash(password, row["password_hash"].split(":")[0])
            if not secrets.compare_digest(expected, row["password_hash"]):
                return None
            token = secrets.token_urlsafe(40)
            await db.execute(
                "INSERT INTO auth_sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), row["id"], time.time() + 604800),
            )
            return token

    async def user_for_token(self, token: str):
        if not token:
            return None
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT users.id, users.email FROM users JOIN auth_sessions "
                    "ON users.id=auth_sessions.user_id WHERE token_hash=? AND expires>?",
                    (hashlib.sha256(token.encode()).hexdigest(), time.time()),
                )
            ).fetchone()
        return dict(row) if row else None

    async def logout(self, token: str):
        async with self.connect() as db:
            await db.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    async def create_thread(self, user_id: str, goal: str, options: dict):
        thread_id = uuid4().hex
        now = time.time()
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,NULL,?,?)",
                (thread_id, user_id, goal[:40], goal, "ready", json.dumps(options), now, now),
            )
        return await self.thread(thread_id)

    async def thread(self, thread_id: str):
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT * FROM conversations WHERE id=?", (thread_id,))
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        return result

    async def threads(self, user_id: str):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,title,goal,status,updated FROM conversations "
                    "WHERE user_id=? ORDER BY updated DESC",
                    (user_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def update_thread(self, thread_id: str, **fields):
        allowed = {"title", "status", "settings", "share_token"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid conversation update")
        if "settings" in fields:
            fields["settings"] = json.dumps(fields["settings"])
        fields["updated"] = time.time()
        async with self.connect() as db:
            await db.execute(
                "UPDATE conversations SET "
                + ",".join(f"{name}=?" for name in fields)
                + " WHERE id=?",
                (*fields.values(), thread_id),
            )

    async def events(self, thread_id: str, after: int = 0):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,kind,payload FROM events WHERE thread_id=? AND id>? "
                    "ORDER BY id LIMIT 500",
                    (thread_id, after),
                )
            ).fetchall()
        return [{"id": r["id"], "kind": r["kind"], "data": json.loads(r["payload"])} for r in rows]

    async def add_document(self, user_id: str, title: str, content: str):
        document_id = uuid4().hex
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?)",
                (document_id, user_id, title, content, time.time()),
            )
        return document_id

    async def documents(self, user_id: str):
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,title,length(content) AS characters FROM documents "
                    "WHERE user_id=? ORDER BY created DESC",
                    (user_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def search_documents(self, user_id: str, query: str):
        terms = re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query)
        terms += [
            s[i : i + 2]
            for s in terms[:]
            if re.search(r"[\u4e00-\u9fff]", s)
            for i in range(min(len(s) - 1, 12))
        ]
        terms = list(dict.fromkeys(t.casefold() for t in terms))[:20]
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT id,title,content FROM documents WHERE user_id=?", (user_id,)
                )
            ).fetchall()
        hits = []
        for row in rows:
            text = row["content"]
            lower = text.casefold()
            score = sum(lower.count(t) for t in terms)
            if score:
                first = min(lower.find(t) for t in terms if t in lower)
                hits.append(
                    {
                        "document_id": row["id"],
                        "title": row["title"],
                        "text": text[max(0, first - 120) : first + 1400],
                        "score": score,
                    }
                )
        return sorted(hits, key=lambda x: x["score"], reverse=True)[:5]

    async def receipt(self, call_key: str):
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT result FROM tool_receipts WHERE call_key=?", (call_key,))
            ).fetchone()
        return json.loads(row["result"]) if row else None

    async def save_receipt(self, call_key: str, result: dict):
        async with self.connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO tool_receipts VALUES(?,?,?)",
                (call_key, json.dumps(result, ensure_ascii=False), time.time()),
            )

    async def shared_thread(self, token: str):
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT id FROM conversations WHERE share_token=?", (token,))
            ).fetchone()
        return await self.thread(row["id"]) if row else None
