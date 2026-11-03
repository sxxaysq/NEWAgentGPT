"""Snapshot-bounded conversation context and preflight model input accounting.

No character slice is allowed to silently remove a constraint. Optional blocks
leave as whole records; oversized required content fails before a model request.
The UTF-8 fallback is an explicitly uncalibrated operating limit, not a tokenizer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


class ContextBudgetError(ValueError):
    """Required content cannot fit; the durable original remains available."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


@lru_cache(maxsize=4)
def _tokenizer(path):
    try:
        from transformers import AutoTokenizer
    except ImportError:
        from tokenizers import Tokenizer

        class TextTokenizer:
            def __init__(self):
                file = Path(path)
                self.backend = Tokenizer.from_file(
                    str(file / "tokenizer.json" if file.is_dir() else file)
                )

            def encode(self, text, add_special_tokens=False):
                return self.backend.encode(text, add_special_tokens=add_special_tokens).ids

        return TextTokenizer()

    return AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)


class TokenBudget:
    def __init__(self, settings, output_tokens=0):
        self.window = settings.model_context_tokens or settings.context_fallback_tokens
        self.output = output_tokens
        self.margin = settings.model_token_safety_margin
        self.tokenizer_url = settings.model_tokenizer_url
        self.model_name = settings.model_name
        self.api_key = settings.model_api_key
        self.tokenizer = None
        self.mode = "estimated_utf8_unverified"
        if settings.model_tokenizer_path:
            try:
                self.tokenizer = _tokenizer(settings.model_tokenizer_path)
                if settings.model_context_tokens and hasattr(self.tokenizer, "apply_chat_template"):
                    self.mode = "local_chat_template_unverified"
            except Exception as exc:
                log.warning("tokenizer unavailable; estimated budget guard: %s", type(exc).__name__)

    def count_text(self, text):
        if self.tokenizer:
            return len(self.tokenizer.encode(text, add_special_tokens=False))
        return len(text.encode("utf-8"))

    def count_request(self, messages, tools=None):
        from langchain_core.messages import convert_to_openai_messages

        wire = convert_to_openai_messages(messages)
        if self.tokenizer and hasattr(self.tokenizer, "apply_chat_template"):
            try:
                return len(
                    self.tokenizer.apply_chat_template(
                        wire,
                        tools=tools or None,
                        tokenize=True,
                        add_generation_prompt=True,
                        enable_thinking=False,
                    )
                )
            except Exception as exc:
                log.warning("chat template failed; estimated budget guard: %s", type(exc).__name__)
        # Includes schema, message metadata/tool call arguments and wrapper overhead.
        self.mode = "estimated_utf8_unverified"
        return len(_json({"messages": wire, "tools": tools or []}).encode("utf-8")) + 64

    def check(self, messages, tools=None):
        tokens = self.count_request(messages, tools)
        self._validate(tokens)
        return tokens

    async def check_async(self, messages, tools=None):
        if not self.tokenizer_url:
            return self.check(messages, tools)
        import httpx
        from langchain_core.messages import convert_to_openai_messages

        payload = {
            "model": self.model_name,
            "messages": convert_to_openai_messages(messages),
            "add_generation_prompt": True,
            "add_special_tokens": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                response = await client.post(
                    self.tokenizer_url,
                    json=payload,
                    headers={"Authorization": "Bearer " + self.api_key},
                )
                response.raise_for_status()
                result = response.json()
            tokens = int(result["count"])
            if tokens < 0:
                raise ValueError("invalid tokenizer count")
            server_window = int(result.get("max_model_len") or 0)
            if server_window:
                self.window = min(self.window, server_window)
        except Exception as exc:
            raise ContextBudgetError(
                "模型计数服务暂时不可用，本次请求未发送；原文已保留，请稍后重试。"
            ) from exc
        self.mode = "server_tokenize"
        self._validate(tokens)
        return tokens

    def _validate(self, tokens):
        log.info(
            "context preflight mode=%s input=%d output=%d margin=%d window=%d",
            self.mode,
            tokens,
            self.output,
            self.margin,
            self.window,
        )
        if tokens + self.output + self.margin > self.window:
            raise ContextBudgetError(
                "本次请求的必需正文、约束或工具结果超过当前上下文预算；"
                "原始输入已保留，请拆分材料或缩小处理范围。"
                f"（计数模式 {self.mode}，输入 {tokens}，窗口 {self.window}）"
            )


class ContextAssembler:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.counter = TokenBudget(settings)

    async def assemble(
        self,
        user_id,
        thread_id,
        query,
        *,
        before_input_seq,
        memory_context="",
        memory_revision=0,
        scope="",
    ) -> dict:
        snapshot = await self.store.context_snapshot(user_id, thread_id, before_input_seq)
        session = copy.deepcopy(snapshot.get("session") or {})
        watermark = session.get("covered_until_input_seq", -1)
        if watermark >= before_input_seq:
            # Never use state computed from a request still queued after this one.
            session, watermark = {}, -1
        if "constraints" in session:
            session["constraints"] = [
                item for item in session["constraints"] if item.get("status", "active") == "active"
            ]
        pending = [
            {key: item[key] for key in ("id", "input_seq", "turn_id", "text") if key in item}
            for item in snapshot.get("inputs", [])
            if watermark < item["input_seq"] < before_input_seq
        ]
        turns = snapshot.get("turns", [])
        previous = next((item for item in reversed(turns) if item.get("assistant_text")), None)
        previous_answer = {}
        if previous:
            previous_answer = {
                "turn_id": previous["id"],
                "text": previous["assistant_text"],
                "turn_revision": previous.get("turn_revision", 0),
            }
            artifact = next(
                (
                    item
                    for item in reversed(snapshot.get("artifacts", []))
                    if item.get("turn_id") == previous["id"]
                ),
                None,
            )
            if artifact:
                previous_answer.update(
                    artifact_id=artifact["id"],
                    version=artifact["version"],
                    content_hash=artifact["content_hash"],
                )
        summary = snapshot.get("summary") or {}
        context = {
            "session": session,
            "pending_user_updates": pending,
            "previous_answer": previous_answer,
            "recent_turns": turns[-3:],
            "thread_summary": summary,
            "turn_summaries": snapshot.get("turn_summaries", [])[-20:],
            "memory_context": memory_context,
            "manifest": {
                "user_id": user_id,
                "thread_id": thread_id,
                "before_input_seq": before_input_seq,
                "memory_scope": scope,
                "memory_revision": memory_revision,
                "state_version": session.get("version", 0),
                "summary_version": summary.get("summary_version", 0),
                "query_hash": hashlib.sha256(query.encode()).hexdigest(),
                "count_mode": self.counter.mode,
            },
        }
        # Mandatory state, new corrections, and the complete target answer remain.
        # Older narrative leaves whole, preserving negations, IDs and table rows.
        budget = self.settings.context_history_tokens
        for field in ("memory_context", "turn_summaries", "thread_summary", "recent_turns"):
            if self.counter.count_text(_json(context)) <= budget:
                break
            context[field] = (
                "" if field == "memory_context" else ([] if field.endswith("s") else {})
            )
        # Context provenance is deliberately separate from document/web evidence.
        # Build the allowlist only from blocks that survived selection.
        sources = []
        if isinstance(context["memory_context"], list):
            context["memory_context"] = copy.deepcopy(context["memory_context"])
            for item in context["memory_context"]:
                identifier = f"memory:{item['id']}:v{item['version']}"
                item["context_id"] = identifier
                sources.append(
                    {
                        "context_id": identifier,
                        "kind": "user_memory",
                        "text": item["content"],
                        "quote": item.get("source_quote", ""),
                        "memory_id": item["id"],
                        "version": item["version"],
                        "source_input_id": item.get("source_message", ""),
                        "source_thread": item.get("source_thread", ""),
                    }
                )
        inputs = {
            item["id"]: item
            for item in snapshot.get("inputs", [])
            if item["input_seq"] < before_input_seq
        }
        excerpts = {}
        for item in session.get("constraints", []) + session.get("objects", []):
            source_id, quote = item.get("source_input_id"), item.get("quote", "")
            source = inputs.get(source_id)
            if source and quote and quote in source["text"]:
                excerpts.setdefault(source_id, set()).add(quote)
        for item in pending:
            excerpts[item["id"]] = {item["text"]}
        for source_id, quotes in excerpts.items():
            sources.append(
                {
                    "context_id": "input:" + source_id,
                    "kind": "user_input",
                    "source_input_id": source_id,
                    "text": "\n".join(sorted(quotes)),
                }
            )
        for source in sources:
            source.update(user_id=user_id, thread_id=thread_id)
        context["context_sources"] = sources
        context["manifest"]["context_ids"] = [source["context_id"] for source in sources]
        # A soft history allocation never licenses deleting an active constraint.
        # Complete request checks at each role enforce the actual hard limit.
        context["manifest"]["history_soft_limit_exceeded"] = (
            self.counter.count_text(_json(context)) > budget
        )
        return context


def role_context(data: dict) -> dict:
    """Select only the Host-supplied context; never infer it from agent messages."""
    return {
        key: data[key]
        for key in (
            "current_request",
            "session",
            "pending_user_updates",
            "previous_answer",
            "recent_turns",
            "thread_summary",
            "turn_summaries",
            "memory_context",
            "context_sources",
            "manifest",
        )
        if key in data
    }


def authorized_context_sources(data: dict, user_id: str, thread_id: str) -> dict:
    """Only authenticated Host-selected context can support personal conversation claims."""
    manifest = data.get("manifest") or {}
    if manifest.get("user_id") != user_id or manifest.get("thread_id") != thread_id:
        return {}
    allowed = set(manifest.get("context_ids") or [])
    return {
        source["context_id"]: source
        for source in data.get("context_sources", [])
        if source.get("context_id") in allowed
        and source.get("user_id") == user_id
        and source.get("thread_id") == thread_id
        and source.get("kind") in {"user_memory", "user_input", "current_user_request"}
    }
