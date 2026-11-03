"""Single application: API, static UI, graph lifecycle and persistent event stream."""

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

import portalocker
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from agentgpt.a2a.auth import RunScope
from agentgpt.a2a.client import A2AClient, Registry
from agentgpt.config import ROOT, ModelOptions, settings
from agentgpt.host.runner import RunManager
from agentgpt.host.summaries import SummaryService
from agentgpt.memory import MemoryService
from agentgpt.store import Store


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.a2a_state_dir.mkdir(parents=True, exist_ok=True)
    # The Host is the only writer of the business database.
    with portalocker.Lock(str(settings.database) + ".lock", timeout=0):
        async with application_services(app):
            yield


@asynccontextmanager
async def application_services(app):
    store = Store(settings.database)
    await store.initialize()
    registry = Registry.from_urls(settings.agent_urls())
    client = A2AClient(registry, settings.a2a_secret)
    await client.discover()
    memory = MemoryService(store, settings)
    summaries = SummaryService(store, settings)
    runs = RunManager(store, client, settings, memory=memory, summaries=summaries)
    app.state.store, app.state.client, app.state.runs = store, client, runs
    app.state.memory = runs.memory
    try:
        memory.start()
        summaries.start()
        await runs.recover()
        yield
    finally:
        await runs.close()
        await summaries.close()
        await memory.close()


app = FastAPI(title="AgentGPT · LangGraph + MCP", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "agentgpt/static"), name="static")


@app.middleware("http")
async def same_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin != expected:
        return PlainTextResponse("Cross-origin mutation rejected", status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


async def current_user(request: Request):
    user = await request.app.state.store.user_for_token(request.cookies.get("agentgpt_session", ""))
    if not user:
        raise HTTPException(401, "请先登录。")
    return user


async def owned_thread(thread_id: str, request: Request, user=Depends(current_user)):
    thread = await request.app.state.store.thread(thread_id)
    if not thread or thread["user_id"] != user["id"]:
        raise HTTPException(404, "会话不存在。")
    return thread


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=8, max_length=200)


class CreateThread(BaseModel):
    goal: str = Field(min_length=2, max_length=8000)
    settings: ModelOptions = Field(default_factory=ModelOptions)


class RunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "start",
        "pause",
        "continue",
        "stop",
        "chat",
        "summarize",
        "append",
        "followup",
        "provide_input",
    ]
    text: str = Field(default="", max_length=8000)
    question_id: str | None = Field(default=None, max_length=100)
    request_id: str | None = Field(default=None, min_length=8, max_length=100)


CATALOG_SCOPE = RunScope(user_id="catalog", thread_id="catalog", run_id="catalog")


async def tool_catalog(request: Request) -> dict:
    """Tool availability lives in the worker process; ask it, never guess."""
    try:
        return await request.app.state.client.extra_get("worker", "/tools", CATALOG_SCOPE)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as unavailable
        return {"tools": [], "servers": {"error": f"{type(exc).__name__}: {exc}"[:200]}}


@app.get("/health")
async def health(request: Request):
    return {
        "status": "ok",
        "model": settings.model_name,
        "agents": request.app.state.client.registry.status(),
        "image_generation_configured": bool(settings.image_api_base and settings.image_model),
        "memory_enabled": settings.memory_enabled,
        "judge_plan": settings.judge_plan,
        "max_parallel": settings.max_parallel,
        "document_embedding_model": settings.document_embedding_model
        if settings.document_embedding_url
        else settings.embedding_model,
        "document_embedding_dimensions": settings.document_embedding_dimensions
        if settings.document_embedding_url
        else None,
        "retrieval_query_count": settings.retrieval_query_count,
        "reranker_configured": bool(settings.rerank_url),
        "retrieval_neighbors": settings.retrieval_neighbors,
    }


@app.post("/api/auth/register", status_code=201)
async def register(body: Credentials, request: Request, response: Response):
    store = request.app.state.store
    try:
        await store.register(body.email.strip(), body.password)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该邮箱已注册。") from None
    token = await store.login(body.email.strip(), body.password)
    response.set_cookie("agentgpt_session", token, httponly=True, samesite="strict", max_age=604800)
    return await store.user_for_token(token)


