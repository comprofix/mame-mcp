"""Plugin-level validation of checkpoints, held inputs and RAM writes against a real MAME 0.289 (Shinobi), with the
TEST-ONLY fixture plugin as an independent observer. Talks to the bridge directly (no MCP server): this is where the
test hooks and the plugin's own refusals can be provoked. The same features through the production MCP server and the
official SDK are in scripts/mcp_checkpoint_e2e.py.

Sections (separate MAME instances where a test hook or another machine is needed):
  I  inputs: exact frame counts (independent port watcher), stopped/paused semantics, cancel, client-disconnect release
  W  RAM writes: widths, mirror, ROM / unmapped / device / I-O refusal, stopped-only, read-back (fixture reads RAM itself)
  C  checkpoints (immediate path): save/load while debugger-stopped and paused, refusal while running, bad ids/files
  G  guards: active capture refuses save/load/write; stale experiment ids after a load
  D  checkpoints (scheduled/deferred path, test hook MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT) incl. a corrupt file
  U  unresolved instrumentation (test hook MAMEMCP_TEST_FAIL_CLEAR) refuses save/load/write
  M  a machine without save-state support is refused clearly
Game/CPU-specific addresses and inputs come from tests/fixtures/profiles/shinobi.json ("checkpoint_input").
Writes runs/checkpoint_input/results.json.   Usage: .venv\\Scripts\\python -I scripts\\validate_checkpoint_input.py [--sections I,W,C,G,D,U,M]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Fixture, Results  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.bridge import BridgeClient  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT, launch  # noqa: E402
from mame_mcp.protocol import BridgeError  # noqa: E402

OUT = PROJECT_ROOT / "runs" / "checkpoint_input"
# ROM set directory holding the machine without save-state support (section M); the default rompath only needs Shinobi
ALT_ROMPATH = Path(os.environ.get("MAMEMCP_ALT_ROMPATH", "D:/arcade/ROMs/MAME 0.289 ROMs (merged)"))
PROFILE = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "shinobi.json").read_text())
CI = PROFILE["checkpoint_input"]
MAIN = PROFILE["maincpu"]["tag"]
HANDLER = int(PROFILE["maincpu"]["handler_pc"], 16)
H = lambda s: int(s, 16)  # noqa: E731
R = Results("checkpoint_input")
check, note = R.check, R.note


class Inst:
    """One isolated MAME with the fixture plugin; `c` is the bridge client, `fx` the independent observer."""

    def __init__(self, name: str, port: int, system: str = "shinobi", fixture: bool = True, test_env: dict | None = None, rompath: Path | None = None):
        self.name, self.run_dir = name, OUT / name
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.proc, self.info = launch(system=system, port=port, run_dir=self.run_dir, fixture=fixture, test_env=test_env, **({"rompath": rompath} if rompath else {}))
        wake = winwake.waker_for((self.proc.pid, self.info.get("pid_created_filetime")), port=port)
        self.fx = Fixture(self.info["fixture_dir"], wake=wake) if fixture else None
        self.c = BridgeClient(port=port, default_timeout=30, wake=wake)
        self.c.connect(timeout=90)
        self.sta = self.run_dir / "home" / "sta" / system
        self.n = 0

    def close(self):
        try:
            self.c.close(graceful=False)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()

    # bridge helpers
    def rq(self, op, args=None, mutating=True, **kw):
        return self.c.request(op, args or {}, mutating=mutating and op not in ("session_info", "input_status", "checkpoint_status", "list_inputs"), **kw)

    def err(self, op, args=None) -> BridgeError | None:
        try:
            self.rq(op, args)
        except BridgeError as e:
            return e
        return None

    def info_(self) -> dict:
        return self.c.request("session_info", {}, retry_safe=True)

    def state(self) -> str:
        return self.info_()["execution"]["state"]

    def frame(self) -> int:
        return self.info_()["clock"]["frame"]

    def wait(self, pred, timeout=30.0, step=0.1):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(step)
        return None

    def run(self):
        self.rq("execution_control", {"action": "resume"})
        assert self.wait(lambda: self.state() == "running", 30), "could not reach running"

    def stop_debugger(self):
        self.fx.call("user_stop")
        assert self.wait(lambda: self.state() == "debugger_stopped", 30), "could not reach debugger_stopped"

    def pause(self):
        if self.state() != "running":  # a pause request is only meaningful from running; from a debugger stop go through run first
            self.run()
        self.rq("execution_control", {"action": "pause"})
        assert self.wait(lambda: self.state() == "paused", 30), "could not reach paused"

    def ram(self, addr: int, width: int = 1) -> int:  # independent read: MAME's own space API through the fixture, not the bridge
        return self.fx.call("read_normal", cpu=MAIN, space="program", address=addr, path={1: "read_u8", 2: "read_u16", 4: "read_u32"}[width])["value"]

    def save(self, cid: str | None = None) -> dict:
        self.n += 1
        cid = cid or f"{self.name}-v{self.n:03d}"
        r = self.c.request("checkpoint_save", {"id": cid}, mutating=True, timeout=30)
        return self.settle(r)

    def settle(self, r: dict) -> dict:
        end = time.time() + 40
        while r.get("state") == "pending" and time.time() < end:
            time.sleep(0.1)
            r = self.c.request("checkpoint_status", {}, retry_safe=True)
        if r.get("state") == "failed":
            e = r["error"]
            raise BridgeError(e["code"], e["message"], detail=e.get("detail"))
        return r

    def load(self, cid: str, fp: dict) -> dict:
        r = self.c.request("checkpoint_load", {"id": cid, "expect": fp}, mutating=True, timeout=30)
        return self.settle(r)


def header(path: Path) -> dict:
    b = path.read_bytes()[:32]
    return {"magic": b[:8].decode("latin1"), "version": b[8], "system": b[10:28].split(b"\0")[0].decode(), "size": path.stat().st_size}


def code(e: BridgeError | None) -> str | None:
    return e.code if e else None


# ------------------------------------------------------------------------------------------------------------------ I
def section_inputs() -> None:
    m = Inst("inputs", 9451)
    try:
        inp = CI["input"]
        P, W = inp["press"], inp["watch"]
        li = m.rq("list_inputs", {"include_unsupported": False}, mutating=False)
        lu = m.rq("list_inputs", {"include_unsupported": True}, mutating=False)
        names = {(p["tag"], f["field"]) for p in li["ports"] for f in p["fields"]}
        check("I list_inputs: machine's real digital fields, DIP/config excluded unless asked", li["fields_listed"] == inp["supported_count"]
              and li["unsupported_fields"] == inp["unsupported_count"] and lu["fields_listed"] == inp["supported_count"] + inp["unsupported_count"]
              and (P[0]["port"], P[0]["field"]) in names and (inp["unsupported"]["analog_or_toggle"]["port"], inp["unsupported"]["analog_or_toggle"]["field"]) not in names,
              supported=li["fields_listed"], unsupported=li["unsupported_fields"], classes=li["field_classes"])
        m.fx.call("user_resume")
        assert m.wait(lambda: m.state() == "running", 30)

        def press_watch(inputs, frames, mask, **kw):
            m.fx.call("watch_port", port=W["port"], mask=mask)
            r = m.rq("input_press", {"inputs": inputs, "frames": frames})
            end = time.time() + 30
            st = r
            while st["state"] == "holding" and time.time() < end:
                time.sleep(0.05)
                st = m.rq("input_status", mutating=False)
            m.wait(lambda: m.rq("input_status", mutating=False).get("release_verified") is not None, 5)
            time.sleep(0.2)
            st = m.rq("input_status", mutating=False)
            w = m.fx.call("watch_port", port=W["port"], mask=mask, stop=True)
            return r, st, w

        for n in (1, 5, 30):
            r0, st, w = press_watch(P, n, W["mask"])
            check(f"I press {n} frame(s): independent watcher sees exactly {n}; released; machine reads idle again",
                  st["state"] == "released" and w["pressed_frames"] == n and st["frames_elapsed"] == n and st["frames_observed_pressed"] == n
                  and st["release_verified"] is True and w["frames_after_last_press"] >= 1, requested=n, watcher=w["pressed_frames"],
                  bridge_elapsed=st["frames_elapsed"], bridge_observed=st["frames_observed_pressed"], release_verified=st["release_verified"],
                  idle_frames_after=w["frames_after_last_press"])
        mask2 = H("0x84")  # Left (0x80) + Button 2 (0x04)
        r0, st, w = press_watch(inp["multi"], 7, mask2)
        check("I two fields pressed together for 7 frames (watcher on both bits)", st["state"] == "released" and w["pressed_frames"] == 7 and len(st["inputs"]) == 2,
              watcher=w["pressed_frames"], inputs=st["inputs"])

        # stopped: held, nothing elapses, frames count from the resume
        m.stop_debugger()
        m.fx.call("watch_port", port=W["port"], mask=W["mask"])
        r = m.rq("input_press", {"inputs": P, "frames": 5})
        time.sleep(1.5)
        st = m.rq("input_status", mutating=False)
        check("I press while debugger-stopped: armed, held, waiting_for_emulation, zero frames elapsed after 1.5 s",
              r["state"] == "holding" and r.get("waiting_for_emulation") is True and st["state"] == "holding" and st["frames_elapsed"] == 0, armed=r["state"], status=st["state"], elapsed=st["frames_elapsed"])
        e_wr, e_ck = m.err("write_memory", {"cpu": MAIN, "space": "program", "address": H(CI["ram"]["address"]), "value": 1, "width": 1}), m.err("checkpoint_save", {"id": "held-1"})
        e_pr = m.err("input_press", {"inputs": P, "frames": 3})
        check("I held input refuses write_memory, checkpoint_save and a second press", code(e_wr) == code(e_ck) == code(e_pr) == "input_active", codes=[code(e_wr), code(e_ck), code(e_pr)])
        m.fx.call("user_resume")
        st = m.wait(lambda: (lambda s: s if s["state"] != "holding" else None)(m.rq("input_status", mutating=False)), 20)
        time.sleep(0.2)
        w = m.fx.call("watch_port", port=W["port"], mask=W["mask"], stop=True)
        check("I after the resume the 5 frames elapse and the watcher sees exactly 5 (stopped time did not count)", st and st["state"] == "released" and st["frames_elapsed"] == 5 and w["pressed_frames"] == 5,
              watcher=w["pressed_frames"], numbers=w["pressed_frame_numbers"], status={k: (st or {}).get(k) for k in ("state", "frames_elapsed", "frames_observed_pressed", "release_verified")})

        # paused behaves like stopped
        m.pause()
        r = m.rq("input_press", {"inputs": P, "frames": 4})
        time.sleep(0.8)
        st = m.rq("input_status", mutating=False)
        check("I press while paused: held, 0 frames elapsed", r.get("waiting_for_emulation") is True and st["frames_elapsed"] == 0 and st["state"] == "holding", status=st["state"], elapsed=st["frames_elapsed"])
        c0 = m.rq("input_cancel")
        check("I cancel while stopped releases (verification pending until a frame runs)", c0["was_holding"] is True and c0["state"] == "cancelled" and c0["end_reason"] == "cancelled" and c0["release_errors"] == [],
              result={k: c0.get(k) for k in ("state", "was_holding", "release_verified", "release_errors")})
        m.run()

        # cancel mid-press (running)
        m.fx.call("watch_port", port=W["port"], mask=W["mask"])
        m.rq("input_press", {"inputs": P, "frames": 600})
        time.sleep(0.4)
        c1 = m.rq("input_cancel")
        time.sleep(0.6)
        w = m.fx.call("watch_port", port=W["port"], mask=W["mask"], stop=True)
        st = m.rq("input_status", mutating=False)
        check("I cancel mid-press: released immediately, watcher sees pressed frames stop, idle afterwards, release verified", c1["was_holding"] and 0 < w["pressed_frames"] < 100
              and w["frames_after_last_press"] >= 10 and st["release_verified"] is True, pressed=w["pressed_frames"], idle_after=w["frames_after_last_press"], status=st["state"])
        c2 = m.rq("input_cancel")
        check("I cancel is idempotent", c2["was_holding"] is False and c2["release_errors"] == [])

        # client disconnect mid-press
        m.fx.call("watch_port", port=W["port"], mask=W["mask"])
        m.rq("input_press", {"inputs": P, "frames": 600})
        time.sleep(0.3)
        m.c.close(graceful=False)
        time.sleep(3.5)
        w = m.fx.call("watch_port", port=W["port"], mask=W["mask"], stop=True)
        m.c.connect(timeout=30)
        st = m.rq("input_status", mutating=False)
        check("I client disconnect mid-press releases the input (watcher: press stopped long before 600 frames; status says why)", st["state"] == "cancelled" and "client_disconnected" in (st["end_reason"] or "")
              and w["pressed_frames"] < 400 and w["frames_after_last_press"] >= 30, pressed=w["pressed_frames"], idle_after=w["frames_after_last_press"], reason=st["end_reason"])

        # refusals
        a = inp["unsupported"]
        codes = [code(m.err("input_press", {"inputs": [a["analog_or_toggle"]], "frames": 3})), code(m.err("input_press", {"inputs": [{"port": a["unknown_port"], "field": "x"}], "frames": 3})),
                 code(m.err("input_press", {"inputs": [a["unknown_field"]], "frames": 3})), code(m.err("input_press", {"inputs": P, "frames": 0})),
                 code(m.err("input_press", {"inputs": P, "frames": 601})), code(m.err("input_press", {"inputs": P * 2, "frames": 3})), code(m.err("input_press", {"inputs": [], "frames": 3}))]
        check("I unsupported (toggle/DIP), unknown port/field, bad frames, duplicates, empty list: all refused, nothing held", codes == ["unsupported_input", "unknown_port", "unknown_field", "bad_argument",
              "bad_argument", "bad_argument", "bad_argument"] and m.rq("input_status", mutating=False)["state"] == "cancelled", codes=codes)
        note("I finished", session=m.info_()["bridge"]["session_id"])
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ W / C / G
def section_memory_and_checkpoints(want: set[str]) -> None:
    m = Inst("mem_ckpt", 9452)
    try:
        ram, rom, unm, dev, io = (H(CI[k]["address"]) for k in ("ram", "rom", "unmapped", "device_handled", "io"))
        mirror = H(CI["ram"]["mirror_address"])

        def wm(addr, value, width, space="program", cpu=MAIN):
            return m.rq("write_memory", {"cpu": cpu, "space": space, "address": addr, "value": value, "width": width})

        m.run()
        m.run()
        e = m.err("write_memory", {"cpu": MAIN, "space": "program", "address": ram, "value": 1, "width": 1})
        e2 = m.err("checkpoint_save", {"id": "run-1"})
        e3 = m.err("checkpoint_load", {"id": "run-1", "expect": {"frame": 1, "cpus": {}}})
        check("W/C writes, saves and loads are refused while emulation is running", code(e) == code(e2) == code(e3) == "execution_running", codes=[code(e), code(e2), code(e3)])
        m.stop_debugger()

        # ---- W: RAM writes ------------------------------------------------------------------------------------
        if "W" in want:
            for width, spec in CI["ram_widths"].items():
                w, addr, val = int(width), H(spec["address"]), H(spec["value"])
                before = m.ram(addr, w)
                r = wm(addr, val, w)
                after = m.ram(addr, w)
                check(f"W {width}-byte write at {spec['address']}: before/requested/actual reported; the fixture's own read agrees", r["verified"] and r["before"]["value"] == before
                      and r["requested"]["value"] == val and r["after"]["value"] == after == val and r["width"] == w, before=r["before"], requested=r["requested"], after=r["after"], independent=after)
            check("W 16-bit write is big-endian in memory (68000): bytes_hex ascending addresses", wm(H(CI["ram_widths"]["2"]["address"]), 0x1234, 2)["after"]["bytes_hex"] == "1234"
                  and m.ram(H(CI["ram_widths"]["2"]["address"]), 1) == 0x12)
            v0 = m.ram(ram, 1)
            r = wm(mirror, (v0 + 1) & 0xFF, 1)
            check("W a write through the map's mirror lands in the same RAM", r["verified"] and m.ram(ram, 1) == (v0 + 1) & 0xFF, mirror=hex(mirror), base_value=m.ram(ram, 1))
            rom_before = m.ram(rom, 2)
            res = {}
            for name, addr in (("rom", rom), ("unmapped", unm), ("device_handled", dev), ("io", io)):
                e = m.err("write_memory", {"cpu": MAIN, "space": "program", "address": addr, "value": 0x11, "width": 1})
                res[name] = code(e)
            check("W ROM, unmapped, device-handled and I/O targets are refused with distinct codes; ROM bytes unchanged", res == {"rom": "rom_target", "unmapped": "unmapped_target", "device_handled": "unsupported_target",
                  "io": "unsupported_target"} and m.ram(rom, 2) == rom_before, codes=res, rom_unchanged=m.ram(rom, 2) == rom_before)
            bad = {
                "bad width": {"cpu": MAIN, "space": "program", "address": ram, "value": 1, "width": 3},
                "misaligned": {"cpu": MAIN, "space": "program", "address": ram + 1, "value": 1, "width": 2},
                "value too large": {"cpu": MAIN, "space": "program", "address": ram, "value": 256, "width": 1},
                "negative": {"cpu": MAIN, "space": "program", "address": ram, "value": -1, "width": 1},
                "no space": {"cpu": MAIN, "address": ram, "value": 1, "width": 1},
                "io space": {"cpu": MAIN, "space": "io", "address": 0, "value": 1, "width": 1},
                "opcodes space": {"cpu": MAIN, "space": "opcodes", "address": ram, "value": 1, "width": 1},
                "unknown space": {"cpu": MAIN, "space": "nope", "address": ram, "value": 1, "width": 1},
                "unknown cpu": {"cpu": ":nope", "space": "program", "address": ram, "value": 1, "width": 1},
                "out of range": {"cpu": MAIN, "space": "program", "address": 0x1000000, "value": 1, "width": 1},
            }
            got = {k: code(m.err("write_memory", v)) for k, v in bad.items()}
            check("W plugin-side argument validation (width, alignment, range, missing/unknown space or cpu; this 68000 has only the program space)", got == {"bad width": "bad_argument", "misaligned": "bad_argument", "value too large": "bad_argument",
                  "negative": "bad_argument", "no space": "bad_argument", "io space": "unknown_space", "opcodes space": "unknown_space", "unknown space": "unknown_space",
                  "unknown cpu": "unknown_cpu", "out of range": "out_of_range"}, codes=got)
            m.pause()
            r = wm(ram, 0x33, 1)
            check("W a write while paused is accepted", r["verified"] and m.ram(ram) == 0x33)
            m.stop_debugger()
            note("W no arbitrary execution surface", operations=m.info_()["bridge"]["operations"])

        # ---- C: checkpoints (immediate path) -----------------------------------------------------------------
        if "C" in want:
            wm(ram, 0x5A, 1)
            fp_probe = m.info_()
            s1 = m.save()
            f1 = m.sta / f"{s1['id']}.sta"
            hd = header(f1)
            check("C save (debugger-stopped): completed only with a valid MAME state file; immediate; execution unchanged; frame recorded", s1["state"] == "completed" and s1["mode"] == "immediate"
                  and hd["magic"] == "MAMESAVE" and hd["system"] == "shinobi" and hd["size"] == s1["size"] and s1["execution"]["state"] == "debugger_stopped" and s1["frames_run_during_operation"] == 0
                  and s1["fingerprint"]["exact"] is True and s1["fingerprint"]["frame"] == fp_probe["clock"]["frame"], header=hd, frame=s1["frame"], fp_ram=s1["fingerprint"].get("ram"), execution=s1["execution"]["state"])
            dup = m.err("checkpoint_save", {"id": s1["id"]})
            check("C checkpoint ids are never overwritten", code(dup) == "checkpoint_exists", code=code(dup))
            wm(ram, 0x77, 1)
            m.fx.call("space_write_u8", cpu=MAIN, space="program", address=H(CI["ram"]["sprite_ram"]), value=0x99)
            check("C state is changed after the save (independent reads)", m.ram(ram) == 0x77 and m.ram(H(CI["ram"]["sprite_ram"])) == 0x99)
            l1 = m.load(s1["id"], s1["fingerprint"])
            v = l1["verification"]
            check("C load (debugger-stopped): reported complete only after frame+registers+RAM match; RAM restored (independent read); execution still stopped",
                  l1["state"] == "completed" and v["exact"] is True and v["load_effect_observed"] is True and m.ram(ram) == 0x5A and m.state() == "debugger_stopped" and v["registers_compared"] > 20
                  and v["frame_after_load"] == s1["frame"], verification=v, ram_after=m.ram(ram), state=m.state())
            check("C a load bumps the state epoch and reports it", l1["state_epoch"] == 1 and m.info_()["state_epoch"] == 1, epoch=l1["state_epoch"])
            m.run()
            time.sleep(1.2)
            m.pause()
            f_before = m.frame()
            wm(ram, 0x11, 1)
            l2 = m.load(s1["id"], s1["fingerprint"])
            check("C after running ~1 s (paused): load rewinds the frame counter to the checkpoint, RAM restored; paused is preserved", l2["verification"]["frame_before_load"] > s1["frame"] and
                  l2["verification"]["frame_after_load"] == s1["frame"] == m.frame() and m.ram(ram) == 0x5A and m.state() == "paused" and f_before > s1["frame"],
                  frame_before_load=l2["verification"]["frame_before_load"], frame_after=m.frame(), checkpoint_frame=s1["frame"], state=m.state())
            s2 = m.save()
            check("C save while paused works too", s2["state"] == "completed" and s2["mode"] == "immediate" and s2["execution"]["state"] == "paused")
            m.stop_debugger()
            bad_ids = {"empty": "", "slash": "a/b", "dots": "..", "space": "a b", "semicolon": "x;y", "long": "x" * 97}
            got = {k: code(m.err("checkpoint_save", {"id": v})) for k, v in bad_ids.items()}
            check("C hostile/invalid ids are rejected before any debugger command is built", set(got.values()) == {"bad_argument"} and len(got) == len(bad_ids), codes=got)
            missing = m.err("checkpoint_load", {"id": "never-saved", "expect": s1["fingerprint"]})
            wrong = m.sta / "wrongsys.sta"
            raw = bytearray(f1.read_bytes())
            raw[10:28] = b"pacman".ljust(18, b"\0")
            wrong.write_bytes(raw)
            short = m.sta / "short.sta"
            short.write_bytes(b"MAMESAVE")
            junk = m.sta / "junk.sta"
            junk.write_bytes(b"not a state file at all, but longer than thirty-two bytes....")
            codes = [code(missing)] + [code(m.err("checkpoint_load", {"id": i, "expect": s1["fingerprint"]})) for i in ("wrongsys", "short", "junk")]
            check("C load refuses a missing file and files for another machine / truncated / not a state file BEFORE touching the machine", codes == ["checkpoint_not_found", "checkpoint_incompatible",
                  "checkpoint_incompatible", "checkpoint_incompatible"] and m.info_()["state_epoch"] == 2 and m.ram(ram) == 0x5A, codes=codes)
            nofp = code(m.err("checkpoint_load", {"id": s1["id"]}))
            check("C load without the recorded fingerprint is refused (completion could not be verified)", nofp == "bad_argument", code=nofp)

        # ---- G: guards ------------------------------------------------------------------------------------------
        if "G" in want:
            s = m.save()
            cap = m.rq("capture_breakpoint", {"cpu": MAIN, "address": HANDLER, "max_events": 3, "max_wall_seconds": 120, "registers": ["D0"], "label": "guard"})
            expa = cap["experiment_id"]
            codes = [code(m.err("checkpoint_save", {"id": "cap-1"})), code(m.err("checkpoint_load", {"id": s["id"], "expect": s["fingerprint"]})),
                     code(m.err("write_memory", {"cpu": MAIN, "space": "program", "address": ram, "value": 1, "width": 1}))]
            check("G an ACTIVE capture refuses save, load and write (nothing changed)", codes == ["capture_active"] * 3 and m.ram(ram) == 0x5A, codes=codes)
            m.rq("cancel_experiment", {"experiment_id": expa})
            check("G after cancelling the capture the same operations are accepted again", code(m.err("write_memory", {"cpu": MAIN, "space": "program", "address": ram, "value": 0x5A, "width": 1})) is None)

            # stale experiments
            m.run()
            cap = m.rq("capture_breakpoint", {"cpu": MAIN, "address": HANDLER, "max_events": 3, "max_wall_seconds": 60, "registers": ["D0"], "label": "before-load"})
            old = cap["experiment_id"]
            st = m.wait(lambda: (lambda x: x if x["state"] in ("completed", "failed", "cancelled") else None)(m.rq("experiment_status", {"experiment_id": old}, mutating=False)), 30)
            m.pause()
            sv = m.save()
            m.rq("capture_breakpoint", {"cpu": MAIN, "address": HANDLER, "max_events": 3, "max_wall_seconds": 60, "registers": ["D0"], "label": "second"})
            ids2 = m.rq("session_info", mutating=False)["active_experiment"]
            m.rq("cancel_experiment", {"experiment_id": ids2})
            ld = m.load(sv["id"], sv["fingerprint"])
            e_status, e_cancel = m.err("experiment_status", {"experiment_id": old}), m.err("cancel_experiment", {"experiment_id": old})
            e_default = m.err("experiment_status", {})
            check("G experiment ids recorded before a load are refused as stale (status and cancel); default status does not inherit them", st["state"] == "completed" and code(e_status) == code(e_cancel)
                  == "experiment_stale_after_load" and code(e_default) == "unknown_experiment" and old in ld["invalidated_experiments"] and ids2 in ld["invalidated_experiments"],
                  codes=[code(e_status), code(e_cancel), code(e_default)], invalidated=ld["invalidated_experiments"], message=(e_status.message if e_status else None)[:140])
            check("G the stale experiment's evidence files are still on disk", (Path(st["evidence"]["manifest_json"])).is_file(), manifest=st["evidence"]["manifest_json"])
            m.stop_debugger()
            new = m.rq("capture_breakpoint", {"cpu": MAIN, "address": HANDLER, "max_events": 3, "max_wall_seconds": 60, "registers": ["D0"], "label": "after-load"})
            m.run()
            st2 = m.wait(lambda: (lambda x: x if x["state"] in ("completed", "failed", "cancelled") else None)(m.rq("experiment_status", {"experiment_id": new["experiment_id"]}, mutating=False)), 30)
            check("G a capture started after the load is current and completes normally; default status resolves to it", st2["state"] == "completed" and st2["counts"]["events_captured"] == 3
                  and m.rq("experiment_status", {}, mutating=False)["experiment_id"] == new["experiment_id"], experiment=new["experiment_id"], counts=st2["counts"])
            check("G session identity is unchanged by a load", m.info_()["bridge"]["session_id"] == m.c.session_id)
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ D
def section_deferred() -> None:
    m = Inst("deferred", 9453, test_env={"MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT": "1"})
    try:
        ram = H(CI["ram"]["address"])
        hooks = m.info_()["test_hooks"]
        check("D the test hook is reported by session_info", hooks == ["MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT=1"], hooks=hooks)
        m.run()
        time.sleep(1.0)
        m.stop_debugger()
        m.rq("write_memory", {"cpu": MAIN, "space": "program", "address": ram, "value": 0x42, "width": 1})
        f0 = m.frame()
        s = m.save()
        hd = header(m.sta / f"{s['id']}.sta")
        check("D scheduled save from debugger-stopped: completes via the poll, emulation ran only briefly, previous stopped state restored; fingerprint inexact", s["state"] == "completed"
              and s["mode"] == "scheduled" and hd["magic"] == "MAMESAVE" and m.state() == "debugger_stopped" and s["execution_before"] == "debugger_stopped" and 0 <= s["frames_run_during_operation"] <= 10
              and s["fingerprint"]["exact"] is False and s["fingerprint"]["frame"] == f0, mode=s["mode"], frames_run=s["frames_run_during_operation"], state=m.state(), fp={k: s["fingerprint"].get(k) for k in ("frame", "frame_max", "exact")})
        m.run()
        time.sleep(1.5)
        m.stop_debugger()
        m.rq("write_memory", {"cpu": MAIN, "space": "program", "address": ram, "value": 0x24, "width": 1})
        f_before = m.frame()
        ld = m.load(s["id"], s["fingerprint"])
        v = ld["verification"]
        check("D scheduled load: verified by frame window, RAM restored (independent read), stopped state restored, stale epoch bumped", ld["state"] == "completed" and ld["mode"] == "scheduled" and v["exact"] is False
              and m.ram(ram) == 0x42 and m.state() == "debugger_stopped" and v["frame_after_load"] <= s["fingerprint"]["frame_max"] + 6 and f_before > v["frame_after_load"] and ld["state_epoch"] == 1,
              verification=v, ram=m.ram(ram), frames_run=ld["frames_run_during_operation"], frame_before=f_before)
        # bad payload: header fine, data corrupt -> MAME refuses/garbles; the bridge must not claim success and must not run for long
        s2 = m.save()
        p = m.sta / f"{s2['id']}.sta"
        raw = bytearray(p.read_bytes())
        for i in range(64, min(len(raw), 4000)):
            raw[i] ^= 0xA5
        p.write_bytes(raw)
        m.run()
        time.sleep(0.8)
        m.stop_debugger()
        f_b = m.frame()
        e = m.err("checkpoint_load", {"id": s2["id"], "expect": s2["fingerprint"]})
        pend = e
        if e is None:
            end = time.time() + 40
            while time.time() < end:
                st = m.rq("checkpoint_status", mutating=False)
                if st["state"] != "pending":
                    pend = BridgeError(st["error"]["code"], st["error"]["message"], detail=st["error"].get("detail")) if st["state"] == "failed" else None
                    break
                time.sleep(0.1)
        d = (pend.detail or {}) if pend else {}
        check("D a corrupt state file is not reported as loaded: checkpoint_not_applied, emulation bounded (<=95 frames), stopped state restored, epoch not bumped", pend is not None and pend.code == "checkpoint_not_applied"
              and d.get("frames_run_during_operation", 999) <= 95 and m.state() == "debugger_stopped" and m.info_()["state_epoch"] == 1, code=code(pend), frames_run=d.get("frames_run_during_operation"), state=m.state(), message=(pend.message if pend else "")[:160])
        busy = m.rq("checkpoint_status", mutating=False)
        check("D nothing is left pending afterwards", busy["state"] == "failed", status=busy["state"])
    finally:
        m.close()


# ------------------------------------------------------------------------------------------------------------------ U / M
def section_unresolved() -> None:
    m = Inst("unresolved", 9454, test_env={"MAMEMCP_TEST_FAIL_CLEAR": "1"})
    try:
        ram = H(CI["ram"]["address"])
        m.stop_debugger()
        s = m.save("unres-base")
        cap = m.rq("capture_breakpoint", {"cpu": MAIN, "address": HANDLER, "max_events": 3, "max_wall_seconds": 60, "registers": ["D0"]})
        cx = m.rq("cancel_experiment", {"experiment_id": cap["experiment_id"]})
        left = m.info_()["unresolved_instrumentation"]
        check("U (hook) the capture's breakpoint could not be removed: unresolved instrumentation is reported", len(left) >= 1 and cx["cleanup"]["verified"] is not True or len(left) >= 1, remaining=left[:1], cleanup=cx.get("cleanup", {}).get("breakpoint_cleared"))
        codes = [code(m.err("checkpoint_save", {"id": "unres-1"})), code(m.err("checkpoint_load", {"id": s["id"], "expect": s["fingerprint"]})),
                 code(m.err("write_memory", {"cpu": MAIN, "space": "program", "address": ram, "value": 1, "width": 1}))]
        check("U unresolved instrumentation refuses save, load and write", codes == ["unresolved_instrumentation"] * 3, codes=codes)
        check("U nothing was written, saved or loaded", not (m.sta / "unres-1.sta").exists() and m.info_()["state_epoch"] == 0)
    finally:
        m.close()


def section_unsupported_machine() -> None:
    system = CI["unsupported_machine"]["system"]
    try:
        m = Inst("unsupported", 9455, system=system, fixture=False, rompath=ALT_ROMPATH)
    except Exception as e:  # ROMs missing or MAME could not start it: say so rather than pass
        check(f"M launch '{system}' (savestate=\"unsupported\")", False, error=str(e)[:200])
        return
    try:
        si = m.info_()
        e1 = m.err("checkpoint_save", {"id": "unsup-1"})
        e2 = m.err("checkpoint_load", {"id": "unsup-1", "expect": {"frame": 1, "cpus": {}}})
        check(f"M machine '{system}' reports supports_save=false; save and load are refused as unsupported_machine and write no file", si["checkpoint"]["supports_save"] is False
              and code(e1) == code(e2) == "unsupported_machine" and not list(m.sta.glob("*.sta")) if m.sta.exists() else True, supports_save=si["checkpoint"]["supports_save"], codes=[code(e1), code(e2)], message=(e1.message if e1 else "")[:150])
    finally:
        m.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default="I,W,C,G,D,U,M")
    want = set(ap.parse_args().sections.split(","))
    if OUT.exists() and want >= set("IWCGDUM"):
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if "I" in want:
        section_inputs()
    if want & set("WCG"):
        section_memory_and_checkpoints(want)
    if "D" in want:
        section_deferred()
    if "U" in want:
        section_unresolved()
    if "M" in want:
        section_unsupported_machine()
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
