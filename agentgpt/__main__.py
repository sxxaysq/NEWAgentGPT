"""Process entry point: one role per process.

python -m agentgpt --role host          # API + orchestration (A2A client)
python -m agentgpt --role planner       # A2A server
python -m agentgpt --role worker        # A2A server, owns MCP tools and the index
python -m agentgpt --role critic        # A2A server
python -m agentgpt --role synthesizer   # A2A server
"""

import argparse
import asyncio
import importlib

from agentgpt.config import settings

ROLES = ("host", "planner", "worker", "critic", "synthesizer")


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentGPT A2A process")
    parser.add_argument("--role", choices=ROLES, default="host")
    args = parser.parse_args()

    if args.role == "host":
        import uvicorn

        uvicorn.run(
            "agentgpt.app:app",
            host=settings.app_host,
            port=settings.app_port,
            workers=1,
            timeout_graceful_shutdown=3,
        )
        return

    module = importlib.import_module(f"agentgpt.agents.{args.role}")
    asyncio.run(module.main(settings))


if __name__ == "__main__":
    main()
