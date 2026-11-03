"""SDK route assembly, authentication and application executor adapter."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from a2a.helpers import get_message_text, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.tasks import TaskUpdater
from a2a.utils.errors import InvalidParamsError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentgpt.a2a import types as t
from agentgpt.a2a.auth import (
    TokenError,
    bearer_from_header,
    remote_task_id,
    scope_from_context,
    verify_token,
)


class InputRequired(Exception):
    """The operation needs caller input before it can safely continue."""


class TaskRejected(Exception):
    """The business request is outside the agent's supported operations."""


class Context:
    def __init__(self, runtime, request, queue):
        self.runtime = runtime
        self.request = request
        self.scope = scope_from_context(request.call_context)
        self.task_id = request.task_id
        self.data = t.part_data(request.message.parts)
        self.text = get_message_text(request.message)
        self.updater = TaskUpdater(queue, request.task_id, self.scope.run_id)
        self.emitted = 0

    async def progress(self, text):
        await self.updater.update_status(t.WORKING, new_text_message(text))

    async def emit(self, artifact):
        await self.updater.add_artifact(
            parts=list(artifact.parts),
            artifact_id=artifact.artifact_id or uuid4().hex,
            name=artifact.name,
            metadata=dict(artifact.metadata),
            last_chunk=True,
        )
        self.emitted += 1

    async def load(self, key):
        return await self.runtime.store.journal_get(self.task_id, key, self.request.call_context)

    async def save(self, key, payload):
        await self.runtime.store.journal_put(self.task_id, key, payload, self.request.call_context)


class _CallContextBuilder(ServerCallContextBuilder):
    def build(self, request):
        return ServerCallContext(
            state={"run_scope": request.state.run_scope, "headers": dict(request.headers)}
        )


class _RequestContextBuilder:
    async def build(self, context, params=None, task_id=None, context_id=None, task=None):
        scope = scope_from_context(context)
        if context_id and context_id != scope.run_id:
            raise InvalidParamsError(message="contextId must match authenticated run")
        if params.message.role != t.Role.ROLE_USER:
            raise InvalidParamsError(message="Client message must have ROLE_USER")
        return RequestContext(
            call_context=context,
            request=params,
            task_id=task_id or remote_task_id(scope, params.message.message_id),
            context_id=scope.run_id,
            task=task,
        )


class _Executor(AgentExecutor):
    def __init__(self, runtime):
        self.runtime = runtime

    async def execute(self, context, event_queue):
        ctx = Context(self.runtime, context, event_queue)
        current = context.current_task or t.Task(id=ctx.task_id, context_id=ctx.scope.run_id)
        current.metadata.update({"restart_recovery": False})
        await self.runtime.store.save(current, context.call_context)
        await ctx.updater.update_status(t.WORKING)
        try:
            await self.runtime.handler(ctx)
            await ctx.updater.update_status(t.COMPLETED if ctx.emitted else t.FAILED)
        except InputRequired as exc:
            await ctx.updater.update_status(t.INPUT_REQUIRED, new_text_message(str(exc)))
        except TaskRejected as exc:
            await ctx.updater.update_status(t.REJECTED, new_text_message(str(exc)))
        except Exception as exc:
            await ctx.updater.update_status(
                t.FAILED, new_text_message(f"{type(exc).__name__}: {exc}"[:800])
            )

    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).update_status(
            t.CANCELED
        )


class _RequestHandler(DefaultRequestHandler):
    """Deduplicate repeated messages while leaving wire handling to the SDK."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message_locks = {}

    def _key(self, params, context):
        scope = scope_from_context(context)
        if params.message.context_id and params.message.context_id != scope.run_id:
            raise InvalidParamsError(message="contextId must match authenticated run")
        return params.message.task_id or remote_task_id(scope, params.message.message_id)

    async def on_message_send(self, params, context):
        key = self._key(params, context)
        async with self.message_locks.setdefault(key, asyncio.Lock()):
            saved = await self.task_store.get(key, context)
            if saved and saved.status.state in t.TERMINAL_STATES:
                return saved
            return await super().on_message_send(params, context)

    async def on_message_send_stream(self, params, context):
        key = self._key(params, context)
        async with self.message_locks.setdefault(key, asyncio.Lock()):
            saved = await self.task_store.get(key, context)
            if saved and saved.status.state in t.TERMINAL_STATES:
                yield saved
                return
            async for event in super().on_message_send_stream(params, context):
                yield event


class AgentRuntime:
    def __init__(self, card, handler, store, secret):
        self.card, self.handler, self.store, self.secret = card, handler, store, secret
        self.sdk_handler = _RequestHandler(
            agent_executor=_Executor(self),
            task_store=store,
            agent_card=card,
            request_context_builder=_RequestContextBuilder(),
        )

    def build(self, lifespan=None):
        @asynccontextmanager
        async def lifecycle(app):
            try:
                if lifespan:
                    async with lifespan(app):
                        yield
                else:
                    yield
            finally:
                await self.sdk_handler.aclose()

        app = FastAPI(title=self.card.name, lifespan=lifecycle)

        @app.middleware("http")
        async def authenticate(request: Request, call_next):
            if request.url.path not in {"/.well-known/agent-card.json", "/health"}:
                try:
                    request.state.run_scope = verify_token(
                        self.secret, bearer_from_header(request.headers.get("authorization"))
                    )
                except TokenError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=401)
            return await call_next(request)

        app.router.routes.extend(create_agent_card_routes(self.card))
        app.router.routes.extend(
            create_jsonrpc_routes(
                self.sdk_handler, rpc_url="/", context_builder=_CallContextBuilder()
            )
        )

        @app.get("/health")
        async def health():
            return {"status": "ok", "agent": self.card.name, "a2a_sdk": "1.3.0"}

        return app
