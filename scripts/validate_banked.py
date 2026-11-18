"""Banked-memory validation against a real MAME 0.289 + 1942 (Revision B): one isolated instance, test fixture loaded.

Target: Z80 :maincpu, program window 0x8000-0xBFFF = "bank1" (4 entries), selected by a write to 0xC806 (bits 0-1).
All machine-specific addresses come from tests/fixtures/profiles/1942.json; the server contains none of them.
Bank-select interventions are made ONLY here (fixture plugin: a normal CPU-space write to the machine's own select
register, or a user breakpoint action) and every one is recorded under "interventions" in results.json.

Sections:  B  baseline: expected bytes per entry from the ROM files, MAME's own debugger, and read_memory
           F  read_memory follows the selected bank while running; bank_context agrees with the bytes
           H  capture memory fields are HIT-time (bank at the hit), not harvest-time
           C  bank changing every periodic callback while reads and a capture run
           W  breakpoint on a logical address inside the window, across banks
           S  CPU / address-space selection
           N  Windows wake nudge: enabled, and identity-bound (stale pid cannot reach another MAME)
Usage:  .venv\\Scripts\\python -I scripts\\validate_banked.py [--sections B,F,H,C,W,S,N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Fixture, Results  # noqa: E402
from validate_hardening import Ctx, wait_for  # noqa: E402  (shared helpers; its main() is not run)

from mame_mcp import winwake  # noqa: E402
from mame_mcp.bridge import BridgeClient  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT, launch  # noqa: E402
from mame_mcp.protocol import BridgeError, capture_args, read_memory_args  # noqa: E402

OUT = PROJECT_ROOT / "runs" / "banked"
PORT, PORT_B = 9451, 9452
ROMS = Path(r"D:\arcade\mame\roms")  # read only; the ROM collection is never modified
PROFILE = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "1942.json").read_text())
MAIN, AUD = PROFILE["maincpu"], PROFILE["audiocpu"]
H = lambda s: int(s, 16)  # noqa: E731
WIN0, WIN1 = H(MAIN["window"]["start"]), H(MAIN["window"]["end"])
SEL = H(MAIN["select_register"])
FRAME, FRAME_NEXT = H(MAIN["frame_handler_pc"]), H(MAIN["frame_handler_next_pc"])
R = Results("banked")
check, note = R.check, R.note
INTERVENTIONS: list[dict] = []


# ------------------------------------------------------------------------------------------ expected data
def region_image() -> tuple[bytes, dict]:
    """The :maincpu region as MAME builds it: 0x20000 bytes of 0xFF with each ROM file at its offset."""
    br = PROFILE["backing_region"]
    img = bytearray(b"\xff" * 0x20000)
    hashes = {}
    with zipfile.ZipFile(ROMS / PROFILE["rom_zip"]["file"]) as z:
        for f in br["rom_files_in_region"]:
            data = z.read(f["name"])
            hashes[f["name"]] = hashlib.sha1(data).hexdigest() == f["sha1"] and len(data) == H(f["size"])
            img[H(f["offset"]): H(f["offset"]) + len(data)] = data
    return bytes(img), hashes


IMG, FILE_HASH_OK = region_image()
BR = PROFILE["backing_region"]


def backing_offset(entry: int, addr: int) -> int:
    return H(BR["base_offset"]) + H(BR["entry_size"]) * entry + (addr - WIN0)


def expected(entry: int, addr: int, n: int) -> bytes:
    o = backing_offset(entry, addr)
    return IMG[o:o + n]


def provenance(entry: int, addr: int) -> dict:
    """Logical address -> selected entry -> backing region offset -> ROM file (or erase fill)."""
    o = backing_offset(entry, addr)
    src = "erase fill (no ROM file loaded there)"
    for f in BR["rom_files_in_region"]:
        if H(f["offset"]) <= o < H(f["offset"]) + H(f["size"]):
            src = f"{f['name']}+{o - H(f['offset']):#x}"
    return {"logical": hex(addr), "entry": entry, "region_offset": hex(o), "source": src}


def le(b: bytes) -> int:
    return int.from_bytes(b, "little")


# ------------------------------------------------------------------------------------------ harness helpers
class Bank:
    """Interventions and independent observation for one run."""

    def __init__(self, x: Ctx):
        self.x, self.fx, self.c = x, x.fx, x.c
        self.stop_pcs: list[int] = []

    def select(self, entry: int, why: str) -> None:
        """INTERVENTION: write the machine's own bank-select register (driver handler runs)."""
        self.fx.call("space_write_u8", cpu=MAIN["tag"], space="program", address=SEL, value=entry)
        INTERVENTIONS.append({"utc": time.strftime("%H:%M:%SZ", time.gmtime()), "kind": "write_select_register",
                              "address": hex(SEL), "value": entry, "state": self.x.state(), "why": why})

    def entry(self) -> int:
        return self.fx.call("bank_list")["banks"][0]["entry"]

    def dbg_bytes(self, cpu: str, addr: int, n: int) -> bytes:
        fmt = "|".join(["%X"] * n)
        line = self.fx.call("debugger_printf", cpu=cpu, text=f'"DB|{fmt}",' + ",".join(f"b@0x{addr + i:X}" for i in range(n)))["line"]
        return bytes(int(v, 16) for v in line.split("|")[1:])

    def read(self, cpu: str, addr: int, n: int, **kw) -> dict:
        return self.c.read_memory({**read_memory_args(cpu, "program", addr, n, bank_context=kw.pop("bank_context", False)), **kw})

    def stop_and_record(self) -> int:
        """Debugger-stop the running machine and record the PC it stopped at."""
        self.x.running()
        self.fx.call("user_stop")
        self.x.wait_state("debugger_stopped", 20)
        pc = int(self.fx.call("debugger_printf", cpu=MAIN["tag"], text='"%X",pc')["line"], 16)
        self.stop_pcs.append(pc)
        return pc

    def obs_lines(self, marker: str, n: int = 4000) -> list[list[str]]:
        lines = self.fx.call("console_tail", n=n)["lines"]
        if marker in lines:
            lines = lines[lines.index(marker) + 1:]
        return [l.split("|")[1:] for l in lines if l.startswith("OBS|")]


