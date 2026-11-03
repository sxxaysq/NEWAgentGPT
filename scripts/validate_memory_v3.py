"""Run 100 seeded, offline engineering scenarios, never a model-quality benchmark.

Usage: python scripts/validate_memory_v3.py --output artifacts/memory_v3_contract.json
All data lives in a temporary database. No deployed service or user data is touched.
The fake extractor/index deliberately exercise contracts, not semantic accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage  # noqa: E402

from agentgpt.config import Settings  # noqa: E402
from agentgpt.host.context import ContextAssembler, ContextBudgetError, TokenBudget  # noqa: E402
from agentgpt.memory import Fact, Facts, MemoryService, Merge  # noqa: E402
from agentgpt.store import Store  # noqa: E402

COUNTS = {
    "single_hop": 10,
    "multi_step": 10,
    "long_text": 6,
    "private_retrieval": 10,
    "web_retrieval": 6,
    "hybrid_retrieval": 6,
    "recovery": 8,
    "typos": 6,
    "isolation": 10,
    "in_session": 14,
    "cross_session": 10,
    "boundary": 4,
}


class FixtureIndex:
    generation = "contract-fixture-v1"

    def __init__(self):
        self.rows = {}

    async def upsert(self, row):
        self.rows[row["id"]] = dict(row)

    async def search(self, user_id, scope, query, limit=20):
        # Deliberately return all users/scopes to audit SQLite authorization.
        return [
            {"id": r["id"], "version": r["version"], "score": 0.99} for r in self.rows.values()
        ][:limit]

    async def close(self):
        pass


class FixtureExtractor:
    def with_structured_output(self, schema, **kwargs):
        class Structured:
            async def ainvoke(self, messages):
                if schema is Merge:
                    return Merge(action="NOOP")
                # Consumes only the user source supplied to the real extraction service.
                query = json.loads(messages[-1].content)["query"]
                return Facts(
                    facts=[Fact(key="contract_fact", fact=query, quote=query, kind="project")]
                )

        return Structured()


async def accept(store, text, *, thread=None, user="alice", scope="", kb="", finish=True):
    thread = thread or await store.create_thread(
        user,
        text,
        {
            "memory_scope": scope,
            "knowledge_base": kb,
        },
    )
    operation = (
        "followup" if (await store.context_snapshot(user, thread["id"], 999))["inputs"] else "start"
    )
    run, _ = await store.enqueue_run(thread, operation, text, "")
    if finish:
        await store.finish_turn(
            run["id"], outcome="completed", assistant_text="处理完毕", memory_enabled=True
        )
    return thread, run


async def scenario(group, number, seed):
    rng = random.Random(f"{seed}:{group}:{number}")
    code = "ZX-" + f"{rng.getrandbits(48):012X}"
    secret = f"凭证-{rng.getrandbits(64):016X}"
    scope = "scope-" + code
    with tempfile.TemporaryDirectory(prefix="agentgpt-memory-contract-") as directory:
        settings = Settings(
            _env_file=None,
            data_dir=Path(directory),
            memory_enabled=True,
            rerank_url="",
            memory_recall_timeout_s=1,
            context_fallback_tokens=8192,
        )
        store = Store(settings.database)
        store.memory_enabled = True
        await store.initialize()
        index = FixtureIndex()
        service = MemoryService(store, settings, index=index, model=FixtureExtractor())
        assembler = ContextAssembler(store, settings)
        checks, metrics = [], {}
        try:
            if group in {
                "single_hop",
                "typos",
                "private_retrieval",
                "web_retrieval",
                "hybrid_retrieval",
            }:
                question = (
                    f"项目 {code} 的布署要求是 {secret}"
                    if group == "typos"
                    else f"项目 {code}: {secret}"
                )
                thread, run = await accept(store, question, scope=scope, kb="shared-spec")
                turn = await store.turn_for_run(run["id"])
                assert turn["user_text"] == question
                assert turn["scope_snapshot"] == {
                    "memory_scope": scope,
                    "knowledge_base": "shared-spec",
                }
                checks = ["original_input_exact", "memory_and_document_scopes_independent"]
                if group in {"private_retrieval", "web_retrieval", "hybrid_retrieval"}:
                    metrics["live_retrieval_quality"] = "not_measured"
                if group == "typos":
                    metrics["semantic_typo_robustness"] = "not_measured"
            elif group in {"multi_step", "in_session"}:
                thread, _ = await accept(store, f"项目 {code} 预算23万；仅内网；输出中文表格")
                await accept(store, "预算改成18万，其他不变", thread=thread)
                # State/summary workers intentionally paused. Raw delta must compensate.
                if group == "in_session" and number >= 7:
                    for i in range(5):
                        await accept(store, f"补充讨论 {i}", thread=thread)
                snapshot = await store.context_snapshot("alice", thread["id"], 999)
                next_seq = len(snapshot["inputs"])
                context = await assembler.assemble(
                    "alice", thread["id"], "请按刚才约束继续", before_input_seq=next_seq
                )
                pending = json.dumps(context["pending_user_updates"], ensure_ascii=False)
                assert "18万" in pending and "仅内网" in pending and code in pending
                assert context["previous_answer"]["text"] == "处理完毕"
                checks = [
                    "pending_correction_and_original_constraints_propagated",
                    "previous_answer_available",
                ]
                metrics["actual_answer_obeys_constraints"] = "not_measured"
            elif group == "long_text":
                text = (
                    f"开头编号 {code}\n"
                    + ("完整原文与否定条件不可截断。\n" * (1000 + number * 100))
                    + secret
                )
                _, run = await accept(store, text)
                assert (await store.turn_for_run(run["id"]))["user_text"] == text
                try:
                    TokenBudget(settings, 2000).check([HumanMessage(content=text)])
                    raise AssertionError("oversized request was accepted")
                except ContextBudgetError:
                    pass
                checks = ["long_original_persisted", "oversize_request_rejected_before_send"]
                metrics["full_document_transformation"] = "not_measured"
            elif group == "recovery":
                thread, run = await accept(store, f"记住 {code} 的凭证是 {secret}", scope=scope)
                first = await store.turn_for_run(run["id"])
                await store.finish_turn(
                    run["id"], outcome="completed", assistant_text="不得覆盖", memory_enabled=True
                )
                await store.initialize()
                await store.reconcile_turns(memory_enabled=True)
                second = await store.turn_for_run(run["id"])
                assert (
                    first["id"] == second["id"]
                    and first["turn_revision"] == second["turn_revision"]
                )
                assert second["assistant_text"] == "处理完毕"
                async with store.connect() as db:
                    count = (
                        await (await db.execute("SELECT count(*) FROM memory_jobs")).fetchone()
                    )[0]
                assert count == 1
                checks = ["reopen_keeps_turn_identity", "repeat_finish_does_not_duplicate_jobs"]
                metrics["process_kill_injection"] = "not_measured"
            elif group in {"cross_session", "isolation"}:
                _, _ = await accept(store, f"记住项目 {code} 的凭证是 {secret}", scope=scope)
                job = await store.next_memory_job()
                await service.extract(job)
                await store.update_memory_job(job["id"], status="done")
                await service.sync_index()
                if group == "cross_session":
                    fresh = await store.create_thread(
                        "alice", f"{code} 的凭证是什么？", {"memory_scope": scope}
                    )
                    before = await assembler.assemble(
                        "alice", fresh["id"], fresh["goal"], before_input_seq=0
                    )
                    assert secret not in json.dumps(before, ensure_ascii=False)
                    result = await service.recall("alice", scope, fresh["goal"])
                    assert secret in json.dumps(result, ensure_ascii=False)
                    await store.set_memory_preference("alice", False)
                    control = await store.create_thread(
                        "alice", fresh["goal"], {"memory_scope": scope}
                    )
                    empty = await service.recall("alice", scope, control["goal"])
                    assert empty == []
                    metrics.update(positive_recall=True, disabled_control=True)
                    checks = [
                        "fresh_session_has_no_gold",
                        "cross_session_recall",
                        "disabled_fresh_control",
                    ]
                else:
                    user, selected_scope = (
                        ("bob", scope) if number < 6 else ("alice", scope + "-other")
                    )
                    seen = []
                    service.settings.rerank_url = "http://fixture-unused"

                    async def rerank(query, candidates):
                        seen.extend(candidates)
                        return dict.fromkeys(range(len(candidates)), 0.99)

                    service._rerank = rerank
                    assert await service.recall(user, selected_scope, code) == []
                    assert secret not in json.dumps(seen, ensure_ascii=False)
                    metrics["isolation_negative"] = True
                    checks = [
                        "unauthorized_candidates_excluded_before_rerank",
                        "no_unauthorized_recall",
                    ]
            else:
                if number == 0:
                    thread, _ = await accept(store, code)
                    try:
                        await store.enqueue_run(thread, "followup", " ", "")
                        raise AssertionError("empty followup accepted")
                    except ValueError:
                        pass
                    checks = ["empty_followup_rejected"]
                elif number == 1:
                    key = await store.edit_memory("alice", secret)
                    await service.sync_index()
                    await store.edit_memory(
                        "alice", "", memory_id=key, expected_version=1, delete=True
                    )
                    assert await service.recall("alice", "", secret) == []
                    checks = ["deletion_overrides_stale_vector"]
                elif number == 2:
                    key = await store.edit_memory("alice", "中文 AB-12 AB12")
                    lexical, exact = await store.lexical_memories("alice", "", "中文")
                    assert [r["id"] for r in lexical] == [key]
                    _, exact = await store.lexical_memories("alice", "", "AB-12")
                    assert [r["id"] for r in exact] == [key]
                    checks = ["short_chinese_fallback", "hyphen_identifier_retained"]
                else:
                    await store.edit_memory("alice", secret)
                    service.settings.memory_recall_timeout_s = 0.01
                    service.settings.memory_recall_max_pending = 1
                    gate = asyncio.Event()

                    async def slow(*args):
                        await gate.wait()
                        return []

                    index.search = slow
                    assert await service.recall("alice", "", code) == []
                    assert await service.recall("alice", "", "second") == []
                    assert len(service._recalls) == 1
                    pending = list(service._recalls.values())
                    gate.set()
                    await asyncio.gather(*pending)
                    checks = ["deadline_without_cancellation", "background_capacity_bound"]
            async with store.connect() as db:
                counters = {
                    name: (await (await db.execute(f"SELECT count(*) FROM {name}")).fetchone())[0]
                    for name in ("runs", "turns", "turn_inputs", "memory_jobs")
                }
            return {
                "id": f"{group}-{number + 1:02}",
                "scenario": group,
                "status": "passed",
                "checks": checks,
                "metrics": metrics,
                "counts": counters,
            }
        finally:
            await service.close()


async def run(seed):
    results = []
    for group, count in COUNTS.items():
        for number in range(count):
            try:
                result = await scenario(group, number, seed)
            except Exception as exc:
                result = {
                    "id": f"{group}-{number + 1:02}",
                    "scenario": group,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)
        passed = sum(r["status"] == "passed" for r in results if r["scenario"] == group)
        print(f"{group}: {passed}/{count}", flush=True)
    return {
        "mode": "deterministic_contract_only",
        "seed": seed,
        "warning": (
            "Fake extractor and index; no model quality, web freshness, "
            "RAG accuracy or token calibration measured."
        ),
        "scenario_count": len(results),
        "real_model_calls": 0,
        "passed": sum(r["status"] == "passed" for r in results),
        "group_counts": COUNTS,
        "positive_recall": sum(r.get("metrics", {}).get("positive_recall", False) for r in results),
        "isolation_negatives": sum(
            r.get("metrics", {}).get("isolation_negative", False) for r in results
        ),
        "disabled_controls": sum(
            r.get("metrics", {}).get("disabled_control", False) for r in results
        ),
        "counts": {
            name: sum(r.get("counts", {}).get(name, 0) for r in results)
            for name in ("runs", "turns", "turn_inputs", "memory_jobs")
        },
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261102)
    parser.add_argument("--output", type=Path, default=Path("artifacts/memory_v3_contract.json"))
    args = parser.parse_args()
    report = asyncio.run(run(args.seed))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, ensure_ascii=False))
    return 0 if report["passed"] == report["scenario_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
