"""Rebuild the derived Qdrant index while the API process is stopped."""

import asyncio

import portalocker

from agentgpt.config import settings
from agentgpt.memory import MemoryService
from agentgpt.store import Store


async def rebuild():
    store = Store(settings.database)
    await store.initialize()
    service = MemoryService(store, settings)
    try:
        async with store.connect() as db:
            await db.execute("UPDATE memories SET indexed_version=0")
        while await store.pending_memory_index():
            await service.sync_index()
        print("Memory index rebuilt from SQLite.")
    finally:
        await service.close()


if __name__ == "__main__":
    with portalocker.Lock(str(settings.database) + ".lock", timeout=0):
        asyncio.run(rebuild())