def mem_fields():
    return [{"space": "program", "address": WIN0, "size": 1}, {"space": "program", "address": WIN0, "size": 4}]


def unpack(ev: dict) -> tuple[int, int]:
    return int(ev["mem"][0]["value"], 16), int(ev["mem"][1]["value"], 16)


# ------------------------------------------------------------------------------------------ boot
def boot_natural(b: Bank):
    """Let the game boot with a write watchpoint on the select register: the game's own code selects entry 0 (a natural
    bank selection, no harness write). Also gives the game time to finish initialising before any intervention."""
    x, fx = b.x, b.fx
    note("BOOT: natural bank selection by the game's own code (watchpoint on 0x%X; observation only)" % SEL)
    fx.call("set_visible_cpu", cpu=MAIN["tag"])
    idx = fx.call("user_wp", cpu=MAIN["tag"], address=SEL, length=1, kind="w", action='printf "NAT|%X|%X",wpdata,pc;g')["index"]
    fx.call("debugger_printf", cpu=MAIN["tag"], text='"MARK|NAT"')
    fx.call("user_resume")
    x.wait_state("running", 40)
    lines = wait_for(lambda: [l for l in fx.call("console_tail", n=400)["lines"] if l.startswith("NAT|")], 30, 0.5) or []
    time.sleep(1.0)
    fx.call("user_wpclear", cpu=MAIN["tag"], index=idx)
    vals = sorted({tuple(l.split("|")[1:]) for l in lines})
    nat = MAIN["natural_select"]
    check("BOOT natural bank selection observed: game code writes %s to 0x%X at boot (watchpoint reports pc %s); entry is now %d" % (nat["value"], SEL, nat["pc"], b.entry()),
          bool(vals) and all(int(v[0], 16) == H(nat["value"]) for v in vals) and b.entry() == 0, observed=vals)


