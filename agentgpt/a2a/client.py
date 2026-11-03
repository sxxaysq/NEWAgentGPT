"""Host client using the official SDK JSON-RPC transport and ProtoJSON types."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

import httpx
from a2a.client.errors import A2AClientError, A2AClientTimeoutError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.helpers import get_message_text
from a2a.types import CancelTaskRequest, GetTaskRequest, SendMessageRequest
from a2a.utils.errors import A2AError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict, ParseDict

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import issue_token, remote_task_id


class AgentError(RuntimeError):
    def __init__(self, agent, message, state=None):
        super().__init__(f"[{agent}] {message}")
        self.agent, self.state = agent, state


class RecoveryPending(AgentError):
    """Transport unavailable: keep the durable run queued, not failed."""


@dataclass
class AgentEndpoint:
    role: str
    url: str
    card: object = None
    ok: bool = False
    error: str = ""


@dataclass
class Registry:
    endpoints: dict = field(default_factory=dict)

    @classmethod
    def from_urls(cls, urls):
        return cls({role: AgentEndpoint(role, url) for role, url in urls.items()})

    def url(self, role):
        return self.endpoints[role].url

    def status(self):
        return [
            {
                "role": e.role,
                "url": e.url,
                "ok": e.ok,
                "error": e.error,
                "skills": [s.id for s in e.card.skills] if e.card else [],
            }
            for e in self.endpoints.values()
        ]


class A2AClient:
    def __init__(self, registry, secret, timeout=180.0, http_transport=None):
        self.registry, self.secret, self.timeout = registry, secret, timeout
        self.http_transport = http_transport

    def _http(self, scope=None, timeout=None):
        headers = {"A2A-Version": "1.0"}
        if scope:
            headers["Authorization"] = "Bearer " + issue_token(self.secret, scope)
        return httpx.AsyncClient(
            timeout=timeout or self.timeout,
            trust_env=False,
            headers=headers,
            transport=self.http_transport,
        )

    async def discover(self):
        for role in self.registry.endpoints:
            await self.refresh(role)

    async def refresh(self, role):
        endpoint = self.registry.endpoints[role]
        try:
            async with self._http(timeout=10) as client:
                response = await client.get(endpoint.url + "/.well-known/agent-card.json")
                response.raise_for_status()
                endpoint.card = ParseDict(response.json(), t.AgentCard())
            endpoint.ok, endpoint.error = True, ""
            return True
        except (httpx.HTTPError, ValueError) as exc:
            endpoint.ok, endpoint.error = False, str(exc)[:200]
            return False

    def _transport(self, http, role):
        endpoint = self.registry.endpoints[role]
        return JsonRpcTransport(
            http, endpoint.card or t.AgentCard(), endpoint.url, is_shared_httpx=True
        )

    async def _send(self, role, scope, data, text, timeout, streaming, on_progress=None):
        fingerprint = hashlib.sha256(
            json.dumps([role, data, text], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        task_id = remote_task_id(scope, fingerprint)
        deadline = time.monotonic() + (timeout or self.timeout)
        try:
            async with self._http(scope, timeout) as http:
                transport = self._transport(http, role)
                # Get-before-send also closes the response-lost-after-commit window.
                while True:
                    try:
                        existing = await transport.get_task(GetTaskRequest(id=task_id))
                    except TaskNotFoundError:
                        existing = None
                    if existing is None or existing.status.state == t.INPUT_REQUIRED:
                        break
                    if existing.status.state in t.TERMINAL_STATES:
                        return existing
                    if time.monotonic() >= deadline:
                        raise RecoveryPending(role, "远端任务仍在执行，将继续查询原任务")
                    if on_progress and existing.status.HasField("message"):
                        await on_progress(get_message_text(existing.status.message))
                    await asyncio.sleep(0.5)
                if existing is not None and not MessageToDict(existing.metadata).get(
                    "restart_recovery", False
                ):
                    return existing
                message = t.Message(
                    role=t.Role.ROLE_USER,
                    message_id=fingerprint,
                    context_id=scope.run_id,
                    parts=[t.DataPart(data=data)],
                )
                if text:
                    message.parts.append(t.TextPart(text=text))
                if existing is not None:
                    message.task_id = existing.id
                request = SendMessageRequest(message=message)
                if streaming:
                    async for event in transport.send_message_streaming(request):
                        if event.HasField("status_update") and on_progress:
                            await on_progress(get_message_text(event.status_update.status.message))
                    return await transport.get_task(GetTaskRequest(id=task_id))
                response = await transport.send_message(request)
                if not response.HasField("task"):
                    raise AgentError(role, "代理未返回 Task")
                return response.task
        except (httpx.HTTPError, OSError) as exc:
            raise RecoveryPending(role, f"连接中断，保留原任务恢复：{exc}") from exc
        except A2AClientError as exc:
            if isinstance(exc, A2AClientTimeoutError) or isinstance(
                exc.__cause__, httpx.RequestError
            ):
                raise RecoveryPending(role, f"连接中断，保留原任务恢复：{exc}") from exc
            raise AgentError(role, str(exc)) from exc
        except A2AError as exc:
            raise AgentError(role, str(exc)) from exc

    async def send(self, role, scope, data, text="", timeout=None):
        return await self._send(role, scope, data, text, timeout, False)

    async def send_streaming(self, role, scope, data, text="", on_progress=None, timeout=None):
        return await self._send(role, scope, data, text, timeout, True, on_progress)

    async def get_task(self, role, scope, task_id):
        async with self._http(scope) as http:
            return await self._transport(http, role).get_task(GetTaskRequest(id=task_id))

    async def cancel(self, role, scope, task_id):
        async with self._http(scope) as http:
            return await self._transport(http, role).cancel_task(CancelTaskRequest(id=task_id))

    async def extra_get(self, role, path, scope):
        async with self._http(scope, 15) as http:
            response = await http.get(self.registry.url(role) + path)
            response.raise_for_status()
            return response.json()


def task_payload(task):
    if not task.artifacts:
        reason = get_message_text(task.status.message)
        raise AgentError("host", reason or f"任务 {task.id} 没有任何产物", task.status.state)
    return t.part_data(task.artifacts[-1].parts)
