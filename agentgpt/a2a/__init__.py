"""A2A protocol layer: wire types, transport, client, task store, run-scoped auth."""

from agentgpt.a2a import types
from agentgpt.a2a.auth import RunScope, TokenError, issue_token, verify_token
from agentgpt.a2a.client import A2AClient, AgentEndpoint, AgentError, Registry, task_payload
from agentgpt.a2a.tasks import TaskStore
from agentgpt.a2a.transport import AgentRuntime, Context, InputRequired, TaskRejected

__all__ = [
    "types",
    "RunScope",
    "TokenError",
    "issue_token",
    "verify_token",
    "A2AClient",
    "AgentEndpoint",
    "AgentError",
    "Registry",
    "task_payload",
    "TaskStore",
    "AgentRuntime",
    "Context",
    "InputRequired",
    "TaskRejected",
]