# ------------------------------------------------------------------------------------------ B
def section_baseline(b: Bank):
    x, fx, c = b.x, b.fx, b.c
    note("SECTION B: baseline per selected entry; three independent sources (ROM files, MAME debugger, bridge)", target=PROFILE["description"])
    check("B0 ROM files in the :maincpu region match the SHA1s in MAME 0.289 1942.cpp", all(FILE_HASH_OK.values()), files=FILE_HASH_OK)
    first8 = {n: IMG[backing_offset(n, WIN0):backing_offset(n, WIN0) + 8].hex() for n in range(4)}
    check("B0 expected first 8 bytes per entry (region offset 0x10000+0x4000*n) equal the profile", all(first8[n] == PROFILE["entries_expected"][str(n)]["first8"] for n in range(4)), expected=first8)
    check("B0 the three populated entries are distinguishable at 0x8000; entry 3 is erase fill", len({first8[0], first8[1], first8[2]}) == 3 and first8[3] == "ff" * 8, first8=first8)
    x.clean_slate()
    x.put_in("stopped")
    b.select(0, "baseline start")
    probes = [WIN0, 0x9FF8, 0xBFF8]  # window start; across the 0x2000 populated boundary of entry 1; window end
    low = c.read_memory(read_memory_args(MAIN["tag"], "program", 0, 8))["data_hex"]
    rows = []
    for n in range(4):
        b.select(n, f"baseline entry {n}")
        ok_entry = b.entry() == n
        for a in probes:
            exp = expected(n, a, 8)
            dbg = b.dbg_bytes(MAIN["tag"], a, 8)
            res = b.read(MAIN["tag"], a, 8, bank_context=True)
            bc = {k["tag"]: k["entry"] for k in res["bank_context"]["banks"]}
            ok = exp == dbg == bytes.fromhex(res["data_hex"]) and bc.get(MAIN["bank_tag"]) == n and ok_entry
            rows.append({**provenance(n, a), "expected": exp.hex(), "mame_debugger": dbg.hex(), "read_memory": res["data_hex"], "bank_context_entry": bc.get(MAIN["bank_tag"]), "ok": ok})
        low_n = c.read_memory(read_memory_args(MAIN["tag"], "program", 0, 8))["data_hex"]
        rows.append({"logical": "0x0", "entry": n, "note": "unbanked ROM", "read_memory": low_n, "ok": low_n == low})
    bad = [r for r in rows if not r["ok"]]
    check("B1 for every entry and probe address: ROM-file expectation == MAME debugger == read_memory, and bank_context names the entry", not bad, cases=len(rows), failures=bad[:3])
    check("B1 unbanked address 0x0 is identical for every entry (a bank does not change non-window memory)", all(r["ok"] for r in rows if r.get("note")), value=low)
    # 0xC806 is write-only: show that reading it does not reveal the entry
    reg = b.read(MAIN["tag"], SEL, 1)["data_hex"]
    note("B2 select register 0xC806 is write-only: read_memory returns the unmapped value, so the entry cannot be read back from it", read_back=reg)
    note("B3 logical address vs bank entry vs backing offset (examples)", examples=[provenance(n, WIN0) for n in range(4)] + [provenance(1, 0xA000)],
         meaning="logical address = what the CPU uses; entry = bank.entry (bank_context); region offset = from MAME source configure_entries, confirmed by byte equality here, NOT given by the API; entry 3 and the upper half of entry 1 are erase fill, not ROM")
    b.select(0, "restore")
    x.clean_slate()


# ------------------------------------------------------------------------------------------ F
def section_follow(b: Bank):
    x, fx, c = b.x, b.fx, b.c
    note("SECTION F: read_memory follows the selected bank while the machine is running (attract mode, window not executed)")
    x.clean_slate()
    check("F0 machine is running", x.state() == "running")
    rows = []
    for n in (1, 2, 3, 0, 2):
        b.select(n, f"running read, entry {n}")
        res = b.read(MAIN["tag"], WIN0, 8, bank_context=True)
        e = {k["tag"]: k["entry"] for k in res["bank_context"]["banks"]}.get(MAIN["bank_tag"])
        rows.append({"selected": n, "bytes": res["data_hex"], "expected": expected(n, WIN0, 8).hex(), "bank_context_entry": e,
                     "ok": res["data_hex"] == expected(n, WIN0, 8).hex() and e == n, "execution": res["execution"]["state"]})
    check("F1 each read returns the selected entry's bytes and bank_context reports that entry", all(r["ok"] for r in rows), rows=rows)
    pcs = {int(l, 16) >> 12 for l in (fx.call("debugger_printf", cpu=MAIN["tag"], text='"%X",pc')["line"] for _ in range(20))}
    check("F2 the CPU was not executing the banked window while it was held (sampled PCs below 0x8000)", all(p < 8 for p in pcs), pc_pages=sorted(pcs))
    ctx_only = b.read(MAIN["tag"], WIN0, 4)
    check("F3 bank_context is opt-in: absent by default", "bank_context" not in ctx_only)
    bc = b.read(MAIN["tag"], WIN0, 4, bank_context=True)["bank_context"]
    check("F3 bank_context states what it does NOT know (range coverage, region offset) and when it was sampled",
          "not determined" in bc["covers_requested_range"] and "not available" in bc["backing_region_offset"] and "same plugin call" in bc["sampled"], bank_context=bc)
    b.select(0, "restore")


