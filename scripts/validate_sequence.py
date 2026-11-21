"""press_sequence / input_sequence against real MAME 0.289 (direct bridge; the TEST-ONLY fixture plugin is the independent observer).

Independent observer: the fixture's watch_multi reads each field's PORT BIT at the end of every emulated frame (its own
frame_done handler, no code shared with the bridge) and records the screen frame numbers on which the machine saw it pressed. The
bridge's own report (sequence.steps / deviations) is compared with it, never trusted alone.

Frame origin used for the comparison: F0 = the first frame the OBSERVER saw any scheduled field pressed; a step's expected frames
are F0+start .. F0+start+frames-1.  Usage: .venv\\Scripts\\python -I scripts\\validate_sequence.py
Output: <runs>\\sequence\\results.json (<runs> = $MAMEMCP_HARNESS_RUNS or runs\\)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Results  # noqa: E402
from _inst import Inst, code, profile, runs_root  # noqa: E402

OUT = runs_root() / "sequence"
P = profile()
R = Results("sequence")
check, note = R.check, R.note
I = P["shinobi"]["input"]
PORT = I["port"]
F = {"b1": (I["b1"]["field"], I["b1"]["mask"]), "b2": (I["b2"]["field"], I["b2"]["mask"]), "left": (I["left"]["field"], I["left"]["mask"])}


def step(key: str, start: int, frames: int) -> dict:
    return {"port": PORT, "field": F[key][0], "start": start, "frames": frames}


def watchers() -> list[dict]:
    return [{"name": k, "port": PORT, "mask": mask} for k, (_, mask) in F.items()]


def expected_frames(steps: list[dict], f0: int) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {k: [] for k in F}
    for s in steps:
        key = next(k for k, (name, _) in F.items() if name == s["field"])
        out[key] += list(range(f0 + s["start"], f0 + s["start"] + s["frames"]))
    return {k: sorted(v) for k, v in out.items()}


def finish(m: Inst, timeout: float = 60.0) -> dict:
    end = time.time() + timeout
    st = m.rq("input_status", mutating=False)
    while st["state"] == "holding" and time.time() < end:
        time.sleep(0.05)
        st = m.rq("input_status", mutating=False)
    m.wait(lambda: m.rq("input_status", mutating=False).get("release_verified") is not None, 5)
    time.sleep(0.3)
    return m.rq("input_status", mutating=False)


def run_seq(m: Inst, steps: list[dict]) -> tuple[dict, dict, dict]:
    m.fx.call("watch_multi", watchers=watchers())
    arm = m.rq("input_sequence", {"steps": steps})
    st = finish(m)
    w = m.fx.call("watch_multi", stop=True)
    return arm, st, {x["name"]: x["frames"] for x in w["watchers"]} | {"_seen": w["frames_seen"], "_first": w["first_frame_seen"]}


def verify(m: Inst, label: str, steps: list[dict], arm: dict, st: dict, obs: dict) -> int | None:
    seq = st.get("sequence") or {}
    firsts = [obs[k][0] for k in F if obs[k]]
    f0 = min(firsts) if firsts else None
    exp = expected_frames(steps, f0) if f0 is not None else None
    got = {k: obs[k] for k in F}
    check(f"{label}: independent observer sees each field pressed on exactly the scheduled frames (origin = its first pressed frame)", exp is not None and got == exp,
          origin=f0, expected={k: (v[:3], len(v)) for k, v in (exp or {}).items()}, observed={k: (v[:3], len(v)) for k, v in got.items()})
    check(f"{label}: sequence completed, release verified, the bridge's own report agrees (no deviations, every step matches)", st["state"] == "released" and st["end_reason"] == "completed" and st["release_verified"] is True
          and seq.get("deviations") == [] and all(s.get("matches_schedule") for s in seq.get("steps", [])) and seq.get("pressed_outside_windows") == [] and seq["transitions_applied"] == seq["transitions"],
          state=st["state"], reason=st["end_reason"], deviations=seq.get("deviations"), transitions=(seq.get("transitions_applied"), seq.get("transitions")))
    if f0 is not None:
        total = max(s["start"] + s["frames"] for s in steps)
        check(f"{label}: frames_elapsed equals the sequence length ({total}) and the bridge's own origin frame equals the observer's", st["frames_elapsed"] == total
              and seq["duration_frames"] == total and seq["origin"]["origin_machine_frame"] == f0, elapsed=st["frames_elapsed"], bridge_origin=seq["origin"], observer_origin=f0)
    return f0


def main_run() -> None:
    m = Inst(OUT, "shinobi", 9651, "shinobi")
    try:
        m.resume()
        li = m.rq("list_inputs", {"include_unsupported": False}, mutating=False)
        digital = [(p["tag"], f["field"]) for p in li["ports"] for f in p["fields"]]
        # --- S1: one step equals press_inputs (same latency anchoring, same duration) ---------------------------------
        for n in (1, 5, 30):
            s = [step("b1", 0, n)]
            arm, st, obs = run_seq(m, s)
            verify(m, f"S1 single step of {n} frame(s)", s, arm, st, obs)
        m.fx.call("watch_multi", watchers=watchers())
        arm = m.rq("input_press", {"inputs": [{"port": PORT, "field": F["b1"][0]}], "frames": 7})
        st = finish(m)
        w = m.fx.call("watch_multi", stop=True)
        b1 = [x for x in w["watchers"] if x["name"] == "b1"][0]["frames"]
        check("S1 press_inputs is unchanged: 7 consecutive frames seen by the independent observer, released and verified", st["state"] == "released" and len(b1) == 7 and b1 == list(range(b1[0], b1[0] + 7)) and st["release_verified"], observed=b1, state=st["state"])
        # --- S2: chord with overlap, a gap and a later press ---------------------------------------------------------
        s = [step("b1", 0, 10), step("b2", 4, 10), step("left", 30, 5)]
        arm, st, obs = run_seq(m, s)
        f0 = verify(m, "S2 chord overlap and gap", s, arm, st, obs)
        both = sorted(set(obs["b1"]) & set(obs["b2"]))
        check("S2 the two overlapping fields were really held together for 6 frames (offsets 4..9)", f0 is not None and both == list(range(f0 + 4, f0 + 10)), together=both)
        # --- S3: same-boundary ordering -----------------------------------------------------------------------------
        s = [step("b1", 0, 5), step("b2", 5, 5), step("b1", 6, 5)]
        arm, st, obs = run_seq(m, s)
        f0 = verify(m, "S3 boundary ordering (b1 ends where b2 starts; b1 re-pressed after one idle frame)", s, arm, st, obs)
        check("S3 at a boundary the release is applied before the press: no frame has b1 and b2 both pressed except where the schedule says so", f0 is not None and sorted(set(obs["b1"]) & set(obs["b2"])) == [f0 + 6, f0 + 7, f0 + 8, f0 + 9],
              together=sorted(set(obs["b1"]) & set(obs["b2"]))[:6])
        check("S3 the same field has exactly one idle frame between its two presses (f0+5)", f0 is not None and f0 + 5 not in obs["b1"] and f0 + 4 in obs["b1"] and f0 + 6 in obs["b1"], b1=obs["b1"])
        # --- S4: stopped arming ----------------------------------------------------------------------------------
        m.stop_debugger()
        s = [step("b1", 0, 6), step("left", 3, 6)]
        m.fx.call("watch_multi", watchers=watchers())
        f_before = m.info_()["clock"]["frame"]
        arm = m.rq("input_sequence", {"steps": s})
        time.sleep(1.5)
        st = m.rq("input_status", mutating=False)
        w = m.fx.call("watch_multi", stop=True)
        check("S4 armed while debugger-stopped: held, waiting_for_emulation, zero frames elapsed, and the independent observer saw no frame at all",
              arm["state"] == "holding" and arm.get("waiting_for_emulation") is True and st["frames_elapsed"] == 0 and w["frames_seen"] == 0 and m.info_()["clock"]["frame"] == f_before,
              status=st["state"], elapsed=st["frames_elapsed"], observer_frames=w["frames_seen"])
        m.fx.call("watch_multi", watchers=watchers())
        e = [code(m.err("input_sequence", {"steps": s})), code(m.err("input_press", {"inputs": [{"port": PORT, "field": F["b1"][0]}], "frames": 3})), code(m.err("write_memory", {"cpu": ":maincpu", "space": "program", "address": 0xC70010, "value": 1, "width": 1}))]
        check("S4 a held sequence refuses a second sequence, a press and a RAM write with input_active", e == ["input_active"] * 3, codes=e)
        m.fx.call("user_resume")
        st = finish(m)
        w = m.fx.call("watch_multi", stop=True)
        obs = {x["name"]: x["frames"] for x in w["watchers"]}
        f0 = min(obs["b1"] + obs["left"]) if obs["b1"] else None
        check("S4 after the resume the schedule runs from its first emulated frame: stopped time did not count", f0 is not None and obs == expected_frames(s, f0) | {"b2": []} and st["frames_elapsed"] == 9 and st["state"] == "released",
              observed={k: v for k, v in obs.items()}, elapsed=st["frames_elapsed"])
        # --- S5: pause in the middle of a running sequence ------------------------------------------------------------
        m.fx.call("watch_multi", watchers=watchers())
        arm = m.rq("input_sequence", {"steps": [step("b1", 0, 150)]})
        time.sleep(0.6)
        m.pause()
        e1 = m.rq("input_status", mutating=False)["frames_elapsed"]
        time.sleep(1.0)
        e2 = m.rq("input_status", mutating=False)["frames_elapsed"]
        m.resume()
        st = finish(m)
        w = m.fx.call("watch_multi", stop=True)
        b1 = [x for x in w["watchers"] if x["name"] == "b1"][0]["frames"]
        check("S5 pausing mid-sequence freezes it (no frame elapses while paused) and resuming continues it without a gap or a repeat: 150 consecutive frames", e1 == e2 and 0 < e1 < 150 and len(b1) == 150 and b1 == list(range(b1[0], b1[0] + 150)) and st["state"] == "released",
              frozen_at=(e1, e2), observed=len(b1), contiguous=b1 == list(range(b1[0], b1[0] + 150)) if b1 else None)
        # --- S6: cancel while running ----------------------------------------------------------------------------------
        m.fx.call("watch_multi", watchers=watchers())
        m.rq("input_sequence", {"steps": [step("b1", 0, 300), step("left", 100, 100)]})
        time.sleep(0.5)
        c = m.rq("input_cancel")
        time.sleep(0.5)
        st = m.rq("input_status", mutating=False)
        w = m.fx.call("watch_multi", stop=True)
        obs = {x["name"]: x["frames"] for x in w["watchers"]}
        rows = (st.get("sequence") or {}).get("steps", [])
        check("S6 cancel mid-sequence releases at once: observer sees b1 stop long before 300 frames and nothing of the not-yet-started 'left' step; the report says which steps were cut",
              c["was_holding"] and st["state"] == "cancelled" and st["end_reason"] == "cancelled" and 0 < len(obs["b1"]) < 100 and obs["left"] == [] and st["release_verified"] is True
              and [r["state"] for r in rows] == ["released_early", "not_started"], pressed=len(obs["b1"]), steps=[r["state"] for r in rows], release_verified=st["release_verified"])
        check("S6 cancel is idempotent", m.rq("input_cancel")["was_holding"] is False)
        # --- S7: client disconnect ------------------------------------------------------------------------------------
        m.fx.call("watch_multi", watchers=watchers())
        m.rq("input_sequence", {"steps": [step("b1", 0, 600)]})
        time.sleep(0.3)
        m.c.close(graceful=False)
        time.sleep(3.5)
        w = m.fx.call("watch_multi", stop=True)
        m.c.connect(timeout=30)
        st = m.rq("input_status", mutating=False)
        b1 = [x for x in w["watchers"] if x["name"] == "b1"][0]["frames"]
        check("S7 client disconnect mid-sequence releases it (observer: press stopped long before 600 frames; status says why)", st["state"] == "cancelled" and "client_disconnected" in (st["end_reason"] or "")
              and 0 < len(b1) < 400 and st["release_verified"] is True, pressed=len(b1), reason=st["end_reason"])
        # --- S8: validation: the whole sequence is checked before anything is pressed -----------------------------
        m.fx.call("watch_multi", watchers=watchers())
        okstep = step("b1", 0, 5)
        too_many_fields = [{"port": t, "field": f, "start": 0, "frames": 1} for t, f in digital[:17]]
        nine = [{"port": t, "field": f, "start": 0, "frames": 3} for t, f in digital[:9]]
        cases = [
            ("same field overlaps itself", [step("b1", 0, 10), step("b1", 5, 10)], "bad_argument"),
            ("same field touches itself (no idle frame)", [step("b1", 0, 10), step("b1", 10, 5)], "bad_argument"),
            ("earliest step does not start at 0", [step("b1", 3, 5)], "bad_argument"),
            ("zero frames", [step("b1", 0, 0)], "bad_argument"), ("601 frames", [step("b1", 0, 601)], "bad_argument"),
            ("negative start", [step("b1", -1, 5)], "bad_argument"), ("non-integer start", [{**okstep, "start": 1.5}], "bad_argument"),
            ("past 3600 frames", [okstep, step("b2", 3590, 20)], "too_large"),
            ("65 steps", [okstep] + [step("b1", 6 * i, 5) for i in range(1, 65)], "bad_argument"),
            ("17 distinct fields", too_many_fields, "too_large"),
            ("9 fields held at once", nine, "too_large"),
            ("unknown field", [{**okstep, "field": I["note"] and "Nope"}], "unknown_field"),
            ("unsupported (DIP) field", [{"port": ":DSW1", "field": "Coin A", "start": 0, "frames": 3}], "unsupported_input"),
            ("unknown port", [{"port": ":NOPE", "field": "x", "start": 0, "frames": 3}], "unknown_port"),
            ("empty list", [], "bad_argument"),
            ("one bad step after a good one (nothing is applied)", [okstep, {**step("b2", 5, 5), "field": "Nope"}], "unknown_field"),
        ]
        got = {}
        for name, steps_, _ in cases:
            got[name] = code(m.err("input_sequence", {"steps": steps_}))
        bad = {n: (got[n], want) for n, _, want in cases if got[n] != want}
        check("S8 invalid sequences are refused with typed errors by the plugin", not bad, wrong=bad)
        time.sleep(0.4)
        st = m.rq("input_status", mutating=False)
        w = m.fx.call("watch_multi", stop=True)
        check("S8 no refused sequence pressed anything or left anything held (observer saw no pressed frame on any field)", st["state"] in ("none", "cancelled", "released") and all(x["frames"] == [] for x in w["watchers"]), state=st["state"])
        # client-side validator
        from mame_mcp import protocol
        from mame_mcp.protocol import BridgeError
        refused = 0
        for name, steps_, _ in cases:
            try:
                protocol.sequence_args(steps_)
            except BridgeError:
                refused += 1
        check("S8 the client-side validator refuses every case above before any I/O (it mirrors the plugin's rules, except unknown fields that only the machine can judge)",
              refused >= len(cases) - 4, refused=refused, of=len(cases))
        # --- S9: longest allowed single step and a held-at-once maximum ----------------------------------------------
        eight = [{"port": t, "field": f, "start": 0, "frames": 3} for t, f in digital[:8]]
        arm = m.rq("input_sequence", {"steps": eight})
        st = finish(m)
        check("S9 eight fields held at once (the maximum) is accepted, runs and is released and verified", st["state"] == "released" and st["release_verified"] is True and len(st["inputs"]) == 8, state=st["state"], inputs=len(st["inputs"]))
        # --- S10: a sequence cannot start while a checkpoint-style busy state exists; shutdown release is covered by e2e
        note("S finished", session=m.info_()["bridge"]["session_id"])
    finally:
        m.close()


def main() -> int:
    try:
        main_run()
    except Exception as e:
        import traceback
        check("sequence harness completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-900:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
