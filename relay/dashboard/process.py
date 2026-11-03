from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from relay.logging_setup import scrub


class BotProcess:
    def __init__(self, root: Path, argv: list[str] | None = None, online_marker: str = "logged in as"):
        self.root = Path(root)
        self.argv = argv or [sys.executable, str(self.root / "main.py")]
        self.marker = online_marker
        self.proc: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.exited_at: float | None = None
        self.stopping = False
        self.online = False
        self._scan_pos = 0
        self._watch: asyncio.Task | None = None

    @property
    def console_path(self) -> Path:
        return self.root / "logs" / "console.log"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("already running")
        self.console_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.console_path, "wb") as f:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, cwd=str(self.root), stdout=f, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.started_at, self.exit_code, self.exited_at = time.time(), None, None
        self.stopping, self.online, self._scan_pos = False, False, 0
        self._watch = asyncio.create_task(self._wait())

    async def _wait(self) -> None:
        code = await self.proc.wait()
        self.exit_code, self.exited_at = code, time.time()

    async def stop(self, timeout: float = 20.0) -> None:
        if not self.running:
            return
        self.stopping = True
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            await self.proc.wait()
        if self._watch:
            await self._watch
        self.online = False

    def _scan(self) -> None:
        if self.online or not self.console_path.exists():
            return
        with open(self.console_path, "rb") as f:
            f.seek(self._scan_pos)
            chunk = f.read()
            self._scan_pos += len(chunk)
        if self.marker.encode() in chunk:
            self.online = True

    def tail(self, n: int = 14) -> list[str]:
        try:
            data = self.console_path.read_bytes()[-12000:].decode("utf-8", "replace")
        except OSError:
            return []
        return [scrub(x) for x in data.splitlines() if x.strip()][-n:]

    def status(self) -> dict:
        if self.running:
            self._scan()
            state = "stopping" if self.stopping else ("online" if self.online else "starting")
        elif self.proc is None:
            state = "stopped"
        elif self.exit_code not in (0, None) and not self.stopping:
            state = "crashed"
        else:
            state = "stopped"
        return {
            "state": state,
            "pid": self.proc.pid if self.running else None,
            "uptime_s": int(time.time() - self.started_at) if self.running and self.started_at else None,
            "exit_code": self.exit_code,
            "tail": self.tail() if state == "crashed" else [],
        }