# ------------------------------------------------------------------------------------------ H
def hit_run(b: Bank, addr: int, nxt: int, events: int, label: str, marker: str):
    """Arm (while debugger-stopped) a capture at `addr` and, at the NEXT instruction `nxt`, a user breakpoint that
    records an independent observation and then switches entry 1 -> 2 (a write issued inside the machine's own run)."""
    x, fx, c = b.x, b.fx, b.c
    x.clean_slate()
    fx.call("set_visible_cpu", cpu=MAIN["tag"])
    stop_pc = b.stop_and_record()
    b.select(1, f"{label}: entry 1 selected before the first hit")
    fx.call("debugger_printf", cpu=MAIN["tag"], text=f'"{marker}"')
    idx = fx.call("user_bp", cpu=MAIN["tag"], address=nxt, action=f'printf "OBS|%X|%X",b@0x{WIN0:X},d@0x{WIN0:X};b@0x{SEL:X}=2;g')["index"]
    INTERVENTIONS.append({"kind": "user_breakpoint_action_writes_select_register", "address": hex(nxt), "value": 2, "why": f"{label}: switch entry 1 -> 2 after the first observation, inside the machine's own execution"})
    cap = x.capture(cpu=MAIN["tag"], address=addr, events=events, wall=30, regs=["PC"], mem=mem_fields(), label=label)
    c.execution_control("resume")
    st = x.wait_done(cap["experiment_id"])
    fx.call("user_bpclear", cpu=MAIN["tag"], index=idx)
    return stop_pc, st, x.events(st), b.obs_lines(marker)


def section_hit_time(b: Bank):
    x, fx, c = b.x, b.fx, b.c
    e1, e2 = expected(1, WIN0, 4), expected(2, WIN0, 4)
    v1, v2 = (e1[0], le(e1)), (e2[0], le(e2))
    note("SECTION H: capture memory fields are read at the hit (bank selected then), not at harvest")
    # H0: MAME behaviour that matters for attribution. A Lua-requested stop lands at PC 0x10; resuming there does not
    # re-trigger a breakpoint at 0x10, so that one handler instance is observed (0x11) but NOT captured.
    stop_pc, st, ev, obs = hit_run(b, FRAME - 1, FRAME, 2, "bank-resume-pc", "MARK|H0")
    cv = [unpack(e) for e in ev]
    ov = [(int(o[0], 16), int(o[1], 16)) for o in obs[:3]]
    check("H0 documented MAME behaviour: stopped at PC 0x%X, a capture breakpoint at that same PC does not record the resumed instance (first observation is entry 1, first event is entry 2)" % stop_pc,
          stop_pc == FRAME - 1 and ov[:1] == [v1] and cv[:1] == [v2], stop_pc=hex(stop_pc), observed=[(hex(a), hex(d)) for a, d in ov], captured=[(hex(a), hex(d)) for a, d in cv],
          consequence="arm captures at an address the stopped machine is not sitting on, or expect the first instance to be missed")
    x.clean_slate()
    # H1..: the real hit-time test. Capture at 0x11 and observe at 0x12; the stop PC (0x10) is before both.
    stop_pc, st, ev, obs = hit_run(b, FRAME, FRAME_NEXT, 3, "bank-hit-time", "MARK|H")
    check("H1 capture completed by event limit with 3 events", st["completion_reason"] == "event_limit" and len(ev) == 3, summary=st["summary"], stop_pc=hex(stop_pc))
    cap_vals = [unpack(e) for e in ev]
    ov = [(int(o[0], 16), int(o[1], 16)) for o in obs[:3]]
    check("H2 event 1 holds entry 1's bytes (the bank selected AT the hit); events 2-3 hold entry 2's",
          cap_vals == [v1, v2, v2], captured=[(hex(a), hex(d)) for a, d in cap_vals], entry1=e1.hex(), entry2=e2.hex(), provenance=[provenance(1, WIN0), provenance(2, WIN0)])
    check("H3 each captured value equals the independent hit-time observation (user breakpoint printf at the next instruction)", ov == cap_vals, observed=[(hex(a), hex(d)) for a, d in ov])
    now = b.read(MAIN["tag"], WIN0, 4, bank_context=True)
    now_entry = {k["tag"]: k["entry"] for k in now["bank_context"]["banks"]}[MAIN["bank_tag"]]
    check("H4 a read AFTER the experiment sees entry 2 (harvest-time state differs from event 1: a later bank sample must not be attributed to the earlier event)",
          now_entry == 2 and now["data_hex"] == e2.hex() and cap_vals[0][1] != le(bytes.fromhex(now["data_hex"])), later_entry=now_entry, later_bytes=now["data_hex"], event1=hex(cap_vals[0][1]))
    mani = x.manifest(st)
    check("H5 manifest states bank context is unavailable per event and why", mani["bank_context"]["per_event"] == "unavailable" and "hit" in mani["bank_context"]["reason"], bank_context=mani["bank_context"])
    note("H6 event time fields (harvest-time unless a separately validated mechanism exists)", event_keys=sorted(ev[0].keys()), first_event={k: ev[0][k] for k in ev[0] if k not in ("mem", "regs")})
    note("H evidence", events=st["evidence"]["events_jsonl"], manifest=st["evidence"]["manifest_json"])
    b.select(0, "restore")
    x.clean_slate()
    x.clean_after("H")