@app.post("/api/auth/login")
async def login(body: Credentials, request: Request, response: Response):
    token = await request.app.state.store.login(body.email.strip(), body.password)
    if not token:
        raise HTTPException(401, "邮箱或密码不正确。")
    response.set_cookie("agentgpt_session", token, httponly=True, samesite="strict", max_age=604800)
    return await request.app.state.store.user_for_token(token)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    await request.app.state.store.logout(request.cookies.get("agentgpt_session", ""))
    response.delete_cookie("agentgpt_session")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user=Depends(current_user)):
    return user


@app.get("/api/tools")
async def tools(request: Request, user=Depends(current_user)):
    catalog = await tool_catalog(request)
    return {
        "tools": catalog.get("tools", []),
        "default_settings": ModelOptions().model_dump(),
        "servers": catalog.get("servers", {}),
        "agents": request.app.state.client.registry.status(),
        "model": settings.model_name,
    }


@app.get("/api/threads")
async def threads(request: Request, user=Depends(current_user)):
    return await request.app.state.store.threads(user["id"])


@app.post("/api/threads", status_code=201)
async def create_thread(body: CreateThread, request: Request, user=Depends(current_user)):
    thread = await request.app.state.store.create_thread(
        user["id"], body.goal.strip(), body.settings.model_dump()
    )
    return {"thread": thread}


@app.get("/api/threads/{thread_id}")
async def thread_detail(request: Request, thread=Depends(owned_thread)):
    view = await request.app.state.store.event_view(thread["id"])
    if not view["state"]:
        view["state"] = await request.app.state.runs.state(thread["id"])
    # The UI reads the event projection and its cursor together; newer events follow via SSE.
    thread["status"] = view["state"]["status"]
    return {
        "thread": thread,
        **view,
        "pending_tools": await request.app.state.store.pending_receipts(thread["id"]),
    }


@app.post("/api/threads/{thread_id}/commands", status_code=202)
async def command(body: RunCommand, request: Request, thread=Depends(owned_thread)):
    if thread["status"] == "ready" and body.operation not in {"start", "stop", "pause"}:
        raise HTTPException(409, "请先开始执行目标。")
    try:
        run_id = await request.app.state.runs.command(
            thread, body.operation, body.text, body.question_id, body.request_id
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True, "thread_id": thread["id"], "run_id": run_id}


