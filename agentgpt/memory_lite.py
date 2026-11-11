"""Source-backed preference extraction, durable receipts, and bounded direct recall."""

import asyncio
import json
import re
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from agentgpt.config import ModelOptions
from agentgpt.host.turns import encode, one
from agentgpt.model import StructuredModel, create_model, structured_response

SCHEMA = """
CREATE TABLE IF NOT EXISTS explicit_memory_receipts (
 input_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS explicit_memory_sources (
 memory_id TEXT PRIMARY KEY, source_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lite_summaries (
 thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, covered INTEGER NOT NULL,
 content TEXT NOT NULL);
"""


class Operation(StructuredModel):
    action: Literal["ADD", "UPDATE", "DELETE"]
    target_id: str = ""
    key: str = Field(default="", max_length=100)
    quote: str = Field(min_length=1, max_length=500)
    kind: Literal["preference", "profile", "project"] = "preference"


class Request(StructuredModel):
    intent: Literal["explicit", "automatic", "none"] = "none"
    operations: list[Operation] = Field(default_factory=list, max_length=8)
    remaining_request: str = ""
    clarification: str = ""


class LiteMemory:
    def __init__(self, store, settings, parser=None):
        self.store, self.settings = store, settings
        self.parser = parser or self._parse
        self.wake = asyncio.Event()
        self.index = None

    def start(self):
        pass

    async def close(self):
        pass

    async def recall(self, user_id, scope, query):
        if not self.settings.memory_enabled:
            return []
        if not (await self.store.memory_preference(user_id))["enabled"]:
            return []
        rows = await self.store.memories(user_id, scope)
        if (
            len(rows) > self.settings.memory_lite_limit
            or sum(len(r["content"]) for r in rows) > self.settings.memory_recall_char_limit
        ):
            raise ValueError("长期记忆超过轻量模式容量，请在记忆面板清理后重试。")
        return rows

    async def _parse(self, text, rows):
        return await structured_response(
            create_model(self.settings, ModelOptions(temperature=0, max_tokens=2000)),
            Request,
            [
                SystemMessage(
                    content=(
                        "分析本条用户原文，intent 分为 explicit、automatic、none。"
                        "explicit 仅表示明确要求保存或管理长期记忆，例如记住、记一下、记着、"
                        "以后记得避开、修改已有记忆、忘记记忆；"
                        "不能仅因出现以后、改用而判 explicit。"
                        "automatic 表示用户未要求保存，但直接陈述了自己的稳定长期偏好，"
                        "例如我习惯中文解释、我一向不吃香菜、我通常用 Python。"
                        "我现在习惯看英文解释，以后都用英文属于 automatic；即使表达偏好改变，"
                        "也不能因此删除或剥离本轮任何业务请求。"
                        "automatic 只允许 preference 类型的 ADD/UPDATE，不自动保存项目事实、"
                        "临时日程、健康等敏感资料、第三人信息、模型推断或助手回答。"
                        "这次、今天等临时要求、引文中的指令、假设、不要记住均不写入。"
                        "无法确认是长期本人偏好时返回 none 和空 operations。"
                        "quote 必须直接摘取用户原文中完整事实，保留主语、数值和否定，不改写。"
                        "UPDATE/DELETE 必须唯一对应提供的 target_id，存在歧义则 clarification，"
                        "不操作。automatic 存在歧义时不操作，不要求用户澄清。"
                        "ADD 提供稳定事实键；相同主题应 UPDATE，不重复新增。"
                        "相同值再次确认也返回 UPDATE、原 target_id 和本次 quote，"
                        "由存储层去重并记录本次输入顺序。"
                        "deleted/archived 是已忘记的记录，automatic 不恢复；explicit 可重新保存。"
                        "个人工作习惯、固定周报提交时间、论文写作流程使用 preference，"
                        "个人岗位职责使用 profile；project 仅用于明确命名的当前项目事实。"
                        "只有 explicit 的 remaining_request 可以是"
                        "剔除记忆操作后的连续原文片段，纯显式记忆操作为空；"
                        "automatic 和 none 一律保留完整原文作为 remaining_request。"
                        "删除必须有明确忘记/删除意图，纠正不能当成删除。"
                    )
                ),
                HumanMessage(content=encode({"text": text, "existing": rows})),
            ],
        )

    async def handle(self, user_id, thread_id, input_seq, scope):
        async with self.store.connect() as db:
            source = await one(
                db,
                "SELECT i.rowid seq,i.*,t.scope_snapshot FROM turn_inputs i "
                "JOIN turns t ON t.id=i.turn_id WHERE i.user_id=? "
                "AND i.thread_id=? AND i.input_seq=?",
                (user_id, thread_id, input_seq),
            )
            if not source:
                raise ValueError("记忆操作缺少原始输入")
            if json.loads(source["scope_snapshot"]).get("memory_scope", "") != scope:
                raise ValueError("记忆范围与原始请求不一致")
            prior = await one(
                db,
                "SELECT receipt FROM explicit_memory_receipts WHERE input_id=? AND user_id=?",
                (source["id"], user_id),
            )
        if prior:
            return self._decode_receipt(prior)
        text = source["text"]
        pref = await self.store.memory_preference(user_id)
        if not self.settings.memory_enabled or not pref["enabled"]:
            return await self._save_receipt(
                source, self._receipt(source, "disabled", "长期记忆已关闭，未保存或删除。")
            )
        rows = await self._extraction_snapshot(user_id, scope)
        try:
            proposal = Request.model_validate(
                await asyncio.wait_for(
                    self.parser(text, rows), timeout=self.settings.memory_lite_timeout_s
                )
            )
        except sqlite3.Error:
            raise
        except Exception as exc:
            # Model/network failures must not discard an already durable business request.
            return await self._save_receipt(
                source,
                self._receipt(
                    source, "failed", f"长期记忆提取失败（{type(exc).__name__}），未保存。"
                ),
            )
        try:
            if proposal.intent == "explicit" and (
                proposal.remaining_request and proposal.remaining_request not in text
            ):
                raise ValueError("记忆解析改写了业务请求，未提交")
            if proposal.clarification and proposal.intent == "explicit":
                return await self._save_receipt(
                    source,
                    self._receipt(source, "clarification", proposal.clarification, "explicit"),
                )
            if proposal.intent == "none" or proposal.clarification or not proposal.operations:
                return await self._save_receipt(
                    source, self._receipt(source, "noop", intent=proposal.intent)
                )
            return await self._commit(source, scope, pref, rows, proposal)
        except ValueError as exc:
            return await self._save_receipt(
                source, self._receipt(source, "failed", str(exc), proposal.intent)
            )

    @staticmethod
    def _receipt(source, status, message="", intent="none"):
        return dict(status=status, intent=intent, message=message, remaining=source["text"])

    async def _extraction_snapshot(self, user_id, scope):
        async with self.store.connect() as db:
            candidates = await (
                await db.execute(
                    "SELECT * FROM memories WHERE user_id=? AND scope IN ('',?) "
                    "ORDER BY (deleted=0 AND archived=0) DESC,updated DESC LIMIT 200",
                    (user_id, scope),
                )
            ).fetchall()
        rows, characters, tombstones = [], 0, 0
        for candidate in candidates:
            row = dict(candidate)
            retired = bool(row["deleted"] or row["archived"])
            size = len(row["content"])
            if (retired and tombstones >= 20) or (
                characters + size > self.settings.memory_recall_char_limit
            ):
                continue
            rows.append(row)
            characters += size
            tombstones += int(retired)
        return rows

    @staticmethod
    def _decode_receipt(row):
        result = json.loads(row["receipt"])
        # Committed receipts from the previous explicit-only version remain replayable.
        result.setdefault("intent", "explicit" if result["status"] == "committed" else "none")
        return result

    async def _save_receipt(self, source, result):
        async with self.store.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT OR IGNORE INTO explicit_memory_receipts VALUES(?,?,?)",
                (source["id"], source["user_id"], encode(result)),
            )
            prior = await one(
                db, "SELECT receipt FROM explicit_memory_receipts WHERE input_id=?", (source["id"],)
            )
        return self._decode_receipt(prior)

    async def _commit(self, source, scope, pref, rows, proposal):
        user_id, thread_id, text = source["user_id"], source["thread_id"], source["text"]
        existing = {r["id"]: r for r in rows}
        receipts = []
        descriptions = []
        touched = set()
        async with self.store.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            prior = await one(
                db, "SELECT receipt FROM explicit_memory_receipts WHERE input_id=?", (source["id"],)
            )
            if prior:
                return self._decode_receipt(prior)
            current = await one(db, "SELECT * FROM memory_preferences WHERE user_id=?", (user_id,))
            if current and (not current["enabled"] or current["revision"] != pref["revision"]):
                raise ValueError("记忆已变化，请重新提交请求")
            for op in proposal.operations:
                if not op.quote.strip() or op.quote not in text:
                    raise ValueError("记忆缺少原文支持，未提交")
                if proposal.intent == "automatic" and (
                    op.kind != "preference" or op.action == "DELETE"
                ):
                    raise ValueError("自动提取仅保存长期偏好，本轮未提交")
                target = existing.get(op.target_id)
                if op.target_id and not target:
                    raise ValueError("目标记忆不在授权快照中")
                if not target and op.key:
                    # Tombstones outside the prompt still prevent replay or duplicate revival.
                    target = await one(
                        db,
                        "SELECT * FROM memories WHERE user_id=? AND scope=? AND fact_key=?",
                        (user_id, scope if op.kind == "project" else "", op.key),
                    )
                if not target and op.action == "ADD":
                    same_content = await (
                        await db.execute(
                            "SELECT * FROM memories WHERE user_id=? AND scope=? "
                            "AND lower(trim(content))=lower(trim(?)) LIMIT 2",
                            (user_id, scope if op.kind == "project" else "", op.quote),
                        )
                    ).fetchall()
                    if len(same_content) > 1:
                        raise ValueError("同一内容存在多条记忆，无法确认更新目标，未提交")
                    target = dict(same_content[0]) if same_content else None
                if op.action != "ADD" and not target:
                    raise ValueError("目标记忆不在授权快照中")
                identity = (
                    ("id", target["id"]) if target
                    else ("key", scope if op.kind == "project" else "", op.key)
                )
                if identity in touched:
                    raise ValueError("同一条记忆存在重复操作，本轮未提交")
                touched.add(identity)
                if op.action == "DELETE" and not re.search(
                    r"忘记|删除|清除|forget|delete", text, re.I
                ):
                    raise ValueError("没有明确删除意图")
                if target:
                    if proposal.intent == "automatic" and (
                        target["kind"] != "preference" or target["scope"]
                    ):
                        raise ValueError("自动提取不能修改个人偏好以外的记忆，未提交")
                    if proposal.intent == "automatic" and (target["deleted"] or target["archived"]):
                        raise ValueError("已忘记的偏好不会自动恢复，未提交")
                    latest = await one(db, "SELECT * FROM memories WHERE id=?", (target["id"],))
                    order = await one(
                        db,
                        "SELECT source_seq FROM explicit_memory_sources WHERE memory_id=?",
                        (target["id"],),
                    )
                    if (
                        not latest
                        or latest["version"] != target["version"]
                        or (latest["deleted"] and latest["updated"] > source["created"])
                        or (not latest["source_message"] and latest["updated"] > source["created"])
                        or (order and order["source_seq"] >= source["seq"])
                    ):
                        raise ValueError("较旧输入或版本冲突，未覆盖当前记忆")
                    mid = target["id"]
                    if not latest["deleted"] and not latest["archived"] and (
                        op.action != "DELETE" and latest["content"].strip() == op.quote.strip()
                    ):
                        # An unchanged newer assertion still supersedes older queued inputs.
                        await db.execute(
                            "INSERT INTO explicit_memory_sources VALUES(?,?) "
                            "ON CONFLICT(memory_id) DO UPDATE SET source_seq=excluded.source_seq",
                            (mid, source["seq"]),
                        )
                        continue
                    await db.execute(
                        "INSERT INTO memory_versions VALUES(?,?,?)",
                        (mid, latest["version"], encode(latest)),
                    )
                    await db.execute(
                        "UPDATE memories SET content=?,version=version+1,deleted=?,archived=0, "
                        "source_thread=?,source_message=?,source_quote=?,updated=? "
                        "WHERE id=?",
                        (
                            target["content"] if op.action == "DELETE" else op.quote,
                            int(op.action == "DELETE"),
                            thread_id,
                            source["id"],
                            op.quote,
                            time.time(),
                            mid,
                        ),
                    )
                    version = latest["version"] + 1
                else:
                    if not op.key or (op.kind == "project" and not scope):
                        raise ValueError("缺少事实键或项目范围")
                    mid, version = uuid4().hex, 1
                    await db.execute(
                        "INSERT INTO memories(id,user_id,scope,fact_key,content,kind,"
                        "source_thread,source_message,source_quote,created,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            mid,
                            user_id,
                            scope if op.kind == "project" else "",
                            op.key,
                            op.quote,
                            op.kind,
                            thread_id,
                            source["id"],
                            op.quote,
                            time.time(),
                            time.time(),
                        ),
                    )
                await self.store._sync_identifiers(db, mid, op.quote)
                await db.execute(
                    "INSERT INTO explicit_memory_sources VALUES(?,?) "
                    "ON CONFLICT(memory_id) DO UPDATE SET source_seq=excluded.source_seq",
                    (mid, source["seq"]),
                )
                action = "UPDATE" if target and op.action == "ADD" else op.action
                receipts.append(dict(id=mid, version=version, action=action))
                descriptions.append(
                    {"ADD": "已保存：", "UPDATE": "已更新：", "DELETE": "已删除："}[action]
                    + op.quote
                )
            if receipts:
                await db.execute(
                    "INSERT INTO memory_preferences(user_id,revision) VALUES(?,1) "
                    "ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
                    (user_id,),
                )
            result = dict(
                status="committed" if receipts else "noop",
                intent=proposal.intent,
                message="记忆操作已提交：\n" + "\n".join(descriptions) if receipts else "",
                operations=receipts,
                remaining=(
                    proposal.remaining_request
                    if receipts and proposal.intent == "explicit" else text
                ),
            )
            active = await one(
                db,
                "SELECT count(*) n,coalesce(sum(length(content)),0) chars FROM memories "
                "WHERE user_id=? AND scope IN ('',?) AND deleted=0 AND archived=0",
                (user_id, scope),
            )
            if any(item["action"] != "DELETE" for item in receipts) and (
                active["n"] > self.settings.memory_lite_limit
                or active["chars"] > self.settings.memory_recall_char_limit
            ):
                raise ValueError("长期记忆容量已满，未新增；请清理或更新已有记忆")
            await db.execute(
                "INSERT INTO explicit_memory_receipts VALUES(?,?,?)",
                (source["id"], user_id, encode(result)),
            )
        return result