# ------------------------------------------------------------------------------------------ C
def section_cycle(b: Bank):
    x, fx, c = b.x, b.fx, b.c
    note("SECTION C: the select register is rewritten on every periodic callback (0,1,2,3,...) while reads and a capture run")
    x.clean_slate()
    fx.call("set_visible_cpu", cpu=MAIN["tag"])
    fx.call("bank_cycle", cpu=MAIN["tag"], address=SEL, values=[0, 1, 2, 3])
    INTERVENTIONS.append({"kind": "bank_cycle", "address": hex(SEL), "values": [0, 1, 2, 3], "why": "C: bank changes between consecutive bridge requests and during an experiment"})
    first = {n: expected(n, WIN0, 4) for n in range(4)}
    seen, bad = set(), []
    for _ in range(80):
        res = b.read(MAIN["tag"], WIN0, 4, bank_context=True)
        e = {k["tag"]: k["entry"] for k in res["bank_context"]["banks"]}[MAIN["bank_tag"]]
        seen.add(e)
        if res["data_hex"] != first[e].hex():
            bad.append({"entry": e, "bytes": res["data_hex"]})
    check("C1 80 reads under continuous bank switching: every read's bytes match the entry reported in ITS OWN bank_context (atomic per read)", not bad and len(seen) >= 2, entries_seen=sorted(seen), mismatches=bad[:3])
    stop_pc = b.stop_and_record()
    fx.call("debugger_printf", cpu=MAIN["tag"], text='"MARK|C"')
    idx = fx.call("user_bp", cpu=MAIN["tag"], address=FRAME_NEXT, action=f'printf "OBS|%X|%X",b@0x{WIN0:X},d@0x{WIN0:X};g')["index"]
    cap = x.capture(cpu=MAIN["tag"], address=FRAME, events=24, wall=40, regs=["PC"], mem=mem_fields(), label="bank-cycle")
    c.execution_control("resume")
    st = x.wait_done(cap["experiment_id"])
    fx.call("user_bpclear", cpu=MAIN["tag"], index=idx)
    stop = fx.call("bank_cycle", stop=True)
    ev = x.events(st)
    vals = [unpack(e) for e in ev]
    obs = [(int(o[0], 16), int(o[1], 16)) for o in b.obs_lines("MARK|C")[:len(ev)]]
    allowed = {(first[n][0], le(first[n])) for n in range(4)}
    check("C2 capture under continuous switching completed with 24 events", st["completion_reason"] == "event_limit" and len(ev) == 24, summary=st["summary"], cycler_writes=stop["writes"])
    check("C3 every event is internally consistent: its 1-byte and 4-byte fields come from ONE bank (both read at the hit)", all(v in allowed for v in vals), distinct=sorted({hex(v[1]) for v in vals}))
    banks_hit = {n for v in vals for n in range(4) if v == (first[n][0], le(first[n]))}
    check("C4 events span more than one bank (the captured bank varies with the hit)", len(banks_hit) >= 2, banks_in_events=sorted(banks_hit))
    mism = [(i, hex(a[1]), hex(o[1])) for i, (a, o) in enumerate(zip(vals, obs)) if a != o]
    check("C5 event i equals the independent observation at the next instruction (0x%X)" % FRAME_NEXT, len(obs) == len(vals) and not mism,
          events=len(vals), observed=len(obs), mismatches=mism[:5],
          caveat="a periodic callback (bank write) landing between the two instructions would show as a mismatch; none expected, any is reported")
    note("C evidence", events=st["evidence"]["events_jsonl"], sequence_of_entries=[next((n for n in range(4) if v[1] == le(first[n])), None) for v in vals])
    b.select(0, "restore")
    x.clean_slate()
    x.clean_after("C")


