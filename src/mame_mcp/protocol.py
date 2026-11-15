"""Protocol constants, error type and client-side argument validation.

Limits mirror plugin/mamemcp/init.lua; the plugin re-validates everything.
"""
from __future__ import annotations

import re
from typing import Any

PROTOCOL_VERSION = 1
MAX_READ_BYTES = 4096
MAX_REGISTERS = 64
MAX_CAPTURE_REGISTERS = 8  # each captured register borrows one shared debugger temporary
CAPTURE_SPACES = ("program", "data", "io")
READ_MODES = ("inspect", "direct")
MAX_MEMORY_FIELDS = 8
MAX_EVENTS = 10000
MAX_LOCATIONS = 8  # breakpoint locations per experiment; each is one MAME breakpoint on one CPU (temps are shared, not per location)
MAX_WALL_SECONDS = 600
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOC_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,23}$")
_TAG = re.compile(r"^:[A-Za-z0-9_:.\-]+$")


class BridgeError(Exception):
    """Error from the bridge or the transport.

    `uncertain` is True when a state-changing request may or may not have been applied
    (timeout / disconnect after send). The caller must inspect state rather than retry.
    """

    def __init__(self, code: str, message: str, *, detail: Any = None, uncertain: bool = False):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail
        self.uncertain = uncertain

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message, "uncertain_state": self.uncertain}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


def _bad(msg: str) -> BridgeError:
    return BridgeError("bad_argument", msg)


def check_tag(cpu: str) -> str:
    if not isinstance(cpu, str) or not _TAG.match(cpu):
        raise _bad("cpu must be a full device tag such as ':maincpu'")
    return cpu


def check_uint(value: Any, what: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise _bad(f"{what} must be an integer")
    if isinstance(value, str):
        try:
            value = int(value, 16) if value.lower().startswith("0x") else int(value, 10)
        except ValueError as e:
            raise _bad(f"{what} is not a valid integer: {value!r}") from e
    if not isinstance(value, int):
        raise _bad(f"{what} must be an integer or a '0x' hex string")
    if value < minimum or (maximum is not None and value > maximum):
        raise _bad(f"{what} out of range ({minimum}..{maximum})")
    return value


def read_memory_args(cpu: str, space: str, address: Any, length: Any, mode: str = "inspect",
                     bank_context: bool = False) -> dict[str, Any]:
    if not isinstance(space, str) or not _IDENT.match(space):
        raise _bad("space must be an address space name such as 'program'")
    if mode not in READ_MODES:
        raise _bad(f"mode must be one of {READ_MODES}")
    if not isinstance(bank_context, bool):
        raise _bad("bank_context must be true or false")
    out = {
        "cpu": check_tag(cpu),
        "space": space,
        "mode": mode,
        "address": check_uint(address, "address", maximum=0xFFFFFFFF),
        "length": check_uint(length, "length", minimum=1, maximum=MAX_READ_BYTES),
    }
    if bank_context:
        out["bank_context"] = True
    return out


def read_registers_args(cpu: str, registers: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"cpu": check_tag(cpu)}
    if registers is not None:
        if len(registers) > MAX_REGISTERS or not all(isinstance(r, str) and _IDENT.match(r) for r in registers):
            raise _bad(f"registers must be up to {MAX_REGISTERS} plain register names")
        out["registers"] = list(registers)
    return out


def capture_args(
    cpu: str,
    address: Any,
    max_events: Any,
    max_wall_seconds: Any,
    registers: list[str] | None = None,
    memory: list[dict[str, Any]] | None = None,
    label: str | None = None,
    locations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a capture request. Give exactly one of `address` (single location, legacy form) or `locations`
    (1..MAX_LOCATIONS dicts {"address": n, "id": optional name}); ids default to L1, L2, ... in order."""
    if isinstance(max_wall_seconds, bool) or not isinstance(max_wall_seconds, (int, float)):
        raise _bad("max_wall_seconds must be a number")
    if not 0 < max_wall_seconds <= MAX_WALL_SECONDS:
        raise _bad(f"max_wall_seconds must be in (0, {MAX_WALL_SECONDS}]")
    regs = list(registers or [])
    if len(regs) > MAX_CAPTURE_REGISTERS or not all(isinstance(r, str) and _IDENT.match(r) for r in regs):
        raise _bad(f"registers must be up to {MAX_CAPTURE_REGISTERS} plain register names")
    mems = []
    for m in memory or []:
        if not isinstance(m, dict) or m.get("size") not in (1, 2, 4):
            raise _bad("each memory field needs address and size (1, 2 or 4)")
        space = m.get("space", "program")
        if space not in CAPTURE_SPACES:
            raise _bad(f"memory.space must be one of {CAPTURE_SPACES}")
        mems.append({"space": space, "address": check_uint(m.get("address"), "memory.address", maximum=0xFFFFFFFF), "size": m["size"]})
    if len(mems) > MAX_MEMORY_FIELDS:
        raise _bad(f"at most {MAX_MEMORY_FIELDS} memory fields")
    if (address is None) == (locations is None):
        raise _bad("give exactly one of 'address' (single location) or 'locations' (several)")
    out: dict[str, Any] = {
        "cpu": check_tag(cpu),
        "max_events": check_uint(max_events, "max_events", minimum=1, maximum=MAX_EVENTS),
        "max_wall_seconds": max_wall_seconds,
        "registers": regs,
        "memory": mems,
    }
    if locations is None:
        out["address"] = check_uint(address, "address", maximum=0xFFFFFFFF)
    else:
        out["locations"] = check_locations(locations)
    if label:
        out["label"] = str(label)[:80]
    return out


def check_locations(locations: Any) -> list[dict[str, Any]]:
    """Reject empty, oversized, malformed or duplicate (same id or same address) location lists before anything is installed."""
    if not isinstance(locations, list) or not 1 <= len(locations) <= MAX_LOCATIONS:
        raise _bad(f"locations must be a list of 1..{MAX_LOCATIONS} entries")
    out, ids, addrs = [], set(), set()
    for i, loc in enumerate(locations, 1):
        if not isinstance(loc, dict) or set(loc) - {"address", "id"}:
            raise _bad("each location is an object with 'address' and an optional 'id'")
        addr = check_uint(loc.get("address"), f"locations[{i - 1}].address", maximum=0xFFFFFFFF)
        lid = loc.get("id", f"L{i}")
        if not isinstance(lid, str) or not _LOC_ID.match(lid):
            raise _bad("location id must be 1-24 characters: a letter, then letters, digits or underscore")
        if lid in ids:
            raise _bad(f"duplicate location id {lid!r}")
        if addr in addrs:
            raise _bad(f"duplicate location address {addr:#x} (MAME evaluates one breakpoint per address)")
        ids.add(lid)
        addrs.add(addr)
        out.append({"id": lid, "address": addr})
    return out
