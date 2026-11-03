"""Committed tool results survive a Worker restart; uncertain writes are never replayed."""

import hashlib
import json

from langchain_core.messages import message_to_dict, messages_from_dict

from agentgpt.a2a.transport import InputRequired

READ_ONLY_TOOLS = {
    "builtin__web_search",
    "builtin__web_fetch",
    "builtin__private_search",
    "builtin__private_read",
}


async def journaled_tool_call(ctx, call, invoke):
    key = "tool:" + call["id"]
    digest = hashlib.sha256(json.dumps(call, sort_keys=True).encode()).hexdigest()
    previous = await ctx.load(key)
    if previous:
        if previous["digest"] != digest:
            raise InputRequired("工具调用标识被复用且参数改变，请人工核查。")
        if previous["status"] == "done":
            return messages_from_dict([previous["message"]])[0]
        if call["name"] not in READ_ONLY_TOOLS:
            raise InputRequired(
                "进程在工具执行期间中断，结果未知，未自动重复可能有副作用的调用：" + call["name"]
            )
    await ctx.save(key, {"digest": digest, "status": "pending"})
    message = await invoke()
    await ctx.save(key, {"digest": digest, "status": "done", "message": message_to_dict(message)})
    return message