# ------------------------------------------------------------------------------------------ W
def section_window_bp(b: Bank):
    x, fx, c = b.x, b.fx, b.c
    note("SECTION W: a breakpoint on a LOGICAL address inside the window (0x%X) fires whichever entry is selected; it carries no bank identity" % WIN0)
    tramp = 0xE100
    for n in (0, 1, 3):
        x.clean_slate()
        fx.call("set_visible_cpu", cpu=MAIN["tag"])
        fx.call("user_stop")
        x.wait_state("debugger_stopped", 20)
        b.select(n, f"W: entry {n} selected before jumping into the window")
        for i, v in enumerate((0xC3, WIN0 & 0xFF, WIN0 >> 8)):  # JP 0x8000 in work RAM
            fx.call("space_write_u8", cpu=MAIN["tag"], space="program", address=tramp + i, value=v)
        fx.call("set_pc", cpu=MAIN["tag"], value=tramp)
        INTERVENTIONS.append({"kind": "ram_trampoline_and_pc", "address": hex(tramp), "bytes": "C3 00 80", "why": f"W: make the CPU execute the logical address {WIN0:#x} with entry {n} selected"})
        pc_now = int(fx.call("debugger_printf", cpu=MAIN["tag"], text='"%X",pc')["line"], 16)
        cap = x.capture(cpu=MAIN["tag"], address=WIN0, events=1, wall=20, regs=["PC"], mem=mem_fields(), label=f"window-bp-entry{n}")
        c.execution_control("resume")
        st = x.wait_done(cap["experiment_id"], 30)
        fx.call("user_stop")
        x.wait_state("debugger_stopped", 20)
        ev = x.events(st)
        e = expected(n, WIN0, 4)
        ok = st["completion_reason"] == "event_limit" and len(ev) == 1 and ev[0]["regs"]["PC"].lower() == hex(WIN0) and unpack(ev[0]) == (e[0], le(e))
        check(f"W{n} entry {n}: breakpoint at logical 0x8000 fired (PC=0x8000) and the memory field is entry {n}'s bytes", ok,
              start_pc=hex(pc_now), event_pc=ev and ev[0]["regs"]["PC"], captured=ev and ev[0]["mem"][1]["value"], expected_le=hex(le(e)), provenance=provenance(n, WIN0),
              reason=st["completion_reason"])
        # leave the junk state: soft reset, the game's own code reselects entry 0
        fx.call("soft_reset")
        fx.call("user_resume")
        x.wait_state("running", 40)
        INTERVENTIONS.append({"kind": "soft_reset", "why": f"W: discard the state after executing window data (entry {n})"})
    time.sleep(2.0)
    nat = b.entry()
    check("W4 after reset the game's own code selects entry 0 (natural bank selection, no harness write)", nat == 0, entry=nat, natural_write=MAIN["natural_select"])
    note("W semantic summary", result="the breakpoint address is a logical CPU address; the same address fires for entries 0, 1 and 3 and the event records only PC and the memory fields. "
         "Which code or data that address meant is determined only by the bank selected at the hit, which the capture cannot report (bank_context unavailable per event)")
    x.clean_slate()


# ------------------------------------------------------------------------------------------ S
def section_selection(b: Bank):
    x, fx, c = b.x, b.fx, b.c
    note("SECTION S: CPU and address-space selection")
    x.clean_slate()
    fx.call("set_visible_cpu", cpu=MAIN["tag"])
    b.select(2, "S: entry 2 so the main window is distinct")
    m = b.read(MAIN["tag"], WIN0, 8)["data_hex"]
    a = b.read(AUD["tag"], WIN0, 8)["data_hex"]
    am = b.dbg_bytes(AUD["tag"], WIN0, 8).hex()
    check("S1 read_memory(:audiocpu) at the same logical address equals that CPU's own debugger bytes and differs from :maincpu's banked window", a == am and a != m, audiocpu=a, audiocpu_debugger=am, maincpu=m)
    fx.call("set_visible_cpu", cpu=AUD["tag"])
    m2 = b.read(MAIN["tag"], WIN0, 8)["data_hex"]
    check("S2 with the debugger's visible CPU set to :audiocpu, a :maincpu read is still :maincpu's bank", m2 == expected(2, WIN0, 8).hex(), visible=fx.call("visible_cpu")["tag"], bytes=m2)
    cap = x.capture(cpu=MAIN["tag"], address=FRAME, events=1, wall=20, regs=["PC"], mem=mem_fields(), label="visible-cpu-negative-control")
    st = x.wait_done(cap["experiment_id"])
    ev = x.events(st)
    e2 = expected(2, WIN0, 4)
    check("S3 capture on :maincpu with the visible CPU on :audiocpu still records :maincpu's bank bytes (explicit device qualification)", len(ev) == 1 and unpack(ev[0]) == (e2[0], le(e2)), captured=ev and ev[0]["mem"][1]["value"], expected=hex(le(e2)))
    fx.call("set_visible_cpu", cpu=MAIN["tag"])
    for space in ("data", "io", "opcodes"):
        try:
            r = c.read_memory(read_memory_args(MAIN["tag"], space, WIN0, 4))
            note(f"S4 read_memory(:maincpu, space={space}) accepted", data_hex=r["data_hex"], same_as_program=r["data_hex"] == expected(2, WIN0, 4).hex())
        except BridgeError as e:
            check(f"S4 read_memory(:maincpu, space={space}) is refused with a stated reason, not answered from the program space", e.code in ("unknown_space", "unsupported_space"), code=e.code)
    try:
        b.read(":nosuchcpu", WIN0, 4)
        check("S5 unknown CPU refused", False)
    except BridgeError as e:
        check("S5 unknown CPU refused", e.code in ("unknown_cpu", "not_found", "bad_argument", "unknown_device"), code=e.code)
    b.select(0, "restore")
    x.clean_slate()


