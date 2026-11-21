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
MAX_INPUT_FIELDS = 8
MAX_INPUT_FRAMES = 600
MAX_SEQUENCE_STEPS = 64  # one step = one press of one field (a press and a release: at most 2 transitions per step)
MAX_SEQUENCE_FRAMES = 3600  # total length of a sequence in emulated frames
MAX_SEQUENCE_FIELDS = 16  # distinct fields used by one sequence (at most MAX_INPUT_FIELDS of them held at once)
WATCH_SPACES = ("program", "data", "io")  # the separate opcodes / cpu_space views are not watchable
WATCH_ACCESS = ("read", "write", "read_write")
MAX_WATCH_REGISTERS = 6  # a watchpoint also needs temporaries for the hit-time pc, access address and access data
MAX_DISASM_INSTRUCTIONS = 64
WRITE_WIDTHS = (1, 2, 4)  # bytes
MAX_LABEL = 80
# <run_id>-cp<NNN>: run_id is <system>-<YYYYMMDD>-<HHMMSS>-<6 hex> (session.py); the id alone names the owning session
CHECKPOINT_ID = re.compile(r"^(?P<run>[a-z0-9][a-z0-9_]{0,31}-\d{8}-\d{6}-[0-9a-f]{6})-cp(?P<n>\d{3,6})$")
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


