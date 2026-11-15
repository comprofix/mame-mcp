"""MCP server (stdio) exposing the proven MAME bridge.

Protocol output stays on stdout (owned by the MCP SDK); diagnostics go to stderr.

Environment:
  MAMEMCP_PORT         bridge port of the running MAME (default 9411)
  MAMEMCP_MAME_PID     optional PID of the MAME to nudge (WM_NULL) while a request waits; Windows only, opt-in.
                       Works around MAME's debugger-stopped GetMessage wait (see docs/VALIDATION.md). Only a
                       process whose image is named mame*.exe is ever nudged.
  MAMEMCP_LAUNCH_INFO  optional path to the launch.json written by `python -m mame_mcp.launcher`
                       (adds the expected ROM hashes / -verifyroms result to session_info)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, protocol, winwake
from .bridge import RETRY_SAFE_OPS, BridgeClient
from .protocol import BridgeError

PORT = int(os.environ.get("MAMEMCP_PORT", "9411"))
LAUNCH_INFO = os.environ.get("MAMEMCP_LAUNCH_INFO")



def _wake_pid() -> int | tuple[int, int | None] | None:
    """MAME to nudge while it is debugger-stopped (Windows only, opt-in): MAMEMCP_MAME_PID, or the pid (and
    creation time) the launcher recorded in launch.json (re-read so a restarted MAME launched into the same run
    dir is followed). winwake re-verifies image name, creation time (when recorded) and ownership of the bridge
    port before every nudge, so a stale PID is refused rather than followed to another process."""
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


_client = BridgeClient(port=PORT, wake=winwake.waker_for(_wake_pid, port=PORT))

mcp = MCPServer(
    "mame-mcp",
    version=__version__,
    instructions=(
        "Bounded, typed access to one already-running local MAME instance that has the mamemcp plugin "
        "loaded (start it with `python -m mame_mcp.launcher`). Reads are inspection-only. "
        "capture_breakpoint starts an asynchronous, limit-bounded experiment inside MAME: poll "
        "experiment_status. Detailed events are written to JSONL evidence files, not returned here. "
        "CPU addresses are not ROM file offsets: banked memory means one CPU address can map to different code."
    ),
)


def _log(msg: str) -> None:
    print(f"[mame-mcp] {msg}", file=sys.stderr, flush=True)


def _fail(e: BridgeError) -> ToolError:
    _log(f"error: {e}")
    return ToolError(json.dumps(e.to_dict()))


def _call(fn, *a, **kw):
    """Run a blocking bridge call, (re)connecting first when needed. State-changing calls and memory reads are never retried; see bridge.RETRY_SAFE_OPS."""
    try:
        if not _client.connected:
            info = _client.connect(timeout=10)
            if "session_changed_from" in info:
                # A different MAME process answered. Experiments/state of the old session are gone;
                # do not silently carry on with whatever call the agent made for the old session.
                raise BridgeError(
                    "session_changed",
                    f"MAME restarted: session {info['session_changed_from']} -> {_client.session_id}. "
                    "Re-check session_info and re-plan; nothing was sent for this call.",
                )
        t0, r0 = _client.timeouts, _client.read_retries
        n0 = len(_client.transport_events)
        out = fn(*a, **kw)
        if isinstance(out, dict) and (_client.timeouts != t0 or _client.read_retries != r0):
            # provenance: this answer was obtained after a request_timeout (and a read-only retry)
            out["transport"] = {
                "timeouts_during_call": _client.timeouts - t0,
                "read_only_retries_during_call": _client.read_retries - r0,
                "wake_posts_total": _client.wake_posts,
                "events": _client.transport_events[n0:],
                "note": "late replies to timed-out attempts are matched by request id and discarded",
            }
        return out
    except BridgeError as e:
        raise _fail(e) from e


def _validated(fn, *a):
    """Client-side argument validation; surfaces the reason instead of a generic tool failure."""
    try:
        return fn(*a)
    except BridgeError as e:
        raise _fail(e) from e


def _launch_rom_info() -> dict[str, Any] | None:
    if not LAUNCH_INFO:
        return None
    try:
        info = json.loads(Path(LAUNCH_INFO).read_text())
        return {
            "source": "launcher launch.json (expected hashes from -listxml; -verifyroms result)",
            "romset_zip": info.get("romset_zip"),
            "verifyroms": info.get("verifyroms", {}).get("output"),
            "expected_roms": info.get("expected_roms_from_listxml"),
        }
    except (OSError, ValueError) as e:
        return {"unavailable": True, "reason": str(e)}


@mcp.tool()
async def session_info() -> dict[str, Any]:
    """MAME version, machine, CPU tags with address spaces and installed breakpoints, execution state
    (running | paused | debugger_stopped), ROM identity and bridge capabilities/limits."""
    info = await anyio.to_thread.run_sync(lambda: _call(_client.session_info))
    info["bridge_client"] = {"timeouts": _client.timeouts, "read_only_retries": _client.read_retries,
                             "stale_replies_discarded": _client.stale_discarded,
                             "wake_configured": _client.wake is not None and _wake_pid() is not None,
                             "wake_posts": _client.wake_posts,
                             "wake_last_refusal": getattr(_client.wake, "last_refusal", None),
                             "default_timeout_s": _client.default_timeout,
                             "retry_safe_ops": sorted(RETRY_SAFE_OPS)}
    rom = _launch_rom_info()
    if rom:
        info["rom_identity"]["launch_info"] = rom
    return info


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
    return await anyio.to_thread.run_sync(lambda: _call(_client.read_memory, args))


@mcp.tool()
async def read_registers(cpu: str, registers: list[str] | None = None) -> dict[str, Any]:
    """Read CPU state registers by exact name (e.g. CURPC, D0, SR, SP for the 68000). Omit `registers`
    for all (rejected if more than 64). Values are unsigned: `hex` ('0x...') and `dec` (decimal string)."""
    args = _validated(protocol.read_registers_args, cpu, registers)
    return await anyio.to_thread.run_sync(lambda: _call(_client.read_registers, args))


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
    return await anyio.to_thread.run_sync(lambda: _call(_client.capture_breakpoint, args))


@mcp.tool()
async def experiment_status(experiment_id: str | None = None) -> dict[str, Any]:
    """Progress, completion reason, counts (captured / hits observed / dropped), errors, cleanup outcome,
    a compact summary, the last few events and the evidence file paths. Defaults to the latest experiment."""
    return await anyio.to_thread.run_sync(lambda: _call(_client.experiment_status, experiment_id))


@mcp.tool()
async def cancel_experiment(experiment_id: str) -> dict[str, Any]:
    """Cancel an experiment: removes only the breakpoint(s) this experiment installed, verifies removal,
    finalises the evidence files. Idempotent for finished experiments."""
    return await anyio.to_thread.run_sync(lambda: _call(_client.cancel_experiment, experiment_id))


@mcp.tool()
async def execution_control(action: Literal["pause", "resume"]) -> dict[str, Any]:
    """Pause the emulation, or resume it (also leaves a debugger stop). The effect is requested, not
    confirmed: re-check with session_info. Included only because experiments need a way to get MAME
    running; it does not single-step or run debugger commands."""
    return await anyio.to_thread.run_sync(lambda: _call(_client.execution_control, action))


def main() -> None:
    _log(f"starting (bridge port {PORT}, sdk stdio)")
    mcp.run("stdio")


if __name__ == "__main__":
    main()
