"""SQLite implementation of the SDK TaskStore with mandatory run ownership."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from a2a.server.tasks import TaskStore as SDKTaskStore
from a2a.types import ListTasksResponse, Task, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToJson, Parse

from agentgpt.a2a.auth import scope_from_context


class TaskStore(SDKTaskStore):
    def __init__(self, path: Path):
        self.path = path

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    async def initialize(self):
        def work():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._db() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("""CREATE TABLE IF NOT EXISTS a2a_tasks (
                    id TEXT PRIMARY KEY, context_id TEXT, state TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
                columns = {r["name"] for r in db.execute("PRAGMA table_info(a2a_tasks)")}
                for name in ("user_id", "thread_id", "run_id"):
                    if name not in columns:
                        db.execute(f"ALTER TABLE a2a_tasks ADD COLUMN {name} TEXT")
                db.execute("""CREATE INDEX IF NOT EXISTS a2a_task_owner
                    ON a2a_tasks(user_id,thread_id,run_id)""")
                db.execute("""CREATE TABLE IF NOT EXISTS execution_journal (
                    task_id TEXT NOT NULL, entry_key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(task_id,entry_key))""")
                # Legacy records have no trustworthy owner and remain inaccessible.
                for row in db.execute("SELECT * FROM a2a_tasks WHERE run_id IS NOT NULL"):
                    task = Parse(row["payload"], Task())
                    if task.status.state in (
                        # The executor's first save can precede the WORKING event commit.
                        TaskState.TASK_STATE_UNSPECIFIED,
                        TaskState.TASK_STATE_SUBMITTED,
                        TaskState.TASK_STATE_WORKING,
                    ):
                        task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
                        task.metadata.update({"restart_recovery": True})
                        db.execute(
                            "UPDATE a2a_tasks SET state=?,payload=?,updated=? WHERE id=?",
                            (
                                TaskState.Name(task.status.state),
                                MessageToJson(task),
                                time.time(),
                                task.id,
                            ),
                        )

        await asyncio.to_thread(work)

    @staticmethod
    def _owner(scope):
        return (scope.user_id, scope.thread_id, scope.run_id)

    async def save(self, task, context):
        scope = scope_from_context(context)
        if task.context_id != scope.run_id:
            raise InvalidParamsError(message="Task context does not match authenticated run")

        def work():
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM a2a_tasks WHERE id=?", (task.id,)).fetchone()
                if row and tuple(row[k] for k in ("user_id", "thread_id", "run_id")) != self._owner(
                    scope
                ):
                    raise TaskNotFoundError()
                now = time.time()
                db.execute(
                    """INSERT INTO a2a_tasks
                    (id,context_id,state,payload,created,updated,user_id,thread_id,run_id)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    state=excluded.state,payload=excluded.payload,updated=excluded.updated""",
                    (
                        task.id,
                        task.context_id,
                        TaskState.Name(task.status.state),
                        MessageToJson(task),
                        now,
                        now,
                        *self._owner(scope),
                    ),
                )

        await asyncio.to_thread(work)

    async def get(self, task_id, context):
        scope = scope_from_context(context)

        def work():
            with self._db() as db:
                row = db.execute(
                    """SELECT payload FROM a2a_tasks WHERE id=?
                    AND user_id=? AND thread_id=? AND run_id=?""",
                    (task_id, *self._owner(scope)),
                ).fetchone()
                return Parse(row[0], Task()) if row else None

        return await asyncio.to_thread(work)

    async def list(self, params, context):
        scope = scope_from_context(context)
        if params.context_id and params.context_id != scope.run_id:
            return ListTasksResponse()

        def work():
            with self._db() as db:
                rows = db.execute(
                    """SELECT payload FROM a2a_tasks WHERE
                    user_id=? AND thread_id=? AND run_id=? ORDER BY created,id""",
                    self._owner(scope),
                ).fetchall()
            tasks = [Parse(r[0], Task()) for r in rows]
            if params.status:
                tasks = [t for t in tasks if t.status.state == params.status]
            try:
                start = int(params.page_token or "0")
            except ValueError:
                raise InvalidParamsError(message="Invalid page token") from None
            if start < 0:
                raise InvalidParamsError(message="Invalid page token")
            size = params.page_size if params.HasField("page_size") else 50
            end = start + size
            return ListTasksResponse(
                tasks=tasks[start:end], next_page_token=str(end) if end < len(tasks) else ""
            )

        return await asyncio.to_thread(work)

    async def delete(self, task_id, context):
        scope = scope_from_context(context)

        def work():
            with self._db() as db:
                db.execute(
                    "DELETE FROM a2a_tasks WHERE id=? AND user_id=? AND thread_id=? AND run_id=?",
                    (task_id, *self._owner(scope)),
                )

        await asyncio.to_thread(work)

    async def journal_get(self, task_id, key, context):
        if await self.get(task_id, context) is None:
            raise TaskNotFoundError()

        def work():
            with self._db() as db:
                row = db.execute(
                    "SELECT payload FROM execution_journal WHERE task_id=? AND entry_key=?",
                    (task_id, key),
                ).fetchone()
                return json.loads(row[0]) if row else None

        return await asyncio.to_thread(work)

    async def journal_put(self, task_id, key, payload, context):
        if await self.get(task_id, context) is None:
            raise TaskNotFoundError()

        def work():
            with self._db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO execution_journal VALUES(?,?,?)",
                    (task_id, key, json.dumps(payload, ensure_ascii=False)),
                )

        await asyncio.to_thread(work)
