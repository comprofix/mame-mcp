"""Multi-address capture validation against real MAME 0.289 + 1942 (Revision B), :maincpu (Z80), fixture plugin loaded.

Independent expectations come from the TEST-ONLY fixture: user breakpoints at the instruction AFTER each location print
a line per hit (MAME's own debugger), so location attribution, counts and global ordering are compared with an observer
that does not use the bridge. Addresses (Z80 frame handler etc.) come from tests/fixtures/profiles/1942.json plus the
constants below (all observed in the earlier bank research).

  0x11  per-frame RST 10h handler (PUSH DE), next instruction 0x12      ~60 hits/s   "hot"
  0x08  RST 08h handler (PUSH HL), next 0x09                           ~45 hits/s
  0x38  RST 38h (DI), next 0x39                                          rare (a few per second at most)
  0x98  frame-wait spin loop                                             extremely hot
  0x166 service-mode-only path                                           never hit in ordinary running

Sections: M1 two locations + attribution/order, M2 hot+rare+unhit, M3 global limit without overrun, M4 cancel,
M5 wall-clock timeout, M6 invalid requests / unrelated breakpoints, M7 partial-installation failure (second MAME with
MAMEMCP_TEST_FAIL_INSTALL_AT=2), M8 single-address compatibility, M9 temp ownership.
Usage:  .venv\\Scripts\\python -I scripts\\validate_multi.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Fixture, Results  # noqa: E402
from validate_hardening import Ctx, wait_for  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.bridge import BridgeClient  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT, launch  # noqa: E402
from mame_mcp.protocol import MAX_LOCATIONS, BridgeError, capture_args  # noqa: E402

OUT = PROJECT_ROOT / "runs" / "multi"
PORT, PORT_F, PORT_S, PORT_C, PORT_D = 9457, 9459, 9463, 9464, 9465
PROFILE_1942 = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "1942.json").read_text())["maincpu"]["unequal_rates"]
PROFILE_SHIN = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "shinobi.json").read_text())["maincpu"]
H = lambda v: int(v, 16)  # noqa: E731
ROMS = Path(r"D:\arcade\ROMs\MAME 0.289 ROMs (merged)")
CPU = ":maincpu"
HOT, IRQ8, RARE, SPIN, NEVER = 0x11, 0x08, 0x38, 0x98, 0x166
R = Results("multi")
check, note = R.check, R.note


def mem_fields():  # noqa
    return [{"space": "program", "address": 0x0, "size": 1}]


class Env:
    def __init__(self, c: BridgeClient, fx: Fixture):
        self.c, self.fx, self.x = c, fx, Ctx(c, fx)

    def stop(self) -> int:
        """Debugger-stop (it lands at PC 0x10, away from every location used here); returns the stop PC."""
        self.x.running()
        self.fx.call("user_stop")
        self.x.wait_state("debugger_stopped", 20)
        return int(self.fx.call("debugger_printf", cpu=CPU, text='"%X",pc')["line"], 16)

    def observers(self, pairs: dict[str, int]) -> list[int]:
        """User breakpoint at each address that prints '<tag>|%X' (pc) and continues. Returns their indexes."""
        return [self.fx.call("user_bp", cpu=CPU, address=a, action=f'printf "{tag}|%X",pc;g')["index"] for tag, a in pairs.items()]

    def clear(self, idxs: list[int]) -> None:
        for i in idxs:
            self.fx.call("user_bpclear", cpu=CPU, index=i)

    def lines(self, marker: str, tags: set[str]) -> list[str]:
        ls = self.fx.call("console_tail", n=5000)["lines"]
        if marker in ls:
            ls = ls[len(ls) - 1 - ls[::-1].index(marker) + 1:]
        return [l.split("|")[0] for l in ls if l.split("|")[0] in tags]

    def capture(self, locations, events, wall, regs=("PC",), label="multi", mem=True):
        a = capture_args(CPU, None, events, wall, list(regs), mem_fields() if mem else [], label, locations=locations)
        return self.c.capture_breakpoint(a)

    def status(self, exp_id: str) -> dict:
        return self.c.experiment_status(exp_id)


def ev_locs(events: list[dict]) -> list[str]:
    return [e["location"] for e in events]


# ------------------------------------------------------------------------------------------ M1
def m1(e: Env):
    x, fx = e.x, e.fx
    note("M1: two locations in one experiment; attribution and global order against independent observers")
    x.clean_slate()
    stop_pc = e.stop()
    fx.call("debugger_printf", cpu=CPU, text='"MARK|M1"')
    obs = e.observers({"OBF": HOT + 1, "OBI": IRQ8 + 1})
    cap = e.capture([{"id": "frame", "address": HOT}, {"id": "irq8", "address": IRQ8}], 30, 30, regs=("PC", "HL"))
    check("M1 install response lists both locations armed and says all_armed", cap["all_armed"] and [l["armed"] for l in cap["locations"]] == [True, True] and x.state() == "debugger_stopped",
          locations=cap["locations"], stop_pc=hex(stop_pc), execution_state=x.state())
    e.c.execution_control("resume")
    st = x.wait_done(cap["experiment_id"])
    e.clear(obs)
    ev = x.events(st)
    want = {"OBF": "frame", "OBI": "irq8"}
    seen = [want[t] for t in e.lines("MARK|M1", set(want))][:len(ev)]
    check("M1 completed by the global event limit with exactly 30 events", st["completion_reason"] == "event_limit" and len(ev) == 30 and st["counts"]["events_captured"] == 30, summary=st["summary"])
    check("M1 sequence numbers are one global 1..30 sequence across both locations", [v["seq"] for v in ev] == list(range(1, 31)))
    check("M1 each event names its location, that location's logical address and a hit-time pc equal to it",
          all((v["location"], v["address"]) in (("frame", "0x11"), ("irq8", "0x8")) and v["pc"] == v["address"] and "captured" in v.get("pc_source", "") for v in ev),
          sample=ev[:2])
    check("M1 both locations fired in the one experiment", set(ev_locs(ev)) == {"frame", "irq8"}, counts={k: ev_locs(ev).count(k) for k in ("frame", "irq8")})
    check("M1 location order of events == order of the independent observers' lines (global ordering and attribution)", ev_locs(ev) == seen, events=ev_locs(ev)[:12], observer=seen[:12])
    pl = {l["id"]: l["events_captured"] for l in st["locations"]}
    check("M1 per-location counts in status equal counts from the events file, and sum to the total",
          pl == {k: ev_locs(ev).count(k) for k in ("frame", "irq8")} and sum(pl.values()) == st["counts"]["events_captured"], per_location=pl, total=st["counts"])
    mani = x.manifest(st)
    check("M1 manifest records configured locations, ids, installation outcome, per-location counts and cleanup",
          [l["id"] for l in mani["parameters"]["locations"]] == ["frame", "irq8"] and mani["installation"]["all_armed"] is True
          and {r["id"]: r["events_captured"] for r in mani["per_location"]} == pl and all(r["cleared"] for r in mani["cleanup"]["locations"]) and mani["cleanup"]["breakpoint_cleared"],
          installation=mani["installation"]["locations"], cleanup=mani["cleanup"]["locations"])
    check("M1 manifest keeps unavailable per-event bank context and harvest-time timestamps", mani["bank_context"]["per_event"] == "unavailable" and "HARVEST" in mani["time_semantics"] and "harvest_frame" in ev[0])
    note("M1 evidence", events=st["evidence"]["events_jsonl"], manifest=st["evidence"]["manifest_json"])
    x.clean_after("M1")


# ------------------------------------------------------------------------------------------ M2
def m2(e: Env):
    x, fx = e.x, e.fx
    note("M2: firing locations beside a location that never fires (armed, no hits)")
    x.clean_slate()
    e.stop()
    fx.call("debugger_printf", cpu=CPU, text='"MARK|M2"')
    obs = e.observers({"OBH": HOT + 1, "OBR": IRQ8 + 1})
    cap = e.capture([{"id": "hot", "address": HOT}, {"id": "rare", "address": IRQ8}, {"id": "never", "address": NEVER}], 600, 40)
    e.c.execution_control("resume")
    st = x.wait_done(cap["experiment_id"], 60)
    e.clear(obs)
    ev = x.events(st)
    want = {"OBH": "hot", "OBR": "rare"}
    seen = [want[t] for t in e.lines("MARK|M2", set(want))][:len(ev)]
    counts = {k: ev_locs(ev).count(k) for k in ("hot", "rare", "never")}
    check("M2 two firing locations (0x11 and 0x08, observed at equal per-frame rates, so NO rate skew is demonstrated here) plus an armed location that never fires (0 events)", counts["hot"] >= counts["rare"] >= 1 and counts["never"] == 0, counts=counts, total=len(ev))
    check("M2 observer order and per-location attribution agree with the events", ev_locs(ev) == seen, observer_counts={k: seen.count(k) for k in ("hot", "rare")})
    loc = {l["id"]: l for l in st["locations"]}
    check("M2 'no hits observed' (armed, 0 events) is distinguished from never armed: the unhit location is armed with no_hits_observed, breakpoint index recorded",
          loc["never"]["armed"] is True and loc["never"]["no_hits_observed"] is True and loc["never"]["breakpoint_index"] is not None and loc["hot"]["no_hits_observed"] is False, locations=st["locations"])
    check("M2 the status response stays bounded (<= 8 recent events, compact location table)", len(st["recent_events"]) <= 5 and len(json.dumps(st)) < 12000, bytes=len(json.dumps(st)))
    note("M2 evidence", events=st["evidence"]["events_jsonl"])
    x.clean_after("M2")


# ------------------------------------------------------------------------------------------ M3
def m3(e: Env):
    x, fx = e.x, e.fx
    note("M3: one global event limit, no overrun, with an extremely hot location (0x98 frame-wait loop)")
    x.clean_slate()
    cap = e.capture([{"id": "spin", "address": SPIN}, {"id": "frame", "address": HOT}], 25, 30, regs=("PC",))
    st = x.wait_done(cap["experiment_id"])
    time.sleep(2.0)
    st2 = e.status(cap["experiment_id"])
    ev = x.events(st2)
    m = x.manifest(st2)
    check("M3 exactly 25 events (limit), none dropped, hits observed == 25", st2["counts"]["events_captured"] == 25 and st2["counts"]["hits_observed"] == 25 and st2["counts"]["events_dropped"] == 0 and len(ev) == 25 and st2["completion_reason"] == "event_limit", counts=st2["counts"])
    check("M3 no overrun: the shared hit counter was 25 at release and the events file did not grow in the 2 s after completion",
          m["cleanup"]["debug_state"]["counter_value_at_release"] == 25 and len(ev) == 25 and m["cleanup"]["debug_state"]["undisturbed"], counter=m["cleanup"]["debug_state"]["counter_value_at_release"])
    check("M3 both breakpoints removed after completion", x.bridge_owned_bps() == [] and m["cleanup"]["breakpoint_cleared"], per_location=st2["locations"])
    x.clean_after("M3")


# ------------------------------------------------------------------------------------------ M4 / M5
def m4(e: Env):
    x, fx = e.x, e.fx
    note("M4: cancellation of a multi-location experiment")
    x.clean_slate()
    fx.call("set_visible_cpu", cpu=CPU)
    user = fx.call("user_bp", cpu=CPU, address=0x7000, cond="temp0==0", action="temp0=1")["index"]
    before = x.user_bps()
    cap = e.capture([{"id": "frame", "address": HOT}, {"id": "irq8", "address": IRQ8}, {"id": "never", "address": NEVER}], 10000, 120)
    ok = x.wait_events(cap["experiment_id"], 3, 30)
    owned_running = x.bridge_owned_bps()
    st = e.c.cancel_experiment(cap["experiment_id"])
    m = x.manifest(st)
    after = x.user_bps()
    check("M4 three bridge-owned breakpoints existed while running", bool(ok) and len(owned_running) == 3, owned=[b["address"] for b in owned_running])
    check("M4 cancel: state cancelled, reason cancelled, every location's breakpoint cleared and verified", st["state"] == "cancelled" and st["completion_reason"] == "cancelled" and all(r["cleared"] for r in m["cleanup"]["locations"]) and not x.bridge_owned_bps(), cleanup=m["cleanup"]["locations"])
    check("M4 the user's unrelated breakpoint is untouched (same index, address, condition, action)", before == after and len(after) == 1, before=before, after=after)
    check("M4 execution state preserved (still running, not paused or stopped by the cancel)", x.state() == "running", state=x.state())
    fx.call("user_bpclear", cpu=CPU, index=user)
    fx.call("set_temp", n=0, value=0)
    x.clean_after("M4")


def m5(e: Env):
    x = e.x
    note("M5: wall-clock timeout with locations that never fire")
    x.clean_slate()
    t0 = time.time()
    cap = e.capture([{"id": "svc", "address": NEVER}, {"id": "win", "address": 0xA000}], 100, 3)
    st = x.wait_done(cap["experiment_id"], 30)
    took = time.time() - t0
    m = x.manifest(st)
    check("M5 completed by wall_clock_limit with 0 events after about 3 s; both locations armed and never hit", st["completion_reason"] == "wall_clock_limit" and st["counts"]["events_captured"] == 0 and 2.5 <= took < 15
          and all(l["armed"] and l["no_hits_observed"] for l in st["locations"]), seconds=round(took, 1), locations=st["locations"])
    check("M5 breakpoints removed, temps released", m["cleanup"]["breakpoint_cleared"] and not x.bridge_owned_bps())
    x.clean_after("M5")


# ------------------------------------------------------------------------------------------ M6
def m6(e: Env):
    x, fx, c = e.x, e.fx, e.c
    note("M6: invalid requests are rejected before anything is installed; unrelated breakpoints preserved")
    x.clean_slate()
    user = fx.call("user_bp", cpu=CPU, address=0x7000, cond="temp0==0", action="temp0=1")["index"]
    base = {"cpu": CPU, "max_events": 5, "max_wall_seconds": 5, "registers": [], "memory": []}
    L = lambda *a: [{"address": v} for v in a]  # noqa: E731
    cases = {
        "duplicate address": {"locations": [{"address": 0x11, "id": "a"}, {"address": 0x11, "id": "b"}]},
        "duplicate id": {"locations": [{"address": 0x11, "id": "a"}, {"address": 0x12, "id": "a"}]},
        f"more than {MAX_LOCATIONS} locations": {"locations": L(*range(0x100, 0x100 + MAX_LOCATIONS + 1))},
        "empty list": {"locations": []},
        "bad id": {"locations": [{"address": 0x11, "id": "bad id|x"}]},
        "address beyond the program space": {"locations": [{"address": 0x11}, {"address": 0x10000}]},
        "both address and locations": {"address": 0x11, "locations": L(0x12)},
        "neither": {},
        "collides with a user breakpoint": {"locations": L(0x11, 0x7000)},
    }
    bad = []
    for name, extra in cases.items():
        before = (x.user_bps(), x.bridge_owned_bps(), x.temps())
        try:
            c.request("capture_breakpoint", {**base, **extra}, mutating=True)
            bad.append((name, "accepted"))
            continue
        except BridgeError as err:
            code = err.code
        after = (x.user_bps(), x.bridge_owned_bps(), x.temps())
        if before != after:
            bad.append((name, "state changed"))
        if code not in ("bad_argument", "out_of_range", "address_in_use"):
            bad.append((name, code))
    check("M6 every invalid request (plugin-side validation, client validation bypassed) is rejected with an error and changes no breakpoint or temp", not bad, cases=len(cases), problems=bad)
    for name, extra in (("duplicate address", cases["duplicate address"]), ("duplicate id", cases["duplicate id"]), ("too many", cases[f"more than {MAX_LOCATIONS} locations"])):
        try:
            capture_args(CPU, extra.get("address"), 5, 5, [], [], None, extra.get("locations"))
            bad.append(("client", name))
        except BridgeError:
            pass
    check("M6 the client-side validation rejects duplicates and oversize lists too", not [b for b in bad if b[0] == "client"])
    cap = e.capture([{"id": "frame", "address": HOT}, {"id": "irq8", "address": IRQ8}], 6, 20)
    st = x.wait_done(cap["experiment_id"])
    check("M6 a valid capture beside the user's breakpoint completes and the user's breakpoint is unchanged",
          st["state"] == "completed" and [b["address"] for b in x.user_bps()] == [0x7000] and x.user_bps()[0]["condition"] == "temp0==0" and x.user_bps()[0]["action"] == "temp0=1", user=x.user_bps())
    fx.call("user_bpclear", cpu=CPU, index=user)
    fx.call("set_temp", n=0, value=0)
    x.clean_after("M6")


# ------------------------------------------------------------------------------------------ M7
def m7(e: Env):
    x, fx, c = e.x, e.fx, e.c
    note("M7: partial installation failure on a MAME started with MAMEMCP_TEST_FAIL_INSTALL_AT=2 (test-only fault injection)")
    x.clean_slate()
    user = fx.call("user_bp", cpu=CPU, address=0x7000, cond="temp0==0", action="temp0=1")["index"]
    ub, tb = x.user_bps(), x.temps()
    try:
        e.capture([{"id": "first", "address": HOT}, {"id": "second", "address": IRQ8}, {"id": "third", "address": RARE}], 10, 10)
        check("M7 install failure is reported", False, got="no error")
        return
    except BridgeError as err:
        d = err.detail or {}
        inst = {r["id"]: r for r in d.get("installation", [])}
        check("M7 error install_failed; per-location outcome: first armed then rolled back, second failed with a reason, third not attempted",
              err.code == "install_failed" and inst["first"]["armed"] and not inst["second"]["armed"] and "injected" in inst["second"]["error"] and inst["third"].get("not_attempted"), code=err.code, installation=list(inst.values()))
        rb = d.get("rollback", {})
        check("M7 rollback removed and verified everything this attempt installed (no cleanup failure)", rb.get("all_removed") is True and [r["id"] for r in rb["removed"]] == ["first"] and rb["removed"][0]["cleared"], rollback=rb)
    check("M7 afterwards: no bridge-owned breakpoint, user breakpoint unchanged, temps unchanged, no active experiment",
          not x.bridge_owned_bps() and x.user_bps() == ub and x.temps() == tb and e.c.session_info()["experiment"].get("active") in (None, False, {}) if "experiment" in e.c.session_info() else not x.bridge_owned_bps() and x.user_bps() == ub and x.temps() == tb,
          user=x.user_bps(), temps=x.temps())
    mani_dirs = sorted(Path(e.evidence_dir).glob("exp-*"))
    failed = json.loads((mani_dirs[-1] / "manifest.json").read_text())
    check("M7 the failed attempt's manifest records status failed, the installation outcome and the rollback", failed["status"] == "failed" and failed["installation"]["all_armed"] is False and failed["installation"]["rollback"]["all_removed"], installation=failed["installation"]["locations"])
    st = c.capture_breakpoint(capture_args(CPU, HOT, 3, 10, ["PC"], [], "after-failure"))
    st2 = x.wait_done(st["experiment_id"])
    check("M7 a following single-address capture is not blocked and completes (state fully restored)", st2["state"] == "completed" and st2["counts"]["events_captured"] == 3, summary=st2["summary"])
    fx.call("user_bpclear", cpu=CPU, index=user)
    fx.call("set_temp", n=0, value=0)
    x.clean_after("M7")
    note("M7 UNTESTED: a failure of the rollback itself (bpclear failing) cannot be injected; the code reports it per location (rollback.removed[].error, all_removed=false) but that path was not exercised")


# ------------------------------------------------------------------------------------------ M8 / M9
def m8(e: Env):
    x = e.x
    note("M8: existing single-address behaviour (legacy `address` form)")
    x.clean_slate()
    cap = e.c.capture_breakpoint(capture_args(CPU, HOT, 4, 20, ["PC", "HL"], mem_fields(), "single"))
    st = x.wait_done(cap["experiment_id"])
    ev = x.events(st)
    m = x.manifest(st)
    check("M8 single address: completes with 4 events; events keep seq/pc/regs/mem and gain location L1 + address; pc is the breakpoint address",
          st["state"] == "completed" and len(ev) == 4 and all(v["location"] == "L1" and v["address"] == "0x11" and v["pc"] == "0x11" and set(v["regs"]) == {"PC", "HL"} and len(v["mem"]) == 1 for v in ev), first=ev[0])
    check("M8 single address: legacy response/manifest fields kept (breakpoint_index, parameters.address, counts)", cap["breakpoint_index"] is not None and m["parameters"]["address"] == "0x11" and m["counts"]["events_captured"] == 4)
    t = m["instrumentation"][0]["debugger_temporaries"]
    check("M8 single address: temp use unchanged (1 counter + 2 registers, no extra pc temp)", sorted(t["registers"]) == ["HL", "PC"] and "(hit-time pc)" not in t["registers"], temps=t)
    x.clean_after("M8")


def m9(e: Env):
    x = e.x
    note("M9: temp ownership with many locations: temps do not multiply with the number of locations")
    x.clean_slate()
    sets = {}
    for n, addrs in ((2, [HOT, IRQ8]), (8, [HOT, IRQ8, RARE, NEVER, 0xA000, 0xA010, 0xA020, 0xA030])):
        cap = e.capture([{"address": a} for a in addrs], 3, 4, regs=("PC", "HL"))
        st = x.wait_done(cap["experiment_id"], 20)
        t = x.manifest(st)["instrumentation"][0]["debugger_temporaries"]
        sets[n] = (t["counter"], sorted(t["registers"].items()), st["state"])
        x.clean_after(f"M9[{n} locations]")
    used = {n: 1 + len(v[1]) for n, v in sets.items()}
    check("M9 2 and 8 locations use the same number of temps: 1 counter + 2 registers + 1 hit-time pc = 4", used[2] == used[8] == 4, used=used, sets={str(k): v for k, v in sets.items()})
    check("M9 8 locations completed (limit or timeout), counter verified and released", all(v[2] == "completed" for v in sets.values()))


# ------------------------------------------------------------------------------------------ M0
def m0(e: Env, ambient_hook_was_set: bool):
    note("M0: test-only fault-injection hooks are off by default and cannot leak in from the ambient environment")
    si = e.c.session_info()
    launch_json = json.loads((Path(e.run_dir) / "launch.json").read_text())
    others = "".join(p.read_text(errors="replace") for p in (PROJECT_ROOT / "src" / "mame_mcp").glob("*.py") if p.name != "launcher.py")
    check("M0 normal launch: session_info.test_hooks is empty and launch.json test_env is empty, although the harness process environment had MAMEMCP_TEST_FAIL_CLEAR=1 set",
          ambient_hook_was_set and si["test_hooks"] == [] and launch_json["test_env"] == {} and "MAMEMCP_TEST" not in " ".join(launch_json["command"]),
          test_hooks=si["test_hooks"], ambient_hook_was_set=ambient_hook_was_set)
    check("M0 no module of the server package other than the launcher mentions MAMEMCP_TEST_ (the hooks live only in the Lua plugin; the launcher strips ambient values and sets them only from an explicit test_env)", "MAMEMCP_TEST" not in others)


# ------------------------------------------------------------------------------------------ M2b
def m2b(e: Env):
    x, fx = e.x, e.fx
    hot, rare = H(PROFILE_1942["hot"]["address"]), H(PROFILE_1942["rare"]["address"])
    ohot, orare = H(PROFILE_1942["hot"]["observer_address"]), H(PROFILE_1942["rare"]["observer_address"])
    note("M2b: unequal firing rates: hot 0x11 (about 60/s) beside less frequent non-zero 0x2BA2; independent observers at the next instruction of each")
    for name, limit, wall in (("small-limit", 30, 20), ("large-limit", 900, 45)):
        x.clean_slate()
        e.stop()
        mark = f"MARK|M2b{name}"
        fx.call("debugger_printf", cpu=CPU, text=f'"{mark}"')
        obs = e.observers({"OBH": ohot, "OBR": orare})
        cap = e.capture([{"id": "hot", "address": hot}, {"id": "rare", "address": rare}, {"id": "never", "address": NEVER}], limit, wall, regs=("PC",))
        e.c.execution_control("resume")
        st = x.wait_done(cap["experiment_id"], wall + 20)
        e.clear(obs)
        ev = x.events(st)
        m = {"OBH": "hot", "OBR": "rare"}
        seen_all = [m[t] for t in e.lines(mark, set(m))]
        seen = seen_all[:len(ev)]
        c_ev = {k: ev_locs(ev).count(k) for k in ("hot", "rare", "never")}
        c_ob = {k: seen.count(k) for k in ("hot", "rare")}
        note(f"M2b[{name}] outcome", limit=limit, completion_reason=st["completion_reason"], events=len(ev), per_location_events=c_ev,
             observer_lines_in_whole_run=len({k for k in seen_all}) and {k: seen_all.count(k) for k in ("hot", "rare")},
             rare_fired_before_limit=c_ev["rare"] > 0,
             statement="the global event limit ended collection %s the less frequent location fired; the bridge gives no fairness or coverage guarantee between locations" % ("AFTER" if c_ev["rare"] > 0 else "BEFORE"))
        check(f"M2b[{name}] events (location sequence and per-location counts) equal the independent observers' first {len(ev)} lines; global seq 1..{len(ev)}",
              ev_locs(ev) == seen and c_ev["hot"] == c_ob["hot"] and c_ev["rare"] == c_ob["rare"] and [v["seq"] for v in ev] == list(range(1, len(ev) + 1)), events=ev_locs(ev)[:10], observer=seen[:10])
        check(f"M2b[{name}] the never-hit location is armed with 0 events and reported as no_hits_observed (separate coverage)",
              c_ev["never"] == 0 and next(l for l in st["locations"] if l["id"] == "never")["no_hits_observed"] and st["state"] == "completed", per_location=st["locations"])
        if name == "large-limit":
            check("M2b[large-limit] unequal rates shown: the hot location fired more than the less frequent location, which fired at least once (the observers agree)",
                  c_ev["hot"] > c_ev["rare"] >= 1 and c_ob["hot"] > c_ob["rare"] >= 1, events=c_ev, observer=c_ob)
        x.clean_after(f"M2b[{name}]")


# ------------------------------------------------------------------------------------------ M10
def m10(e: Env):
    x, fx = e.x, e.fx
    ml = PROFILE_SHIN["multi_location"]
    locs = [{"id": l["id"], "address": H(l["address"])} for l in ml["locations"]]
    regs = ml["registers"]
    note("M10: multi-location capture on Shinobi :maincpu (68000): attribution, hit-time PC (curpc), registers, sequence, limit, cleanup vs MAME's own debugger")
    x.clean_slate()
    stop_pc = e.stop()
    mark = "MARK|M10"
    fx.call("debugger_printf", cpu=CPU, text=f'"{mark}"')
    fmt = 'printf "%s|%%X|%%X|%%X|%%X|%%X|%%X|%%X",pc,d0,d1,a0,a1,sp,sr;g'
    obs_idx, obs_tag = [], {}
    for l in ml["locations"]:
        tag = "OB_" + l["id"]
        obs_tag[tag] = l["id"]
        obs_idx.append(fx.call("user_bp", cpu=CPU, address=H(l["observer_address"]), action=fmt % tag)["index"])
    cap = e.capture(locs, 40, 30, regs=regs, mem=False, label="m10-shinobi")
    check("M10 install: 2 locations armed, execution state unchanged (debugger-stopped)", cap["all_armed"] and x.state() == "debugger_stopped", locations=cap["locations"], stop_pc=hex(stop_pc))
    e.c.execution_control("resume")
    st = x.wait_done(cap["experiment_id"], 40)
    e.clear(obs_idx)
    ev = x.events(st)
    m = x.manifest(st)
    rows = [l.split("|") for l in e.fx.call("console_tail", n=6000)["lines"]]
    start = max((i for i, l in enumerate(e.fx.call("console_tail", n=6000)["lines"]) if l == mark), default=-1)
    ob = [(obs_tag[r[0]], [int(v, 16) for v in r[1:]]) for r in rows[start + 1:] if r[0] in obs_tag and len(r) == 8]
    # align: the stop may have landed inside the handler; drop observer lines before the first one matching the first event's location
    skip = next((i for i, (loc, _) in enumerate(ob) if loc == ev[0]["location"]), 0)
    ob = ob[skip:][:len(ev)]
    check("M10 40 events exactly (global limit), one global sequence, completed by event_limit, 0 dropped",
          st["completion_reason"] == "event_limit" and len(ev) == 40 and [v["seq"] for v in ev] == list(range(1, 41)) and st["counts"]["events_dropped"] == 0, counts=st["counts"])
    check("M10 attribution: event location order equals the order of MAME's own debugger observers (<= 2 leading observer lines skipped for alignment)",
          ev_locs(ev) == [loc for loc, _ in ob] and skip <= 2, events=ev_locs(ev)[:8], observer=[loc for loc, _ in ob][:8], skipped=skip)
    check("M10 hit-time PC via curpc equals the location's logical address on every event", all(v["pc"] == v["address"] and "curpc" in v["pc_source"] for v in ev) and {v["address"] for v in ev} == {"0x404", "0x2870"}, sample=ev[:2])
    same = {l["id"]: l["observer_state_identical"] for l in ml["locations"]}
    mism = []
    for v, (loc, vals) in zip(ev, ob):
        names = ["pc", "D0", "D1", "A0", "A1", "SP", "SR"]
        for nme, got in zip(names[1:], vals[1:]):
            if nme in same[loc] and int(v["regs"][nme], 16) != got:
                mism.append((v["seq"], loc, nme, v["regs"][nme], hex(got)))
    check("M10 selected registers (D0 D1 A0 A1 SP SR) equal MAME's own debugger values at the neighbouring instruction (SR excluded where tst.b intervenes)", not mism and len(ob) == len(ev), mismatches=mism[:5])
    check("M10 per-location counts (status) equal events-file counts and sum to 40", {l["id"]: l["events_captured"] for l in st["locations"]} == {k: ev_locs(ev).count(k) for k in ("irq4", "body")} and sum(l["events_captured"] for l in st["locations"]) == 40, per_location=st["locations"])
    t = m["instrumentation"][0]["debugger_temporaries"]
    check("M10 cleanup: both breakpoints cleared and verified; 8 temps borrowed (1 counter + 6 registers + hit-time pc) and released; counter 40 at release",
          m["cleanup"]["breakpoint_cleared"] and m["cleanup"]["debug_state"]["undisturbed"] and m["cleanup"]["debug_state"]["counter_value_at_release"] == 40 and "(hit-time pc)" in t["registers"] and len(t["registers"]) == 7, temps=t["registers"])
    x.clean_after("M10")


# ------------------------------------------------------------------------------------------ M11 / M12
def leftover(x, what):
    return [b for b in x.user_bps() if "MCP|" in (b.get("action") or "")]


def m11(e: Env):
    x, fx, c = e.x, e.fx, e.c
    note("M11: bpclear failure during finalize (TEST-ONLY MAMEMCP_TEST_FAIL_CLEAR=2: the bridge's clear of location 2 is suppressed, the breakpoint really stays installed)")
    x.clean_slate()
    si = c.session_info()
    check("M11 hook visible in session_info.test_hooks", si["test_hooks"] == ["MAMEMCP_TEST_FAIL_CLEAR=2"], hooks=si["test_hooks"])
    cap = e.capture([{"id": "a", "address": HOT}, {"id": "b", "address": IRQ8}, {"id": "c", "address": NEVER}], 10, 20, regs=("PC", "HL"))
    st = x.wait_done(cap["experiment_id"])
    m = x.manifest(st)
    cl = m["cleanup"]
    left = leftover(x, "after")
    check("M11 the experiment ends FAILED (not completed): cleanup not clean, location 2 not cleared, locations 1 and 3 cleared",
          st["state"] == "failed" and st["completion_reason"] == "event_limit" and cl["breakpoint_cleared"] is False and [r["cleared"] for r in cl["locations"]] == [True, False, True], state=st["state"], cleanup=cl["locations"], errors=cl["errors"])
    check("M11 the error is explicit and the remaining instrumentation is identified (location, address, breakpoint index) with a recovery text",
          any("bpclear failed" in er and "injected" in er for er in cl["errors"]) and [(r["location"], r["address"]) for r in cl["remaining_instrumentation"]] == [("b", "0x8")] and "bpclear" in cl["recovery"], remaining=cl["remaining_instrumentation"])
    check("M11 MAME's own breakpoint list confirms exactly that breakpoint is still installed",
          [b["address"] for b in left] == [IRQ8] and left[0]["index"] == cl["remaining_instrumentation"][0]["breakpoint_index"], mame_breakpoints=left)
    check("M11 the temporaries are NOT reset or released and the manifest says so (no clean-state claim)", cl["debug_state"]["retained"] is True and cl["debug_state"]["verified"] is False and any(v != 0 for v in x.temps()), temps=x.temps(), debug_state=cl["debug_state"])
    si = c.session_info()
    check("M11 session_info lists the unresolved instrumentation", [r["location"] for r in si["unresolved_instrumentation"]] == ["b"], unresolved=si["unresolved_instrumentation"])
    before = x.user_bps()
    refused = []
    for kind in ("single", "multi"):
        try:
            if kind == "single":
                c.capture_breakpoint(capture_args(CPU, RARE, 3, 5, ["PC"], [], "over-orphan"))
            else:
                e.capture([{"address": RARE}, {"address": 0xA000}], 3, 5)
            refused.append((kind, "accepted"))
        except BridgeError as err:
            if err.code != "unresolved_instrumentation" or not err.detail.get("remaining_instrumentation"):
                refused.append((kind, err.code))
    check("M11 a new capture (single and multi) is refused with unresolved_instrumentation while the leftover exists (the bridge retries the clear itself; the hook suppresses that retry too); nothing new is installed",
          not refused and x.user_bps() == before, refused=refused)
    # documented recovery: remove the listed breakpoint in MAME's debugger, then retry (the bridge re-checks by itself)
    fx.call("user_bpclear", cpu=CPU, index=left[0]["index"])
    cap2 = c.capture_breakpoint(capture_args(CPU, HOT, 3, 20, ["PC"], [], "after-recovery"))
    st2 = x.wait_done(cap2["experiment_id"])
    check("M11 recovery works: after bpclear in the debugger the next capture is accepted, reports what it recovered, and completes cleanly",
          [r["location"] for r in cap2["recovered_instrumentation"]] == ["b"] and st2["state"] == "completed" and st2["counts"]["events_captured"] == 3, recovered=cap2["recovered_instrumentation"], summary=st2["summary"])
    check("M11 after recovery: session_info has no unresolved instrumentation", c.session_info()["unresolved_instrumentation"] == [])
    x.clean_after("M11 after recovery")


def m12(e: Env):
    x, fx, c = e.x, e.fx, e.c
    note("M12: rollback failure after a partial install (TEST-ONLY MAMEMCP_TEST_FAIL_INSTALL_AT=2 + MAMEMCP_TEST_FAIL_CLEAR=1)")
    x.clean_slate()
    try:
        e.capture([{"id": "first", "address": HOT}, {"id": "second", "address": IRQ8}], 5, 10)
        check("M12 install failure reported", False, got="no error")
        return
    except BridgeError as err:
        rb = (err.detail or {}).get("rollback", {})
        check("M12 error install_failed says CLEANUP FAILED; rollback.all_removed false; the location that could not be removed is identified; recovery text given",
              err.code == "install_failed" and "CLEANUP FAILED" in err.message and rb.get("all_removed") is False and [r["id"] for r in rb["removed"] if not r["cleared"]] == ["first"] and "injected" in rb["removed"][0]["error"] and "bpclear" in rb["recovery"], rollback=rb)
    left = leftover(x, "after")
    check("M12 MAME's own breakpoint list confirms the first location's breakpoint is still installed", [b["address"] for b in left] == [HOT], mame_breakpoints=left)
    mani = json.loads(sorted(Path(e.evidence_dir).glob("exp-*"))[-1].joinpath("manifest.json").read_text())
    check("M12 the failed manifest records the rollback failure", mani["status"] == "failed" and mani["installation"]["rollback"]["all_removed"] is False, installation=mani["installation"]["rollback"])
    si = c.session_info()
    check("M12 session_info lists it as unresolved", [r["location"] for r in si["unresolved_instrumentation"]] == ["first"], unresolved=si["unresolved_instrumentation"])
    try:
        c.capture_breakpoint(capture_args(CPU, RARE, 3, 5, ["PC"], [], "over-orphan"))
        check("M12 a new capture is refused while the leftover exists", False, got="accepted")
    except BridgeError as err:
        check("M12 a new capture is refused with unresolved_instrumentation (the bridge's own retry of the clear is also suppressed by the hook)", err.code == "unresolved_instrumentation" and err.detail["remaining_instrumentation"][0]["location"] == "first", code=err.code)
    check("M12 nothing new was installed by the refused request", [b["address"] for b in leftover(x, "x")] == [HOT])
    fx.call("user_bpclear", cpu=CPU, index=left[0]["index"])
    try:
        c.request("capture_breakpoint", {"cpu": CPU, "max_events": 5, "max_wall_seconds": 5}, mutating=True)
    except BridgeError as err:
        check("M12 recovery: after bpclear in the debugger the unresolved-instrumentation guard no longer blocks (an invalid request now fails on its own arguments)", err.code == "bad_argument", code=err.code)
    check("M12 recovery: unresolved list empty, retained temporaries zeroed, no bridge-owned or leftover breakpoint", c.session_info()["unresolved_instrumentation"] == [] and all(v == 0 for v in x.temps()) and not leftover(x, "x"), temps=x.temps())


# ------------------------------------------------------------------------------------------ main
def main() -> int:
    def go(port, run, inject, system="1942", test_env=None):
        test_env = dict(test_env or {})
        if inject:
            test_env["MAMEMCP_TEST_FAIL_INSTALL_AT"] = str(inject)
        p, i = launch(system=system, port=port, run_dir=OUT / run, rompath=ROMS if system == "1942" else None, fixture=True, test_env=test_env)
        created = winwake.creation_time(p.pid)
        w = winwake.waker_for(lambda: (p.pid, created), port=port)
        fx = Fixture(i["fixture_dir"], wake=w)
        c = BridgeClient(port=port, default_timeout=30, wake=w)
        c.connect(timeout=90)
        e = Env(c, fx)
        e.evidence_dir = i["evidence_dir"]
        e.run_dir = i["run_dir"]
        e.proc = p
        fx.call("set_visible_cpu", cpu=CPU)
        fx.call("user_resume")
        e.x.wait_state("running", 40)
        time.sleep(3.0)  # let the game boot
        return e

    procs = []
    try:
        os.environ["MAMEMCP_TEST_FAIL_CLEAR"] = "1"  # ambient value: the launcher must NOT pass it on (checked in M0)
        try:
            e = go(PORT, "run", None)
        finally:
            os.environ.pop("MAMEMCP_TEST_FAIL_CLEAR", None)
        procs.append(e.proc)
        m0(e, True)
        si = e.c.session_info()
        check("environment: MAME 0.289, 1942, fixture, nudge configured (identity-bound)", si["mame_version"] == "0.289" and si["machine"]["name"] == "1942" and e.fx.call("ping")["pong"] and e.c.wake is not None)
        for name, fn in (("M1", m1), ("M2", m2), ("M2b", m2b), ("M3", m3), ("M4", m4), ("M5", m5), ("M6", m6), ("M8", m8), ("M9", m9)):
            try:
                fn(e)
            except BridgeError as err:
                check(f"{name}: UNEXPECTED bridge error", False, error=err.to_dict())
            except Exception:
                import traceback
                check(f"{name}: UNEXPECTED harness exception", False, error=traceback.format_exc()[-900:])
        note("transport", timeouts=e.c.timeouts, retries=e.c.read_retries, wake_posts=e.c.wake_posts)
        e.c.close(graceful=False)
        f = go(PORT_F, "run_inject", 2)
        procs.append(f.proc)
        try:
            m7(f)
        except Exception:
            import traceback
            check("M7: UNEXPECTED harness exception", False, error=traceback.format_exc()[-900:])
        f.c.close(graceful=False)
        for name, fn, port, run, kw in (("M10", m10, PORT_S, "run_shinobi", dict(system="shinobi")),
                                        ("M11", m11, PORT_C, "run_clearfail", dict(test_env={"MAMEMCP_TEST_FAIL_CLEAR": "2"})),
                                        ("M12", m12, PORT_D, "run_rollbackfail", dict(inject=2, test_env={"MAMEMCP_TEST_FAIL_CLEAR": "1"}))):
            try:
                g = go(port, run, kw.pop("inject", None), **kw)
                procs.append(g.proc)
                fn(g)
                g.c.close(graceful=False)
            except Exception:
                import traceback
                check(f"{name}: UNEXPECTED harness exception", False, error=traceback.format_exc()[-1200:])
    finally:
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
    return R.save(OUT / "results.json", mame="0.289", target="1942 (Revision B) :maincpu Z80; Shinobi :maincpu 68000 (M10)")


if __name__ == "__main__":
    sys.exit(main())
