"""Recover scoped traces for benchmark deadlines without rerunning any scenario."""

import argparse
import json
import re
import sqlite3

from benchmark_live_100 import read_trace


def main(root, output):
    count = 0
    with sqlite3.connect(f"file:{root / 'ui_data/agentgpt.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for path in output.glob("*.json"):
            case = json.loads(path.read_text(encoding="utf-8"))
            match = re.search(r"TimeoutError: run ([0-9a-f]{32}) exceeded", case.get("error", ""))
            if not match or any(r["run_id"] == match[1] for r in case["runs"]):
                continue
            run_id = match[1]
            row = db.execute(
                "SELECT r.*,u.email FROM runs r JOIN users u ON u.id=r.user_id WHERE r.id=?",
                (run_id,),
            ).fetchone()
            nonce = case["synthetic_code"].removeprefix("BENCH-").lower()
            if not row or not row["email"].startswith("bench-" + nonce + "-"):
                raise ValueError("Timed-out run is not owned by this benchmark scenario")
            turn = db.execute(
                "SELECT t.* FROM turns t JOIN turn_runs r ON r.turn_id=t.id WHERE r.run_id=?",
                (run_id,),
            ).fetchone()
            case["runs"].append(
                {
                    "run_id": run_id,
                    "thread_id": row["thread_id"],
                    "input": turn["user_text"],
                    "status": "timeout",
                    "status_after_stop": turn["outcome"],
                    "answer": turn["assistant_text"]
                    or "评测等待超过360秒，已请求停止；未取得最终答案。",
                    "elapsed_seconds": 360,
                    "deadline_seconds": 360,
                    **read_trace(root, run_id, row["user_id"]),
                }
            )
            case["notes"].append(
                "达到360秒评测期限；随后从同一run的持久记录补采行动轨迹，未重跑用例。"
            )
            path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
            count += 1
    print("enriched timeout traces:", count)


if __name__ == "__main__":
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    main(args.root, args.output)
