"""MCP server (stdio) exposing the proven MAME bridge plus a managed MAME lifecycle.

Protocol output stays on stdout (owned by the MCP SDK); diagnostics go to stderr.

Startup never needs MAME: the server initialises with no emulator running. `start_session` launches an isolated,
server-owned MAME on demand (several may run at once, up to MAMEMCP_MAX_SESSIONS) and `stop_session` stops one of them
(see session.py). Every session-bound tool takes `session_id`; omitting it selects the sole live session and is refused as
ambiguous when there are several. Configuration (environment, all optional):
  MAMEMCP_MAME_EXE     MAME executable: a path, or a command name looked up on PATH (takes precedence over MAMEMCP_MAME_DIR)
  MAMEMCP_MAME_DIR     folder containing the executable (mame.exe on Windows, mame elsewhere). Without either variable:
                       Windows uses D:\\arcade\\mame (or MAME_DIR), other platforms use `mame` found on PATH
  MAMEMCP_MAME_PLUGINS MAME's own plugin folder (boot.lua, json). Default: `plugins` next to the executable if it is there,
                       otherwise the pluginspath of MAME's own configuration (-showconfig)
  MAMEMCP_ROMPATH      ROM search directories, ';'-separated; the first holding <system>.zip/.7z/folder is used.
                       Default: `roms` next to the executable if it exists, otherwise MAME's configured rompath
  MAMEMCP_RUNS_DIR     parent of the per-session run directories (default <repo>/runs/sessions)
  MAMEMCP_MAX_SESSIONS maximum live (starting, ready or stopping) managed sessions, 1..16 (default 3); an invalid value
                       makes start_session refuse to launch anything until it is fixed

Optional EXTERNAL attachment (compatibility; not owned by this server, never started, stopped or killed by it):
  MAMEMCP_PORT         bridge port of an already-running MAME with the mamemcp plugin. Without it nothing is
                       attached implicitly and bridge tools report `no_session` until start_session.
  MAMEMCP_MAME_PID     optional PID of that MAME to nudge (WM_NULL) while a request waits; Windows only, opt-in
                       (see winwake.py). Only a process whose image is named mame*.exe is ever nudged.
  MAMEMCP_LAUNCH_INFO  optional launch.json of that MAME (adds ROM hashes / -verifyroms to session_info)
An external attachment is addressable explicitly as session_id "external". When session_id is omitted a managed session takes
precedence over it while one exists; with no managed session an omitted session_id uses the external attachment. The
managed-only tools (checkpoints, inputs, RAM writes, screenshots) never use it.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Literal

import base64

import anyio
from mcp_types import CallToolResult, ImageContent, TextContent
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, protocol, winwake
from .bridge import RETRY_SAFE_OPS, BridgeClient
from .protocol import BridgeError
from .session import EXTERNAL_ID, READY, SessionConfig, SessionManager

_PORT_ENV = os.environ.get("MAMEMCP_PORT")
PORT = int(_PORT_ENV) if _PORT_ENV else None
LAUNCH_INFO = os.environ.get("MAMEMCP_LAUNCH_INFO")


def _wake_pid() -> int | tuple[int, int | None] | None:
    """EXTERNAL attachment only: MAME to nudge while it is debugger-stopped (Windows only, opt-in): MAMEMCP_MAME_PID,
    or the pid (and creation time) the launcher recorded in launch.json (re-read so a restarted MAME launched into the
    same run dir is followed). winwake re-verifies image name, creation time (when recorded) and ownership of the
    bridge port before every nudge, so a stale PID is refused rather than followed to another process. Managed
    sessions do not use this: their wake identity is built from the launch result."""
    env = os.environ.get("MAMEMCP_MAME_PID")
    if env and env.isdigit():
        return int(env)  # no creation time known: the bridge-port ownership check still applies
    if LAUNCH_INFO:
        try:
            d = json.loads(Path(LAUNCH_INFO).read_text())
            return (d["pid"], d.get("pid_created_filetime")) if d.get("pid") else None
        except (OSError, ValueError, KeyError):
            return None
    return None


_external = BridgeClient(port=PORT, wake=winwake.waker_for(_wake_pid, port=PORT)) if PORT else None
manager = SessionManager(SessionConfig.from_env(), reserved_ports=frozenset({PORT}) if PORT else frozenset())

mcp = MCPServer(
    "mame-mcp",
    version=__version__,
    instructions=(
        "Bounded, typed debugging access to local MAME instances. The server starts with no emulator: call start_session "
        "(e.g. system='shinobi') to launch an isolated MAME with the debugger and get its session_id; several sessions (even of "
        "the same system) can be live at once, up to a configured limit, each with its own process, bridge, output directory, "
        "captures, inputs and checkpoints. Pass that session_id to every session-bound tool; omitting it works only while exactly "
        "one session is live and is refused as ambiguous otherwise (list_sessions shows them; an unknown or ended id is refused, "
        "never redirected). Call stop_session(session_id) when done (it finalises an active capture and stops only that MAME, "
        "which this server started). "
        "MAME starts stopped in the debugger: resume it with execution_control. session_info works at any time and "
        "reports no_session/starting/ready/stopping/exited/failed. Execution: `state` is the authoritative running/paused/"
        "debugger_stopped classification (`paused` is MAME's raw pause flag and can vary while the debugger is stopped); "
        "start_session returns a startup snapshot; execution_control only acknowledges a request, so confirm with a fresh "
        "session_info; a running state does not prove frames are advancing. Reads are inspection-only. "
        "capture_breakpoint starts an asynchronous, limit-bounded experiment inside MAME: poll "
        "experiment_status. Detailed events are written to JSONL evidence files, not returned here. "
        "Experiment ids belong to one session; ids from another or an earlier session are refused. "
        "capture_watchpoint is the same kind of experiment for ONE explicit memory range and access type (events carry the logical "
        "access address, data, hit-time PC and chosen registers, never a bank id). disassemble_memory decodes instructions of a "
        "stopped CPU through the view MAME decodes (opcodes space when it exists) and reports the address-map and bank context. "
        "CPU addresses are not ROM file offsets: banked memory means one CPU address can map to different code. "
        "Gameplay checkpoints (managed sessions only): list_inputs shows the machine's real digital inputs; press_inputs "
        "holds them for a bounded number of EMULATED frames and press_sequence runs a validated frame-offset schedule of such "
        "presses (nothing elapses while stopped, neither advances emulation); save_checkpoint / "
        "load_checkpoint need paused or debugger-stopped execution, no active capture and no held input, and a load makes "
        "every earlier experiment id stale; a checkpoint id never selects the session it is loaded into, so name the target session_id and "
        "the machine and MAME version must match; write_memory changes one explicit RAM location (stopped execution only, "
        "plain RAM only) and reads it back. list_screens / capture_screenshot return the emulated game screen as a PNG image plus its "
        "evidence path, frame number, execution state and freshness (capture never pauses, resumes or advances emulation; a screenshot "
        "shows what the screen displayed, not what the game is doing). There is no arbitrary Lua or debugger command execution."
    ),
)


def _log(msg: str) -> None:
    print(f"[mame-mcp] {msg}", file=sys.stderr, flush=True)


def _fail(e: BridgeError) -> ToolError:
    _log(f"error: {e}")
    return ToolError(json.dumps(e.to_dict()))


def _invoke(client: BridgeClient, name: str, *a):
    """Run a blocking bridge call on `client`, (re)connecting first when needed. State-changing calls and memory reads
    are never retried; see bridge.RETRY_SAFE_OPS."""
    if not client.connected:
        info = client.connect(timeout=10)
        if "session_changed_from" in info:
            # A different MAME process answered. Experiments/state of the old session are gone;
            # do not silently carry on with whatever call the agent made for the old session.
            raise BridgeError(
                "session_changed",
                f"MAME restarted: session {info['session_changed_from']} -> {client.session_id}. "
                "Re-check session_info and re-plan; nothing was sent for this call.",
            )
    t0, r0 = client.timeouts, client.read_retries
    n0 = len(client.transport_events)
    out = getattr(client, name)(*a)
    if isinstance(out, dict) and (client.timeouts != t0 or client.read_retries != r0):
        # provenance: this answer was obtained after a request_timeout (and a read-only retry)
        out["transport"] = {
            "timeouts_during_call": client.timeouts - t0,
            "read_only_retries_during_call": client.read_retries - r0,
            "wake_posts_total": client.wake_posts,
            "events": client.transport_events[n0:],
            "note": "late replies to timed-out attempts are matched by request id and discarded",
        }
    return out


def _call(name: str, session_id: str | None, *a, experiment_id: str | None = None):
    """Route one bridge call to the explicitly selected session (or the sole live one, or the optional external attachment).
    Lifecycle changes of THAT session are serialised against it: see SessionManager.use_session."""
    try:
        with manager.use_session(_external, session_id) as (client, managed, s):
            try:
                protocol.check_experiment_owner(experiment_id, s.session_id if s is not None else client.session_id)
                return _invoke(client, name, *a)
            except BridgeError as e:
                if managed and e.code in ("mame_disconnected", "connect_failed", "request_timeout"):
                    snap = manager.snapshot(s, settle=2.0 if e.code != "request_timeout" else 0.0)  # also notices an exited process
                    if snap["state"] == "exited":
                        raise BridgeError(
                            "session_exited",
                            f"the managed MAME exited ({snap.get('exit_reason')}, code {snap.get('exit_code')}) during this call",
                            detail={"exit_code": snap.get("exit_code"), "evidence_dir": snap.get("evidence_dir")},
                            uncertain=e.uncertain) from e
                raise
    except BridgeError as e:
        raise _fail(e) from e


def _validated(fn, *a):
    """Client-side argument validation; surfaces the reason instead of a generic tool failure."""
    try:
        return fn(*a)
    except BridgeError as e:
        raise _fail(e) from e


def _rom_info(launch_info: dict[str, Any] | None) -> dict[str, Any]:
    info = launch_info or {}
    return {
        "source": "launcher launch.json (expected hashes from -listxml; -verifyroms result)",
        "romset_zip": info.get("romset_zip"),
        "verifyroms": (info.get("verifyroms") or {}).get("output"),
        "expected_roms": info.get("expected_roms_from_listxml"),
    }


def _external_rom_info() -> dict[str, Any] | None:
    if not LAUNCH_INFO:
        return None
    try:
        return _rom_info(json.loads(Path(LAUNCH_INFO).read_text()))
    except (OSError, ValueError) as e:
        return {"unavailable": True, "reason": str(e)}


def _bridge_client_info(client: BridgeClient) -> dict[str, Any]:
    return {"timeouts": client.timeouts, "read_only_retries": client.read_retries,
            "stale_replies_discarded": client.stale_discarded,
            "wake_configured": client.wake is not None, "wake_posts": client.wake_posts,
            "wake_last_refusal": getattr(client.wake, "last_refusal", None),
            "default_timeout_s": client.default_timeout, "retry_safe_ops": sorted(RETRY_SAFE_OPS)}


def _external_session_info_blocking() -> dict[str, Any]:
    """session_info(session_id="external"): ONLY the externally attached MAME. Managed sessions are never selected, consulted or
    reported here (no managed state, ids or history), so this works and answers the same however many managed sessions exist."""
    if _external is None:
        raise _fail(BridgeError("unknown_session", "no external attachment is configured (MAMEMCP_PORT)"))
    session: dict[str, Any] = {"state": "no_session", "ownership": "external", "session_id": EXTERNAL_ID, "port": PORT,
                               "config": manager.cfg.public()}
    out: dict[str, Any] = {}
    try:
        with manager.use_session(_external, EXTERNAL_ID) as (client, managed, _s):
            assert not managed and client is _external
            try:
                out = _invoke(client, "session_info")
            except BridgeError as e:
                if e.code not in ("connect_failed", "mame_disconnected"):
                    raise _fail(e) from e  # e.g. session_changed: the agent must see it
                session["external_attach"] = {"configured": True, "port": PORT, "available": False, "error": e.to_dict()}
            else:
                out["bridge_client"] = _bridge_client_info(client)
                rom = _external_rom_info()
                if rom and "rom_identity" in out:
                    out["rom_identity"]["launch_info"] = rom
                session["state"] = "ready"
    except BridgeError as e:
        raise _fail(e) from e
    out["session"] = session
    return out


def _session_info_blocking(session_id: str | None = None) -> dict[str, Any]:
    if session_id == EXTERNAL_ID:
        return _external_session_info_blocking()
    try:  # an ambiguous or unknown id is an error: nothing is answered for a session that was not selected
        session = manager.snapshot(session_id)
    except BridgeError as e:
        raise _fail(e) from e
    target: str | None = session.get("run_id")
    out: dict[str, Any] = {}
    if session["state"] in ("ready", "no_session"):
        try:
            with manager.use_session(_external, target) as (client, managed, _s):
                try:
                    out = _invoke(client, "session_info")
                except BridgeError as e:
                    if managed:  # connection trouble or exit: report it next to the (refreshed) state
                        session = {**manager.snapshot(target, settle=2.0 if e.code != "request_timeout" else 0.0), "bridge_error": e.to_dict()}
                    elif e.code not in ("connect_failed", "mame_disconnected"):
                        raise _fail(e) from e  # e.g. session_changed: the agent must see it, exactly as before
                    else:  # external attachment configured but nothing is listening: "no session", not a failure
                        session["external_attach"] = {"configured": True, "port": PORT, "available": False, "error": e.to_dict()}
                else:
                    out["bridge_client"] = _bridge_client_info(client)
                    rom = _rom_info(manager.launch_info(target)) if managed else _external_rom_info()
                    if rom and "rom_identity" in out:
                        out["rom_identity"]["launch_info"] = rom
                    if not managed:
                        session = {"state": "ready", "ownership": "external", "session_id": EXTERNAL_ID, "port": PORT, "config": session["config"],
                                   **({"last_session": session["last_session"]} if "last_session" in session else {})}
        except BridgeError:
            pass  # no session configured, or the state changed under us: the session block explains it
    out["session"] = session
    return out


def _list_sessions_blocking() -> dict[str, Any]:
    base = manager.list_sessions()

    def probe(entry: dict[str, Any]) -> dict[str, Any]:  # one slow or hung session never delays reporting of the others
        if entry["state"] != READY:
            return {"state": entry["state"], "note": "no bridge query: the session is not ready"}
        try:
            with manager.use_session(None, entry["run_id"]) as (client, _managed, _s):
                if not client.connected:
                    return {"state": "unknown", "note": "bridge client not connected"}
                info = client.request("session_info", {}, timeout=5.0, retry_safe=True)
            return {**(info.get("execution") or {}), "clock": info.get("clock"), "active_experiment": info.get("active_experiment")}
        except BridgeError as e:
            return {"state": "unknown", "error": e.to_dict()}

    entries = base["sessions"]
    results: list[dict[str, Any]] = [{} for _ in entries]
    threads = []
    for i, e in enumerate(entries):
        threads.append(threading.Thread(target=lambda i=i, e=e: results.__setitem__(i, probe(e)), daemon=True))
        threads[-1].start()
    for th in threads:
        th.join(10.0)
    out_sessions = [{"session_id": e["session_id"], "run_id": e["run_id"], "system": e["system"], "ownership": "managed",
                     "lifecycle": e["state"], "execution": results[i] or {"state": "unknown", "note": "status probe did not finish"},
                     "pid": e["pid"], "port": e["port"], "run_dir": e["run_dir"], "evidence_dir": e["evidence_dir"],
                     "started_utc": e["started_utc"], "ready_utc": e["ready_utc"],
                     **({"exit_code": e.get("exit_code"), "exit_reason": e.get("exit_reason")} if "exit_reason" in e else {}),
                     **({"failure": {"code": e["failure"].get("code"), "message": e["failure"].get("message")}} if e.get("failure") else {})}
                    for i, e in enumerate(entries)]
    return {"sessions": out_sessions, "max_sessions": base["max_sessions"], "live_count": base["live_count"],
            "external": {"configured": _external is not None, "session_id": EXTERNAL_ID, "ownership": "external",
                         "port": PORT} if _external is not None else {"configured": False},
            "recent_ended": [{k: h.get(k) for k in ("session_id", "run_id", "system", "state", "exit_reason", "exit_code", "ended_utc")}
                             for h in base["recent_ended"]]}


@mcp.tool()
async def start_session(system: str, ready_timeout_s: float = 60.0) -> dict[str, Any]:
    """Launch an isolated MAME for `system` (a MAME short name such as 'shinobi') with the mamemcp plugin and the
    debugger enabled, wait (bounded by `ready_timeout_s`, 5..120) until the bridge answers, and return the NEW, unique
    `session_id` (pass it to every other tool), machine identity, an `execution` snapshot and the run/evidence directories.
    Several sessions, including several of the same system, can be live at once up to the server's limit (default 3,
    MAMEMCP_MAX_SESSIONS; sessions that are still starting count); a start beyond the limit is refused before anything is
    launched (`session_limit_reached`). Each session has its own process, bridge port and output directory. That snapshot was
    observed while the bridge was starting up and is not re-read when the reply is returned: call session_info for the current
    execution state. MAME starts stopped in the debugger; use execution_control(action='resume', session_id=...) to run it. The
    executable, ROM paths and MAME arguments are fixed by server configuration and cannot be set here. On failure the
    diagnostics are in the error, only the process this attempt launched is stopped, and other sessions are not affected."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.start(system, ready_timeout_s))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def stop_session(session_id: str | None = None) -> dict[str, Any]:
    """Stop ONE managed MAME session, the one named by `session_id` (or the sole live session when it is omitted; with several
    live sessions an omitted id is refused as ambiguous and nothing is stopped): cancel any active capture (breakpoints removed
    and verified, evidence finalised; execution state untouched), then stop only the MAME process this server launched for that
    session after verifying its identity. Other sessions keep running untouched. Reports capture cleanup, process exit code,
    any cleanup failures and the sessions still live. Never touches a MAME this server did not start (the external attachment
    cannot be stopped). With no managed session, or for a session that already ended, it is a no-op."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.stop(session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def list_sessions() -> dict[str, Any]:
    """List every session this server knows: `session_id`, `run_id`, system, ownership, `lifecycle` (starting | ready | stopping |
    exited | failed), a freshly read `execution` status for each ready session (running | paused | debugger_stopped, with the
    emulated clock and any active experiment), process id, bridge port, run/evidence directories and, for ended or failed
    sessions, the exit or failure reason. Also reports `max_sessions`, the live count, the external attachment (addressable as
    session_id "external") and recently ended sessions. Works with no MAME running. Does not need or accept a session_id."""
    return await anyio.to_thread.run_sync(_list_sessions_blocking)


@mcp.tool()
async def session_info(session_id: str | None = None) -> dict[str, Any]:
    """Session state for ONE session (`session_id`, or the sole live session when omitted; several live sessions and no id is
    an error, see list_sessions) (`session.state`: no_session | starting | ready | stopping | exited | failed, with ownership
    managed/external, ids, run and evidence directories, exit code and failure diagnostics) and, when connected,
    the MAME version, machine, CPU tags with address spaces and installed breakpoints, an `execution` block read fresh
    at call time, ROM identity and bridge capabilities/limits. In `execution`, `state` (running | paused |
    debugger_stopped) is the authoritative combined classification: a debugger stop takes precedence over a pause.
    `debugger_state` is the debugger's run/stop state and `paused` is MAME's raw pause flag, which can vary while
    `debugger_state` is `stop`, so do not rely on it alone. A `running` state does not by itself prove frames or gameplay
    are advancing: `clock` (emulated time and frame) read across calls shows that. Works with no MAME running."""
    return await anyio.to_thread.run_sync(lambda: _session_info_blocking(session_id))


@mcp.tool()
async def read_memory(cpu: str, address: int | str, length: int, space: str = "program",
                      mode: Literal["inspect", "direct"] = "inspect", bank_context: bool = False,
                      session_id: str | None = None) -> dict[str, Any]:
    """Read `length` (1..4096) bytes from `cpu`'s address space (program, data, io, opcodes) at `address`.

    `cpu` is a full device tag (':maincpu'). Addresses are the space's native units (bytes for the
    spaces seen so far; spaces with a non-zero address shift are rejected). Result: lowercase hex, 2 chars
    per byte, ascending addresses. The result states the read mode and its limits.
    mode='inspect' (default): MAME's side-effects-disabled debugger read; handlers that honour that flag
    skip their effects (others may not). mode='direct': program space only, bypasses every device handler;
    bytes with no direct pointer (I/O, unmapped) read as 0x00 and are indistinguishable from zeros.
    The address is a CPU-visible logical address: on banked machines the same address returns different bytes
    depending on the bank selected at that moment. bank_context=true adds the currently selected entry of every
    memory bank, sampled in the same plugin call as the bytes (atomic for this read). It does NOT say which bank
    backs the requested range or which ROM region offset an entry maps to."""
    args = _validated(protocol.read_memory_args, cpu, space, address, length, mode, bank_context)
    return await anyio.to_thread.run_sync(lambda: _call("read_memory", session_id, args))


@mcp.tool()
async def read_registers(cpu: str, registers: list[str] | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Read CPU state registers by exact name (e.g. CURPC, D0, SR, SP for the 68000). Omit `registers`
    for all (rejected if more than 64). Values are unsigned: `hex` ('0x...') and `dec` (decimal string)."""
    args = _validated(protocol.read_registers_args, cpu, registers)
    return await anyio.to_thread.run_sync(lambda: _call("read_registers", session_id, args))


