#!/usr/bin/env python3
"""Wrapper that runs a command, logs output, and notifies Discord when done.

Progress notifications are exponential: 1m, 5m, 25m, 125m, ... (capped at 8h).

Usage:
    # Run mode: spawn a command, log output, notify when done
    tmux_watcher.py "task name" -- command arg1 arg2 ...

    # Monitor mode: watch an existing tmux session, log its output, notify when it ends
    tmux_watcher.py --monitor SESSION_NAME "task name"

Examples:
    python3 ~/kv/public-scripts/tmux_watcher.py "L003 upload" -- rclone copy /src gdrive:dest
    python3 ~/kv/public-scripts/tmux_watcher.py --monitor dispatcher "72 bulk download"

Logs to: ~/kv/tmux-watcher/logs/<slugified-name>.log
"""
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1458770306796621947/rQk9OllBY1VQNRkoDhprfs-z8JonKi-hQuU_nNqpExghfquqTsVVudy60n5Rlv2YhGG1"
LOG_DIR = Path.home() / "kv/tmux-watcher/logs"
HOSTNAME = socket.gethostname()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def read_tail(log_file: Path, n: int = 15) -> str:
    if not log_file.exists():
        return "(no log yet)"
    lines = log_file.read_text().splitlines()
    return "\n".join(lines[-n:])


def post_discord(title: str, body: str, color: int) -> None:
    requests.post(DISCORD_WEBHOOK, json={
        "embeds": [{
            "title": title,
            "description": f"```\n{body[-1500:]}\n```",
            "color": color,
        }]
    })


def progress_loop(name: str, log_file: Path, stop_event: threading.Event) -> None:
    interval_min = 1
    max_interval_min = 480  # 8 hours
    while not stop_event.wait(interval_min * 60):
        tail = read_tail(log_file)
        post_discord(f"[{HOSTNAME}] {name} (in progress, next in {interval_min * 5}m)", tail, 0x3498DB)
        interval_min = min(interval_min * 5, max_interval_min)


def tmux_session_alive(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


def tmux_capture_pane(session: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", "-200"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def monitor_session(session: str, name: str, log_file: Path) -> None:
    print(f"[tmux-watcher] Monitoring tmux session: {session}", flush=True)
    print(f"[tmux-watcher] Log: {log_file}", flush=True)

    if not tmux_session_alive(session):
        print(f"[tmux-watcher] Session '{session}' not found!", flush=True)
        post_discord(f"[{HOSTNAME}] {name} ERROR", f"tmux session '{session}' not found", 0xFF0000)
        return

    stop_event = threading.Event()
    print("[tmux-watcher] Progress: 1m, 5m, 25m, 125m, ... (exponential, cap 8h)", flush=True)
    t = threading.Thread(target=progress_loop, args=(name, log_file, stop_event), daemon=True)
    t.start()

    prev_content = ""
    while tmux_session_alive(session):
        content = tmux_capture_pane(session)
        if content != prev_content:
            log_file.write_text(content)
            prev_content = content
        time.sleep(10)

    # Session ended — capture final state
    stop_event.set()
    tail = read_tail(log_file, n=30)
    print(f"[tmux-watcher] Session '{session}' ended", flush=True)
    post_discord(f"[{HOSTNAME}] {name} ENDED", tail, 0xFFA500)
    print("[tmux-watcher] Discord notified", flush=True)


def run_command_foreground(cmd: list[str], name: str, log_file: Path) -> None:
    """Actually run the command (called inside the tmux session)."""
    stop_event = threading.Event()
    t = threading.Thread(target=progress_loop, args=(name, log_file, stop_event), daemon=True)
    t.start()

    with open(log_file, "w") as f:
        result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)

    stop_event.set()

    status = "FINISHED" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    color = 0x00FF00 if result.returncode == 0 else 0xFF0000
    tail = read_tail(log_file, n=30)

    post_discord(f"[{HOSTNAME}] {name} {status}", tail, color)


def run_command(cmd: list[str], name: str, log_file: Path) -> None:
    """Spawn a tmux session that runs the command, then return immediately."""
    session_name = slugify(name)

    # NOTE: re-invoke ourselves with --_foreground inside the tmux session
    watcher_cmd = [sys.executable, __file__, "--_foreground", name, "--"] + cmd
    tmux_cmd = ["tmux", "new-session", "-d", "-s", session_name, " ".join(f"'{a}'" for a in watcher_cmd)]

    subprocess.run(tmux_cmd, check=True)

    print(f"[tmux-watcher] Started tmux session: {session_name}", flush=True)
    print(f"[tmux-watcher] Log: {log_file}", flush=True)
    print(f"[tmux-watcher] Progress: 1m, 5m, 25m, 125m, ... (exponential, cap 8h)", flush=True)
    print(f"[tmux-watcher] Attach with: tmux attach -t {session_name}", flush=True)


def main() -> None:
    monitor_session_name = None
    foreground = False
    argv = sys.argv[1:]

    # NOTE: --progress is accepted but ignored (kept for backwards compat)
    if "--progress" in argv:
        idx = argv.index("--progress")
        argv = argv[:idx] + argv[idx + 2:]

    if "--monitor" in argv:
        idx = argv.index("--monitor")
        monitor_session_name = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2:]

    if "--_foreground" in argv:
        idx = argv.index("--_foreground")
        foreground = True
        argv = argv[:idx] + argv[idx + 1:]

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if monitor_session_name:
        name = " ".join(argv) if argv else monitor_session_name
        log_file = LOG_DIR / f"{slugify(name)}.log"
        monitor_session(monitor_session_name, name, log_file)
        return

    if "--" not in argv:
        print(f"Usage: {sys.argv[0]} \"name\" -- cmd ...")
        print(f"       {sys.argv[0]} --monitor SESSION \"name\"")
        sys.exit(1)

    sep = argv.index("--")
    name = " ".join(argv[:sep])
    cmd = argv[sep + 1:]

    if not name or not cmd:
        print(f"Usage: {sys.argv[0]} \"name\" -- cmd ...")
        sys.exit(1)

    log_file = LOG_DIR / f"{slugify(name)}.log"
    if foreground:
        run_command_foreground(cmd, name, log_file)
    else:
        run_command(cmd, name, log_file)


if __name__ == "__main__":
    main()
