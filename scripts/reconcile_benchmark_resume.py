"""Correct an early paused projection using the SAME continued run's final record."""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from benchmark_live_100 import read_trace


def main(root, output):
    changed = 0
    with sqlite3.connect(f"file:{root / 'ui_data/agentgpt.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for path in output.glob("recovery-*.json"):
            case = json.loads(path.read_text(encoding="utf-8"))
            for run in case["runs"]:
                if run["status"] != "paused" or not any(
                    c["name"] == "resume_same_run" and c["passed"] for c in case["checks"]
                ):
                    continue
                row = db.execute(
                    "SELECT r.*,u.email FROM runs r JOIN users u ON r.user_id=u.id WHERE r.id=?",
                    (run["run_id"],),
                ).fetchone()
                nonce = case["synthetic_code"].removeprefix("BENCH-").lower()
                if not row or not row["email"].startswith("bench-" + nonce + "-"):
                    raise ValueError("Wrong benchmark owner")
                turn = db.execute(
                    "SELECT t.* FROM turns t JOIN turn_runs r ON r.turn_id=t.id WHERE r.run_id=?",
                    (run["run_id"],),
                ).fetchone()
                if turn["phase"] != "terminal":
                    raise RuntimeError("Continuation still active: " + run["run_id"])
                backup = output / "collector_original"
                backup.mkdir(exist_ok=True)
                if not (backup / path.name).exists():
                    shutil.copy2(path, backup / path.name)
                run.update(
                    collector_initial_status="paused",
                    status=turn["outcome"],
                    answer=turn["assistant_text"],
                    elapsed_seconds=round(turn["finished"] - row["created"], 2),
                    **read_trace(root, run["run_id"], row["user_id"]),
                )
                for check in case["checks"]:
                    if check["name"] == "run_completed":
                        check["passed"] = turn["outcome"] == "completed"
                    if check["name"] == "usable_resumed_answer":
                        check["passed"] = bool(turn["assistant_text"].strip())
                case["passed"] = not case.get("error") and all(c["passed"] for c in case["checks"])
                case["notes"].append(
                    "采集器最初读到continue响应后的旧paused投影；现按同一run持久终态校正并补采轨迹，未重新执行场景。原采集快照保留在collector_original。"
                )
                path.write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
                changed += 1
    print("reconciled same-run continuations:", changed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    main(args.root, args.output)