@mcp.tool()
async def capture_breakpoint(
    cpu: str,
    max_events: int,
    max_wall_seconds: float,
    address: int | str | None = None,
    registers: list[str] | None = None,
    memory: list[dict[str, int]] | None = None,
    label: str | None = None,
    locations: list[dict[str, int | str]] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Start an asynchronous capture of up to `max_events` (1..10000) breakpoint hits on `cpu`'s program space,
    for at most `max_wall_seconds` host seconds (<=600). Both limits are required and apply to the whole experiment.
    Give EITHER `address` (one location) OR `locations`: 1..8 objects {"address": n, "id": "name"} on this one CPU
    (id optional, default L1, L2, ...; ids and addresses must be unique). All locations are installed together
    before the call returns; if any fails, everything installed by the attempt is removed and the error reports
    each location's outcome. One global event limit and one global event sequence span all locations.
    Each hit records the chosen `registers` (at most 8; names as in read_registers) and `memory` fields
    ({"space": "program"|"data"|"io", "address": n, "size": 1|2|4}, read from the named `cpu`) inside MAME, then
    execution resumes after the hit. Each event names its `location`, the logical breakpoint `address` and the hit-time `pc`
    (captured at the hit for multi-location experiments). The call itself never changes execution state: if MAME is
    paused or debugger-stopped nothing is recorded until it is resumed. Only one experiment may be active; refuses
    an address that already has a breakpoint. Borrows shared debugger temporaries (temp0..temp9; one extra for
    multi-location) and reports which. Event time fields are harvest-time, not hit-time; there is no per-event bank id.
    If an earlier experiment could not remove its breakpoints, the call fails with `unresolved_instrumentation`
    (listing them and the recovery steps) until they are gone; failure to arm every location fails with `install_failed`.
    Returns an experiment_id immediately; poll experiment_status (per-location counts included)."""
    args = _validated(protocol.capture_args, cpu, address, max_events, max_wall_seconds, registers, memory, label, locations)
    return await anyio.to_thread.run_sync(lambda: _call("capture_breakpoint", session_id, args))


@mcp.tool()
async def capture_watchpoint(
    cpu: str,
    space: Literal["program", "data", "io"],
    address: int | str,
    length: int,
    access: Literal["read", "write", "read_write"],
    max_events: int,
    max_wall_seconds: float,
    registers: list[str] | None = None,
    label: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Start an asynchronous, bounded capture of memory accesses to ONE explicit range of `cpu`'s address `space`.
    Everything is required: `cpu` (device tag), `space` (program, data or io; the space must have byte addresses,
    i.e. address shift 0), `address` and `length` (the watched range is [address, address+length-1], LOGICAL CPU-visible
    byte addresses), `access` (read, write or read_write), `max_events` (1..10000) and `max_wall_seconds` (<=600).
    A watchpoint matches the address the CPU issues: a mirror of the same storage is a different address and needs its own
    range, and no bank is ever inferred. Each event records the access type (a literal baked into the read or write
    watchpoint that fired; read_write installs both), the byte-precise access `address` (not the range start), the transferred
    `data` (value read or written, shifted to the low bits; MAME does not report the access width) and the hit-time
    instruction `pc` (CURPC: the executing instruction, not the prefetch-adjusted PC), plus up to 6 chosen `registers` read
    at the hit. There is NO bank id on an event: the logical address alone does not say which bank was selected, and the
    result never attributes an event to a bank. Events are ordered by exact sequence numbers; their time fields are
    harvest-time. The call never changes execution state (paused or debugger-stopped: armed, records nothing until
    execution_control resume); after each recorded hit execution continues. Same ownership, limits, dropped-event reporting,
    verified cleanup and `unresolved_instrumentation` / `install_failed` behaviour as capture_breakpoint; only one experiment
    may be active. Not a debugger interface: no expressions, conditions or commands can be supplied.
    Returns an experiment_id immediately; poll experiment_status; events are in the JSONL evidence file."""
    args = _validated(protocol.watchpoint_args, cpu, space, address, length, access, max_events, max_wall_seconds, registers, label)
    return await anyio.to_thread.run_sync(lambda: _call("capture_watchpoint", session_id, args))


@mcp.tool()
async def disassemble_memory(cpu: str, address: int | str, count: int, session_id: str | None = None) -> dict[str, Any]:
    """Disassemble `count` (1..64) instructions of `cpu` starting at the logical program-space byte `address`, through the
    view MAME itself decodes: the CPU's `opcodes` space when the machine defines one (decrypted or otherwise separate
    instruction fetches), otherwise the program space; the view used is reported and cannot be selected. Requires paused
    or debugger-stopped execution (execution_control pause) so the view is stable. Returns, per instruction, its address,
    MAME's decoded text, its length in bytes and its bytes (lowercase hex, ascending address order) read from that view; a
    program-view byte string is added only where it differs from the instruction view. Lengths come from consecutive
    instruction addresses, so variable-length instructions are handled. `mapping` reports the declared address-map handlers
    covering the range and, for bank handlers, the bank and the entry selected right now - the disassembly shows that
    entry only. Limits: decoding starts at the given address (no flow following, no CPU mode inference), addresses are
    logical (not ROM offsets), and a CPU without a debug interface, a program space with an address shift, or an address
    beyond the space is refused with a typed error. Read-only: it does not change emulation or machine state."""
    args = _validated(protocol.disassemble_args, cpu, address, count)
    return await anyio.to_thread.run_sync(lambda: _call("disassemble", session_id, args))


@mcp.tool()
async def experiment_status(experiment_id: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Progress, completion reason, counts (captured / hits observed / dropped), errors, cleanup outcome,
    a compact summary, the last few events and the evidence file paths. Defaults to the latest experiment OF THE SELECTED
    SESSION. An experiment id belongs to the session it was started on: with another session_id it is refused
    (`experiment_from_other_session`) and nothing is sent."""
    return await anyio.to_thread.run_sync(lambda: _call("experiment_status", session_id, experiment_id, experiment_id=experiment_id))


@mcp.tool()
async def cancel_experiment(experiment_id: str, session_id: str | None = None) -> dict[str, Any]:
    """Cancel an experiment: removes only the breakpoint(s) this experiment installed, verifies removal,
    finalises the evidence files. Idempotent for finished experiments. The experiment must belong to the selected session;
    an id of another session is refused and nothing is sent."""
    return await anyio.to_thread.run_sync(lambda: _call("cancel_experiment", session_id, experiment_id, experiment_id=experiment_id))


@mcp.tool()
async def execution_control(action: Literal["pause", "resume"], session_id: str | None = None) -> dict[str, Any]:
    """Pause the emulation, or resume it (also leaves a debugger stop). The reply only acknowledges the request: its
    `before` and `after_request` are read inside the request and are not a confirmed result. Confirm the resulting
    `execution.state` with a fresh session_info; a `running` state does not by itself prove frames or gameplay are
    advancing. Included only because experiments need a way to get MAME
    running; it does not single-step or run debugger commands."""
    return await anyio.to_thread.run_sync(lambda: _call("execution_control", session_id, action))


@mcp.tool()
async def list_inputs(include_unsupported: bool = False, session_id: str | None = None) -> dict[str, Any]:
    """List the machine's input ports and fields as MAME exposes them: port tag, field name, class, player, mask, and the
    port's current value. Only digital controller/misc fields (joystick directions, buttons, coin, start, service) can be
    pressed; `supported` and `unsupported_reason` say which. Analog, toggle, DIP-switch and configuration fields are
    unsupported and listed only with include_unsupported=true. Use the exact `port` and `field` strings with press_inputs.
    Read-only."""
    args = _validated(protocol.list_inputs_args, include_unsupported)
    return await anyio.to_thread.run_sync(lambda: _call("list_inputs", session_id, args))


@mcp.tool()
async def press_inputs(inputs: list[dict[str, str]], frames: int, wait: bool = True, max_wait_s: float | None = None,
                       session_id: str | None = None) -> dict[str, Any]:
    """Hold 1..8 distinct digital inputs ({"port": ":P1", "field": "P1 Button 1"}, names from list_inputs) for `frames`
    (1..600) EMULATED frames, then release them. Frames are counted from the emulator's frame notifier, so they elapse
    only while emulation RUNS: a press made while paused or debugger-stopped is armed and held, its frame count starts when
    emulation resumes, and no other state-changing operation is accepted meanwhile. The machine sees the input from the
    next frame for exactly `frames` frames. With wait=true (default) the call returns once the press has been released (or
    execution stopped, or max_wait_s passed); with wait=false it returns as soon as the press is armed - poll
    session_info.input. Released on completion, release_inputs, stop_session, server shutdown, a bridge error during the
    wait, and when the bridge client disconnects. Analog, toggle, DIP-switch and configuration fields are refused."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.press_inputs(inputs, frames, wait, max_wait_s, session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def press_sequence(steps: list[dict[str, str | int]], wait: bool = True, max_wait_s: float | None = None,
                         session_id: str | None = None) -> dict[str, Any]:
    """Run a frame-based sequence of digital input presses. `steps` is 1..64 objects {"port", "field", "start", "frames"}
    (port/field from list_inputs): hold that field for `frames` (1..600) EMULATED frames beginning `start` frames after the
    sequence origin. Origin: offset 0 is the first emulated frame in which the machine reads the offset-0 press(es), the same
    anchoring as press_inputs, so the earliest step must start at 0. Different fields may overlap (chords); two steps of one
    field need at least one idle frame between them. At one frame boundary every release is applied before every press (both
    happen between frames, so the machine sees no order). Limits: total length <= 3600 frames, <= 64 steps (at most 2
    transitions each), <= 16 distinct fields, <= 8 held at once; the WHOLE sequence is validated before any input is pressed.
    Like press_inputs it never advances emulation: armed while paused or debugger-stopped, only the offset-0 inputs are held and
    nothing elapses until execution_control resume; no other state-changing operation is accepted while it runs. With
    wait=true (default) it returns when the sequence has finished, execution stopped or max_wait_s passed; wait=false returns once
    armed. Released on completion, release_inputs, error, stop_session, server shutdown and client disconnect. The result reports
    per-step OBSERVED timing (first/last observed frame offset, frames observed pressed) and every deviation from the schedule; the
    observations are the bridge's own per-frame port reads, so this is not independent proof of frame-exact timing."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.press_sequence(steps, wait, max_wait_s, session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def release_inputs(session_id: str | None = None) -> dict[str, Any]:
    """Release any held inputs of the selected session now (idempotent). Reports whether anything was held and why/how it
    ended. Other sessions' held inputs are untouched."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.release_inputs(session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def save_checkpoint(label: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    """Save the emulated machine state to a session-owned file and return a checkpoint id. Requires paused or
    debugger-stopped execution (execution_control pause), no active capture, no unresolved instrumentation and no held
    input; a machine MAME reports as not supporting save states is refused. Returns only after the file exists and has been
    validated (size, SHA-256, MAME header); the execution state is unchanged (see `mode`: 'immediate' is exact; if MAME
    could not save at that instant it runs briefly and the result says so). Files and metadata stay in the session's run
    directory, so ids remain loadable by later sessions of the same machine and MAME version."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.save_checkpoint(label, session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def load_checkpoint(checkpoint_id: str, session_id: str | None = None) -> dict[str, Any]:
    """Restore a checkpoint into the session named by `session_id` (or the sole live session; with several live sessions an
    omitted id is refused). The checkpoint id never selects the target: it may come from this session, another live session or
    an earlier one, but only if its machine and MAME version match the target and its file validates. Same preconditions as
    save_checkpoint. The id, machine, MAME version and file SHA-256 are validated first; the load is reported complete only
    after the frame counter and every CPU's registers match what was recorded at save time (`verification`). Experiment ids
    recorded before the load become stale and are refused (their evidence files stay on disk); the bridge session id is
    re-checked. Execution stays in the state it was in."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.load_checkpoint(checkpoint_id, session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def list_screens(session_id: str | None = None) -> dict[str, Any]:
    """List the machine's screens (device tag such as ':screen', type, native visible size before rotation, orientation in
    degrees, refresh rate, current frame counter and the frame counter at which MAME last finished rendering it) plus the
    execution state and video settings. Use a tag with capture_screenshot(screen=...). Read-only; needs a running session."""
    return await anyio.to_thread.run_sync(lambda: _call("screen_list", session_id))


@mcp.tool()
async def capture_screenshot(screen: str | None = None, session_id: str | None = None) -> CallToolResult:
    """Capture the emulated game screen as a PNG and return it as image content together with its evidence path and metadata.
    `screen` is a screen tag from list_screens; it may be omitted when the machine has exactly one screen. Managed
    sessions only. The PNG is written by MAME itself to <session evidence dir>/screenshots/shot-NNNN-<screen>-f<frame>.png
    (unique, never overwritten; a .json sidecar holds the same metadata). The image is the screen only: rotated to the
    displayed orientation (a vertical game is a portrait image), one pixel per emulated pixel, no bezel, artwork, debugger
    or desktop. Capture works while running, paused or debugger-stopped and never pauses, resumes or advances emulation.
    Timing: `frame.image_frame` is the frame counter when MAME last finished rendering the screen and `frame.current_frame` the
    counter now; `frame.freshness` is 'fresh' (image is the current frame), 'behind' (emulation is past the last rendered
    frame, as can happen while running), 'not_rendered' (no frame rendered yet), 'stale_after_checkpoint_load' or
    'rendered_after_load_not_advanced' (a checkpoint load happened and no emulated frame has run since: colours/layers may not
    match what the game shows once running - resume briefly, pause, and capture again). Repeated captures while paused
    return the same frame and an identical image. Frame 0 means nothing has been emulated yet, so a black image is expected.
    A screenshot shows what the screen displayed; it does not by itself establish what the game is doing. Refused while a
    checkpoint save/load is pending; serialised with checkpoint, input and RAM-write operations and with stop_session."""
    try:
        meta, data = await anyio.to_thread.run_sync(lambda: manager.capture_screenshot(screen, session_id))
    except BridgeError as e:
        raise _fail(e) from e
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(meta, indent=2)),
                 ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mime_type="image/png")],
        structured_content=meta)


@mcp.tool()
async def list_checkpoints(include_previous_sessions: bool = True, session_id: str | None = None) -> dict[str, Any]:
    """List checkpoints of every session this server holds (live, or ended and not yet acknowledged; `live_run_ids` names only
    the live ones) and, by default, of earlier sessions under the runs directory (metadata
    only; works with no MAME running and needs no session). With `session_id`, each entry also reports whether it is
    `compatible` with that session (the machine and MAME version load_checkpoint requires)."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.list_checkpoints(include_previous_sessions, session_id))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def write_memory(cpu: str, space: str, address: int | str, value: int | str, width: Literal[1, 2, 4],
                       session_id: str | None = None) -> dict[str, Any]:
    """Write ONE value to plain RAM: `cpu` (device tag), `space` ('program' or 'data'), `address` (aligned to `width`),
    `value` (fits in `width` bytes; the width-bit integer in the space's native endianness) and `width` in BYTES (1, 2 or 4)
    - all required. Requires paused or debugger-stopped execution, no active capture, no unresolved instrumentation, no
    held input and no pending checkpoint. The target must be RAM in the CPU's address map: ROM, unmapped, I/O, banked and
    device-handled addresses are refused. The value is read back; the result reports before, requested and actual values,
    and a mismatch is rolled back and reported as an error. Not a debugger or Lua interface."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.write_memory(cpu, space, address, value, width, session_id))
    except BridgeError as e:
        raise _fail(e) from e


def _install_exit_handlers() -> None:
    """Stop the owned MAME on normal shutdown (stdin closed, Ctrl+C, SIGTERM/SIGBREAK, interpreter exit).
    A forced kill of this process (TerminateProcess, Task Manager) runs none of this and can leave MAME running."""
    atexit.register(manager.shutdown)

    def _bye(signum, frame):  # noqa: ARG001
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _bye)
            except (ValueError, OSError):
                pass


def main() -> None:
    _log(f"starting (managed sessions; external attach {'port %d' % PORT if PORT else 'off'}; sdk stdio)")
    _install_exit_handlers()
    try:
        mcp.run("stdio")
    finally:
        rep = manager.shutdown()
        if rep:
            for run_id, r in rep["sessions"].items():
                cap, proc = r.get("capture_cleanup") or {}, r.get("process") or {}
                _log(f"shutdown cleanup: session={run_id} capture_verified={cap.get('verified')} process_terminated={proc.get('terminated')} "
                     f"exit_code={proc.get('exit_code')} failures={r.get('cleanup_failures')}")


if __name__ == "__main__":
    main()
