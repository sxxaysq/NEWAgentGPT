"""Run-scoped bearer tokens: the only source of identity inside an agent.

A2A messages arrive over the network, so nothing in the body may be trusted for
identity. Every agent resolves ``actor_id`` from the token only. This is what keeps
the data-isolation rule ("a model cannot name another user") true after the split
into separate processes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

TOKEN_TTL = 900


@dataclass(frozen=True)
class RunScope:
    user_id: str
    thread_id: str
    run_id: str
    knowledge_base: str = ""

    @property
    def context_id(self) -> str:
        return self.run_id


class TokenError(ValueError):
    """The bearer token is missing, forged, expired or belongs to another run."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())


def issue_token(secret: str, scope: RunScope, ttl: int = TOKEN_TTL) -> str:
    payload = {
        "sub": scope.user_id,
        "th": scope.thread_id,
        "run": scope.run_id,
        "kb": scope.knowledge_base,
        "exp": int(time.time()) + ttl,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _sign(secret, body)


def verify_token(secret: str, token: str) -> RunScope:
    if not token:
        raise TokenError("缺少调用凭据")
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        raise TokenError("凭据格式无效") from None
    if not hmac.compare_digest(_sign(secret, body), signature):
        raise TokenError("凭据签名无效")
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        raise TokenError("凭据内容无效") from None
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("凭据已过期")
    user_id, thread_id, run_id = payload.get("sub"), payload.get("th"), payload.get("run")
    if not (user_id and thread_id and run_id):
        raise TokenError("凭据缺少运行范围")
    return RunScope(user_id, thread_id, run_id, payload.get("kb", ""))


def bearer_from_header(header: str | None) -> str:
    if not header:
        return ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def scope_from_context(context):
    """Only middleware-verified identity may authorize persistence."""
    scope = context.state.get("run_scope") if context is not None else None
    if not isinstance(scope, RunScope):
        raise TokenError("缺少已验证的运行范围")
    return scope


def remote_task_id(scope, message_id):
    import uuid

    return uuid.uuid5(
        uuid.NAMESPACE_URL, json.dumps([scope.user_id, scope.thread_id, scope.run_id, message_id])
    ).hex