# ------------------------------------------------------------------------------------------------ watchpoints
def watchpoint_args(
    cpu: str,
    space: str,
    address: Any,
    length: Any,
    access: str,
    max_events: Any,
    max_wall_seconds: Any,
    registers: list[str] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """One explicit memory-access capture: every field is required (no default CPU, space, range or access type).

    `address`/`length` are LOGICAL addresses in `space`, in the space's address units (bytes: address shift 0 is
    required, the plugin refuses anything else). The range is [address, address + length - 1]; a watchpoint matches the
    address the CPU issues, so a mirror of the same storage is a different address and needs its own range."""
    if not isinstance(space, str) or space not in WATCH_SPACES:
        raise _bad(f"space must be one of {WATCH_SPACES}")
    if not isinstance(access, str) or access not in WATCH_ACCESS:
        raise _bad(f"access must be one of {WATCH_ACCESS}")
    if isinstance(max_wall_seconds, bool) or not isinstance(max_wall_seconds, (int, float)):
        raise _bad("max_wall_seconds must be a number")
    if not 0 < max_wall_seconds <= MAX_WALL_SECONDS:
        raise _bad(f"max_wall_seconds must be in (0, {MAX_WALL_SECONDS}]")
    regs = list(registers or [])
    if len(regs) > MAX_WATCH_REGISTERS or not all(isinstance(r, str) and _IDENT.match(r) for r in regs):
        raise _bad(f"registers must be up to {MAX_WATCH_REGISTERS} plain register names")
    if len(set(regs)) != len(regs):
        raise _bad("registers must not repeat a name")
    start = check_uint(address, "address", maximum=0xFFFFFFFF)
    size = check_uint(length, "length", minimum=1, maximum=0x100000000)
    if start + size - 1 > 0xFFFFFFFF:
        raise _bad("address + length - 1 exceeds 0xFFFFFFFF")
    out: dict[str, Any] = {
        "cpu": check_tag(cpu), "space": space, "address": start, "length": size, "access": access,
        "max_events": check_uint(max_events, "max_events", minimum=1, maximum=MAX_EVENTS),
        "max_wall_seconds": max_wall_seconds, "registers": regs,
    }
    if label:
        out["label"] = str(label)[:MAX_LABEL]
    return out


# ------------------------------------------------------------------------------------------------ disassembly
def disassemble_args(cpu: str, address: Any, count: Any) -> dict[str, Any]:
    """CPU, start address and instruction count are all explicit; the count is bounded (1..MAX_DISASM_INSTRUCTIONS)."""
    return {
        "cpu": check_tag(cpu),
        "address": check_uint(address, "address", maximum=0xFFFFFFFF),
        "count": check_uint(count, "count", minimum=1, maximum=MAX_DISASM_INSTRUCTIONS),
    }


# ------------------------------------------------------------------------------------------------ memory writes
def write_memory_args(cpu: str, space: str, address: Any, value: Any, width: Any) -> dict[str, Any]:
    """Every field is explicit: no defaults for the CPU, address space, address, value or width (bytes: 1, 2 or 4)."""
    if isinstance(width, bool) or not isinstance(width, int) or width not in WRITE_WIDTHS:
        raise _bad(f"width must be one of {WRITE_WIDTHS} (bytes)")
    if not isinstance(space, str) or not _IDENT.match(space):
        raise _bad("space must be an address space name such as 'program'")
    addr = check_uint(address, "address", maximum=0xFFFFFFFF)
    if addr % width:
        raise _bad(f"address {addr:#x} is not aligned to the width ({width})")
    return {
        "cpu": check_tag(cpu), "space": space, "address": addr, "width": width,
        "value": check_uint(value, "value", maximum=(1 << (8 * width)) - 1),
    }


# ------------------------------------------------------------------------------------------------ inputs
_FIELD_NAME = re.compile(r"^[\x20-\x7e]{1,64}$")  # printable ASCII; MAME field names such as "Enemy's Bullet Speed"


def list_inputs_args(include_unsupported: Any) -> dict[str, Any]:
    if not isinstance(include_unsupported, bool):
        raise _bad("include_unsupported must be true or false")
    return {"include_unsupported": include_unsupported}


def press_args(inputs: Any, frames: Any) -> dict[str, Any]:
    """1..MAX_INPUT_FIELDS distinct {port, field} digital inputs held together for 1..MAX_INPUT_FRAMES emulated frames."""
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= MAX_INPUT_FIELDS:
        raise _bad(f"inputs must be a list of 1..{MAX_INPUT_FIELDS} objects with 'port' and 'field'")
    out, seen = [], set()
    for i, item in enumerate(inputs):
        if not isinstance(item, dict) or set(item) != {"port", "field"}:
            raise _bad(f"inputs[{i}] must be an object with exactly 'port' and 'field'")
        port, name = check_tag(item["port"]), item["field"]
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
            raise _bad(f"inputs[{i}].field must be 1-64 printable ASCII characters (a field name from list_inputs)")
        if (port, name) in seen:
            raise _bad(f"duplicate input {port}/{name}")
        seen.add((port, name))
        out.append({"port": port, "field": name})
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= MAX_INPUT_FRAMES:
        raise _bad(f"frames must be an integer 1..{MAX_INPUT_FRAMES} (emulated frames)")
    return {"inputs": out, "frames": frames}


def sequence_args(steps: Any) -> dict[str, Any]:
    """Validate a whole input sequence before anything is pressed (the plugin repeats every check).

    Each step presses one digital field for `frames` emulated frames starting `start` frames after the sequence origin.
    Rules: the earliest step starts at offset 0; steps of one field never overlap or touch (>= 1 idle frame between them);
    at most MAX_INPUT_FIELDS fields are held at once; <= MAX_SEQUENCE_STEPS steps, <= MAX_SEQUENCE_FIELDS distinct fields,
    total length <= MAX_SEQUENCE_FRAMES."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_SEQUENCE_STEPS:
        raise _bad(f"steps must be a list of 1..{MAX_SEQUENCE_STEPS} objects with 'port', 'field', 'start' and 'frames'")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(steps):
        if not isinstance(item, dict) or set(item) != {"port", "field", "start", "frames"}:
            raise _bad(f"steps[{i}] must be an object with exactly 'port', 'field', 'start' and 'frames'")
        port, name = check_tag(item["port"]), item["field"]
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
            raise _bad(f"steps[{i}].field must be 1-64 printable ASCII characters (a field name from list_inputs)")
        for k in ("start", "frames"):
            if isinstance(item[k], bool) or not isinstance(item[k], int):
                raise _bad(f"steps[{i}].{k} must be an integer number of emulated frames")
        start, frames = item["start"], item["frames"]
        if start < 0:
            raise _bad(f"steps[{i}].start must be >= 0")
        if not 1 <= frames <= MAX_INPUT_FRAMES:
            raise _bad(f"steps[{i}].frames must be 1..{MAX_INPUT_FRAMES}")
        if start + frames > MAX_SEQUENCE_FRAMES:
            raise _bad(f"steps[{i}] ends at frame offset {start + frames}; a sequence spans at most {MAX_SEQUENCE_FRAMES} emulated frames")
        out.append({"port": port, "field": name, "start": start, "frames": frames})
    if len({(s["port"], s["field"]) for s in out}) > MAX_SEQUENCE_FIELDS:
        raise _bad(f"a sequence may use at most {MAX_SEQUENCE_FIELDS} distinct fields")
    if min(s["start"] for s in out) != 0:
        raise _bad("the earliest step must start at frame offset 0 (offsets are relative to the first observed press)")
    by_field: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
    for i, s in enumerate(out):
        by_field.setdefault((s["port"], s["field"]), []).append((s["start"], s["start"] + s["frames"], i))
    for (port, name), wins in by_field.items():
        wins.sort()
        for (s0, e0, i0), (s1, _e1, i1) in zip(wins, wins[1:]):
            if s1 <= e0:
                raise _bad(f"steps {i0} and {i1} press {port}/{name} with overlapping or touching windows; leave at least one idle frame between two presses of one field")
    peak = max(sum(1 for o in out if o["start"] <= s["start"] < o["start"] + o["frames"]) for s in out)
    if peak > MAX_INPUT_FIELDS:
        raise _bad(f"{peak} fields would be held at once; at most {MAX_INPUT_FIELDS} may be held together")
    return {"steps": out}


# ------------------------------------------------------------------------------------------------ checkpoints
def check_label(label: Any) -> str | None:
    if label is None:
        return None
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL or any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise _bad(f"label must be 1-{MAX_LABEL} printable characters")
    return label.strip()


EXPERIMENT_ID = re.compile(r"^exp-(?P<sid>s\d+-[0-9a-f]{6})-\d{3,}$")  # exp-<bridge session id>-NNN, as the plugin names them


def check_experiment_owner(experiment_id: Any, session_id: str | None) -> None:
    """Refuse an experiment id that names a different bridge session than the one the call is routed to, before anything is
    sent. The plugin makes the same refusal on its side (experiment_from_other_session); this is the cheap first line, and is
    what stops an id from one session being used against another session of the same server."""
    m = EXPERIMENT_ID.fullmatch(experiment_id) if isinstance(experiment_id, str) else None
    if m is not None and session_id and m.group("sid") != session_id:
        raise BridgeError("experiment_from_other_session",
                          f"experiment {experiment_id} belongs to session {m.group('sid')}, not session {session_id}; "
                          "pass the session_id the experiment was started on. Nothing was sent.")


def check_checkpoint_id(cid: Any) -> re.Match[str]:
    m = CHECKPOINT_ID.fullmatch(cid) if isinstance(cid, str) else None  # fullmatch: '$' would accept a trailing newline
    if m is None:
        raise _bad("checkpoint_id must be an id returned by save_checkpoint/list_checkpoints (<run_id>-cpNNN)")
    return m


# ------------------------------------------------------------------------------------------------ screenshots
MAX_SCREEN_TAG = 64


def screenshot_args(screen: Any) -> dict[str, Any]:
    """`screen` is None (the machine's only screen) or a screen device tag such as ':screen' from list_screens. There is
    deliberately no argument for an output path, size, format or view: the file name and location are chosen by the plugin."""
    if screen is None:
        return {}
    if not isinstance(screen, str) or len(screen) > MAX_SCREEN_TAG or not _TAG.fullmatch(screen):  # fullmatch: "$" would accept a trailing newline
        raise _bad("screen must be a screen device tag such as ':screen' (see list_screens)")
    return {"screen": screen}
