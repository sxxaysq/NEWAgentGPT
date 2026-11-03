"""Discover, namespace and adapt MCP tools to the common AgentTool contract."""

import asyncio
import copy
import json
import logging
import os
import re
import secrets
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from agentgpt.config import ROOT, Settings
from agentgpt.retrieval import RetrievalBridge, RetrievalService
from agentgpt.store import Store
from agentgpt.tools import AgentTool, builtin_tools

log = logging.getLogger(__name__)


class MCPTool(AgentTool):
    def __init__(self, server_key, descriptor, session, actor_id=None, knowledge_base=""):
        self.server_key = server_key
        self.remote_name = descriptor.name
        self.name = server_key + "__" + descriptor.name
        self.description = descriptor.description or descriptor.name
        self.parameters = copy.deepcopy(descriptor.inputSchema)
        self.read_only = bool(descriptor.annotations and descriptor.annotations.readOnlyHint)
        self.session = session
        self.actor_id = actor_id
        self.knowledge_base = knowledge_base
        if server_key == "builtin" and descriptor.name in {
            "private_search",
            "private_read",
            "web_fetch",
        }:
            self.parameters.get("properties", {}).pop("user_id", None)
            required = self.parameters.get("required", [])
            self.parameters["required"] = [field for field in required if field != "user_id"]
        if server_key == "builtin" and descriptor.name in {"private_search", "private_read"}:
            self.parameters.get("properties", {}).pop("kb", None)

    async def execute(self, arguments):
        args = dict(arguments)
        if self.server_key == "builtin" and self.remote_name in {
            "private_search",
            "private_read",
            "web_fetch",
        }:
            if not self.actor_id:
                raise ValueError("Source retrieval requires an authenticated actor")
            args["user_id"] = self.actor_id
        if self.server_key == "builtin" and self.remote_name in {"private_search", "private_read"}:
            args["kb"] = self.knowledge_base
        async with asyncio.timeout(190):
            result = await self.session.call_tool(self.remote_name, args)
        text = "\n".join(item.text for item in result.content if item.type == "text")
        if result.isError:
            raise ValueError(text or "MCP tool execution failed")
        # Preserve image/resource content returned by external MCP servers as JSON.
        other = [
            item.model_dump(mode="json", by_alias=True)
            for item in result.content
            if item.type != "text"
        ]
        if other:
            return {"text": text, "content": other}
        return text


class ToolManager:
    def __init__(self, settings: Settings, store: Store, shared_index=None):
        self.settings = settings
        self.store = store
        self.stack = AsyncExitStack()
        self.discovered = []
        self.server_status = {}
        self.retry_safe_tools = {
            "builtin__web_search",
            "builtin__web_fetch",
            "builtin__private_search",
            "builtin__private_read",
        }
        self.builtin = {tool.name: tool for tool in builtin_tools(settings, store)}
        self.retrieval = RetrievalService(store, settings, shared_index)
        self.bridge = RetrievalBridge(self.retrieval, secrets.token_urlsafe(32))

    async def start(self):
        await self.retrieval.start()
        port = await self.bridge.start()
        servers = {
            "builtin": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "agentgpt.mcp_server"],
                "cwd": str(ROOT),
                "env": {
                    "DATA_DIR": str(self.settings.data_dir),
                    "RETRIEVAL_PORT": str(port),
                    "RETRIEVAL_TOKEN": self.bridge.token,
                    "SEARCH_PROXY": self.settings.search_proxy,
                    "SEARCH_PROVIDER": self.settings.search_provider,
                    "TAVILY_API_KEY": self.settings.tavily_api_key,
                    "WEB_TIMEOUT": str(self.settings.web_timeout),
                    "WEB_MAX_BYTES": str(self.settings.web_max_bytes),
                    "IMAGE_API_BASE": self.settings.image_api_base,
                    "IMAGE_MODEL": self.settings.image_model,
                    "IMAGE_API_KEY": self.settings.image_api_key,
                },
            }
        }
        if self.settings.mcp_config.is_file():
            config = json.loads(self.settings.mcp_config.read_text())
            for key, value in config.get("mcpServers", {}).items():
                if key == "builtin" or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,20}", key):
                    raise ValueError("Invalid or reserved MCP server name: " + key)
                servers[key] = value
                self.retry_safe_tools.update(
                    key + "__" + name for name in value.get("replay_safe_tools", [])
                )
        for key, config in servers.items():
            if not config.get("enabled", True):
                continue
            # Keep anyio transport scopes in the lifespan task; do not wrap them in wait_for.
            try:
                transport = config.get("transport", "stdio")
                if transport == "stdio":
                    streams = await self.stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=config["command"],
                                args=config.get("args", []),
                                cwd=config.get("cwd", str(ROOT)),
                                env={**os.environ, **config.get("env", {})},
                            )
                        )
                    )
                elif transport == "streamable_http":
                    streams = await self.stack.enter_async_context(
                        streamablehttp_client(config["url"], headers=config.get("headers"))
                    )
                else:
                    raise ValueError("Unsupported MCP transport: " + transport)
                session = await self.stack.enter_async_context(
                    ClientSession(streams[0], streams[1])
                )
                await session.initialize()
                cursor = None
                count = 0
                while True:
                    page = await session.list_tools(cursor=cursor)
                    for descriptor in page.tools:
                        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", descriptor.name):
                            raise ValueError("Tool name is not compatible with model tool calling")
                        self.discovered.append((key, descriptor, session))
                        count += 1
                    cursor = page.nextCursor
                    if not cursor:
                        break
                self.server_status[key] = {"status": "connected", "tools": count}
            except Exception as exc:
                if key == "builtin":
                    raise
                log.exception("MCP server %s failed", key)
                self.server_status[key] = {"status": "error", "error": str(exc)[:300]}

    def tools_for(self, user_id: str, enabled: list[str], knowledge_base=""):
        tools = []
        for server_key, descriptor, session in self.discovered:
            proxy = MCPTool(
                server_key, descriptor, session, actor_id=user_id, knowledge_base=knowledge_base
            )
            builtin = self.builtin.get(descriptor.name) if server_key == "builtin" else None
            if proxy.name in enabled and (builtin is None or builtin.available):
                tools.append(proxy.as_langchain_tool())
        return tools

    def replay_safe(self, name):
        # Remote MCP hints alone are not authority to repeat an uncertain side effect.
        return name in self.retry_safe_tools

    def catalog(self):
        result = []
        for server_key, descriptor, _ in self.discovered:
            builtin = self.builtin.get(descriptor.name) if server_key == "builtin" else None
            result.append(
                {
                    "name": server_key + "__" + descriptor.name,
                    "description": descriptor.description,
                    "server": server_key,
                    "available": builtin.available if builtin else True,
                    "reason": builtin.unavailable_reason
                    if builtin and not builtin.available
                    else "",
                    "read_only": bool(
                        descriptor.annotations and descriptor.annotations.readOnlyHint
                    ),
                }
            )
        return result

    async def close(self):
        await self.stack.aclose()
        await self.bridge.close()
        await self.retrieval.close()
