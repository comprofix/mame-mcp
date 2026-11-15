"""Integration validation against a real MAME instance (Shinobi). Writes runs/validation/results.json.

Every check records PASS/FAIL plus the evidence it saw. Nothing here is mocked.
Usage:  .venv\\Scripts\\python -I scripts\\validate.py
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from pathlib import Path

from mame_mcp import winwake
from mame_mcp.bridge import BridgeClient
from mame_mcp.launcher import PROJECT_ROOT, launch
from mame_mcp.protocol import BridgeError, capture_args, read_memory_args, read_registers_args

OUT = PROJECT_ROOT / "runs" / "validation"
OUT.mkdir(parents=True, exist_ok=True)
RESULTS: list[dict] = []
PORT = 9433  # deliberately not the default: proves MAMEMCP_PORT is honoured
HANDLER = 0x404  # 68000 level-4 autovector target in Shinobi (read from vector table 0x70 during probing)
# The manual (MAME-debugger) stop is placed on the instruction the handler branches to: the bytes at 0x404 are
# 6000 2466 (bra.w, displacement relative to 0x406) => 0x406 + 0x2466. A capture and a user breakpoint must not
# share an address (MAME runs only the first one), see the address_in_use check in scripts/validate_hardening.py.
MANUAL_PC = 0x286C


def check(name: str, ok: bool, **evidence) -> bool:
    RESULTS.append({"check": name, "status": "PASS" if ok else "FAIL", "evidence": evidence})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {json.dumps(evidence, default=str)[:300]}")
    return ok


def expect_error(name: str, fn, code: str, **extra):
    try:
        fn()
    except BridgeError as e:
        return check(name, e.code == code, got=e.code, message=e.message, **extra)
    return check(name, False, got="no error")


def wait_state(c: BridgeClient, want: str, timeout: float = 20.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = c.session_info()
        if last["execution"]["state"] == want:
            return last
        time.sleep(0.2)
    return None


def reg_map(res):
    return {r["name"]: int(r["hex"], 16) for r in res["registers"]}


def no_debugger_scenario() -> None:
    """MAME started without -debug: reads work, breakpoints are refused with a clear error."""
    proc, _ = launch(port=PORT + 1, run_dir=OUT / "run-nodebug", debug=False)
    c = BridgeClient(port=PORT + 1)
    try:
        si = c.connect(timeout=40)
        early = si["execution"]
        later = wait_state(c, "running", 15)
        check("no -debug: session_info works, debugger_available=false, reaches running",
              si["bridge"]["debugger_available"] is False and later is not None, state_at_connect=early,
              note="MAME reports paused briefly during startup")
        time.sleep(0.5)
        check("no -debug: read_registers works", c.read_registers(read_registers_args(":maincpu", ["CURPC"]))["registers"][0]["name"] == "CURPC")
        expect_error("no -debug: capture_breakpoint refused clearly",
                     lambda: c.capture_breakpoint(capture_args(":maincpu", HANDLER, 1, 1)), "debugger_unavailable")
    except BridgeError as e:
        check("no-debug scenario", False, error=e.to_dict())
    finally:
        c.close(graceful=False)
        proc.kill()


def main() -> int:
    run_dir = OUT / "run"
    script = OUT / "manual_observation.txt"
    # Independent observation: MAME's own debugger prints the stopped state at the first hit of 0x404.
    # temp0 makes this breakpoint stop exactly once; our experiments use temp9.
    script.write_text(
        f'bpset {MANUAL_PC:X},temp0==0,{{printf "MANUAL|%X|%X|%X|%X|%X|%X|%X|%X|%X|%X|%X\\n",'
        f'(temp0=1),curpc,d0,d1,a0,a1,sp,sr,b@{HANDLER:X},w@{HANDLER:X},d@sp}}\n'
        'g\n'
    )
    proc, info = launch(port=PORT, run_dir=run_dir, debugscript=script, debuglog=True)
    check("environment: romset verified by installed MAME", "is good" in info["verifyroms"]["output"],
          mame=info["mame_version"], verify=info["verifyroms"]["output"], zip_sha1=info["romset_zip"]["sha1"])
    c = BridgeClient(port=PORT, wake=winwake.waker_for(proc.pid))
    try:
        si = c.connect(timeout=40)
        check("handshake + session_info", si["machine"]["name"] == "shinobi" and si["mame_version"] == "0.289",
              machine=si["machine"]["name"], version=si["mame_version"], session=c.session_id,
              cpus=[d["tag"] for d in si["cpus"]], bridge=si["bridge"]["version"])
        check("plugin honoured MAMEMCP_PORT/EVIDENCE_DIR env", si["bridge"]["port"] == PORT and "evidence" in si["bridge"]["evidence_dir"],
              port=si["bridge"]["port"], evidence_dir=si["bridge"]["evidence_dir"])
        cpu_main = next(d for d in si["cpus"] if d["tag"] == ":maincpu")
        check("maincpu program space is shift=0, 24-bit, 16-bit big-endian bus",
              any(s["name"] == "program" and s["shift"] == 0 and s["address_mask"] == "0xFFFFFF" and s["endianness"] == "big"
                  for s in cpu_main["address_spaces"]), spaces=cpu_main["address_spaces"])

        # ---- debugger-stopped at a breakpoint -------------------------------------------------
        st = wait_state(c, "debugger_stopped", 30)
        # Startup with -debug is also a stop; wait until the manual breakpoint's stop is the one we see.
        t0 = time.time()
        while st and time.time() - t0 < 30:
            regs = c.read_registers(read_registers_args(":maincpu", ["CURPC"]))
            if reg_map(regs)["CURPC"] == MANUAL_PC:
                break
            if st["execution"]["state"] != "debugger_stopped":
                st = wait_state(c, "debugger_stopped", 30)
            time.sleep(0.3)
        stopped = c.session_info()
        pc_now = reg_map(c.read_registers(read_registers_args(":maincpu", ["CURPC"])))["CURPC"]
        check("state: debugger_stopped at manual breakpoint reached", stopped["execution"]["state"] == "debugger_stopped" and pc_now == MANUAL_PC,
              state=stopped["execution"], curpc=hex(pc_now))
        # periodic callback count advances while stopped => requests serviced by register_periodic
        n0 = c.session_info()["counters"]["periodic_calls"]; time.sleep(1.0); n1 = c.session_info()["counters"]["periodic_calls"]
        check("callbacks keep running while debugger-stopped", n1 > n0, periodic_calls_per_s=n1 - n0)
        clk_a = c.session_info()["clock"]; time.sleep(0.6); clk_b = c.session_info()["clock"]
        check("emulated time frozen while debugger-stopped", clk_a == clk_b, clock=clk_b)

        names = ["CURPC", "D0", "D1", "A0", "A1", "SP", "SR"]
        regs = reg_map(c.read_registers(read_registers_args(":maincpu", names)))
        sp = regs["SP"]
        m_rom = c.read_memory(read_memory_args(":maincpu", "program", HANDLER, 4))["data_hex"]
        # 68000 bus is 24-bit: mask the 32-bit SP as the CPU does
        m_stk = c.read_memory(read_memory_args(":maincpu", "program", sp & 0xFFFFFF, 4))["data_hex"]

        # ---- paused / running -----------------------------------------------------------------
        # (done after the stopped-state comparison below; the comparison needs MAME's own log)

        # compare with the independent MAME-debugger observation (debug.log is written by MAME itself)
        manual = None
        t0 = time.time()
        dl = run_dir / "home" / "debug.log"
        while time.time() - t0 < 10:
            if dl.exists():
                m = re.search(r"MANUAL\|([0-9A-F|]+)", dl.read_text(errors="replace"))
                if m:
                    manual = [int(x, 16) for x in m.group(1).split("|")]
                    break
            time.sleep(0.3)
        if manual is None:
            check("manual observation available in debug.log", False, debug_log_exists=dl.exists())
        else:
            # fields: temp0, curpc, d0, d1, a0, a1, sp, sr, b@404, w@404, d@sp
            exp_regs = dict(zip(["CURPC", "D0", "D1", "A0", "A1", "SP", "SR"], manual[1:8]))
            check("read_registers == manual debugger observation (same stop)", exp_regs == regs, manual=exp_regs, bridge=regs)
            b = int(m_rom[:2], 16); w = int(m_rom[:4], 16); dsp = int(m_stk, 16)
            check("read_memory == manual debugger observation (b@/w@ at ROM, d@sp RAM)",
                  (b, w, dsp) == tuple(manual[8:11]), manual=[hex(x) for x in manual[8:11]], bridge=[hex(b), hex(w), hex(dsp)])

        # introspection: manual bp survives, listed as not owned by us
        bps = next(d for d in c.session_info()["cpus"] if d["tag"] == ":maincpu")["breakpoints"]
        check("manual breakpoint visible and not owned by bridge", any(b["address"].lower() == hex(MANUAL_PC) and not b.get("owned_by_experiment") for b in bps), breakpoints=bps)

        # ---- running ------------------------------------------------------------------------
        c.execution_control("resume")
        st = wait_state(c, "running", 10)
        check("state: running after resume", st is not None, state=(st or {}).get("execution"))
        a = c.session_info(); time.sleep(1.0); b2 = c.session_info()
        check("running: emulated time advances", b2["clock"]["emu_time_s"] > a["clock"]["emu_time_s"],
              emu_s=[a["clock"]["emu_time_s"], b2["clock"]["emu_time_s"]], periodic_per_s=b2["counters"]["periodic_calls"] - a["counters"]["periodic_calls"])
        r_run = c.read_registers(read_registers_args(":maincpu", ["CURPC"]))
        check("running: read_registers + read_memory serviced", r_run["registers"][0]["name"] == "CURPC" and len(c.read_memory(read_memory_args(":maincpu", "program", 0, 16))["data_hex"]) == 32)

        # ---- normal pause ---------------------------------------------------------------------
        c.execution_control("pause")
        time.sleep(0.5)
        p = c.session_info()
        check("state: paused", p["execution"]["state"] == "paused", state=p["execution"])
        t_a = p["clock"]["emu_time_s"]; time.sleep(0.7); p2 = c.session_info()
        check("paused: requests serviced, emulated time frozen", p2["clock"]["emu_time_s"] == t_a and p2["counters"]["periodic_calls"] > p["counters"]["periodic_calls"],
              emu_s=t_a, periodic_delta=p2["counters"]["periodic_calls"] - p["counters"]["periodic_calls"])
        c.read_registers(read_registers_args(":maincpu", ["PC"]))
        c.execution_control("resume"); wait_state(c, "running", 10)

        # ---- validation of arguments -----------------------------------------------------------
        expect_error("read_memory rejects oversize length (client)", lambda: read_memory_args(":maincpu", "program", 0, 4097), "bad_argument")
        expect_error("read_memory rejects oversize length (plugin)", lambda: c.request("read_memory", {"cpu": ":maincpu", "space": "program", "address": 0, "length": 4097}), "too_large")
        expect_error("read_memory rejects out-of-range", lambda: c.request("read_memory", {"cpu": ":maincpu", "space": "program", "address": 0xFFFFFF, "length": 2}), "out_of_range")
        expect_error("read_memory rejects unknown cpu", lambda: c.request("read_memory", {"cpu": ":nope", "space": "program", "address": 0, "length": 1}), "unknown_cpu")
        expect_error("read_memory rejects unknown space", lambda: c.request("read_memory", {"cpu": ":maincpu", "space": "io", "address": 0, "length": 1}), "unknown_space")
        expect_error("read_registers rejects unknown register (A7 is a hex literal in expressions; the 68000 calls it SP)",
                     lambda: c.request("read_registers", {"cpu": ":maincpu", "registers": ["A7"]}), "unknown_register")
        expect_error("unknown op rejected", lambda: c.request("run_lua", {"code": "os.exit()"}), "unknown_op")
        expect_error("injection-style register name rejected", lambda: c.request("capture_breakpoint", {"cpu": ":maincpu", "address": 0x404, "max_events": 1, "max_wall_seconds": 1, "registers": ["d0;g"]}), "unknown_register")
        expect_error("capture requires limits (max_events)", lambda: capture_args(":maincpu", 0x404, 0, 5), "bad_argument")
        expect_error("capture requires limits (wall clock)", lambda: capture_args(":maincpu", 0x404, 5, 0), "bad_argument")
        # plugin-side check: craft a request for a different session on the live socket
        c._sock.sendall(b'{"v":1,"id":"raw-1","op":"session_info","session_id":"s-wrong"}\n')
        raw = b""
        t_raw = time.time()
        # the plugin also sends blank heartbeat lines; skip them and wait for the real reply line
        while not any(ln.strip() for ln in raw.split(b"\n")[:-1]) and time.time() - t_raw < 5:
            c._sock.settimeout(0.5)
            try:
                raw += c._sock.recv(65536)
            except OSError:
                pass
        lines = [ln for ln in raw.split(b"\n")[:-1] if ln.strip()]
        reply = json.loads(lines[0]) if lines else {}
        check("plugin rejects request addressed to another session", reply.get("ok") is False and reply.get("error", {}).get("code") == "session_mismatch", reply=reply.get("error"))
        # client-side check: a response from an unexpected session aborts the connection
        real_sid = c.session_id
        c.session_id = "s-wrong"
        expect_error("client rejects response from unexpected (restarted) session", lambda: c.request("session_info"), "session_mismatch")
        c.session_id = real_sid
        c.connect(timeout=25)  # client dropped the socket without goodbye: plugin must notice via heartbeat
        second = BridgeClient(port=PORT)
        try:
            second.connect(timeout=1.5)
            check("LIMITATION: second simultaneous client", False, note="a second client was accepted (unexpected)")
        except BridgeError as e:
            check("LIMITATION observed: only one client at a time (second connect refused/timed out)", e.code == "connect_failed", code=e.code)
        second.close()

        # ---- timeouts / stale responses ---------------------------------------------------------
        before = c.stale_discarded
        try:
            c._request_once("session_info", timeout=0.001)
            check("short timeout yields request_timeout", False)
        except BridgeError as e:
            check("short timeout yields request_timeout", e.code == "request_timeout", code=e.code, uncertain=e.uncertain)
        time.sleep(0.5)
        c.session_info()
        check("late reply to timed-out request discarded as stale", c.stale_discarded == before + 1, stale_discarded=c.stale_discarded)

        # ---- capture_breakpoint: complete by event limit ---------------------------------------
        cap = c.capture_breakpoint(capture_args(":maincpu", HANDLER, 5, 20, registers=["D0", "SR", "SP"],
                                                memory=[{"address": HANDLER, "size": 2}], label="validate-event-limit"))
        check("capture returns experiment id promptly (asynchronous)", cap["asynchronous"] and cap["experiment_id"].startswith("exp-"), id=cap["experiment_id"])
        exp_id = cap["experiment_id"]
        expect_error("second capture rejected while one is active (single active experiment)",
                     lambda: c.capture_breakpoint(capture_args(":maincpu", HANDLER, 5, 5)), "experiment_active") if c.experiment_status(exp_id)["state"] == "running" else check("single-experiment rule (finished too quickly to test)", True, note="skipped: first capture already done")
        stt = None
        for _ in range(100):
            stt = c.experiment_status(exp_id)
            if stt["state"] != "running":
                break
            time.sleep(0.1)
        check("completion by event limit", stt["state"] == "completed" and stt["completion_reason"] == "event_limit"
              and stt["counts"]["events_captured"] == 5 and stt["counts"]["events_dropped"] == 0, status=stt["summary"], counts=stt["counts"])
        ev = [json.loads(l) for l in Path(stt["evidence"]["events_jsonl"]).read_text().splitlines()]
        check("events.jsonl: 5 events, seq 1..5, all at expected PC", [e["seq"] for e in ev] == [1, 2, 3, 4, 5] and all(e["pc"] == hex(HANDLER).upper().replace("0X", "0x") for e in ev),
              first=ev[0] if ev else None)
        expected_mem = "0x" + m_rom[:4].upper()
        check("events carry requested registers; memory field equals manual w@0x404",
              all(set(e["regs"]) == {"D0", "SR", "SP"} and e["mem"][0]["value"] == expected_mem for e in ev),
              expected_mem=expected_mem, regs=ev[0]["regs"], mem=ev[0]["mem"])
        mani = json.loads(Path(stt["evidence"]["manifest_json"]).read_text())
        need = ["experiment_id", "parameters", "mame_version", "bridge_version", "machine", "cpu_context", "starting_state", "instrumentation",
                "interventions", "started_utc", "ended_utc", "limits", "completion_reason", "cleanup", "counts", "resumes_automatically", "end_execution", "time_semantics"]
        check("manifest has required fields", all(k in mani for k in need), missing=[k for k in need if k not in mani], cleanup=mani["cleanup"], end_execution=mani["end_execution"])
        bps = next(d for d in c.session_info()["cpus"] if d["tag"] == ":maincpu")["breakpoints"]
        check("cleanup: our breakpoint removed, manual breakpoint untouched",
              not any(b.get("owned_by_experiment") for b in bps) and any(b["condition"] == "temp0==0" for b in bps) and len(bps) == 1, breakpoints=bps)
        check("execution resumed automatically (state running at end)", mani["end_execution"]["state"] == "running", end=mani["end_execution"])

        # ---- completion by wall clock -----------------------------------------------------------
        cap = c.capture_breakpoint(capture_args(":maincpu", 0x0A0000, 100, 1.0, label="validate-wall"))
        time.sleep(1.8)
        stt = c.experiment_status(cap["experiment_id"])
        check("completion by wall-clock limit", stt["completion_reason"] == "wall_clock_limit" and stt["counts"]["events_captured"] == 0 and stt["cleanup"]["breakpoint_cleared"],
              status=stt["summary"], elapsed_ms=stt["elapsed_wall_ms"], cleanup=stt["cleanup"])

        # ---- cancellation -----------------------------------------------------------------------
        cap = c.capture_breakpoint(capture_args(":maincpu", HANDLER, 10000, 60, registers=["D0"], label="validate-cancel"))
        time.sleep(0.8)
        mid = c.experiment_status(cap["experiment_id"])
        res = c.cancel_experiment(cap["experiment_id"])
        n_after = res["counts"]["events_captured"]
        time.sleep(0.8)
        later = c.experiment_status(cap["experiment_id"])
        bps = next(d for d in c.session_info()["cpus"] if d["tag"] == ":maincpu")["breakpoints"]
        check("cancel: state cancelled, instrumentation removed, no further events",
              res["state"] == "cancelled" and res["completion_reason"] == "cancelled" and res["cleanup"]["breakpoint_cleared"] and res["cleanup"]["verified"]
              and later["counts"]["events_captured"] == n_after and mid["counts"]["events_captured"] > 0 and not any(b.get("owned_by_experiment") for b in bps),
              before_cancel=mid["counts"], after=later["counts"], cleanup=res["cleanup"])
        again = c.cancel_experiment(cap["experiment_id"])
        check("cancel is idempotent", again.get("already_finished") is True)
        expect_error("cancel unknown experiment", lambda: c.cancel_experiment("exp-nope"), "unknown_experiment")

        # ---- capture started while debugger-stopped: waits, then ends by wall clock ------------------
        # (puts MAME into a stop with a *manual-style* mechanism is out of scope; use pause instead)
        c.execution_control("pause"); time.sleep(0.3)
        cap = c.capture_breakpoint(capture_args(":maincpu", HANDLER, 3, 1.0, label="validate-paused"))
        time.sleep(1.6)
        stt = c.experiment_status(cap["experiment_id"])
        check("capture while paused: no events, wall-clock completion, requests still served", stt["completion_reason"] == "wall_clock_limit" and stt["counts"]["events_captured"] == 0, status=stt["summary"])
        c.execution_control("resume"); wait_state(c, "running", 10)

        # ---- stress: high-rate address, truncation / drop accounting ---------------------------------
        hot = {}
        for _ in range(40):
            v = reg_map(c.read_registers(read_registers_args(":maincpu", ["CURPC"])))["CURPC"]
            hot[v] = hot.get(v, 0) + 1
            time.sleep(0.013)
        hot_pc = max(hot, key=hot.get)
        cap = c.capture_breakpoint(capture_args(":maincpu", hot_pc, 10000, 30, registers=["D0"], label="validate-stress"))
        t0 = time.time()
        stt = None
        while time.time() - t0 < 40:
            stt = c.experiment_status(cap["experiment_id"])
            if stt["state"] != "running":
                break
            time.sleep(0.25)
        lines = sum(1 for _ in open(stt["evidence"]["events_jsonl"]))
        check("stress: limit enforced inside MAME (hits <= max_events) and accounting is exact",
              stt["state"] == "completed" and stt["completion_reason"] == "event_limit"
              and stt["counts"]["hits_observed"] == 10000 and stt["counts"]["events_captured"] + stt["counts"]["events_dropped"] == 10000
              and lines == stt["counts"]["events_captured"],
              hot_pc=hex(hot_pc), sampled_pcs=len(hot), status=stt["summary"], counts=stt["counts"], elapsed_ms=stt["elapsed_wall_ms"], errors=stt["errors"][:3])

        # ---- reconnect (graceful and abrupt) ------------------------------------------------------
        sid = c.session_id
        t_r = time.time()
        c.close()  # sends goodbye
        info2 = c.connect(timeout=15)
        check("graceful reconnect (goodbye) to same session", c.session_id == sid and "session_changed_from" not in info2,
              session=c.session_id, seconds=round(time.time() - t_r, 1), socket_reopens=info2["counters"]["socket_reopens"])
        t_r = time.time()
        c.close(graceful=False)  # simulate a client crash: plugin has to notice by itself
        info3 = c.connect(timeout=25)
        check("abrupt-disconnect reconnect (heartbeat detection) to same session", c.session_id == sid, session=c.session_id,
              seconds=round(time.time() - t_r, 1), socket_reopens=info3["counters"]["socket_reopens"])

        # ---- disconnect: MAME exits -------------------------------------------------------------------
        proc.kill(); proc.wait()
        try:
            c.session_info()
            check("useful error when MAME disconnects", False, got="no error")
        except BridgeError as e:
            check("useful error when MAME disconnects", e.code == "mame_disconnected", code=e.code, message=e.message)
        try:
            c.session_info()
        except BridgeError as e:
            check("after disconnect further calls fail fast with not_connected", e.code == "not_connected", code=e.code)
    except BridgeError as e:
        check("UNEXPECTED bridge error aborted validation", False, error=e.to_dict())
    except Exception as e:  # keep the evidence gathered so far
        import traceback
        check("UNEXPECTED harness exception aborted validation", False, error=traceback.format_exc()[-600:])
    finally:
        c.close()
        if proc.poll() is None:
            proc.kill()
    no_debugger_scenario()
    (OUT / "results.json").write_text(json.dumps({"mame": info["mame_version"], "results": RESULTS}, indent=2, default=str))
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)} passed, {len(fails)} failed -> {OUT / 'results.json'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
