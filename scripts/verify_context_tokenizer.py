"""Read-only deployment tokenizer comparison; prints counts, never prompt contents."""

import asyncio
import json
import sys

import httpx
from langchain_core.messages import HumanMessage, SystemMessage, convert_to_openai_messages

from agentgpt.config import Settings
from agentgpt.host.context import TokenBudget


async def main():
    settings = Settings()
    settings.model_context_tokens = 131072
    settings.model_tokenizer_path = ""
    settings.model_tokenizer_url = "http://127.0.0.1:8000/tokenize"
    cases = [
        (
            [
                SystemMessage(content="你是助手。"),
                HumanMessage(content="预算从23万改18万，其他不变。"),
            ],
            [],
        ),
        (
            [
                SystemMessage(content="保留表格。"),
                HumanMessage(content="|编号|值|\n|ZQ-8E4C91|137420 CNY|"),
            ],
            [],
        ),
        (
            [SystemMessage(content="使用工具。"), HumanMessage(content="查询项目预算")],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "查找材料",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    },
                }
            ],
        ),
    ]
    counter = TokenBudget(settings)
    async with httpx.AsyncClient(timeout=30) as client:
        if "--schema" in sys.argv:
            schema = (await client.get("http://127.0.0.1:8000/openapi.json")).json()
            print(
                json.dumps(
                    {k: v for k, v in schema["components"]["schemas"].items() if "Tokenize" in k},
                    ensure_ascii=False,
                )
            )
            return
        results = []
        for messages, tools in cases:
            payload = {
                "model": settings.model_name,
                "messages": convert_to_openai_messages(messages),
                "add_generation_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            if tools:
                payload["tools"] = tools
            response = await client.post("http://127.0.0.1:8000/tokenize", json=payload)
            response.raise_for_status()
            server = response.json()
            count = await counter.check_async(messages, tools)
            completion_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"add_generation_prompt", "chat_template_kwargs"}
            }
            completion_payload.update(
                max_tokens=16, temperature=0, chat_template_kwargs={"enable_thinking": False}
            )
            if tools:
                completion_payload.update(tool_choice="auto", parallel_tool_calls=False)
            completion = await client.post(
                "http://127.0.0.1:8000/v1/chat/completions", json=completion_payload
            )
            completion.raise_for_status()
            actual = completion.json()["usage"]["prompt_tokens"]
            result = {
                "tools": bool(tools),
                "mode": counter.mode,
                "local": count,
                "server": server.get("count"),
                "actual_prompt_tokens": actual,
                "matches": count == server.get("count") == actual,
                "max_model_len": server.get("max_model_len"),
            }
            results.append(result)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
