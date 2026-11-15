"""Hardening validation against a real MAME 0.289 + Shinobi (one isolated instance, test fixture plugin loaded).

Sections:  T  debugger temporary ownership/collision/cleanup
           E  capture execution-state matrix (running / paused / debugger-stopped x complete / cancel / timeout)
           Z  second CPU (:soundcpu Z80) compared with MAME's own debugger at the same stop
           R  memory-read semantics (side-effect read taps)
Game/CPU specific addresses come from tests/fixtures/profiles/shinobi.json.
Writes runs/hardening/results.json (PASS / FAIL / INFO per check; INFO = observation, not judged).
Usage:  .venv\\Scripts\\python -I scripts\\validate_hardening.py [--sections T,E,Z,R]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Fixture, Results  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.bridge import BridgeClient  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT, launch  # noqa: E402
from mame_mcp.protocol import BridgeError, capture_args, read_memory_args, read_registers_args  # noqa: E402

OUT = PROJECT_ROOT / "runs" / "hardening"
PORT = 9441
PROFILE = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "shinobi.json").read_text())
MAIN, SND = PROFILE["maincpu"], PROFILE["soundcpu"]
H = lambda s: int(s, 16)  # noqa: E731
HANDLER = H(MAIN["handler_pc"])
R = Results("hardening")
check, note = R.check, R.note


# ---------------------------------------------------------------------------------------------- helpers
def wait_for(pred, timeout=30.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


class Ctx:
    def __init__(self, c: BridgeClient, fx: Fixture):
        self.c, self.fx = c, fx

    def state(self) -> str:
        return self.c.session_info()["execution"]["state"]

    def wait_state(self, want: str, timeout=30.0):
        return wait_for(lambda: self.state() == want, timeout)

    def temps(self) -> list[int]:
        return self.fx.call("temps")["temps"]

    def user_bps(self) -> list[dict]:
        return self.fx.call("bplist")["breakpoints"]

    def bridge_owned_bps(self) -> list[dict]:
        out = []
        for d in self.c.session_info()["cpus"]:
            bps = d.get("breakpoints")
            if isinstance(bps, list):
                out += [b for b in bps if b.get("owned_by_experiment")]
        return out

    def running(self):
        self.fx.call("user_resume")
        assert self.wait_state("running", 40), "could not reach running"

    def put_in(self, which: str):
        self.running()
        if which == "paused":
            self.fx.call("user_pause")
            assert self.wait_state("paused", 20), "could not pause"
        elif which == "stopped":
            self.fx.call("user_stop")
            assert self.wait_state("debugger_stopped", 20), "could not debugger-stop"

    def clean_slate(self):
        try:
            st = self.c.experiment_status()
            if st["state"] == "running":
                self.c.cancel_experiment(st["experiment_id"])
        except BridgeError:
            pass
        for b in self.user_bps():
            self.fx.call("user_bpclear", cpu=b["cpu"], index=b["index"])
        for n in range(10):
            self.fx.call("set_temp", n=n, value=0)
        self.running()

    def capture(self, cpu=MAIN["tag"], address=HANDLER, events=5, wall=20, regs=None, mem=None, label="hardening"):
        return self.c.capture_breakpoint(capture_args(cpu, address, events, wall, regs, mem, label))

    def wait_done(self, exp_id: str, timeout=40.0):
        def done():
            st = self.c.experiment_status(exp_id)
            return st if st["state"] != "running" else None
        return wait_for(done, timeout, 0.25)

    def wait_events(self, exp_id: str, n: int, timeout=30.0):
        return wait_for(lambda: self.c.experiment_status(exp_id)["counts"]["events_captured"] >= n, timeout, 0.1)

    def manifest(self, st: dict) -> dict:
        return json.loads(Path(st["evidence"]["manifest_json"]).read_text())

    def events(self, st: dict) -> list[dict]:
        return [json.loads(x) for x in Path(st["evidence"]["events_jsonl"]).read_text().splitlines() if x.strip()]

    def clean_after(self, label: str):
        t = self.temps()
        check(f"{label}: all debugger temporaries back to 0", all(v == 0 for v in t), temps=t)
        check(f"{label}: no bridge-owned breakpoint left", not self.bridge_owned_bps(), owned=self.bridge_owned_bps())


# ---------------------------------------------------------------------------------------------- T
def section_temps(x: Ctx):
    c, fx = x.c, x.fx
    note("SECTION T: debugger temporary ownership", policy="borrow temps that are 0, unreferenced by any breakpoint and unclaimed; verify and release; never overwrite")
    x.clean_slate()
    base = x.temps()
    check("T1 baseline: all temps 0 before any capture", all(v == 0 for v in base), temps=base)
    cap = x.capture(regs=["D0", "SR"], events=5)
    dt = cap["debugger_temporaries"]
    check("T1 capture reports the temps it borrowed (counter + 2 registers)", dt["counter"].startswith("temp") and len(dt["registers"]) == 2, borrowed=dt)
    st = x.wait_done(cap["experiment_id"])
    check("T1 completes by event limit", st and st["completion_reason"] == "event_limit", summary=st and st["summary"])
    cl = st["cleanup"]["debug_state"]
    check("T1 cleanup verified: undisturbed, reset to zero, verified", cl["undisturbed"] and cl["reset_to_zero"] and cl["verified"], debug_state=cl)
    x.clean_after("T1")

    # T2 collision: the user owns temp9 and temp8 (non-zero)
    fx.call("set_temp", n=9, value=5)
    fx.call("set_temp", n=8, value=7)
    cap = x.capture(regs=["D0"], events=3)
    chosen = [cap["debugger_temporaries"]["counter"]] + list(cap["debugger_temporaries"]["registers"].values())
    check("T2 capture skips non-zero user temps (temp9, temp8)", "temp9" not in chosen and "temp8" not in chosen, chosen=chosen)
    sel = [r for r in x.manifest(x.c.experiment_status(cap["experiment_id"]))["instrumentation"][0]["debugger_temporaries"]["selection_report"]]
    check("T2 manifest records why each temp was or was not available", any(s["temp"] == 9 and not s["available"] for s in sel), selection=sel[:3])
    st = x.wait_done(cap["experiment_id"])
    t = x.temps()
    check("T2 user's temp9/temp8 never overwritten (5 and 7 still set)", t[9] == 5 and t[8] == 7, temps=t)
    check("T2 capture completed normally", st["state"] == "completed", summary=st["summary"])
    for n in (8, 9):
        fx.call("set_temp", n=n, value=0)

    # T3 a user breakpoint references temp6 in its condition (bp never hit): temp6 must be avoided
    fx.call("user_bp", cpu=MAIN["tag"], address=H(MAIN["never_hit_pc"]), cond="temp6==0", action="temp5=5")
    fx.call("set_temp", n=9, value=1)  # force the allocator down to temp8 so temp6 is reachable with 4+ regs
    cap = x.capture(regs=["D0", "D1", "A0"], events=2)
    chosen = [cap["debugger_temporaries"]["counter"]] + list(cap["debugger_temporaries"]["registers"].values())
    check("T3 temps referenced by a user breakpoint (temp6, temp5) are avoided", "temp6" not in chosen and "temp5" not in chosen, chosen=chosen)
    x.wait_done(cap["experiment_id"])
    fx.call("set_temp", n=9, value=0)
    for b in x.user_bps():
        fx.call("user_bpclear", cpu=b["cpu"], index=b["index"])

    # T4 exhaustion: every temp in use -> refused, nothing installed, nothing touched
    for n in range(10):
        fx.call("set_temp", n=n, value=n + 1)
    before_bps = x.user_bps()
    try:
        x.capture(regs=["D0"])
        check("T4 all temps in use: capture refused", False, got="no error")
    except BridgeError as e:
        check("T4 all temps in use: capture refused with debug_state_unavailable", e.code == "debug_state_unavailable", code=e.code, message=e.message[:120])
    t = x.temps()
    check("T4 refusal leaves user temps and breakpoints untouched", t == [n + 1 for n in range(10)] and x.user_bps() == before_bps and not x.bridge_owned_bps(), temps=t)
    for n in range(10):
        fx.call("set_temp", n=n, value=0)

    # T5 external interference: lower the hit counter while the experiment is armed, then cancel (machine paused)
    x.running()
    cap = x.capture(regs=["D0"], events=10000, wall=60)
    counter = int(cap["debugger_temporaries"]["counter"][4:])
    x.wait_events(cap["experiment_id"], 3)
    fx.call("user_pause")
    x.wait_state("paused", 20)
    fx.call("set_temp", n=counter, value=1)
    res = x.c.cancel_experiment(cap["experiment_id"])
    ds = (res.get("cleanup") or {}).get("debug_state", {})
    check("T5 interference detected: cleanup not verified, run reported failed (explicit failure, not silent)",
          res["state"] == "failed" and ds.get("undisturbed") is False and ds.get("verified") is False and ds.get("reset_to_zero") is False,
          state=res["state"], debug_state=ds, errors=res["errors"][:2])
    check("T5 disturbed counter left untouched by cleanup (still 1)", x.temps()[counter] == 1, counter=f"temp{counter}", value=x.temps()[counter])
    check("T5 breakpoint still removed and verified despite the temp disturbance", res["cleanup"]["breakpoint_cleared"] and res["cleanup"]["verified"], cleanup={k: res["cleanup"][k] for k in ("breakpoint_cleared", "verified")})
    check("T5 cleanup did not resume emulation (still paused)", x.state() == "paused" and res["final_execution"]["state"] == "paused", final=res["final_execution"])
    t = x.temps()
    note("T5 NOTE (limitation): a disturbed temp is reported only if the counter ended BELOW the hit count the bridge saw; "
         "an external write that raises it is indistinguishable from hits lost from the console log", temps_after=t)
    x.clean_slate()

    # T6 error path: user removes the experiment's own breakpoint behind its back
    cap = x.capture(regs=["D0"], events=10000, wall=2.0)
    x.wait_events(cap["experiment_id"], 2)
    idx = cap["breakpoint_index"]
    fx.call("user_bpclear", cpu=MAIN["tag"], index=idx)
    st = x.wait_done(cap["experiment_id"], 15)
    check("T6 instrumentation removed externally: experiment still ends (wall clock) and cleanup reports", st is not None and st["completion_reason"] == "wall_clock_limit",
          state=st and st["state"], reason=st and st["completion_reason"], cleanup=st and st["cleanup"])
    x.clean_after("T6")
    x.clean_slate()


# ---------------------------------------------------------------------------------------------- E
def section_matrix(x: Ctx):
    c, fx = x.c, x.fx
    note("SECTION E: capture execution-state matrix",
         policy="capture_breakpoint/cancel/timeout never change execution state; only the breakpoint action 'g' after a recorded hit resumes")
    for start in ("running", "paused", "stopped"):
        label = f"E[{start}]"
        want = {"running": "running", "paused": "paused", "stopped": "debugger_stopped"}[start]
        x.clean_slate()
        x.put_in(start)
        cap = x.capture(events=3, wall=1.5, label=f"matrix-{start}-timeout")
        check(f"{label} capture accepted without changing execution state", cap["execution"]["state"] == want and x.state() == want, at_start=cap["execution"], policy=cap["execution_policy"][:60])
        if start == "running":
            # at ~60 Hz this fills 3 events almost at once: use a never-hit address for the timeout case
            x.wait_done(cap["experiment_id"])
            cap = x.capture(address=H(MAIN["never_hit_pc"]), events=3, wall=1.5, label="matrix-running-timeout")
        st = x.wait_done(cap["experiment_id"])
        ok_state = (st["final_execution"]["state"] == want) if start != "running" else st["final_execution"]["state"] == "running"
        check(f"{label} wall-clock timeout: reason, zero events, final state reported accurately and unchanged",
              st["completion_reason"] == "wall_clock_limit" and st["counts"]["events_captured"] == 0 and ok_state and x.state() == want,
              reason=st["completion_reason"], counts=st["counts"], final=st["final_execution"], now=x.state())
        check(f"{label} timeout cleanup verified", st["cleanup"]["breakpoint_cleared"] and st["cleanup"]["verified"] and st["cleanup"]["debug_state"]["verified"], cleanup=st["cleanup"])
        x.clean_after(f"{label} timeout")

        cap = x.capture(events=10000, wall=30, regs=["D0"], label=f"matrix-{start}-cancel")
        if start == "running":
            x.wait_events(cap["experiment_id"], 2)
        res = c.cancel_experiment(cap["experiment_id"])
        check(f"{label} cancel: state/reason, cleanup verified, execution state NOT changed",
              res["state"] == "cancelled" and res["cleanup"]["verified"] and res["final_execution"]["state"] == want and x.state() == want,
              state=res["state"], final=res["final_execution"], now=x.state(), counts=res["counts"])
        x.clean_after(f"{label} cancel")

        if start != "running":
            cap = x.capture(events=3, wall=40, regs=["D0"], label=f"matrix-{start}-resume")
            time.sleep(0.7)
            idle = c.experiment_status(cap["experiment_id"])
            check(f"{label} armed capture records nothing while not running", idle["counts"]["hits_observed"] == 0 and x.state() == want, counts=idle["counts"])
            c.execution_control("resume")
            st = x.wait_done(cap["experiment_id"])
            check(f"{label} after explicit resume the capture completes by event limit",
                  st["completion_reason"] == "event_limit" and st["counts"]["events_captured"] == 3, summary=st["summary"])
            check(f"{label} completion leaves emulation running (the action's 'g' after the last hit, documented)", st["final_execution"]["state"] == "running", final=st["final_execution"])
            x.clean_after(f"{label} resume")

    # user stop / pause DURING a running capture
    for kind, want in (("user_stop", "debugger_stopped"), ("user_pause", "paused")):
        label = f"E[during {kind}]"
        x.clean_slate()
        cap = x.capture(events=10000, wall=2.5, regs=["D0"], label=f"matrix-{kind}")
        x.wait_events(cap["experiment_id"], 2)
        fx.call(kind)
        x.wait_state(want, 20)
        n0 = c.experiment_status(cap["experiment_id"])["counts"]["hits_observed"]
        time.sleep(0.5)
        mid = c.experiment_status(cap["experiment_id"])
        check(f"{label} experiment keeps running, no hits while user holds the stop", mid["state"] == "running" and mid["counts"]["hits_observed"] == n0 and x.state() == want,
              hits=[n0, mid["counts"]["hits_observed"]], state=x.state())
        st = x.wait_done(cap["experiment_id"])
        mani = x.manifest(st)
        check(f"{label} wall-clock end preserves the user's state and reports it; transition recorded in manifest",
              st["completion_reason"] == "wall_clock_limit" and st["final_execution"]["state"] == want and x.state() == want
              and any(t["to"] == want for t in mani["execution_transitions"]),
              final=st["final_execution"], transitions=mani["execution_transitions"])
        x.clean_after(label)

    # race: pause requested while hits are arriving must survive (the action's 'g' must not undo it)
    survived, trials = 0, 10
    for i in range(trials):
        x.clean_slate()
        cap = x.capture(events=10000, wall=20, regs=["D0"], label="matrix-pause-race")
        x.wait_events(cap["experiment_id"], 2)
        fx.call("user_pause")
        time.sleep(0.4)
        held = x.state() == "paused"
        time.sleep(0.4)
        held = held and x.state() == "paused"
        c.cancel_experiment(cap["experiment_id"])
        held = held and x.state() == "paused"  # cancel/cleanup must not resume either
        survived += 1 if held else 0
    check(f"E[race] a user pause issued while hits arrive survives, also through cancel ({trials} trials)", survived == trials, survived=survived, trials=trials)
    x.clean_slate()

    # same-address refusal
    fx.call("user_bp", cpu=MAIN["tag"], address=H(MAIN["never_hit_pc"]), cond="temp0==0", action="temp0=1")
    bps = x.user_bps()
    t0 = x.temps()
    try:
        x.capture(address=H(MAIN["never_hit_pc"]))
        check("E[address] capture at an address that already has a user breakpoint refused", False, got="no error")
    except BridgeError as e:
        check("E[address] capture at an address that already has a user breakpoint refused (address_in_use)", e.code == "address_in_use", code=e.code, message=e.message[:140])
    check("E[address] refusal leaves breakpoints and temps untouched", x.user_bps() == bps and x.temps() == t0)
    x.clean_slate()


# ---------------------------------------------------------------------------------------------- Z
def section_z80(x: Ctx):
    c, fx = x.c, x.fx
    note("SECTION Z: second CPU (:soundcpu Z80) vs MAME's own debugger at the same stop", profile=SND)
    x.clean_slate()
    hot = H(SND["hot_pc"])
    lo = H(SND["loop_range"][0])
    fx.call("user_bp", cpu=SND["tag"], address=hot, cond="temp0==0", action='temp0=1;printf "NEG|%X",pc')
    ok = x.wait_state("debugger_stopped", 60)
    sreg = c.read_registers(read_registers_args(SND["tag"], SND["registers"]))
    got = {r["name"]: int(r["hex"], 16) for r in sreg["registers"]}
    check("Z1 user breakpoint on :soundcpu stops MAME; Z80 PC == breakpoint address", bool(ok) and got["PC"] == hot, state=x.state(), pc=hex(got["PC"]))

    # negative control: an UNQUALIFIED register in a breakpoint action
    neg = [ln for ln in fx.call("console_tail", n=10)["lines"] if ln.startswith("NEG|")]
    main_pc = {r["name"]: int(r["hex"], 16) for r in c.read_registers(read_registers_args(MAIN["tag"], ["CURPC"]))["registers"]}["CURPC"]
    printed = int(neg[-1].split("|")[1], 16) if neg else None
    note("Z1 NEGATIVE CONTROL (observation): a breakpoint ACTION that prints an unqualified 'pc' on a :soundcpu breakpoint",
         printed_pc=printed and hex(printed), z80_pc=hex(got["PC"]), maincpu_pc_now=hex(main_pc),
         interpretation=("action saw the Z80's PC" if printed == got["PC"] else
                         "action did NOT see the Z80's PC: unqualified registers in an action are unreliable for a non-visible CPU (value looks like a 68000 PC)"),
         design_consequence="captures read registers in the CONDITION (compiled in the breakpoint device's own symbol table), never unqualified in the action")

    names = SND["registers"]
    fmt = "|".join(["%X"] * len(names))
    sym = ",".join(SND["debugger_symbols"][n] for n in names)
    ref = fx.call("debugger_printf", cpu=SND["tag"], text=f'"ZREF|{fmt}",{sym}')["line"]
    refv = [int(v, 16) for v in ref.split("|")[1:]]
    check("Z2 read_registers(:soundcpu) == MAME debugger printf at the same stop (PC, SP, A, BC, HL, AF)", refv == [got[n] for n in names],
          debugger=dict(zip(names, map(hex, refv))), bridge={n: hex(got[n]) for n in names})

    # memory: explicit CPU / space
    mem_s = c.read_memory(read_memory_args(SND["tag"], "program", lo, 6))
    mem_m = c.read_memory(read_memory_args(MAIN["tag"], "program", lo, 6))
    z_dbg = fx.call("debugger_printf", cpu=SND["tag"], text='"ZMEM|%X|%X|%X|%X|%X|%X",' + ",".join(f"b@{lo + i:X}" for i in range(6)))["line"]
    m_dbg = fx.call("debugger_printf", cpu=MAIN["tag"], text='"MMEM|%X|%X|%X|%X|%X|%X",' + ",".join(f"b@{lo + i:X}" for i in range(6)))["line"]
    zb = bytes(int(v, 16) for v in z_dbg.split("|")[1:]).hex()
    mb = bytes(int(v, 16) for v in m_dbg.split("|")[1:]).hex()
    check("Z3 read_memory(:soundcpu program) == that CPU's own debugger bytes", mem_s["data_hex"] == zb, bridge=mem_s["data_hex"], debugger=zb)
    check("Z3 read_memory(:maincpu program) at the same address == the 68000's debugger bytes", mem_m["data_hex"] == mb, bridge=mem_m["data_hex"], debugger=mb)
    check("Z3 the two CPUs' memory differs at that address (explicit CPU qualification is real)", mem_s["data_hex"] != mem_m["data_hex"], z80=mem_s["data_hex"], m68k=mem_m["data_hex"])
    d_s = c.read_memory({**read_memory_args(SND["tag"], "program", lo, 6), "mode": "direct"})
    check("Z3 direct mode on :soundcpu program ROM equals inspect mode", d_s["data_hex"] == mem_s["data_hex"], direct=d_s["data_hex"])
    try:
        c.read_memory({**read_memory_args(SND["tag"], "io", 0, 1), "mode": "direct"})
        check("Z3 direct mode refused for io space", False, got="no error")
    except BridgeError as e:
        check("Z3 direct mode refused for io space", e.code == "unsupported_space", code=e.code)
    z0 = fx.call("debugger_printf", cpu=SND["tag"], text='"B0|%X",b@0')["line"]
    m0 = fx.call("debugger_printf", cpu=MAIN["tag"], text='"B0|%X",b@0')["line"]
    note("Z3 address 0: reset byte per CPU from the debugger", z80=z0, m68k=m0)

    # capture on the Z80, armed while the machine is stopped at the same address, then resumed
    w_dbg = fx.call("debugger_printf", cpu=SND["tag"], text=f'"W|%X",w@{lo:X}')["line"]
    w_ref = int(w_dbg.split("|")[1], 16)
    b0_ref = int(z0.split("|")[1], 16)
    for b in x.user_bps():
        fx.call("user_bpclear", cpu=b["cpu"], index=b["index"])
    fx.call("set_temp", n=0, value=0)
    capregs = SND["capture_registers"]
    cap = x.capture(cpu=SND["tag"], address=hot, events=3, wall=40, regs=capregs,
                    mem=[{"space": "program", "address": lo, "size": 1}, {"space": "program", "address": 0, "size": 1},
                         {"space": "program", "address": lo, "size": 2}], label="z80-capture")
    check("Z4 capture on :soundcpu armed while debugger-stopped on that CPU; execution unchanged", x.state() == "debugger_stopped" and cap["execution"]["state"] == "debugger_stopped")
    c.execution_control("resume")
    st = x.wait_done(cap["experiment_id"])
    ev = x.events(st)
    check("Z4 capture completes by event limit with 3 events", st["completion_reason"] == "event_limit" and len(ev) == 3, summary=st["summary"])
    pcs = {e["regs"]["PC"] for e in ev}
    check("Z4 every event's PC register == the breakpoint address (condition-time register read of the Z80)", pcs == {hex(hot).upper().replace("0X", "0x")}, pcs=sorted(pcs))
    m = ev[0]["mem"]
    check("Z4 memory fields use the requested CPU: b@loop, b@0, w@loop equal :soundcpu debugger values",
          [int(f["value"], 16) for f in m] == [int(zb[:2], 16), b0_ref, w_ref], captured=[f["value"] for f in m],
          debugger=[hex(int(zb[:2], 16)), hex(b0_ref), hex(w_ref)])
    check("Z4 captured b@0 is the Z80's byte, not the 68000's", int(m[1]["value"], 16) != int(m0.split("|")[1], 16), z80_b0=m[1]["value"], m68k_b0=m0)
    mani = x.manifest(st)
    check("Z4 manifest records the Z80 cpu context and borrowed temps", mani["parameters"]["cpu"] == SND["tag"] and mani["cleanup"]["debug_state"]["verified"], cpu_context=mani["cpu_context"]["tag"])
    note("Z4 INFO (not judged): the earlier-stop debugger registers and the capture's registers were taken at different times and are NOT compared; "
         "HL is only constant while the idle loop runs and changes when the sound CPU processes a command (see Z5)",
         earlier_stop={n: hex(got[n]) for n in capregs}, first_event={n: ev[0]["regs"][n] for n in capregs})
    x.clean_after("Z4")
    x.clean_slate()
    z5_hit_time_registers(x, hot, capregs)


def z5_hit_time_registers(x: Ctx, hot: int, capregs: list[str]):
    """Captured registers vs an independent observation of the SAME hit (same occurrence), not of an earlier stop.

    The capture breakpoint sits on `hot`. A separate user breakpoint (the observer) sits on the next instruction. After the hit
    on `hot` executes `bit 0,a` (CB 47) the very next stop is at the observer, i.e. in the same loop iteration, and MAME's own
    debugger then reads the Z80 registers. The capture's per-hit counter (global event sequence) read at that stop names the hit
    that was just executed, so we compare that event's registers with the observer. `bit 0,a` leaves SP, BC and HL unchanged
    (it only writes F), so those three are comparable; PC differs by the instruction length and A/AF are not compared."""
    c, fx = x.c, x.fx
    obs = H(SND["observer_pc"])
    preserved = SND["observer_preserved_registers"]
    sym = SND["debugger_symbols"]
    ibytes = c.read_memory(read_memory_args(SND["tag"], "program", hot, obs - hot + 2))["data_hex"]
    check("Z5 premise: the instruction at the hit address is `bit 0,a` (CB 47) and the observer address is the next instruction (reads from ROM)",
          obs - hot == 2 and ibytes[:4].lower() == SND["hit_instruction_bytes"].lower(), bytes_at_hit_and_next=ibytes)
    x.clean_slate()
    fx.call("user_stop")
    assert x.wait_state("debugger_stopped", 20), "could not debugger-stop"
    cap = x.capture(cpu=SND["tag"], address=hot, events=SND["observer_event_limit"], wall=120, regs=capregs, label="z80-hit-time")
    exp = cap["experiment_id"]
    ctemp = cap["debugger_temporaries"]["counter"]          # e.g. "temp9": the capture's global hit counter
    targets = SND["observer_hit_counts"]
    # The observer stops the machine on the instruction after the capture address only when the capture's hit counter equals a chosen
    # value, so each observation is of a known, reproducible occurrence (hit number K), not of whatever happens to be running.
    idx = fx.call("user_bp", cpu=SND["tag"], address=obs, cond=f"{ctemp}==#{targets[0]}")["index"]
    observed = []
    for n, k in enumerate(targets):
        c.execution_control("resume")
        stopped = x.wait_state("debugger_stopped", 60)
        temps = x.temps()
        fmt = "|".join(["%X"] * len(preserved) + ["%X"])
        text = f'"Z5|{fmt}",' + ",".join(sym[r] for r in preserved + ["PC"])
        line = fx.call("debugger_printf", cpu=SND["tag"], text=text)["line"]
        vals = [int(v, 16) for v in line.split("|")[1:]]
        observed.append({"stopped": bool(stopped), "target": k, "temps": temps, **dict(zip(preserved + ["PC"], vals))})
        fx.call("user_bpclear", cpu=SND["tag"], index=idx)
        if n + 1 < len(targets):
            idx = fx.call("user_bp", cpu=SND["tag"], address=obs, cond=f"{ctemp}==#{targets[n + 1]}")["index"]
    c.execution_control("resume")
    c.cancel_experiment(exp)
    st = x.wait_done(exp)
    ev = {e["seq"]: e for e in x.events(st)}
    mani = x.manifest(st)
    counter = int(ctemp.replace("temp", ""))
    check("Z5 every observer round stopped on the Z80 at the observer address", all(o["stopped"] and o["PC"] == obs for o in observed),
          observed=[{"pc": hex(o["PC"]), "stopped": o["stopped"]} for o in observed])
    seqs = [o["temps"][counter] for o in observed]
    check("Z5 the hit counter read at each stop equals the requested hit number, and that hit exists in the captured events",
          seqs == targets and all(k in ev for k in seqs), counter_temp=ctemp, requested=targets, counter_at_stop=seqs, captured_events=len(ev))
    for o, k in zip(observed, seqs):
        e = ev.get(k)
        if e is None:
            check(f"Z5 hit {k}: captured registers equal the observer's reading of the same hit", False, reason="event missing")
            continue
        cap_regs = {r: int(e["regs"][r], 16) for r in preserved + ["PC"]}
        check(f"Z5 hit {k}: captured SP/BC/HL equal MAME's debugger reading of the same hit one instruction later; PC == hit address",
              all(cap_regs[r] == o[r] for r in preserved) and cap_regs["PC"] == hot,
              captured={r: hex(v) for r, v in cap_regs.items()}, observer={r: hex(o[r]) for r in preserved + ["PC"]}, counter=k)
    check("Z5 capture cancelled cleanly", st["state"] == "cancelled", state=st["state"])
    x.clean_after("Z5")


# ---------------------------------------------------------------------------------------------- R
def section_reads(x: Ctx):
    c, fx = x.c, x.fx
    tap = PROFILE["read_tap"]
    a, b = H(tap["start"]), H(tap["end"])
    note("SECTION R: memory-read semantics via read taps that record machine.side_effects_disabled()", tap=tap)
    x.clean_slate()
    fx.call("tap_install", cpu=tap["cpu"], space=tap["space"], start=a, **{"end": b})
    fx.call("tap_take")
    time.sleep(1.0)
    base = fx.call("tap_take")
    check("R0 control: tap sees no traffic from normal execution (so counts below are ours)", base["side_effects_disabled_calls"] == 0 and base["side_effects_enabled_calls"] == 0, tap=base)
    res = c.read_memory(read_memory_args(tap["cpu"], tap["space"], a, 4))
    t = fx.call("tap_take")
    check("R1 inspect mode: handler reached with side effects DISABLED, none enabled", t["side_effects_disabled_calls"] > 0 and t["side_effects_enabled_calls"] == 0, tap=t)
    check("R1 inspect result states mode, semantics and the limits of the guarantee", res["read_mode"] == "inspect" and "honour" in res["side_effects"] and "Not proven" in res["side_effects"], read_mode=res["read_mode"], side_effects=res["side_effects"][:90])
    d = c.read_memory({**read_memory_args(tap["cpu"], tap["space"], a, 4), "mode": "direct"})
    t = fx.call("tap_take")
    check("R2 direct mode: no handler invoked at all (tap sees nothing)", t["side_effects_disabled_calls"] == 0 and t["side_effects_enabled_calls"] == 0, tap=t)
    check("R2 direct mode over a tapped (handler-backed) range returns zeros: the documented limitation, demonstrated",
          d["data_hex"] == "00000000" and res["data_hex"] != "00000000", direct=d["data_hex"], inspect=res["data_hex"], limitation=d["limitations"][:70])
    di = c.read_memory(read_memory_args(MAIN["tag"], "program", HANDLER, 8))
    dd = c.read_memory({**read_memory_args(MAIN["tag"], "program", HANDLER, 8), "mode": "direct"})
    check("R2 direct == inspect over plain ROM (no handler involved)", di["data_hex"] == dd["data_hex"] and di["data_hex"] != "00" * 8, inspect=di["data_hex"], direct=dd["data_hex"])
    for path in ("read_u8", "readv_u8"):
        fx.call("read_normal", cpu=tap["cpu"], space=tap["space"], address=a, path=path)
        t = fx.call("tap_take")
        check(f"R3 control: plain Lua space:{path} is a NORMAL read (side effects enabled) - why the bridge does not use it", t["side_effects_enabled_calls"] > 0 and t["side_effects_disabled_calls"] == 0, tap=t)
    # capture memory fields (the '@' operator)
    cap = x.capture(events=3, wall=20, regs=["D0"], mem=[{"space": "program", "address": a, "size": 4}], label="read-tap")
    st = x.wait_done(cap["experiment_id"])
    t = fx.call("tap_take")
    ev = x.events(st)
    check("R4 capture memory fields read with side effects disabled ('@' operator)", t["side_effects_disabled_calls"] > 0 and t["side_effects_enabled_calls"] == 0 and st["state"] == "completed", tap=t)
    check("R4 captured memory value == inspect read of the same bytes", all(int(e["mem"][0]["value"], 16) == int(res["data_hex"], 16) for e in ev), captured=ev[0]["mem"][0]["value"], inspect=res["data_hex"])
    note("R5 UNTESTED: I/O reads with real device side effects (e.g. a latch-clearing port) were not exercised; 'inspect' is source-supported "
         "(symbol_table::memory_value disable_se, express.cpp) and tap-demonstrated for ROM, not proven for any specific device")
    x.clean_slate()


# ---------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default="T,E,Z,R")
    a = ap.parse_args()
    want = set(a.sections.split(","))
    proc, info = launch(port=PORT, run_dir=OUT / "run", fixture=True)
    wake = winwake.waker_for(proc.pid)
    fx = Fixture(info["fixture_dir"], wake=wake)
    c = BridgeClient(port=PORT, default_timeout=30, wake=wake)
    try:
        si = c.connect(timeout=60)
        check("environment: MAME 0.289, shinobi, fixture plugin answering", si["mame_version"] == "0.289" and si["machine"]["name"] == "shinobi" and fx.call("ping")["pong"],
              mame=si["mame_version"], romset=info["romset_zip"]["sha1"], verify=info["verifyroms"]["output"][:60])
        x = Ctx(c, fx)
        for name, fn in (("T", section_temps), ("E", section_matrix), ("Z", section_z80), ("R", section_reads)):
            if name in want:
                try:
                    fn(x)
                except BridgeError as e:
                    check(f"section {name}: UNEXPECTED bridge error", False, error=e.to_dict())
                except Exception:
                    import traceback
                    check(f"section {name}: UNEXPECTED harness exception", False, error=traceback.format_exc()[-700:])
        note("transport", timeouts=c.timeouts, read_only_retries=c.read_retries, stale_discarded=c.stale_discarded, wake_posts=c.wake_posts, events=c.transport_events[-10:])
    finally:
        c.close(graceful=False)
        proc.kill()
    return R.save(OUT / "results.json", mame=info["mame_version"], profile=PROFILE)


if __name__ == "__main__":
    sys.exit(main())
