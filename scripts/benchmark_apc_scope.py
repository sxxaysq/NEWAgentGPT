"""Supplementary project-scope quality check, separate from timed main cases."""

import argparse
import asyncio
import json
from pathlib import Path

from benchmark_live_100 import Case


class ScopeCase(Case):
    async def execute(self):
        client, owner = await self.register(memory=True)
        try:
            code = "PFX-SCOPE-731"
            response = await client.post("/api/memories", json={
                "content": "当前项目内部代号是 " + code,
                "scope": self.scope, "kind": "project",
            })
            response.raise_for_status()
            question = "当前项目的内部代号是什么？仅根据该项目有效记忆回答，不知道就说不知道。"
            _, positive = await self.ask(client, owner, question)
            self.check("same_project_positive", code in positive["answer"]
                       and positive["context"]["memory_count"] > 0)
            _, negative = await self.ask(client, owner, question, scope="other-" + self.scope)
            self.check("other_project_no_injection", negative["context"]["memory_count"] == 0)
            self.check("other_project_no_disclosure", code not in negative["answer"])
        finally:
            await client.aclose()


async def main(args):
    result = await ScopeCase(args, "project_scope", 0).run()
    result["design"] = {
        "fixture": "explicit API-created project memory PFX-SCOPE-731",
        "rubric": "same-project positive recall; different-project no context or disclosed code",
        "reason": "supplements cross-user-only coverage; global preferences are not project-local",
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"scope_supplement": result["passed"], "error": result.get("error")}),
          flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base", default="http://127.0.0.1:18310")
    asyncio.run(main(parser.parse_args()))
