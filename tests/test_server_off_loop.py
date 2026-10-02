"""Tool calls that block must not stop the event loop.

FastMCP dispatches every request as a task on one event loop. Tools declared
`async def` that call chromadb or the embedding model synchronously ran on that
loop, so one call stuck inside chromadb froze every other request. That is how
the Mac daemon stopped answering on 2026-09-26: socket open, initialize timing
out after 10s.
"""

import asyncio
import inspect
import threading
import time

import pytest

from delegation_core import server


SYNC_TOOLS = [
    "read_note", "write_note", "vault_stats", "export_session",
    "vault_list_notes", "vault_find_notes", "vault_rename_note", "vault_note_links",
    "vault_inbox_status", "vault_find_similar", "vault_update_note",
    "graph_list", "graph_report",
    "process_create", "process_list", "process_update", "process_get",
]


@pytest.mark.parametrize("name", SYNC_TOOLS)
def test_blocking_tools_are_plain_functions_so_fastmcp_threads_them(name):
    assert not inspect.iscoroutinefunction(getattr(server, name)), (
        f"{name} is async def around synchronous work: it runs on the event loop")


def test_the_tools_are_still_registered_with_their_parameters():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in SYNC_TOOLS:
        assert name in tools, f"{name} is no longer registered"
    assert set(tools["write_note"].parameters["properties"]) >= {"folder", "title", "content"}
    assert set(tools["vault_update_note"].parameters["properties"]) == {
        "note_name", "append_content"}


def test_a_slow_search_leaves_the_loop_free(monkeypatch):
    class SlowVault:
        def search(self, *a, **k):
            time.sleep(0.6)
            return []

    monkeypatch.setattr(server, "_vault", SlowVault())

    async def main():
        gaps = []

        async def ticker():
            last = time.monotonic()
            for _ in range(10):
                await asyncio.sleep(0.05)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        async def search_once_the_ticker_runs():
            # search_vault would otherwise run to completion before the ticker
            # took its first step, and a stall nobody was timing looks like none.
            await asyncio.sleep(0.1)
            await server.search_vault("q", scope="all")

        await asyncio.gather(ticker(), search_once_the_ticker_runs())
        return max(gaps)

    worst = asyncio.run(main())
    assert worst < 0.3, f"the loop stalled {worst:.2f}s behind a 0.6s search"


def test_writing_tools_stay_serialised_among_themselves(monkeypatch):
    """The loop used to serialise every tool. Moving the writers to threads must
    not let two of them interleave."""
    active = []
    overlap = []

    class Tracker:
        def create(self, name, description="", steps=""):
            active.append(1)
            if len(active) > 1:
                overlap.append(True)
            time.sleep(0.05)
            active.pop()
            return {"id": name}

    monkeypatch.setattr(server, "_tracker", Tracker())
    threads = [threading.Thread(target=server.process_create, args=(f"p{i}",))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert not overlap, "two writing tools ran at the same time"
