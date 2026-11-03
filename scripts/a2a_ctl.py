"""Start, stop and inspect the five A2A processes.

    .venv/bin/python scripts/a2a_ctl.py start|stop|restart|status

Host: 18210 (API + orchestrator). Agents: planner 18211, worker 18212,
critic 18213, synthesizer 18214. Every process binds 127.0.0.1 only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
PIDS = ROOT / "a2a_state" / "pids"
LOGS = ROOT / "logs"

PORTS = {
    "worker": 18212,
    "critic": 18213,
    "synthesizer": 18214,
    "planner": 18211,
    "host": 18210,
}
# Host last: it discovers agent cards at startup.
START_ORDER = ("worker", "critic", "synthesizer", "planner", "host")


def env_for(role: str) -> dict:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ROLE": role}
    env.setdefault("DATA_DIR", str(ROOT / "ui_data"))
    return env


def pid_file(role: str) -> Path:
    return PIDS / f"{role}.pid"


def _is_ours(role: str, cmdline: bytes) -> bool:
    """The host runs under uvicorn, so it has no --role flag to match on."""
    if role == "host":
        return b"agentgpt.app:app" in cmdline
    return f"--role\x00{role}".encode() in cmdline


def running_pid(role: str) -> int | None:
    path = pid_file(role)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return None
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text()
        if stat.split(") ", 1)[1][0] == "Z":
            return None
        if (proc / "cwd").resolve() != ROOT:
            return None
        cmdline = (proc / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        # The process exited between the stat and the read; treat it as already gone.
        return None
    if not _is_ours(role, cmdline):
        return None
    return pid


def start_role(role: str) -> int:
    pid = running_pid(role)
    if pid:
        return pid
    PIDS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"a2a-{role}.log").open("ab")
    command = [PYTHON, "-m", "agentgpt", "--role", role]
    if role == "host":
        command = [
            PYTHON,
            "-m",
            "uvicorn",
            "agentgpt.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORTS["host"]),
        ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env_for(role),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file(role).write_text(str(process.pid))
    return process.pid


def stop_role(role: str) -> None:
    pid = running_pid(role)
    if not pid:
        pid_file(role).unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file(role).unlink(missing_ok=True)
        return
    for _ in range(80):
        if running_pid(role) is None:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_file(role).unlink(missing_ok=True)


def wait_healthy(role: str, timeout: float = 40.0) -> bool:
    port = PORTS[role]
    path = "/health" if role == "host" else "/.well-known/agent-card.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - retry until the deadline
            time.sleep(0.3)
    return False


def start(only: str = "") -> int:
    """Agents first, and the Host only once every card answers.

    The Host discovers all four cards in its lifespan; one fetched too early would
    leave the registry permanently degraded.
    """
    order = [only] if only else list(START_ORDER)
    agents = [role for role in order if role != "host"]
    for role in agents:
        print(f"{role:12s} pid={start_role(role)}")
    failed = [role for role in agents if not wait_healthy(role)]
    if failed:
        for role in failed:
            print(f"{role:12s} NOT READY", file=sys.stderr)
        print("查看日志：", LOGS / "a2a-*.log", file=sys.stderr)
        return 1
    if "host" in order:
        print(f"{'host':12s} pid={start_role('host')}")
        if not wait_healthy("host"):
            print("host         NOT READY", file=sys.stderr)
            print("查看日志：", LOGS / "a2a-*.log", file=sys.stderr)
            return 1
    for role in order:
        print(f"{role:12s} ok")
    return 0


def stop(only: str = "") -> int:
    for role in reversed([only] if only else list(START_ORDER)):
        stop_role(role)
        print(f"{role:12s} stopped")
    return 0


def status() -> int:
    for role in START_ORDER:
        pid = running_pid(role)
        health = "up" if wait_healthy(role, timeout=1.5) else "down"
        print(f"{role:12s} pid={pid or '-':>8} health={health}")
    try:
        with urllib.request.urlopen("http://127.0.0.1:18210/health", timeout=3) as response:
            print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - status output only
        print("host health unavailable:", exc)
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    if only and only not in PORTS:
        raise SystemExit(f"未知角色 {only}；可选：{', '.join(PORTS)}")
    if action == "start":
        raise SystemExit(start(only))
    if action == "stop":
        raise SystemExit(stop(only))
    if action == "restart":
        stop(only)
        raise SystemExit(start(only))
    raise SystemExit(status())
