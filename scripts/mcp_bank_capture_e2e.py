"""examples/bank_capture.py run end to end on materially different machines (real MAME 0.289, official MCP SDK, production server).

The example is a CLIENT of the server (it starts its own server over stdio and uses only public tools); this harness runs it as a
subprocess with an explicit config file per machine and checks its summary.json against the fixture expectations:

  1942   Z80 :maincpu, banked ROM window (0x8000..0xBFFF), select register watched, two breakpoint sites with a sampled window byte
         and configured signatures, scripted coin/start inputs, disassembly of the select writers, bank_context read
  1944   Z80 :audiocpu, bank switch through the QSound handler: select watched, disassembly of the writer; no sites, no signatures
  shinobi 68000 :maincpu, NOT banked: watch RAM writes, one site, sampled word, disassembly (the example makes no bank claim)
  refusals  a config with no system, a CPU the machine does not have (reported, nothing started beyond the session), an existing --out

Machine-specific configs come from tests/fixtures/profiles/watch_disasm_sequence.json (the example itself carries none).
Writes <runs>/mcp_bank_capture_e2e/results.json.   Usage: .venv\\Scripts\\python -I scripts\\mcp_bank_capture_e2e.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import MAME_DIR, ROMPATH, Results  # noqa: E402
from _inst import profile, runs_root  # noqa: E402

from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

OUT = runs_root() / "mcp_bank_capture_e2e"
P = profile()
R = Results("mcp_bank_capture_e2e")
check, note = R.check, R.note
EXAMPLE = PROJECT_ROOT / "examples" / "bank_capture.py"


def run_example(name: str, cfg: dict, timeout: float = 420) -> tuple[int, dict | None, str]:
    d = OUT / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    cfg_path = d / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=1))
    p = subprocess.run([sys.executable, "-I", str(EXAMPLE), "--config", str(cfg_path), "--out", str(d / "out"), "--mame-dir", str(MAME_DIR), "--rompath", str(ROMPATH)],
                       capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_ROOT))
    summ = d / "out" / "summary.json"
    return p.returncode, (json.loads(summ.read_text()) if summ.is_file() else None), p.stdout + p.stderr


def caps(summary: dict) -> dict[str, dict]:
    return {c["label"]: c for c in summary["captures"]}


def common(label: str, summary: dict | None, rc: int, out: str, n_expected: int) -> dict[str, dict]:
    ok = summary is not None and rc == 0 and summary["result"] == "complete"
    check(f"{label}: exit 0, result complete, session stopped cleanly with nothing left behind", ok and summary["stop"]["stopped"] is True and (summary["stop"].get("capture_cleanup") or {}).get("verified") is True
          and (summary["stop"].get("capture_cleanup") or {}).get("unresolved_instrumentation") == [], rc=rc, result=(summary or {}).get("result"), error=(summary or {}).get("error"), out=out[-300:])
    if not ok:
        return {}
    c = caps(summary)
    check(f"{label}: {n_expected} capture(s), each ended by its own limit (not an error) with verified cleanup and no dropped events", len(c) == n_expected and all(
        v.get("started") and v["state"] == "completed" and v["completion_reason"] in ("event_limit", "wall_clock_limit") and v["cleanup_verified"] is True and v["counts"]["events_dropped"] == 0 for v in c.values()),
        captures={k: (v.get("state"), v.get("completion_reason"), v.get("error")) for k, v in c.items()})
    return c


def main_run() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # ---- 1942 --------------------------------------------------------------------------------------------------------------
    c1942 = P["1942"]["bank_capture_config"]
    rc, summ, out = run_example("1942", c1942)
    cs = common("1942", summ, rc, out, 3)
    if cs:
        exp = P["1942"]["bank_capture_expect"]
        sel = cs["select"]
        vals = {v for per in sel["data_values_by_access"].values() for v in per}
        check("1942 select watch: only write events with values 0..3, from named instruction addresses, no bank attributed", set(sel["data_values_by_access"]) == {"write"} and vals <= set(exp["select_values_seen_subset_of"])
              and sel["events_read"] == c1942["limits"]["max_events"] and "no event is attributed to a bank" in sel["note"] and all(e.get("regs") for e in sel["first_events"]), values=sorted(vals), pcs=sel["accessing_pcs"])
        dis = sel.get("writer_disassembly", [])
        check("1942 the select writers were disassembled while paused (the example paused and resumed the machine itself), with MAME's text", bool(dis) and all(d["instructions"] and d["error"] is None for d in dis)
              and all(i[1] for d in dis for i in d["instructions"]), writers=[(d["pc"], d["instructions"][:2]) for d in dis])
        for site_label, want in (("site 0x11", exp["site_entries"]["0x11"]), ("site 0x2ba2", exp["site_entries"]["0x2BA2"])):
            sc = cs[site_label]
            check(f"1942 {site_label}: entry identified from the configured signature only ({want}); no unmatched samples; registers recorded", sc["entries_seen"] == want and sc["unmatched_samples"] == 0 and all(e["regs"] for e in sc["events"]) and len(sc["events"]) == 6,
                  entries=sc["entries_seen"], unmatched=sc["unmatched_samples"])
            bc = sc.get("bank_context_after") or {}
            check(f"1942 {site_label}: bank_context read after the capture is labelled as such (not attributed to the hits)", "banks" in bc and "emulation does not advance" in bc.get("sampled", ""), keys=sorted(bc))
        check("1942 the scripted inputs ran through press_sequence and were released", (summ.get("inputs") or {}).get("state") == "released" and summ["inputs"]["error"] is None, inputs=summ.get("inputs", {}).get("state"))
    # ---- 1944 --------------------------------------------------------------------------------------------------------------
    c1944 = P["1944"]["bank_capture_config"]
    rc, summ, out = run_example("1944", c1944)
    cs = common("1944", summ, rc, out, 1)
    if cs:
        sel = cs["select"]
        dis = sel.get("writer_disassembly", [])
        check("1944 audio Z80: the QSound bank-switch register writes were captured on a different CPU/architecture with the same unmodified script (a decrypted 68000 sits next to it)", sel["events_read"] == c1944["limits"]["max_events"]
              and set(sel["data_values_by_access"]) == {"write"} and bool(sel["accessing_pcs"]) and bool(dis) and dis[0]["instructions"][0][1].startswith("ld"), pcs=sel["accessing_pcs"], values=sel["data_values_by_access"], dis=dis[:1])
    # ---- shinobi 68000 (not banked) ----------------------------------------------------------------------------------------
    cm = P["shinobi"]["m68k"]["bank_capture_config"]
    rc, summ, out = run_example("shinobi", cm)
    cs = common("shinobi", summ, rc, out, 2)
    if cs:
        site = cs["site 0x404"]
        check("shinobi 68000: the site breakpoint hit with D0/SR recorded and a sampled word; with no signatures configured no entry is claimed", len(site["events"]) == 8 and all("sampled" in e and e["regs"] and "entry_from_signature" not in e for e in site["events"])
              and "entries_seen" not in site, first=site["events"][:1])
        check("shinobi 68000: select (RAM write) events are reported as logical accesses only; variable-length instructions at the writers were disassembled", cs["select"]["events_read"] == 8 and cs["select"].get("writer_disassembly") is not None, pcs=cs["select"]["accessing_pcs"])
    # ---- refusals ----------------------------------------------------------------------------------------------------------
    bad = {k: v for k, v in c1942.items() if k != "system"}
    rc, summ, out = run_example("refuse_missing_system", bad, timeout=60)
    check("refusal: a config without a system is refused before anything starts (exit 2, no summary, no session)", rc == 2 and summ is None and "missing required key 'system'" in out, rc=rc, out=out[-200:])
    wrong = {**c1942, "cpu": ":nosuchcpu"}
    rc, summ, out = run_example("refuse_wrong_cpu", wrong, timeout=300)
    check("refusal: a CPU the machine does not have is reported with the machine's CPU list; the session is stopped; exit 1", rc == 1 and summ is not None and summ["result"] == "config error" and ":maincpu" in summ["error"]
          and summ["stop"]["stopped"] is True and summ["captures"] == [], rc=rc, error=(summ or {}).get("error"))
    d = OUT / "1942"
    p = subprocess.run([sys.executable, "-I", str(EXAMPLE), "--config", str(d / "config.json"), "--out", str(d / "out")], capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT))
    check("refusal: an --out that already holds a summary is never overwritten", p.returncode == 2 and "already exists" in p.stderr, rc=p.returncode, err=p.stderr[-200:])


def main() -> int:
    try:
        main_run()
    except Exception as e:
        import traceback
        check("example harness completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-900:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
