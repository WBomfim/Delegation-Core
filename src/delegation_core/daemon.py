"""
daemon.py: the client half of the HTTP transport.

Why this exists
---------------
v0.11 moved the MCP server from stdio to a single HTTP daemon so that one
process owns the BGE model and the ChromaDB index. That fixed the *client* side
of multi-writer (four editors no longer spawn four servers) but left the
*command line* side untouched: ``delegation-core reindex``, ``maintain`` and
``ingest`` still each build their own ``VaultManager``, load BGE onto the GPU
again, and open a second ``PersistentClient`` against the same index directory
while the daemon holds it open.

That is not a theoretical race. The hooks fire exactly those commands as
detached processes: ``hooks/session_export.py`` runs ``reindex`` after writing
a transcript, ``hooks/session_start_brief.py`` runs ``maintain`` and a backstop
``reindex``, so the common path through the product is the one that
reintroduces the concurrent writer. ``VaultManager._reload_if_disk_changed()``
exists to survive it: the running daemon notices the mtime change and reopens
the collection, and this line shows up in the journal minutes after a session
starts:

    [INFO] Index changed on disk by another process: reopening

That guard stays: a person can always run the CLI while the daemon is down, or
edit the vault from a shell, but it should be the safety net, not the design.
When the daemon is up, the work belongs to it.

What routing buys, beyond correctness
-------------------------------------
The second process was not free: it loaded its own copy of BGE-m3 (~2.4 GiB of
VRAM measured on this machine) for the duration of a reindex, on top of the
daemon's. Routing turns that into an HTTP call whose whole cost is a JSON
round-trip, and the model that is already resident does the work.

Failure policy
--------------
A call that never reached a daemon (nothing listening, connection refused
mid-flight) raises ``DaemonUnavailable``, and callers fall back to running
in-process: a machine with no daemon must still be able to reindex. A call
that *did* reach the daemon and failed there raises ``DaemonCallFailed``, which
callers must not paper over by running locally: that would answer "the daemon
had a problem" by starting the exact second writer this module removes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

logger = logging.getLogger("daemon")

#: A loopback TCP connect is either immediate or never; this only has to be long
#: enough to survive a busy machine, not a network.
PROBE_TIMEOUT_SEC = 0.5

#: Ceiling on how long a single tool call may take. Generous because the two
#: submit calls are cheap but ``ingest_folder``-style synchronous tools are not.
CALL_TIMEOUT_SEC = 120.0

#: Poll pacing while waiting for a background job. The daemon's own
#: ``check_again_in_seconds`` is the long-run signal (it comes from the median
#: duration of past runs) but it is tuned for an agent spending a whole turn
#: per poll, and it has a floor of 30s. A blocked CLI process is not paying by
#: the turn, and the job it usually waits on is an incremental reindex that
#: finishes in well under a second: obeying the hint made a 70ms reindex take
#: 10.6s of wall clock, measured. So the interval starts short and grows,
#: bounded by the hint: fast jobs return immediately, long jobs settle into a
#: slow beat instead of hammering the daemon.
POLL_INITIAL_SEC = 0.25
POLL_GROWTH = 1.6
POLL_MAX_SEC = 10.0

#: Give up on a background job rather than blocking a hook forever. A full
#: reindex of a five-figure vault is minutes, not an hour.
JOB_WAIT_TIMEOUT_SEC = 3600.0


class DaemonUnavailable(RuntimeError):
    """No daemon answered: the caller may safely do the work itself."""


class DaemonCallFailed(RuntimeError):
    """A daemon answered and the call failed. Do not fall back to local work."""


def is_listening(cfg, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """True if something accepts connections on the configured host/port.

    Only a liveness probe: it proves a socket, not that the process behind it is
    a delegation-core daemon. Anything else is caught by the call itself, which
    is why this is a fast pre-check and not the authority.
    """
    try:
        with socket.create_connection((cfg.server_host, cfg.server_port), timeout):
            return True
    except OSError:
        return False


def _payload(result) -> dict:
    """Unwrap a CallToolResult into the dict the tool returned.

    Every tool in server.py returns ``json.dumps(...)`` as its single text
    content block. A tool that ever returns bare text still produces something
    usable here rather than an exception.
    """
    content = getattr(result, "content", None) or []
    text = getattr(content[0], "text", "") if content else ""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"result": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _build_client(cfg, timeout: float):
    from fastmcp import Client
    import mcp.types

    from . import __version__

    # The MCP client libraries log every request and every session id at INFO,
    # and these commands run under logging.basicConfig(INFO) from a hook whose
    # output is a log file a person reads. Transport chatter is not what that
    # file is for; a failure still surfaces as an exception, not a log line.
    for noisy in ("httpx", "mcp.client.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return Client(
        cfg.server_url,
        auth=cfg.server_token,
        timeout=timeout,
        init_timeout=timeout,
        # Identify as the CLI rather than inheriting a generic default: the
        # daemon keys its connected-client rows on the initialize handshake, so
        # this is what makes a hook-triggered reindex visible in
        # list_mcp_clients() as the CLI's doing instead of an anonymous session.
        client_info=mcp.types.Implementation(
            name="delegation-core-cli", version=__version__),
    )


def _run(coro, what: str):
    """Run a daemon coroutine, mapping transport failures onto the two errors.

    The port was open a moment ago, so a connection failure here means the
    daemon went away between the probe and the call (a restart, most likely):
    that is still "no daemon", and the caller may proceed alone.
    """
    try:
        return asyncio.run(coro)
    except (DaemonUnavailable, DaemonCallFailed):
        raise
    except Exception as exc:
        if _is_connection_error(exc):
            raise DaemonUnavailable(f"daemon went away during {what}: {exc}") from exc
        raise DaemonCallFailed(f"{what} failed on the daemon: {exc}") from exc


def call_tool(cfg, tool: str, arguments: dict | None = None,
              timeout: float = CALL_TIMEOUT_SEC) -> dict:
    """Call one MCP tool on the running daemon and return its parsed result.

    Raises ``DaemonUnavailable`` if nothing is listening or the connection is
    refused, ``DaemonCallFailed`` for anything the daemon itself rejected.
    """
    if not is_listening(cfg):
        raise DaemonUnavailable(f"nothing listening on {cfg.server_host}:{cfg.server_port}")

    async def _once():
        async with _build_client(cfg, timeout) as client:
            return _payload(await client.call_tool(tool, arguments or {}))

    return _run(_once(), tool)


def _is_connection_error(exc: BaseException) -> bool:
    """True if this exception (or anything it wraps) is a failure to connect.

    httpx raises ConnectError/ConnectTimeout for this, and both derive from
    OSError only indirectly, so the check walks __cause__/__context__; FastMCP
    and anyio both re-wrap transport errors on the way up.
    """
    conn_error_names = {
        "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
        "ProtocolError", "LocalProtocolError", "CloseError", "NetworkError",
    }

    # Iterative, with ONE `seen` for the whole walk. This used to recurse into
    # `ExceptionGroup.exceptions`, and each recursive call started a fresh
    # `seen`: the loop below was cycle-safe, the recursion around it was not.
    # A group whose member's __context__ points back at the group makes the two
    # call frames hand the same pair to each other forever, and the walk dies
    # with RecursionError instead of answering the question.
    #
    # That is not hypothetical shape-hunting: anyio task groups wrap in these,
    # and a re-raised transport error inside a task group is exactly how a
    # member ends up carrying the group as its context. A linear chain of
    # deeply nested groups had the same problem more quietly, spending one
    # stack frame per level.
    seen: set[int] = set()
    pendentes: list[BaseException] = [exc]

    while pendentes:
        current: BaseException | None = pendentes.pop()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (ConnectionError, OSError, TimeoutError)):
                return True
            if type(current).__name__ in conn_error_names:
                return True
            if isinstance(current, ExceptionGroup):  # anyio task groups wrap in these
                pendentes.extend(current.exceptions)
            current = current.__cause__ or current.__context__
    return False


class _Poll:
    """Mutable pacing state for one wait, so the policy below stays pure."""

    def __init__(self, timeout: float):
        self.interval = POLL_INITIAL_SEC
        self.deadline = time.monotonic() + timeout
        self.timeout = timeout


def next_poll_wait(status: dict, job_id: str, tool: str, poll: _Poll) -> float | None:
    """How long to wait before polling again, or None when the job is finished.

    All the decisions that can go wrong live here, in one synchronous function
    with no transport underneath it, because two of them already did:

    * ``status`` is the decisive field, not the presence of an ``"error"`` key.
      ``jobs.submit()`` seeds every job with ``error=None``, so a perfectly
      healthy job carries that key. Keying on the key rather than its value
      declared a *finished* reindex lost while the daemon's log showed the work
      completing. Not-found is the one shape with no ``"status"`` at all.
    * the daemon's ``check_again_in_seconds`` is a floor of 30s aimed at an
      agent spending a turn per poll. Obeying it turned a 70ms incremental
      reindex into 10.6s of wall clock.
    """
    state = status.get("status")
    if state is None:
        # In-memory job ids do not survive a daemon restart, so a job that
        # vanishes is not necessarily a job that failed: say which it is.
        raise DaemonCallFailed(
            f"the daemon lost job {job_id} ({status.get('error')}) : it may have "
            f"restarted mid-run; the work's state is unknown")
    if state == "done":
        return None
    if state == "error":
        raise DaemonCallFailed(f"{tool} failed on the daemon: {status.get('error')}")
    if time.monotonic() >= poll.deadline:
        raise DaemonCallFailed(
            f"{tool} (job {job_id}) still running after {int(poll.timeout)}s: "
            f"left running on the daemon; check task_status({job_id})")

    hint = float(status.get("check_again_in_seconds") or POLL_MAX_SEC)
    wait = min(poll.interval, hint, POLL_MAX_SEC,
               max(poll.deadline - time.monotonic(), 0.0))
    poll.interval = min(poll.interval * POLL_GROWTH, POLL_MAX_SEC)
    return wait


def submit_and_wait(cfg, tool: str, arguments: dict | None = None, *,
                    on_wait=None, timeout: float | None = JOB_WAIT_TIMEOUT_SEC) -> dict:
    """Call a ``*_bg`` tool, then poll ``task_status`` until the job finishes.

    Returns the finished job dict (``job["result"]`` is whatever the underlying
    function returned). A tool that answers synchronously (no ``job_id``) is
    returned as-is, so this is safe to point at either kind.

    ``on_wait(seconds, job)`` is called before each sleep, for progress output.

    ``timeout`` bounds the whole operation, and that includes the tool call
    itself. Until 26/09/2026 it bounded only the polling loop, and the call was
    always capped at ``CALL_TIMEOUT_SEC``: a SYNCHRONOUS tool never reaches the
    polling loop, so for one of those the argument governed nothing. Measured
    that day on `vault_rename_note`, which takes 64 to 125 s on this vault
    because it re-embeds every note whose links it repointed, with the encoder
    on CPU: calls passing ``timeout=600`` failed at 123 s anyway, and about half
    the batch died on a ceiling the caller believed it had lifted. The floor
    stays at ``CALL_TIMEOUT_SEC`` so a caller asking for less than the default
    cannot make an ordinary call more fragile than it was.

    The submit and every poll share one MCP session. A session per call also
    works, but each costs an initialize/GET/DELETE round trip and registers its
    own row in the daemon's connected-client tracking (one `maintain` showed up
    as three separate clients).
    """
    if not is_listening(cfg):
        raise DaemonUnavailable(f"nothing listening on {cfg.server_host}:{cfg.server_port}")

    effective_timeout = float("inf") if (timeout is None or timeout <= 0) else float(timeout)
    # `inf` means "wait as long as it takes" for the poll loop, but an HTTP
    # client needs a number, so an unbounded wait keeps the default ceiling
    # rather than being handed infinity.
    call_timeout = (CALL_TIMEOUT_SEC if effective_timeout == float("inf")
                    else max(CALL_TIMEOUT_SEC, effective_timeout))

    async def _session():
        async with _build_client(cfg, call_timeout) as client:
            submitted = _payload(await client.call_tool(tool, arguments or {}))
            job_id = submitted.get("job_id")
            if not job_id:
                return submitted

            poll = _Poll(effective_timeout)
            while True:
                status = _payload(
                    await client.call_tool("task_status", {"job_id": job_id}))
                wait = next_poll_wait(status, job_id, tool, poll)
                if wait is None:
                    return status
                if on_wait:
                    on_wait(wait, status)
                await asyncio.sleep(wait)

    return _run(_session(), tool)