# ------------------------------------------------------------------------------------------ N
def section_nudge(b: Bank, proc, created, relaunch):
    x, fx, c = b.x, b.fx, b.c
    note("SECTION N: Windows wake nudge (documented opt-in mitigation, not a timing guarantee)", enabled_for_this_run=c.wake is not None, wake_posts_so_far=c.wake_posts, pid=proc.pid, bound_to_port=PORT)
    fx.call("set_visible_cpu", cpu=MAIN["tag"])
    x.clean_slate()
    fx.call("user_stop")
    x.wait_state("debugger_stopped", 20)
    posts0, lat = c.wake_posts, []
    for idle in (3.0, 6.0, 9.0):  # idle gaps of the kind that stalled an un-nudged request in validate_latency.py
        time.sleep(idle)
        t0 = time.time()
        try:
            c.session_info()
            lat.append(round(time.time() - t0, 3))
        except BridgeError as e:
            lat.append(e.code)
    check("N1 with the identity-bound nudge enabled, first requests after idle while debugger-stopped were all answered (no request_timeout) in this run",
          all(isinstance(v, float) for v in lat), latencies_s=lat, wake_posts_in_these_requests=c.wake_posts - posts0, total_wake_posts=c.wake_posts,
          claim="a demonstrated mitigation in this run, not a guarantee of timing; see validate_latency.py for the un-nudged comparison")
    x.clean_slate()
    # decoy: a second MAME on another port, and a recording shim around the posting primitive
    procB, infoB = launch(system="1942", port=PORT_B, run_dir=OUT / "run_b", rompath=ROMS, fixture=False)
    posted: list[int] = []
    real_wake = winwake.wake
    winwake.wake = lambda pid: (posted.append(pid), real_wake(pid))[1]
    try:
        createdB = winwake.creation_time(procB.pid)
        time.sleep(1.0)
        wk = lambda src, port: winwake.waker_for(src, port=port)  # noqa: E731
        w_ok = wk((proc.pid, created), PORT)
        check("N2 correct pid + creation time + bridge port: nudge is sent to the bridge's own MAME", w_ok() > 0 and posted[-1:] == [proc.pid], posted=posted)
        posted.clear()
        w = wk(procB.pid, PORT)
        n = w()
        check("N3 stale/mistaken pid that names a DIFFERENT live MAME (another port) is refused, nothing posted", n == 0 and not posted and "port" in str(w.last_refusal), refusal=w.last_refusal, posted=posted)
        w = wk((proc.pid, createdB), PORT)
        n = w()
        check("N4 recycled-pid case: right pid, wrong recorded creation time -> refused, nothing posted", n == 0 and not posted and "creation time" in str(w.last_refusal), refusal=w.last_refusal, posted=posted)
        w = wk((procB.pid, createdB), PORT)
        n = w()
        check("N5 even an exact (pid, creation time) of the other MAME is refused for this bridge port", n == 0 and not posted, refusal=w.last_refusal, posted=posted)
        w = wk(os.getpid(), PORT)
        check("N6 a non-MAME process (this Python) is refused", w() == 0 and not posted and "not mame" in str(w.last_refusal), refusal=w.last_refusal)
        procB.kill()
        procB.wait()
        time.sleep(0.5)
        w = wk((procB.pid, createdB), PORT_B)
        check("N7 a pid whose MAME has exited is refused as stale", w() == 0 and not posted and "not found" in str(w.last_refusal), refusal=w.last_refusal)
        # restart: kill the real MAME, start a new one on the SAME port; the old identity must not reach the new process
        old_pid, old_created = proc.pid, created
        c.close(graceful=False)
        proc.kill()
        proc.wait()
        time.sleep(1.0)
        proc2, info2 = relaunch()
        time.sleep(2.0)
        new_created = winwake.creation_time(proc2.pid)
        w_old = wk((old_pid, old_created), PORT)
        w_env = wk(old_pid, PORT)  # MAMEMCP_MAME_PID-style: pid only, no creation time recorded
        w_new = wk((proc2.pid, new_created), PORT)
        posted.clear()
        r_old, r_env, r_new = w_old(), w_env(), w_new()
        check("N8 after a MAME restart on the same port: the old (pid, creation time) is refused, the new identity is accepted",
              r_old == 0 and r_env == 0 and r_new > 0 and posted == [proc2.pid], old_refusal=w_old.last_refusal, pid_only_refusal=w_env.last_refusal, new=r_new, posted=posted,
              old_pid=old_pid, new_pid=proc2.pid)
        note("N note", scope="identity = image name mame*, creation time (when recorded) and ownership of the bridge TCP port. Not general process control: the only action is posting WM_NULL, and only after all checks pass",
             limits="a configured pid-only (MAMEMCP_MAME_PID) setting relies on the port-ownership check alone; Windows only; verified for MAME 0.289 -debug windows front-end only")
        return proc2
    finally:
        winwake.wake = real_wake
        try:
            procB.kill()
        except Exception:
            pass


