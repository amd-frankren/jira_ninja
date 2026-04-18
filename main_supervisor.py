#!/usr/bin/env python3
"""
Process supervisor for main.py.

Features:
- Start main.py as a child process
- Monitor process state continuously
- Auto-restart when child exits unexpectedly
- Graceful shutdown on Ctrl+C / SIGTERM
- Optional restart delay and max restart limit

Usage:
    python main_supervisor.py
    python main_supervisor.py --check-interval 2 --restart-delay 3
    python main_supervisor.py --max-restarts 10
"""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

# Default args passed to main.py (can be adjusted here directly)
# DEFAULT_MAIN_ARGS: List[str] = [
#     "--interval", "60",
#     "--since-minutes", "0",
#     "--disable-sharepoint-upload",
#     "--debug-skip-target-scp-check",
#     "--debug-treat-updated-as-created",
# ]

DEFAULT_MAIN_ARGS: List[str] = [
    "--interval", "60",
    "--since-minutes", "60",
    "--disable-sharepoint-upload",
]


class MainSupervisor:
    def __init__(
        self,
        target_script: Path,
        check_interval: float = 30.0,
        restart_delay: float = 3.0,
        max_restarts: int = 0,
        script_args: Optional[List[str]] = None,
    ) -> None:
        self.target_script = target_script
        self.check_interval = check_interval
        self.restart_delay = restart_delay
        self.max_restarts = max_restarts  # 0 means unlimited
        extra_args = script_args or []
        self.script_args = [*DEFAULT_MAIN_ARGS, *extra_args]
        self.restart_count = 0
        self.proc: Optional[subprocess.Popen] = None
        self._running = True

    def _log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def _start_child(self) -> None:
        cmd = [sys.executable, str(self.target_script), *self.script_args]
        self.proc = subprocess.Popen(cmd)
        self._log(f"[info] started main.py, pid={self.proc.pid}")

    def _stop_child(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            return

        self._log(f"[info] stopping child pid={self.proc.pid}")
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
            self._log("[info] child terminated gracefully")
        except subprocess.TimeoutExpired:
            self._log("[warn] child did not exit in time, killing...")
            self.proc.kill()
            self.proc.wait()
            self._log("[info] child killed")

    def _handle_signal(self, signum, _frame) -> None:
        self._log(f"[info] received signal={signum}, shutting down supervisor...")
        self._running = False
        self._stop_child()

    def run(self) -> int:
        if not self.target_script.exists():
            self._log(f"[error] target script not found: {self.target_script}")
            return 1

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        self._log("[info] supervisor started")
        self._start_child()

        while self._running:
            time.sleep(self.check_interval)

            if not self.proc:
                continue

            ret = self.proc.poll()
            if ret is None:
                continue  # child still running

            self._log(f"[warn] child exited with code={ret}")

            if not self._running:
                break

            if self.max_restarts > 0 and self.restart_count >= self.max_restarts:
                self._log(
                    f"[error] reached max restarts ({self.max_restarts}), supervisor exiting."
                )
                return 2

            self.restart_count += 1
            self._log(
                f"[info] restarting child in {self.restart_delay:.1f}s "
                f"(restart #{self.restart_count})"
            )
            time.sleep(self.restart_delay)
            self._start_child()

        self._log("[info] supervisor stopped")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start and supervise main.py; auto-restart on crash."
    )
    parser.add_argument(
        "--script",
        default="main.py",
        help="Target script to supervise (default: main.py)",
    )
    parser.add_argument(
        "--check-interval",
        type=float,
        default=2.0,
        help="Health check interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=3.0,
        help="Delay before restarting crashed process in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=0,
        help="Max restart count; 0 means unlimited (default: 0)",
    )
    parser.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments passed to target script. Use '--' before extra args.",
    )
    args = parser.parse_args()

    if args.check_interval <= 0:
        parser.error("--check-interval must be > 0")
    if args.restart_delay < 0:
        parser.error("--restart-delay must be >= 0")
    if args.max_restarts < 0:
        parser.error("--max-restarts must be >= 0")

    return args


def main() -> int:
    args = parse_args()

    child_args = args.script_args
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]

    supervisor = MainSupervisor(
        target_script=Path(args.script).resolve(),
        check_interval=args.check_interval,
        restart_delay=args.restart_delay,
        max_restarts=args.max_restarts,
        script_args=child_args,
    )
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
