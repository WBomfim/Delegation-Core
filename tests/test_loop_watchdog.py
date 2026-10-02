"""The daemon ends itself when its event loop stops, so the supervisor restarts it.

On 2026-09-26 and 2026-09-29 the Mac daemon kept port 8787 open and answered
nothing, and launchd, which restarts only a process that exits, left it so. The
stuck thread held the GIL, so the stand-in here is native code that holds it
too: ctypes.PyDLL does not release the GIL around the call, unlike time.sleep.

Each case runs in a child process, because the watchdog's job is to _exit it.
"""

import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from delegation_core import server

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="libc sleep via ctypes")

SRC = str(Path(server.__file__).resolve().parents[1])


def _run(code: str, tmp_path, timeout: float = 30) -> tuple[subprocess.CompletedProcess, float, str]:
    log = tmp_path / "watchdog.log"
    prelude = textwrap.dedent(f"""
        import asyncio, ctypes, sys
        from pathlib import Path
        from delegation_core import server
        server.WATCHDOG_LOG = Path({str(log)!r})
        def hold_the_gil(seconds):
            ctypes.PyDLL(None).sleep(seconds)
    """)
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)],
                          capture_output=True, text=True, timeout=timeout, env=env)
    return done, time.monotonic() - started, log.read_text() if log.exists() else ""


def test_a_loop_stuck_holding_the_gil_ends_the_process(tmp_path):
    done, took, log = _run("""
        async def main():
            asyncio.get_running_loop().create_task(server._rearm_watchdog_forever(1.0, 0.2))
            await asyncio.sleep(0.3)
            hold_the_gil(20)
        asyncio.run(main())
        print("survived")
    """, tmp_path)

    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert "survived" not in done.stdout
    assert took < 10, f"took {took:.1f}s to end a loop stuck for 20s"
    assert "Thread" in log and "hold_the_gil" in log, f"no stacks written: {log!r}"


def test_a_healthy_loop_is_left_alone(tmp_path):
    done, _, log = _run("""
        async def main():
            asyncio.get_running_loop().create_task(server._rearm_watchdog_forever(0.6, 0.1))
            await asyncio.sleep(2.0)          # over three timeouts, re-armed throughout
        asyncio.run(main())
        print("survived")
    """, tmp_path)

    assert done.returncode == 0, (done.returncode, done.stderr)
    assert "survived" in done.stdout
    assert log == ""


def test_zero_turns_it_off():
    assert server._arm_watchdog(0) is False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_http_server_arms_it_through_the_lifespan(tmp_path):
    """The lifespan is what re-arms it, and only a real HTTP run proves fastmcp
    enters it. A tool that holds the GIL on the loop stands in for the hang."""
    port = _free_port()
    done, took, log = _run(f"""
        import threading, time
        from types import SimpleNamespace
        server._engine = SimpleNamespace(cfg=SimpleNamespace(loop_watchdog_sec=1.5))

        @server.mcp.tool()
        async def hang_the_loop() -> str:
            hold_the_gil(30)
            return "survived"

        def call_it():
            from fastmcp import Client
            async def go():
                async with Client("http://127.0.0.1:{port}/mcp") as c:
                    await c.call_tool("hang_the_loop", {{}})
            time.sleep(3.0)                   # healthy for two timeouts first
            try:
                asyncio.run(go())
            except Exception:
                pass

        threading.Thread(target=call_it, daemon=True).start()
        server.mcp.run(transport="http", host="127.0.0.1", port={port},
                       show_banner=False)
    """, tmp_path, timeout=60)

    assert done.returncode == 1, (done.returncode, done.stderr[-2000:])
    assert "hang_the_loop" in log, f"stacks missing: {log[-1500:]!r}"
    assert took < 20, f"took {took:.1f}s"
