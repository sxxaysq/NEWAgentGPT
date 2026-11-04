"""Evaluate saved answers only; never repeat application runs or tools."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_natural100 import NaturalCase


async def main(directory):
    async def no_execution():
        pass

    while True:
        files = [p for p in directory.glob("*.json") if p.name != "manifest.json"]
        for path in files:
            result = json.loads(path.read_text(encoding="utf-8"))
            if (
                not result.get("review", {})
                .get("reason", "")
                .startswith("Reviewer error: ModuleNotFoundError")
            ):
                continue
            case = NaturalCase(
                SimpleNamespace(root=Path.cwd(), base="http://127.0.0.1:18210"), result["design"]
            )
            case.execute = no_execution
            case.runs = result["runs"]
            case.checks = [c for c in result["checks"] if c["name"] != "model_rubric_review"]
            judged = await case.run()
            result["collector_review_correction"] = (
                "初次评阅进程导入路径错误，仅重做已保存答案的评阅；原Agent运行及工具记录未重跑。"
            )
            result["initial_review"] = result["review"]
            for key in ["review", "checks", "passed"]:
                result[key] = judged[key]
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(path.stem, result["passed"], result["review"], flush=True)
        if len(files) == 100:
            break
        await asyncio.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    asyncio.run(main(parser.parse_args().directory))