class MemoryEdit(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    scope: str = Field(default="", max_length=80)
    kind: Literal["preference", "profile", "project"] = "preference"
    version: int | None = None


class MemoryPreference(BaseModel):
    enabled: bool


@app.get("/api/context-jobs")
async def context_jobs(request: Request, user=Depends(current_user)):
    return {"jobs": await request.app.state.store.context_job_status(user["id"])}


@app.post("/api/context-jobs/retry")
async def retry_context_jobs(request: Request, user=Depends(current_user)):
    count = await request.app.state.store.retry_context_jobs(user["id"])
    request.app.state.runs.summaries.wake.set()
    return {"retried": count}


@app.get("/api/threads/{thread_id}/artifacts/{artifact_id}")
async def turn_artifact(artifact_id: str, request: Request, thread=Depends(owned_thread)):
    artifact = await request.app.state.store.read_turn_artifact(
        thread["user_id"], thread["id"], artifact_id
    )
    if not artifact:
        raise HTTPException(404, "产物不存在。")
    return artifact


@app.get("/api/memories")
async def memories(request: Request, user=Depends(current_user)):
    store = request.app.state.store
    return {
        "items": await store.memories(user["id"]),
        "preference": await store.memory_preference(user["id"]),
        "jobs": await store.memory_job_status(user["id"]),
        "available": settings.memory_enabled,
    }


@app.patch("/api/memory-preference")
async def memory_preference(body: MemoryPreference, request: Request, user=Depends(current_user)):
    await request.app.state.store.set_memory_preference(user["id"], body.enabled)
    return {"ok": True}


@app.post("/api/memory-jobs/retry")
async def retry_memory_jobs(request: Request, user=Depends(current_user)):
    count = await request.app.state.store.retry_memory_jobs(user["id"])
    request.app.state.memory.wake.set()
    return {"retried": count}


@app.post("/api/memories", status_code=201)
async def add_memory(body: MemoryEdit, request: Request, user=Depends(current_user)):
    try:
        memory_id = await request.app.state.store.edit_memory(
            user["id"], body.content, scope=body.scope, kind=body.kind
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    request.app.state.memory.wake.set()
    return {"id": memory_id}


@app.patch("/api/memories/{memory_id}")
async def update_memory(
    memory_id: str, body: MemoryEdit, request: Request, user=Depends(current_user)
):
    try:
        await request.app.state.store.edit_memory(
            user["id"], body.content, memory_id=memory_id, expected_version=body.version
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    request.app.state.memory.wake.set()
    return {"ok": True}


@app.delete("/api/memories/{memory_id}")
async def delete_memory(memory_id: str, version: int, request: Request, user=Depends(current_user)):
    try:
        await request.app.state.store.edit_memory(
            user["id"], "", memory_id=memory_id, expected_version=version, delete=True
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    request.app.state.memory.wake.set()
    return {"ok": True}


class ReceiptResolution(BaseModel):
    call_key: str
    action: Literal["retry", "record_success", "record_failure"]
    result: str = Field(default="", max_length=16000)


@app.post("/api/threads/{thread_id}/resolve-tool")
async def resolve_tool(body: ReceiptResolution, request: Request, thread=Depends(owned_thread)):
    store = request.app.state.store
    if not body.call_key.startswith(thread["id"] + ":"):
        raise HTTPException(404, "调用不存在。")
    receipt = await store.receipt(body.call_key)
    if not receipt or receipt.get("status") != "pending":
        raise HTTPException(409, "调用已处理或不存在。")
    active = request.app.state.runs.running.get(thread["id"])
    if active and not active.done():
        raise HTTPException(409, "请等待当前运行停止后核实。")
    if body.action == "retry":
        await store.save_receipt(body.call_key, {**receipt, "status": "retry", "attempts": 0})
    else:
        if not body.result.strip():
            raise HTTPException(400, "请填写已核实的执行结果。")
        await store.save_receipt(
            body.call_key,
            {
                "status": "done",
                "resolved_by": thread["user_id"],
                "message": {
                    "content": body.result,
                    "tool_call_id": receipt.get("call_id", body.call_key.split(":")[-1]),
                    "name": receipt.get("tool", "unknown"),
                    "status": "success" if body.action == "record_success" else "error",
                },
            },
        )
    return {"ok": True}


@app.patch("/api/threads/{thread_id}/settings")
async def update_settings(body: ModelOptions, request: Request, thread=Depends(owned_thread)):
    active = request.app.state.runs.running.get(thread["id"])
    if active and not active.done():
        raise HTTPException(409, "请先暂停，再修改运行配置。")
    catalog = {tool["name"]: tool for tool in (await tool_catalog(request)).get("tools", [])}
    if catalog and any(
        name not in catalog or not catalog[name]["available"] for name in body.enabled_tools
    ):
        raise HTTPException(400, "配置包含未知或不可用的工具。")
    await request.app.state.store.update_thread(thread["id"], settings=body.model_dump())
    return {"ok": True}


@app.get("/api/threads/{thread_id}/events")
async def events(request: Request, after: int = 0, thread=Depends(owned_thread)):
    try:
        after = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "Invalid event cursor") from None

    async def generate():
        cursor = after
        idle = 0
        while not await request.is_disconnected():
            rows = await request.app.state.store.events(thread["id"], cursor)
            for event in rows:
                cursor = event["id"]
                data = json.dumps(event["data"], ensure_ascii=False)
                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {data}\n\n"
            if rows:
                idle = 0
            else:
                idle += 1
                if idle % 30 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/threads/{thread_id}/share")
async def share(request: Request, thread=Depends(owned_thread)):
    token = thread["share_token"] or secrets.token_urlsafe(24)
    await request.app.state.store.update_thread(thread["id"], share_token=token)
    return {"path": "/s/" + token}


@app.delete("/api/threads/{thread_id}/share")
async def revoke_share(request: Request, thread=Depends(owned_thread)):
    await request.app.state.store.update_thread(thread["id"], share_token=None)
    return {"ok": True}


@app.get("/api/shared/{token}")
async def shared(token: str, request: Request):
    thread = await request.app.state.store.shared_thread(token)
    if not thread:
        raise HTTPException(404, "分享不存在或已撤销。")
    return {"title": thread["title"], "state": await request.app.state.runs.state(thread["id"])}


@app.get("/api/threads/{thread_id}/export")
async def export(
    request: Request, format: Literal["md", "json"] = "md", thread=Depends(owned_thread)
):
    state = await request.app.state.runs.state(thread["id"])
    if format == "json":
        content = json.dumps(state, ensure_ascii=False, indent=2)
        media_type = "application/json"
    else:
        content = f"# {thread['title']}\n\n{thread['goal']}\n\n"
        content += "\n\n".join(message["text"] for message in state["messages"])
        media_type = "text/markdown"
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="agentgpt-{thread["id"][:8]}.{format}"'
        },
    )


@app.get("/api/documents")
async def documents(request: Request, user=Depends(current_user)):
    return await request.app.state.store.document_listing(user["id"])


@app.post("/api/documents", status_code=201)
async def upload_document(
    file: UploadFile,
    request: Request,
    user=Depends(current_user),
    kb: str = Form(default=""),
    document_id: str | None = Form(default=None),
):
    raw = await file.read(4_000_001)
    if len(raw) > 4_000_000:
        raise HTTPException(413, "单个文件上限为4MB。")
    name = (file.filename or "document.txt")[:200]
    if not name.lower().endswith((".pdf", ".txt", ".md", ".csv", ".json")):
        raise HTTPException(400, "支持 TXT、Markdown、CSV、JSON 和文本PDF。")
    if not raw or not re.fullmatch(r"[\w\-\u4e00-\u9fff ]{0,80}", kb):
        raise HTTPException(400, "空文件或无效的知识库名称")
    if not name.lower().endswith(".pdf"):
        try:
            if not raw.decode("utf-8-sig").strip():
                raise ValueError("文件没有文本")
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(400, "请上传包含文本的UTF-8文件") from None
    digest = hashlib.sha256(raw).hexdigest()
    directory = settings.data_dir / "documents" / user["id"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / digest
    if not path.exists():
        temporary = directory / (digest + "." + secrets.token_hex(8) + ".tmp")
        await asyncio.to_thread(temporary.write_bytes, raw)
        await asyncio.to_thread(temporary.replace, path)
    try:
        queued = await request.app.state.store.queue_document(
            user["id"], name, str(path), digest, document_id=document_id, kb=kb
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    # Ingestion is owned by the worker process; it polls ingestion_jobs every ~3s.
    return {
        "id": queued["document_id"],
        "version_id": queued["id"],
        "status": queued["status"],
        "title": name,
    }


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, request: Request, user=Depends(current_user)):
    try:
        await request.app.state.store.remove_document(user["id"], document_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return {"ok": True}


@app.post("/api/documents/{document_id}/retry")
async def retry_document(document_id: str, request: Request, user=Depends(current_user)):
    try:
        await request.app.state.store.retry_document(user["id"], document_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return {"ok": True}


@app.get("/api/evidence/{evidence_id}")
async def read_evidence(
    evidence_id: str,
    request: Request,
    user=Depends(current_user),
    offset: int = Query(default=0, ge=0, le=200000),
    length: int = Query(default=8000, ge=1, le=16000),
):
    try:
        record = await request.app.state.store.evidence(user["id"], evidence_id)
        if record["source_type"] == "web":
            from agentgpt.web_retrieval import evidence_window

            return evidence_window(record, offset, length)
        return record
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@app.get("/artifacts/{name}")
async def artifact(name: str):
    if not re.fullmatch(r"[a-f0-9]{32}\.png", name):
        raise HTTPException(404)
    path = settings.data_dir / "artifacts" / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


@app.get("/")
@app.get("/s/{token}")
async def index():
    return FileResponse(ROOT / "agentgpt/static/index.html")