# ------------------------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default="B,F,H,C,W,S,N")
    want = set(ap.parse_args().sections.split(","))
    run_dir = OUT / "run"

    def start():
        p, i = launch(system="1942", port=PORT, run_dir=run_dir, rompath=ROMS, fixture=True)
        return p, i
    proc, info = start()
    created = winwake.creation_time(proc.pid)
    wake = winwake.waker_for(lambda: (proc.pid, created), port=PORT)
    fx = Fixture(info["fixture_dir"], wake=wake)
    c = BridgeClient(port=PORT, default_timeout=30, wake=wake)
    holder = {"proc": proc}
    try:
        si = c.connect(timeout=90)
        check("environment: MAME 0.289, machine 1942 (Revision B), fixture answering, nudge enabled",
              si["mame_version"] == "0.289" and si["machine"]["name"] == "1942" and fx.call("ping")["pong"] and wake is not None,
              mame=si["mame_version"], machine=si["machine"]["description"], zip_sha1=info["romset_zip"]["sha1"], profile_sha1=PROFILE["rom_zip"]["sha1"],
              verify=info["verifyroms"]["output"][:60], nudge="enabled (WM_NULL, identity-bound to pid+creation time+bridge port)")
        check("environment: ROM zip is the profiled one and -verifyroms reports it good", info["romset_zip"]["sha1"] == PROFILE["rom_zip"]["sha1"] and "is good" in info["verifyroms"]["output"])
        x = Ctx(c, fx)
        b = Bank(x)
        try:
            boot_natural(b)
        except Exception:
            import traceback
            check("BOOT: UNEXPECTED harness exception", False, error=traceback.format_exc()[-900:])
        table = (("B", section_baseline), ("F", section_follow), ("H", section_hit_time), ("C", section_cycle), ("W", section_window_bp), ("S", section_selection))
        for name, fn in table:
            if name in want:
                try:
                    fn(b)
                except BridgeError as e:
                    check(f"section {name}: UNEXPECTED bridge error", False, error=e.to_dict())
                except Exception:
                    import traceback
                    check(f"section {name}: UNEXPECTED harness exception", False, error=traceback.format_exc()[-900:])
        if "N" in want:
            try:
                def relaunch():
                    p, i = start()
                    holder["proc"] = p
                    return p, i
                holder["proc"] = section_nudge(b, proc, created, relaunch) or holder["proc"]
            except Exception:
                import traceback
                check("section N: UNEXPECTED harness exception", False, error=traceback.format_exc()[-900:])
        note("transport", timeouts=c.timeouts, read_only_retries=c.read_retries, stale_discarded=c.stale_discarded, wake_posts=c.wake_posts, events=c.transport_events[-6:])
        note("interventions (all bank-select writes were made by this harness; the server has no write tool)", count=len(INTERVENTIONS), first=INTERVENTIONS[:3])
    finally:
        c.close(graceful=False)
        for p in {proc, holder["proc"]}:
            try:
                p.kill()
            except Exception:
                pass
    return R.save(OUT / "results.json", mame="0.289", profile=PROFILE["description"], interventions=INTERVENTIONS,
                  nudge={"enabled": True, "kind": "WM_NULL, identity-bound", "note": "demonstrated mitigation, not a timing guarantee"})


if __name__ == "__main__":
    sys.exit(main())
