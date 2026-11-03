"""Bounded DeepSearch-style query expansion and coarse/fine score fusion."""

import asyncio
import math

import httpx
from pydantic import BaseModel, ConfigDict, Field


class QueryVariants(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    queries: list[str] = Field(max_length=2)


async def expand_query(query, settings):
    if settings.retrieval_query_count == 1:
        return [query], {}, []
    try:
        async with asyncio.timeout(20):
            async with httpx.AsyncClient(timeout=18, trust_env=False) as client:
                response = await client.post(
                    settings.model_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + settings.model_api_key},
                    json={
                        "model": settings.model_name,
                        "temperature": 0,
                        "max_tokens": 400,
                        "chat_template_kwargs": {"enable_thinking": False},
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "system",
                                "content": "将检索问题改写成两个不同措辞的自包含查询。"
                                "保留实体、数字、否定和约束，"
                                '不猜测答案，不添加事实。仅输出JSON对象：{"queries":["查询1","查询2"]}。',
                            },
                            {"role": "user", "content": query},
                        ],
                    },
                )
                response.raise_for_status()
                data = response.json()
        parsed = QueryVariants.model_validate_json(data["choices"][0]["message"]["content"])
        queries = [query]
        for variant in parsed.queries:
            variant = variant.strip()
            if variant and len(variant) <= 500 and variant not in queries:
                queries.append(variant)
        return queries[: settings.retrieval_query_count], data.get("usage", {}), []
    except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError, IndexError):
        return [query], {}, ["query_expansion_unavailable"]


def normalize(values):
    lo, hi = min(values), max(values)
    return [(v - lo) / (hi - lo) for v in values] if hi > lo else [1.0] * len(values)


def fuse_rerank(sources, results, weight):
    if not isinstance(results, list) or len(results) != len(sources):
        raise ValueError("Incomplete reranker response")
    indices = [r["index"] for r in results]
    if any(type(i) is not int for i in indices) or set(indices) != set(range(len(sources))):
        raise ValueError("Invalid reranker indices")
    scores = {r["index"]: float(r["relevance_score"]) for r in results}
    if not all(math.isfinite(s) and 0 <= s <= 1 for s in scores.values()):
        raise ValueError("Reranker must return finite sigmoid scores")
    coarse = normalize([s["retrieval_score"] for s in sources])
    fine = normalize([scores[i] for i in range(len(sources))])
    output = [
        dict(s, rerank_score=scores[i], final_score=weight * coarse[i] + (1 - weight) * fine[i])
        for i, s in enumerate(sources)
    ]
    return sorted(output, key=lambda s: (-s["final_score"], -s["rerank_score"], s["evidence_id"]))


async def rerank_sources(query, sources, settings):
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.post(
            settings.rerank_url.rstrip("/") + "/rerank",
            headers={"Authorization": "Bearer " + settings.rerank_api_key}
            if settings.rerank_api_key
            else {},
            json={
                "model": settings.rerank_model,
                "query": query,
                "documents": [s["text"] for s in sources],
                "return_documents": False,
            },
        )
        response.raise_for_status()
        return fuse_rerank(sources, response.json()["results"], settings.retrieval_coarse_weight)
