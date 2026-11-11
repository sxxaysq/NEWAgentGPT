"""Full Host/A2A quality runs, fixed before the APC/layout experiment.

Operational checks are immediate; semantic grading is deferred until every group
finishes so a grader cannot change the workload or its timing.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from benchmark_live_100 import Case
from benchmark_natural100 import NaturalCase


class QualityCase(NaturalCase):
    async def execute(self):
        if self.group != "memory_delete":
            return await super().execute()
        client, owner = await self.register(memory=True)
        try:
            await self.ask(client, owner, self.item["prompts"][0])
            rows = (await client.get("/api/memories")).json()["items"]
            self.check("memory_persisted", bool(rows))
            await self.ask(client, owner, self.item["prompts"][1])
            self.check("positive_recall_context", self.runs[-1]["context"]["memory_count"] > 0)
            for row in rows:
                response = await client.delete(f"/api/memories/{row['id']}",
                                               params={"version": row["version"]})
                response.raise_for_status()
            self.check("deleted_from_active_store",
                       not (await client.get("/api/memories")).json()["items"])
            await self.ask(client, owner, self.item["prompts"][1])
            self.check("deleted_memory_not_injected",
                       self.runs[-1]["context"]["memory_count"] == 0)
        finally:
            await client.aclose()

    async def run(self):
        result = await Case.run(self)
        result["design"] = self.item
        return result


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    (args.output / "manifest.json").write_text(json.dumps({
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "group": args.group, "case_order": [x["case_id"] for x in corpus],
        "sampling": {"temperature": 0, "seed": 42},
        "scope": "real Host + Planner/Worker/Critic/Synthesizer; sequential cases",
    }, indent=2), encoding="utf-8")
    for item in corpus:
        destination = args.output / (item["case_id"] + ".json")
        if destination.exists():
            continue
        result = await QualityCase(args, item).run()
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"group": args.group, "case": item["case_id"],
                          "operational_pass": result["passed"], "error": result.get("error")}),
              flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--group", required=True)
    parser.add_argument("--base", default="http://127.0.0.1:18310")
    asyncio.run(main(parser.parse_args()))
