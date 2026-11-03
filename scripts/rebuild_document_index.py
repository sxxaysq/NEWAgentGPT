"""Rebuild active document vectors and FTS from SQLite; stop the API first."""

import asyncio
import json

import portalocker

from agentgpt.config import settings
from agentgpt.document_store import lexical_tokens
from agentgpt.memory import MemoryIndex
from agentgpt.retrieval import DocumentIndex
from agentgpt.store import Store


async def main():
    with portalocker.Lock(str(settings.database) + ".lock", timeout=0):
        store = Store(settings.database)
        # Do not call Store.initialize: rebuilding an index must not change run status.
        shared = MemoryIndex(settings)
        index = DocumentIndex(shared)
        try:
            async with store.connect() as db:
                if index.remote:
                    await db.execute(
                        "CREATE TABLE IF NOT EXISTS document_vector_jobs ("
                        "collection TEXT,version_id TEXT,status TEXT,"
                        "attempts INTEGER DEFAULT 0,error TEXT DEFAULT '',"
                        "PRIMARY KEY(collection,version_id))"
                    )
                versions = await (
                    await db.execute(
                        "SELECT v.*,d.user_id,m.kb FROM document_versions v JOIN document_meta m "
                        "ON m.active_version=v.id JOIN documents d ON d.id=m.document_id "
                        "WHERE m.deleted=0 AND v.status='ready'"
                    )
                ).fetchall()
            count = 0
            for row in versions:
                async with store.connect() as db:
                    chunks = [
                        dict(r)
                        for r in await (
                            await db.execute(
                                "SELECT * FROM document_chunks WHERE version_id=? ORDER BY ordinal",
                                (row["id"],),
                            )
                        ).fetchall()
                    ]
                if settings.rag_enabled:
                    await index.upsert(dict(row), chunks)
                async with store.connect() as db:
                    if index.remote:
                        await db.execute(
                            "INSERT OR REPLACE INTO document_vector_jobs "
                            "(collection,version_id,status) VALUES(?,?,'ready')",
                            (index.collection, row["id"]),
                        )
                    for chunk in chunks:
                        await db.execute(
                            "DELETE FROM document_fts WHERE chunk_id=?", (chunk["id"],)
                        )
                        await db.execute(
                            "INSERT INTO document_fts VALUES(?,?)",
                            (chunk["id"], " ".join(lexical_tokens(chunk["text"]))),
                        )
                count += len(chunks)
            print(
                json.dumps(
                    {"versions": len(versions), "chunks": count, "dense": settings.rag_enabled}
                )
            )
        finally:
            await shared.close()


if __name__ == "__main__":
    asyncio.run(main())
