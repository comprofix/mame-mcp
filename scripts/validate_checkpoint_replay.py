"""Replaying the same controlled input after restoring a checkpoint, on real MAME 0.289 (Shinobi). Test-only; the bridge is driven
directly and the TEST-ONLY fixture plugin is the independent observer (same arrangement as validate_sequence.py).

One MAME is started with the production launcher plus the fixture plugin, reaches a debugger-stopped frame F with no held input and
no unresolved instrumentation, and saves a checkpoint (exact fingerprint). Continuation A, then restoring the checkpoint and
continuation B, each do exactly this from F:
  - reset debugger temporary temp0, read it back (raw console line), then set a user breakpoint at the level-4 handler (0x404) whose
    condition counts hits in temp0 and stops at a DECIMAL threshold: `(temp0=temp0+1)>=#60` (a bare number is hexadecimal);
  - arm the established 35-frame press_sequence schedule while stopped (it counts from the first emulated frame) and resume;
  - at the stop: temp0 (raw console line, parsed as hexadecimal) must be 60; the fixture's read tap on the 68000 level-4 autovector
    (0x70-0x73) must show two CPU reads per hit; the fixture's per-frame observer must have seen exactly the scheduled pressed frames.
    The endpoint is the 60th verified hit, NOT a frame count: 60 hits can span 59 or 60 screen frames depending on interrupt phase.
    The actual frame delta (final frame - F) is recorded, and the observer must account for it exactly (one distinct frame per emulated
    frame: first = F+1, last = final frame, count = delta).
A and B are then compared on: final frame, observer frame evidence, observed input frames, hit count, vector reads, selected RAM hashes
and selected CPU registers (no fixed F+60 is asserted).
The result states what that covers; it does NOT show whole-machine determinism.

Usage: .venv\\Scripts\\python -I scripts\\validate_checkpoint_replay.py     Output: <runs>\\checkpoint_replay\\results.json
(<runs> = $MAMEMCP_HARNESS_RUNS or runs\\)
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Results  # noqa: E402
from _inst import Inst, profile, runs_root  # noqa: E402
from validate_sequence import F as FIELDS  # noqa: E402
from validate_sequence import expected_frames, finish, step, watchers  # noqa: E402

OUT = runs_root() / "checkpoint_replay"
R = Results("checkpoint_replay")
check, note = R.check, R.note
M68 = profile()["shinobi"]["m68k"]
MAIN = M68["cpu"]
SND = profile()["shinobi"]["z80"]["cpu"]
HANDLER = int(M68["disasm"]["address"], 16)  # 0x404, the level-4 autovector target: hit once per interrupt
VECTOR = (0x70, 0x73)  # the level-4 autovector (a 32-bit entry): the 68000 reads it as two 16-bit words per interrupt
HITS = 60
COND = f"(temp0=temp0+1)>=#{HITS}"  # decimal threshold; the same string is used for A and B
SCHEDULE = [step("b1", 0, 10), step("b2", 4, 10), step("left", 30, 5)]  # the established 35-frame schedule (validate_sequence S2)
SCHEDULE_FRAMES = 35
RUN_UP_FRAME = 300
CHECKPOINT_ID = "replay-f"
MAIN_REGS = ["CURPC", *[f"D{i}" for i in range(8)], *[f"A{i}" for i in range(7)], "SP", "SR"]
SND_REGS = ["PC", "SP"]
# (name, cpu, address, length): plain RAM only (0xFFC000 is the mirror of the battery RAM share at 0xC70000, so that is one region, not two); read with the bridge's inspect-mode read_memory (at most 4096 bytes per call)
RAM_REGIONS = [("m68k_work_ram", MAIN, 0xFFC000, 0x4000), ("z80_work_ram", SND, int(profile()["shinobi"]["z80"]["ram"]["address"], 16), 0x800)]


def parse_hex(line: str | None) -> int | None:
    """Value of a 'T0|<hex>' console line, parsed as hexadecimal (printf %X output has no prefix)."""
    try:
        return int((line or "").split("|")[1].strip(), 16)
    except (IndexError, ValueError):
        return None


def temp0_line(m: Inst) -> str:
    return m.fx.call("debugger_printf", cpu=MAIN, text='"T0|%X",temp0')["line"]


def ram_hashes(m: Inst) -> dict[str, str]:
    out = {}
    for name, cpu, addr, length in RAM_REGIONS:
        h = hashlib.sha256()
        for off in range(0, length, 4096):
            r = m.rq("read_memory", {"cpu": cpu, "space": "program", "address": addr + off, "length": min(4096, length - off), "mode": "inspect"}, mutating=False)
            h.update(bytes.fromhex(r["data_hex"]))
        out[name] = h.hexdigest()
    return out


def registers(m: Inst) -> dict[str, str]:
    out = {}
    for cpu, names in ((MAIN, MAIN_REGS), (SND, SND_REGS)):
        r = m.rq("read_registers", {"cpu": cpu, "registers": names}, mutating=False)
        out.update({f"{cpu}:{x['name']}": x["hex"] for x in r["registers"]})
    return out


def breakpoints(m: Inst) -> list[dict]:
    return [b for b in m.fx.call("bplist")["breakpoints"] if b["cpu"] == MAIN]


def continuation(m: Inst, label: str, F: int) -> dict:
    """One replay from the debugger-stopped frame F. Records every check; returns the measurements for the A/B comparison."""
    check(f"{label}: starts debugger-stopped at the checkpoint frame F={F}", m.state() == "debugger_stopped" and m.info_()["clock"]["frame"] == F, frame=m.info_()["clock"]["frame"], state=m.state())
    # reset and read back temp0 (raw console line)
    m.fx.call("set_temp", n=0, value=0)
    reset_line = temp0_line(m)
    check(f"{label}: temp0 was reset and reads back 0 (raw console line, parsed as hexadecimal)", parse_hex(reset_line) == 0, raw_line=reset_line)
    # the counting breakpoint: raw condition and listing preserved
    idx = m.fx.call("user_bp", cpu=MAIN, address=HANDLER, cond=COND, action="")["index"]
    listing = breakpoints(m)
    note(f"{label}: breakpoint listing as MAME reports it", requested_condition=COND, listing=listing)
    check(f"{label}: exactly one breakpoint on the main CPU, at {HANDLER:#x}, whose condition counts in temp0 against the decimal threshold #{HITS}",
          len(listing) == 1 and listing[0]["address"] == HANDLER and "temp0" in listing[0]["condition"] and f"#{HITS}" in listing[0]["condition"], listing=listing)
    # independent instruments, then the schedule armed while stopped (counted from the first emulated frame)
    m.fx.call("tap_log_take", name="vec")
    m.fx.call("watch_multi", watchers=watchers())
    arm = m.rq("input_sequence", {"steps": SCHEDULE})
    check(f"{label}: the {SCHEDULE_FRAMES}-frame schedule is armed while stopped: held, waiting for emulation, no frame elapsed", arm["state"] == "holding" and arm.get("waiting_for_emulation") is True,
          state=arm["state"], waiting=arm.get("waiting_for_emulation"))
    m.fx.call("user_resume")
    stopped = m.wait(lambda: (lambda i: i["execution"]["state"] == "debugger_stopped" and i["clock"]["frame"] > F)(m.info_()), 60)
    check(f"{label}: emulation ran and the breakpoint stopped it again within 60 s", bool(stopped), state=m.state(), frame=m.info_()["clock"]["frame"])
    if not stopped:
        raise RuntimeError(f"{label}: no breakpoint stop")
    # measurements: first the raw temp0 line, then the tap and observer, then the bridge's reads (which do not touch the vector)
    stop_line = temp0_line(m)
    hits = parse_hex(stop_line)
    tap = m.fx.call("tap_log_take", name="vec")
    w = m.fx.call("watch_multi", stop=True)
    st = finish(m)
    final_frame = m.info_()["clock"]["frame"]
    obs = {x["name"]: x["frames"] for x in w["watchers"]}
    firsts = [obs[k] for k in FIELDS if obs[k]]
    f0 = min(x[0] for x in firsts) if firsts else None
    seq = st.get("sequence") or {}
    exp = expected_frames(SCHEDULE, f0) if f0 is not None else None
    regs, hashes = registers(m), ram_hashes(m)
    m.fx.call("user_bpclear", cpu=MAIN, index=idx)
    left = breakpoints(m)
    note(f"{label}: raw stop line, tap and observer summary", stop_line=stop_line, vector_reads=tap["seen"], tap_offsets=sorted({e["off"] for e in tap["events"]}), tap_disabled_ignored=tap["side_effects_disabled_ignored"],
         observer_frames=w["frames_seen"], observer_first=w["first_frame_seen"], observer_last=w["last_frame_seen"], final_frame=final_frame)
    check(f"{label}: temp0 at the stop (raw console line parsed as hexadecimal) is {HITS} decimal", hits == HITS, raw_line=stop_line, parsed_hex=hits)
    check(f"{label}: the independent vector-read tap counted exactly two CPU reads per hit ({2 * HITS})", tap["seen"] == 2 * HITS and len(tap["events"]) == tap["seen"], reads=tap["seen"], expected=2 * HITS)
    delta = final_frame - F
    note(f"{label}: actual frame delta for {HITS} hits", checkpoint_frame=F, final_frame=final_frame, frame_delta=delta)
    check(f"{label}: the observer accounts for the actual frame delta exactly: first frame F+1, last frame = final frame, one distinct frame each ({delta})",
          delta > 0 and w["first_frame_seen"] == F + 1 and w["last_frame_seen"] == final_frame and w["frames_seen"] == delta,
          checkpoint_frame=F, final_frame=final_frame, frame_delta=delta, observer_frames=w["frames_seen"], observer_first=w["first_frame_seen"], observer_last=w["last_frame_seen"])
    check(f"{label}: the independent observer sees each field pressed on exactly the scheduled frames (origin = its first pressed frame)", exp is not None and {k: obs[k] for k in FIELDS} == exp,
          origin=f0, expected={k: (v[:3], len(v)) for k, v in (exp or {}).items()}, observed={k: (obs[k][:3], len(obs[k])) for k in FIELDS})
    check(f"{label}: the bridge's own report agrees (completed, release verified, no deviations, {SCHEDULE_FRAMES} frames, same origin as the observer) and nothing is held",
          st["state"] == "released" and st["end_reason"] == "completed" and st["release_verified"] is True and seq.get("deviations") == [] and all(s.get("matches_schedule") for s in seq.get("steps", []))
          and seq.get("pressed_outside_windows") == [] and st["frames_elapsed"] == SCHEDULE_FRAMES and f0 is not None and seq["origin"]["origin_machine_frame"] == f0,
          state=st["state"], reason=st["end_reason"], elapsed=st["frames_elapsed"], bridge_origin=seq.get("origin"), observer_origin=f0)
    check(f"{label}: the counting breakpoint was removed and nothing unresolved remains", left == [] and m.info_()["unresolved_instrumentation"] == [], left=left)
    return {"final_frame": final_frame, "frame_delta": delta, "observer_frames": w["frames_seen"], "observer_first": w["first_frame_seen"], "observer_last": w["last_frame_seen"], "pressed_frames": {k: obs[k] for k in FIELDS}, "pressed_origin_offset": None if f0 is None else f0 - F,
            "hits": hits, "vector_reads": tap["seen"], "ram_sha256": hashes, "registers": regs}


def main_run() -> None:
    m = Inst(OUT, "shinobi", 9671, "shinobi")
    try:
        m.resume()
        reached = m.wait(lambda: m.info_()["clock"]["frame"] >= RUN_UP_FRAME, 120)
        m.stop_debugger()
        F = m.info_()["clock"]["frame"]
        st0 = m.rq("input_status", mutating=False)
        check("F: reached an observed checkpoint frame, debugger-stopped, with no held input, no unresolved instrumentation and no breakpoint",
              bool(reached) and F >= RUN_UP_FRAME and m.state() == "debugger_stopped" and st0["state"] in ("none", "released") and m.info_()["unresolved_instrumentation"] == [] and breakpoints(m) == [],
              frame=F, input=st0["state"])
        m.fx.call("tap_log", name="vec", cpu=MAIN, space="program", kind="r", start=VECTOR[0], end=VECTOR[1], cap=1000)
        s = m.save(CHECKPOINT_ID)
        fp, cov = s["fingerprint"], s["fingerprint_coverage"]
        check("F: checkpoint saved at frame F with an exact fingerprint", s["state"] == "completed" and s["frame"] == F and fp["exact"] is True, frame=s["frame"], exact=fp["exact"], size=s.get("size"))
        res = {"A": continuation(m, "A", F)}
        ld = m.load(CHECKPOINT_ID, fp)
        v = ld["verification"]
        check("load: exact verification (frame, registers, RAM digest), load effect observed, frame back to F, still debugger-stopped",
              ld["state"] == "completed" and v["exact"] is True and v["load_effect_observed"] is True and v["frame_after_load"] == F and m.info_()["clock"]["frame"] == F and m.state() == "debugger_stopped",
              verification={k: v.get(k) for k in ("method", "exact", "frame_before_load", "frame_after_load", "load_effect_observed", "registers_compared")}, state=m.state())
        res["B"] = continuation(m, "B", F)
        a, b = res["A"], res["B"]
        check("A/B: same final frame and same actual frame delta from F (60 hits)", a["final_frame"] == b["final_frame"] and a["frame_delta"] == b["frame_delta"],
              checkpoint_frame=F, final=(a["final_frame"], b["final_frame"]), delta=(a["frame_delta"], b["frame_delta"]))
        check("A/B: same observer frame evidence (first, last, count) and same observed input frames per field, with the same offset from F of the first pressed frame",
              (a["observer_first"], a["observer_last"], a["observer_frames"]) == (b["observer_first"], b["observer_last"], b["observer_frames"]) and a["pressed_frames"] == b["pressed_frames"]
              and a["pressed_origin_offset"] == b["pressed_origin_offset"], observer=((a["observer_first"], a["observer_last"], a["observer_frames"]), (b["observer_first"], b["observer_last"], b["observer_frames"])),
              offset=(a["pressed_origin_offset"], b["pressed_origin_offset"]), pressed={k: len(v) for k, v in a["pressed_frames"].items()})
        check("A/B: same hit count and same vector-read count", a["hits"] == b["hits"] == HITS and a["vector_reads"] == b["vector_reads"] == 2 * HITS, hits=(a["hits"], b["hits"]), reads=(a["vector_reads"], b["vector_reads"]))
        check("A/B: identical SHA-256 of every selected RAM region", a["ram_sha256"] == b["ram_sha256"] and len(a["ram_sha256"]) == len(RAM_REGIONS),
              differing=[k for k in a["ram_sha256"] if a["ram_sha256"][k] != b["ram_sha256"].get(k)], hashes={k: x[:16] for k, x in a["ram_sha256"].items()})
        check("A/B: identical selected CPU registers", a["registers"] == b["registers"] and len(a["registers"]) == len(MAIN_REGS) + len(SND_REGS),
              differing={k: (a["registers"][k], b["registers"].get(k)) for k in a["registers"] if a["registers"][k] != b["registers"].get(k)}, count=len(a["registers"]))
        note("A/B coverage actually measured (NOT whole-machine determinism)",
             ram_regions=[{"name": n, "cpu": c, "address": hex(ad), "bytes": ln} for n, c, ad, ln in RAM_REGIONS], registers=sorted(a["registers"]),
             checkpoint_fingerprint_coverage={k: cov.get(k) for k in ("registers_compared_on", "no_state_interface", "ram", "not_covered")}, checkpoint_frame=F,
             not_measured="video/tile/sprite/palette RAM, registers of the :upd7751 CPU, device-internal state, audio output and screen pixels")
    finally:
        m.close()


def main() -> int:
    try:
        main_run()
    except Exception as e:
        import traceback
        check("checkpoint-replay harness completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-900:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
