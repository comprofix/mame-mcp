"""capture_watchpoint against real MAME 0.289 (direct bridge, TEST-ONLY fixture plugin as the independent observers).

What is compared, per access (address, data, hit-time PC, SP register, access type):
  * the bridge's events.jsonl, against
  * a SECOND MAME debugger watchpoint installed by the fixture on the same range that prints its own values with printf, and
  * a Lua memory TAP on the same range (bus-level offset / lane mask / data, CURPC) - a different MAME mechanism that shares no
    code with debugger watchpoints.
Both observers are armed while MAME is debugger-stopped together with the capture, so all three streams start at the same access.

Sections: Z (shinobi Z80, write/read/read_write, limits, timeout, cancel, ownership, invalid arguments), M (1944 68000 byte/word
lanes, reads and writes, device without a debug interface), I (failed installation + verified rollback), C (failed cleanup,
unresolved instrumentation, recovery), F (instruction fetches: reported by the Z80 core, not by the 68000 core).
Usage: .venv\\Scripts\\python -I scripts\\validate_watchpoint.py [Z M I C F]
Output: runs\\watchpoint\\results.json
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Results  # noqa: E402
from _inst import H, Inst, code, profile, runs_root  # noqa: E402

OUT = runs_root() / "watchpoint"
P = profile()
R = Results("watchpoint")
check, note = R.check, R.note


def tap_norm(ev: dict, dw: int, endian: str) -> tuple[int, int]:
    """(byte-precise address, lane-shifted data) of a bus-level tap event."""
    off, data, mask = ev["off"], ev["data"], ev["mask"]
    if dw == 8:
        return off, data & 0xFF
    if dw == 16 and endian == "big":
        if mask == 0xFFFF:
            return off, data & 0xFFFF
        if mask == 0xFF00:
            return off, (data >> 8) & 0xFF
        if mask == 0x00FF:
            return off + 1, data & 0xFF
    raise ValueError(f"unsupported tap event shape dw={dw} endian={endian} mask={mask:#x}")


def lanes(events: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        out[f"{e['mask']:#x}"] = out.get(f"{e['mask']:#x}", 0) + 1
    return out


def used_temps(cap: dict) -> set[int]:
    dt = cap["debugger_temporaries"]
    return {int(dt["counter"][4:])} | {int(v[4:]) for v in dt["registers"].values()}


_RUN = [0]


def arm_observers(m: Inst, cpu: str, space: str, start: int, length: int, kinds: list[str], used: set[int], run: int) -> list[int]:
    """User (fixture) debugger watchpoints on the same range that PRINT hit-time values: wpaddr, wpdata, curpc and sp.
    Every line carries this run's number (OB<run>R / OB<run>W) because MAME's console log keeps the lines of earlier runs."""
    free = [t for t in range(10) if t not in used][:4]
    assert len(free) == 4, "not enough free debugger temporaries for the observer"
    a, d, p, s = free
    cond = f"((temp{a}=wpaddr)|1)&&((temp{d}=wpdata)|1)&&((temp{p}=curpc)|1)&&((temp{s}=sp)|1)"
    idx = []
    for k in kinds:
        tag = f"OB{run}{'R' if k == 'r' else 'W'}"
        act = f'g;printf "{tag}|%X|%X|%X|%X",temp{a},temp{d},temp{p},temp{s}'
        idx.append(m.fx.call("user_wp", cpu=cpu, space=space, kind=k, address=start, length=length, cond=cond, action=act)["index"])
    return idx


def attrib(m: Inst, label: str, cpu: str, space: str, start: int, length: int, access: str, n: int, *, expect_lanes: bool = False) -> dict:
    """One watched run compared against both observers. Returns the status and a verdict dict."""
    kinds = {"read": ["r"], "write": ["w"], "read_write": ["r", "w"]}[access]
    m.stop_debugger()
    taps = {k: m.fx.call("tap_log", cpu=cpu, space=space, kind=k, start=start, end=start + length - 1, name=f"{label}_{k}", cap=n + 300) for k in kinds}
    cap = m.rq("capture_watchpoint", dict(cpu=cpu, space=space, address=start, length=length, access=access, max_events=n, max_wall_seconds=90, registers=["SP"], label=label))
    _RUN[0] += 1
    run = _RUN[0]
    obs_idx = arm_observers(m, cpu, space, start, length, kinds, used_temps(cap), run)
    time.sleep(1.0)
    st0 = m.rq("experiment_status", {}, mutating=False)
    check(f"{label}: armed while debugger-stopped records nothing and does not resume", st0["state"] == "running" and st0["counts"]["events_captured"] == 0 and st0["execution"]["state"] == "debugger_stopped",
          counts=st0["counts"], execution=st0["execution"]["state"])
    m.resume()
    st = m.poll_done(cap["experiment_id"], timeout=120)
    ours = m.events(st)
    obs_lines = m.fx.call("console_lines", prefix=f"OB{run}", max=n * 3)["lines"]
    obs = [(("OBR" if l.split("|")[0].endswith("R") else "OBW"), *[int(x, 16) for x in l.split("|")[1:]]) for l in obs_lines]
    tap_events = {k: m.fx.call("tap_log_take", name=f"{label}_{k}", stop=True) for k in kinds}
    for i in obs_idx:
        m.fx.call("user_wpclear", cpu=cpu, space=space, index=i)
    verdict: dict = {"ours": len(ours), "observer": len(obs)}
    # event structure
    ok_struct = [e["seq"] for e in ours] == list(range(1, len(ours) + 1)) and all(e["access"] in {"read": ("read",), "write": ("write",), "read_write": ("read", "write")}[access] for e in ours) \
        and all(e["location"] == e["access"] and e["space"] == space and start <= int(e["address"], 16) < start + length for e in ours) and all("bank" not in e for e in ours)
    check(f"{label}: events are sequenced 1..N, typed by the access that fired, inside the range, carry no bank id", ok_struct and len(ours) == n, events=len(ours), first=ours[:1])
    # vs debugger observer
    bad = []
    for i, e in enumerate(ours[:len(obs)]):
        tag, oa, od, op, osp = obs[i]
        want_tag = "OBR" if e["access"] == "read" else "OBW"
        if not (tag == want_tag and int(e["address"], 16) == oa and int(e["data"], 16) == od and int(e["pc"], 16) == op and int(e["regs"]["SP"], 16) == osp):
            bad.append((i, e, obs[i]))
    check(f"{label}: every event equals the independent debugger watchpoint's printed access type, address, data, hit-time PC and SP", not bad and len(obs) >= len(ours),
          compared=min(len(ours), len(obs)), mismatches=len(bad), first_bad=bad[:2])
    # vs Lua tap (per access type)
    dw, endian = next(iter(taps.values()))["data_width"], next(iter(taps.values()))["endianness"]
    tbad, seen = [], {}
    for k in kinds:
        acc = "read" if k == "r" else "write"
        mine = [e for e in ours if e["access"] == acc]
        tp = tap_events[k]["events"]
        seen[acc] = {"ours": len(mine), "tap": len(tp), "lanes": lanes(tp[:len(mine)])}
        for e, t in zip(mine, tp):
            a, d = tap_norm(t, dw, endian)
            if not (int(e["address"], 16) == a and int(e["data"], 16) == d and int(e["pc"], 16) == t["pc"]):
                tbad.append((e, t, (a, d)))
    check(f"{label}: every event equals the independent Lua memory tap (byte-precise address, lane-shifted data, CURPC)", not tbad and all(v["tap"] >= v["ours"] for v in seen.values()),
          per_access=seen, mismatches=len(tbad), first_bad=tbad[:2])
    if expect_lanes:
        masks = {mk for v in seen.values() for mk in v["lanes"]}
        check(f"{label}: byte-lane (not only full-word) accesses were present, so lane/address semantics were exercised", len(masks) >= 2, masks=sorted(masks))
    check(f"{label}: stopped at the event limit with the limit's own counts, cleanup verified, nothing dropped",
          st["state"] == "completed" and st["completion_reason"] == "event_limit" and st["counts"]["events_captured"] == n and st["counts"]["events_dropped"] == 0
          and st["cleanup"]["verified"] and st["cleanup"]["breakpoint_cleared"] and all(r["cleared"] for r in st["cleanup"]["locations"]), counts=st["counts"], reason=st["completion_reason"])
    return st


def wplist(m: Inst, cpu: str, space: str) -> list:
    return m.fx.call("wplist", cpu=cpu, space=space)["watchpoints"]


# ------------------------------------------------------------------------------------------------------------------ Z
def section_z() -> None:
    m = Inst(OUT, "shinobi", 9631, "shinobi")
    try:
        z = P["shinobi"]["z80"]
        cpu, ram = z["cpu"], z["ram"]
        start, length = H(ram["address"]), H(ram["length"])
        st = attrib(m, "Z1 z80 write", cpu, "program", start, length, "write", 200)
        evs = m.events(st)
        mf = __import__("json").loads(Path(st["evidence"]["manifest_json"]).read_text())
        check("Z1 manifest states the semantics: logical address, access literal, no per-event bank", mf["parameters"]["watch"]["access"] == "write" and mf["bank_context"]["per_event"] == "unavailable"
              and "wpaddr" in mf["event_semantics"]["address"] and mf["instrumentation"][0]["kind"] == "watchpoints", watch=mf["parameters"]["watch"])
        attrib(m, "Z2 z80 read", cpu, "program", start, length, "read", 200)
        attrib(m, "Z3 z80 read_write", cpu, "program", start, length, "read_write", 300)
        st = m.rq("experiment_status", {}, mutating=False)
        both = {e["access"] for e in m.events(st)}
        check("Z3 read_write produced both access types in one ordered stream", both == {"read", "write"}, access_types=sorted(both))

        # limits: high-rate range, large limit -> captured + dropped == hits observed (dropped events are reported, never hidden)
        m.stop_debugger()
        cap = m.rq("capture_watchpoint", dict(cpu=cpu, space="program", address=start, length=length, access="write", max_events=10000, max_wall_seconds=60, registers=[], label="Z4 flood"))
        m.resume()
        st = m.poll_done(cap["experiment_id"], timeout=90)
        c = st["counts"]
        check("Z4 high event rate: counts are consistent (captured + dropped == hits observed) and cleanup is verified", c["events_captured"] + c["events_dropped"] == c["hits_observed"] and st["state"] in ("completed", "failed")
              and st["cleanup"]["verified"] is True, counts=c, state=st["state"], reason=st["completion_reason"], errors=st["errors"][:2])

        # timeout: a range nothing writes
        rom = z["rom_unwritten"]
        cap = m.rq("capture_watchpoint", dict(cpu=cpu, space="program", address=H(rom["address"]), length=H(rom["length"]), access="write", max_events=5, max_wall_seconds=2, registers=[], label="Z5 timeout"))
        t0 = time.time()
        st = m.poll_done(cap["experiment_id"], timeout=30)
        check("Z5 wall-clock limit ends an idle capture with zero events and a verified cleanup", st["completion_reason"] == "wall_clock_limit" and st["counts"]["events_captured"] == 0 and st["cleanup"]["verified"]
              and 1.5 < time.time() - t0 < 12, reason=st["completion_reason"], waited=round(time.time() - t0, 1))
        check("Z5 the watchpoint is really gone (independent wplist)", wplist(m, cpu, "program") == [], wplist=wplist(m, cpu, "program"))

        # cancel + ownership: a user watchpoint on the same space must survive
        # armed while debugger-stopped so nothing can execute (and nothing can reach the event limit) before the checks below
        m.stop_debugger()
        user = m.fx.call("user_wp", cpu=cpu, space="program", kind="w", address=H(rom["address"]), length=1, cond="", action="g")["index"]
        cap = m.rq("capture_watchpoint", dict(cpu=cpu, space="program", address=H(rom["address"]), length=H(rom["length"]), access="read_write", max_events=50, max_wall_seconds=60, registers=[], label="Z6 cancel"))
        both_listed = wplist(m, cpu, "program")
        check("Z6 read_write installs one MAME watchpoint per access type next to the user's", len(both_listed) == 3 and {w["type"] for w in both_listed} >= {"r", "w"}, listed=[(w["index"], w["type"]) for w in both_listed])
        owned = [w for w in m.info_()["cpus"][[c["tag"] for c in m.info_()["cpus"]].index(cpu)]["watchpoints"]["listed"] if w.get("owned_by_experiment")]
        check("Z6 session_info flags exactly the bridge's own watchpoints as owned", len(owned) == 2 and all(w["owned_by_experiment"] == cap["experiment_id"] for w in owned), owned=len(owned))
        cx = m.rq("cancel_experiment", {"experiment_id": cap["experiment_id"]})
        left = wplist(m, cpu, "program")
        check("Z6 cancel removes and verifies only the bridge's watchpoints; the user's watchpoint is untouched",
              cx["state"] == "cancelled" and cx["cleanup"]["verified"] and [w["index"] for w in left] == [user], state=cx["state"], left=[w["index"] for w in left])
        m.fx.call("user_wpclear", cpu=cpu, space="program", index=user)
        check("Z6 cancelling a stopped machine did not resume it", m.state() == "debugger_stopped", state=m.state())
        m.resume()

        # invalid arguments (direct, bypassing the client-side validator so the plugin's own checks are exercised)
        base = dict(cpu=cpu, space="program", address=start, length=16, access="write", max_events=5, max_wall_seconds=5, registers=[])
        cases = [
            ("space opcodes", {**base, "space": "opcodes"}, "bad_argument"), ("space cpu_space", {**base, "space": "cpu_space"}, "bad_argument"),
            ("space data (Z80 has none)", {**base, "space": "data"}, "unknown_space"), ("access", {**base, "access": "rw"}, "bad_argument"),
            ("length 0", {**base, "length": 0}, "bad_argument"), ("range past the mask", {**base, "address": 0xFFF8, "length": 16}, "out_of_range"),
            ("address past the mask", {**base, "address": 0x10000}, "out_of_range"), ("7 registers", {**base, "registers": ["PC", "SP", "A", "BC", "DE", "HL", "IX"]}, "too_large"),
            ("unknown register", {**base, "registers": ["NOPE"]}, "unknown_register"), ("unknown cpu", {**base, "cpu": ":nocpu"}, "unknown_cpu"),
            ("not a cpu", {**base, "cpu": P["shinobi"]["no_spaces_device"]}, "not_a_cpu"),
            ("max_events 0", {**base, "max_events": 0}, "bad_argument"), ("max_events 10001", {**base, "max_events": 10001}, "bad_argument"),
            ("max_wall 0", {**base, "max_wall_seconds": 0}, "bad_argument"), ("max_wall 601", {**base, "max_wall_seconds": 601}, "bad_argument"),
        ]
        got = {name: code(m.err("capture_watchpoint", args)) for name, args, _ in cases}
        check("Z7 invalid arguments are refused by the plugin with typed errors", all(got[n] == want for n, _, want in cases), got={n: g for n, g in got.items() if g != dict((c[0], c[2]) for c in cases)[n]} or "all as expected")
        info = m.info_()
        check("Z7 no refused request left anything installed or running", info.get("active_experiment") in (None, {}) and wplist(m, cpu, "program") == [], active=info.get("active_experiment"))
        # protocol client-side validator must refuse the same malformed input before any I/O
        from mame_mcp import protocol
        from mame_mcp.protocol import BridgeError
        refused = 0
        for bad in ({"space": "opcodes"}, {"access": "x"}, {"length": 0}, {"address": -1}, {"registers": list("ABCDEFG")}, {"max_events": 0}):
            try:
                protocol.watchpoint_args(**{**dict(cpu=":soundcpu", space="program", address=0, length=1, access="write", max_events=1, max_wall_seconds=1), **bad})
            except BridgeError:
                refused += 1
        check("Z7 the client-side validator refuses the same classes of input", refused == 6, refused=refused)
        # a second capture while one is active
        cap = m.rq("capture_watchpoint", dict(**base, label="Z8 first"))
        e2 = m.err("capture_watchpoint", {**base, "address": start + 0x20, "label": "Z8 second"})
        check("Z8 only one experiment may be active", code(e2) in ("experiment_active", "capture_active"), code=code(e2))
        m.rq("cancel_experiment", {"experiment_id": cap["experiment_id"]})
        note("Z finished", session=m.info_()["bridge"]["session_id"])
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ M
def section_m() -> None:
    m = Inst(OUT, "m1944", 9632, "1944")
    try:
        mm = P["1944"]["m68k"]
        cpu = mm["cpu"]
        b, w, r = mm["read_range"], mm["write_range"], mm["word_read_range"]   # 68000 on a big-endian 16-bit bus: byte lanes and words
        m.resume()
        time.sleep(mm["warmup_seconds"])   # these access patterns only exist after the game has been running for a while
        attrib(m, "M1 68000 reads with byte lanes", cpu, b["space"], H(b["address"]), H(b["length"]), "read", b["events"], expect_lanes=True)
        st = attrib(m, "M2 68000 writes with mixed byte and word accesses", cpu, w["space"], H(w["address"]), H(w["length"]), "write", w["events"], expect_lanes=True)
        attrib(m, "M2b 68000 full-word reads", cpu, r["space"], H(r["address"]), H(r["length"]), "read", r["events"])
        odd_writes = [int(e["address"], 16) for e in m.events(st) if int(e["address"], 16) & 1]
        check("M2 the capture reported at least one write to an odd (lower-lane) byte address, to use for the one-byte-range check", bool(odd_writes), n=len(odd_writes))
        start = odd_writes[0] - 1 if odd_writes else H(w["address"]) + 1
        nodbg = P["1944"]["no_debug_device"]
        e = m.err("capture_watchpoint", dict(cpu=nodbg, space="program", address=0, length=1, access="write", max_events=1, max_wall_seconds=1, registers=[]))
        check("M3 a device with address spaces but no debug interface is refused with a typed error", code(e) in ("no_debug_interface", "not_a_cpu"), code=code(e), message=(e.message if e else None))
        # address units: a 16-bit bus CPU still watches byte addresses (shift 0): watch ONE odd byte (`start`+1, taken from the
        # writes observed above), expect only accesses covering it
        m.stop_debugger()
        cap = m.rq("capture_watchpoint", dict(cpu=cpu, space="program", address=start + 1, length=1, access="write", max_events=20, max_wall_seconds=30, registers=[], label="M4 odd byte"))
        m.resume()
        st = m.poll_done(cap["experiment_id"], timeout=60)
        evs = m.events(st)
        check("M4 a one-byte range on an odd address only reports accesses that cover that byte (a byte write at it, or the word write containing it)",
              len(evs) > 0 and all(int(e["address"], 16) in (start, start + 1) for e in evs), n=len(evs), addresses=sorted({e["address"] for e in evs})[:4])
        note("M finished", session=m.info_()["bridge"]["session_id"])
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ I
def section_i() -> None:
    m = Inst(OUT, "install_fail", 9633, "shinobi", test_env={"MAMEMCP_TEST_FAIL_INSTALL_AT": "2"})
    try:
        z = P["shinobi"]["z80"]
        cpu = z["cpu"]
        m.stop_debugger()
        args = dict(cpu=cpu, space="program", address=H(z["ram"]["address"]), length=0x10, access="read_write", max_events=10, max_wall_seconds=10, registers=[])
        e = m.err("capture_watchpoint", args)
        rb = ((e.detail or {}).get("rollback") or {}) if e else {}
        check("I1 failed installation of the 2nd watchpoint: install_failed, the 1st is rolled back and verified removed", code(e) == "install_failed" and rb.get("all_removed") is True
              and [r["id"] for r in rb.get("removed", [])] == ["read"] and all(r["cleared"] for r in rb["removed"]), code=code(e), rollback=rb)
        check("I1 independent wplist: nothing of the failed attempt remains", wplist(m, cpu, "program") == [], wplist=wplist(m, cpu, "program"))
        check("I1 no experiment is active and the temporaries were released", m.info_().get("active_experiment") in (None, {}), active=m.info_().get("active_experiment"))
        ok = m.rq("capture_watchpoint", {**args, "access": "write"})
        check("I1 a later capture is accepted (a single watchpoint is location 1, not 2)", ok["state"] == "running" and ok["all_armed"] is True)
        m.rq("cancel_experiment", {"experiment_id": ok["experiment_id"]})
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ C
def section_c() -> None:
    m = Inst(OUT, "clear_fail", 9634, "shinobi", test_env={"MAMEMCP_TEST_FAIL_CLEAR": "1"})
    try:
        z = P["shinobi"]["z80"]
        cpu, rom = z["cpu"], z["rom_unwritten"]
        args = dict(cpu=cpu, space="program", address=H(rom["address"]), length=H(rom["length"]), access="write", max_events=5, max_wall_seconds=60, registers=[])
        cap = m.rq("capture_watchpoint", args)
        cx = m.rq("cancel_experiment", {"experiment_id": cap["experiment_id"]})
        cl = cx["cleanup"]
        check("C1 a clear that does not happen is reported: experiment failed, cleanup not verified, the watchpoint listed as remaining instrumentation",
              cx["state"] == "failed" and cl["breakpoint_cleared"] is False and any("wpclear failed" in x and "injected" in x for x in cl["errors"])
              and [(r["location"], r.get("kind"), r.get("space")) for r in cl["remaining_instrumentation"]] == [("write", "watchpoint", "program")] and "wpclear" in cl["recovery"], cleanup_errors=cl["errors"][:2])
        left = wplist(m, cpu, "program")
        check("C1 independent wplist confirms the watchpoint really is still installed", len(left) == 1 and left[0]["type"] == "w", left=left)
        e = m.err("capture_watchpoint", args)
        check("C2 a new capture is refused with unresolved_instrumentation naming the watchpoint", code(e) == "unresolved_instrumentation"
              and (e.detail or {}).get("remaining_instrumentation", [{}])[0].get("kind") == "watchpoint", code=code(e))
        e2 = m.err("capture_breakpoint", dict(cpu=cpu, address=0x7000, max_events=1, max_wall_seconds=1, registers=[], memory=[]))
        check("C2 capture_breakpoint is refused the same way (shared guard)", code(e2) == "unresolved_instrumentation", code=code(e2))
        m.fx.call("user_wpclear", cpu=cpu, space="program", index=left[0]["index"])
        ok = m.rq("capture_watchpoint", args)
        check("C3 after the user removes it, the next capture is accepted and reports what was recovered", ok["state"] == "running" and ok.get("recovered_instrumentation")
              and ok["recovered_instrumentation"][0].get("kind") == "watchpoint", recovered=ok.get("recovered_instrumentation"))
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ F
def section_f() -> None:
    """Whether instruction fetches reach a read watchpoint depends on the CPU core.
    Control: an execution breakpoint on the same instruction proves the code really runs while the watch is armed."""
    m = Inst(OUT, "fetch", 9635, "shinobi")
    try:
        m.resume()
        time.sleep(3)
        z, d = P["shinobi"]["z80"], P["shinobi"]["m68k"]
        cases = (("Z80", z["cpu"], H(z["disasm"]["address"]), 8, 40, 5), ("68000", d["cpu"], H(d["disasm"]["address"]), d["disasm"]["expected_lengths"][0], 2000, 8))
        for name, cpu, addr, length, max_events, wall in cases:
            b = m.rq("capture_breakpoint", dict(cpu=cpu, address=addr, max_events=10, max_wall_seconds=15, registers=[], label=f"{name} exec control"))
            sb = m.poll_done(b["experiment_id"], 30)
            w = m.rq("capture_watchpoint", dict(cpu=cpu, space="program", address=addr, length=length, access="read", max_events=max_events, max_wall_seconds=wall, registers=[], label=f"{name} fetch test"))
            sw = m.poll_done(w["experiment_id"], 60)
            ev = m.events(sw)
            fetches = [e for e in ev if addr <= H(e["pc"]) < addr + length and addr <= H(e["address"]) < addr + length]
            note(f"F {name} read watch over executing code: events={len(ev)} with pc inside the watched bytes={len(fetches)}",
                 sample=[(e["pc"], e["address"], e["data"]) for e in ev[:3]])
            check(f"F {name} control: the execution breakpoint at {hex(addr)} hit {sb['counts']['events_captured']} times, so the code runs while watched",
                  sb["state"] == "completed" and sb["counts"]["events_captured"] >= 10 and sw["state"] == "completed" and sw["cleanup"]["verified"] is True)
            if name == "Z80":
                check("F Z80: opcode/operand fetches ARE reported as reads (events whose pc is the instruction being executed, inside the watched bytes)",
                      len(fetches) >= 4 and all(e["access"] == "read" for e in fetches), fetches=len(fetches), events=len(ev))
            else:
                check("F 68000: fetches of the executing handler are NOT reported (many data-read events by other instructions, none with pc inside the handler)",
                      len(ev) >= 50 and not fetches, fetches=len(fetches), events=len(ev))
    finally:
        m.close()


def main() -> int:
    want = set(sys.argv[1:]) or {"Z", "M", "I", "C", "F"}
    for name, fn in (("Z", section_z), ("M", section_m), ("I", section_i), ("C", section_c), ("F", section_f)):
        if name in want:
            print(f"\n=== section {name}", flush=True)
            try:
                fn()
            except Exception as e:  # a harness failure is a failure, recorded and continued
                import traceback
                check(f"{name} section completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-800:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
