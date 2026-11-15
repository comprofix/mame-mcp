"""MCP server (stdio) exposing the proven MAME bridge plus a managed MAME lifecycle.

Protocol output stays on stdout (owned by the MCP SDK); diagnostics go to stderr.

Startup never needs MAME: the server initialises with no emulator running. `start_session` launches an isolated,
server-owned MAME on demand and `stop_session` stops it (see session.py). Configuration (environment, all optional):
  MAMEMCP_MAME_DIR     folder containing mame.exe (default D:\\arcade\\mame, or MAME_DIR)
  MAMEMCP_ROMPATH      ROM search directories, ';'-separated; the first holding <system>.zip/.7z/folder is used
  MAMEMCP_RUNS_DIR     parent of the per-session run directories (default <repo>\\runs\\sessions)

Optional EXTERNAL attachment (compatibility; not owned by this server, never started, stopped or killed by it):
  MAMEMCP_PORT         bridge port of an already-running MAME with the mamemcp plugin. Without it nothing is
                       attached implicitly and bridge tools report `no_session` until start_session.
  MAMEMCP_MAME_PID     optional PID of that MAME to nudge (WM_NULL) while a request waits; Windows only, opt-in
                       (see docs/VALIDATION.md). Only a process whose image is named mame*.exe is ever nudged.
  MAMEMCP_LAUNCH_INFO  optional launch.json of that MAME (adds ROM hashes / -verifyroms to session_info)
A managed session takes precedence while it exists; after stop_session external attachment resumes.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, protocol, winwake
from .bridge import RETRY_SAFE_OPS, BridgeClient
from .protocol import BridgeError
from .session import SessionConfig, SessionManager

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
manager = SessionManager(SessionConfig.from_env())

mcp = MCPServer(
    "mame-mcp",
    version=__version__,
    instructions=(
        "Bounded, typed debugging access to a local MAME. The server starts with no emulator: call start_session "
        "(e.g. system='shinobi') to launch an isolated MAME with the debugger, inspect/capture with the other tools, and "
        "call stop_session when done (it finalises an active capture and stops only the MAME this server started). "
        "MAME starts stopped in the debugger: resume it with execution_control. session_info works at any time and "
        "reports no_session/starting/ready/stopping/exited/failed. Reads are inspection-only. "
        "capture_breakpoint starts an asynchronous, limit-bounded experiment inside MAME: poll "
        "experiment_status. Detailed events are written to JSONL evidence files, not returned here. "
        "Experiment ids belong to one session; ids from an earlier session are refused. "
        "CPU addresses are not ROM file offsets: banked memory means one CPU address can map to different code."
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


def _call(name: str, *a):
    """Route one bridge call to the managed session (or the optional external attachment). Lifecycle changes are
    serialised against it: see SessionManager.use_client."""
    try:
        with manager.use_client(_external) as (client, managed):
            try:
                return _invoke(client, name, *a)
            except BridgeError as e:
                if managed and e.code in ("mame_disconnected", "connect_failed", "request_timeout"):
                    snap = manager.snapshot(settle=2.0 if e.code != "request_timeout" else 0.0)  # also notices an exited process
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


def _session_info_blocking() -> dict[str, Any]:
    session = manager.snapshot()
    out: dict[str, Any] = {}
    if session["state"] in ("ready", "no_session"):
        try:
            with manager.use_client(_external) as (client, managed):
                try:
                    out = _invoke(client, "session_info")
                except BridgeError as e:
                    if managed:  # connection trouble or exit: report it next to the (refreshed) state
                        session = {**manager.snapshot(settle=2.0 if e.code != "request_timeout" else 0.0), "bridge_error": e.to_dict()}
                    elif e.code not in ("connect_failed", "mame_disconnected"):
                        raise _fail(e) from e  # e.g. session_changed: the agent must see it, exactly as before
                    else:  # external attachment configured but nothing is listening: "no session", not a failure
                        session["external_attach"] = {"configured": True, "port": PORT, "available": False, "error": e.to_dict()}
                else:
                    out["bridge_client"] = {
                        "timeouts": client.timeouts, "read_only_retries": client.read_retries,
                        "stale_replies_discarded": client.stale_discarded,
                        "wake_configured": client.wake is not None, "wake_posts": client.wake_posts,
                        "wake_last_refusal": getattr(client.wake, "last_refusal", None),
                        "default_timeout_s": client.default_timeout, "retry_safe_ops": sorted(RETRY_SAFE_OPS)}
                    rom = _rom_info(manager.launch_info()) if managed else _external_rom_info()
                    if rom and "rom_identity" in out:
                        out["rom_identity"]["launch_info"] = rom
                    if not managed:
                        session = {"state": "ready", "ownership": "external", "port": PORT, "config": session["config"],
                                   **({"last_session": session["last_session"]} if "last_session" in session else {})}
        except BridgeError:
            pass  # no session configured, or the state changed under us: the session block explains it
    out["session"] = session
    return out


@mcp.tool()
async def start_session(system: str, ready_timeout_s: float = 60.0) -> dict[str, Any]:
    """Launch an isolated MAME for `system` (a MAME short name such as 'shinobi') with the mamemcp plugin and the
    debugger enabled, wait (bounded by `ready_timeout_s`, 5..120) until the bridge answers, and return the session id,
    machine identity, execution state and the run/evidence directories. MAME starts stopped in the debugger; use
    execution_control(action='resume') to run it. Refused while a managed session is active. The executable, ROM
    paths and MAME arguments are fixed by server configuration and cannot be set here. On failure the diagnostics
    are in the error and only the process this attempt launched is stopped."""
    try:
        return await anyio.to_thread.run_sync(lambda: manager.start(system, ready_timeout_s))
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def stop_session() -> dict[str, Any]:
    """Stop the managed MAME session: cancel any active capture (breakpoints removed and verified, evidence
    finalised; execution state untouched), then stop only the MAME process this server launched after verifying its
    identity. Reports capture cleanup, process exit code and any cleanup failures. Never touches a MAME this server
    did not start. With no managed session it is a no-op."""
    try:
        return await anyio.to_thread.run_sync(manager.stop)
    except BridgeError as e:
        raise _fail(e) from e


@mcp.tool()
async def session_info() -> dict[str, Any]:
    """Session state (`session.state`: no_session | starting | ready | stopping | exited | failed, with ownership
    managed/external, ids, run and evidence directories, exit code and failure diagnostics) and, when connected,
    the MAME version, machine, CPU tags with address spaces and installed breakpoints, execution state
    (running | paused | debugger_stopped), ROM identity and bridge capabilities/limits. Works with no MAME running."""
    return await anyio.to_thread.run_sync(_session_info_blocking)


@mcp.tool()
async def read_memory(cpu: str, address: int | str, length: int, space: str = "program",
                      mode: Literal["inspect", "direct"] = "inspect", bank_context: bool = False) -> dict[str, Any]:
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
    return await anyio.to_thread.run_sync(lambda: _call("read_memory", args))


@mcp.tool()
async def read_registers(cpu: str, registers: list[str] | None = None) -> dict[str, Any]:
    """Read CPU state registers by exact name (e.g. CURPC, D0, SR, SP for the 68000). Omit `registers`
    for all (rejected if more than 64). Values are unsigned: `hex` ('0x...') and `dec` (decimal string)."""
    args = _validated(protocol.read_registers_args, cpu, registers)
    return await anyio.to_thread.run_sync(lambda: _call("read_registers", args))


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
    return await anyio.to_thread.run_sync(lambda: _call("capture_breakpoint", args))


@mcp.tool()
async def experiment_status(experiment_id: str | None = None) -> dict[str, Any]:
    """Progress, completion reason, counts (captured / hits observed / dropped), errors, cleanup outcome,
    a compact summary, the last few events and the evidence file paths. Defaults to the latest experiment."""
    return await anyio.to_thread.run_sync(lambda: _call("experiment_status", experiment_id))


@mcp.tool()
async def cancel_experiment(experiment_id: str) -> dict[str, Any]:
    """Cancel an experiment: removes only the breakpoint(s) this experiment installed, verifies removal,
    finalises the evidence files. Idempotent for finished experiments."""
    return await anyio.to_thread.run_sync(lambda: _call("cancel_experiment", experiment_id))


@mcp.tool()
async def execution_control(action: Literal["pause", "resume"]) -> dict[str, Any]:
    """Pause the emulation, or resume it (also leaves a debugger stop). The effect is requested, not
    confirmed: re-check with session_info. Included only because experiments need a way to get MAME
    running; it does not single-step or run debugger commands."""
    return await anyio.to_thread.run_sync(lambda: _call("execution_control", action))


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
            cap, proc = rep.get("capture_cleanup") or {}, rep.get("process") or {}
            _log(f"shutdown cleanup: capture_verified={cap.get('verified')} process_terminated={proc.get('terminated')} "
                 f"exit_code={proc.get('exit_code')} failures={rep.get('cleanup_failures')}")


if __name__ == "__main__":
    main()
