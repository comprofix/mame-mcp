"""EXAMPLE CLIENT (not a server tool): bounded bank-select / bank-use capture for ONE machine described by an explicit config file.

This script is a client of the production MCP server. It uses only the public tools (start_session, execution_control,
press_sequence, capture_watchpoint, capture_breakpoint, experiment_status, read_memory, disassemble_memory, stop_session)
through the official MCP SDK. It is NOT part of the server, adds no MCP capability and has no Lua or debugger access of its own.

Nothing is assumed about any machine: the system, CPU, every address, the registers, the limits and the optional bank signatures
come from the config file you pass, and a missing field is an error. In particular it never guesses a bank-select address, a
window, an entry count or which byte identifies an entry. Machine-specific values belong in the config (kept outside this
file); one-off investigations of a single ROM stay in the git-ignored research/ workspace.

Usage:
  .venv\\Scripts\\python -I examples\\bank_capture.py --config CONFIG.json --out OUT_DIR [--mame-dir DIR] [--rompath DIR]
  .venv\\Scripts\\python -I examples\\bank_capture.py --config CONFIG.json --check     (validate the config only; starts nothing)

Config (JSON object; every key below marked required must be present):
  system        required  MAME short name, e.g. "shinobi"
  cpu           required  device tag, e.g. ":maincpu"
  registers     required  list (0..6) of register names to record at every hit ([] for none)
  warmup_seconds required number 0..30: emulation runs this long (wall clock) before the first capture
  limits        required  {"max_events": 1..10000, "wall_seconds": 1..120 per capture, "total_wall_seconds": 10..1800 whole run}
  select        optional  watch the bank-SELECT register: {"space": "program"|"data"|"io", "address": n, "length": n,
                          "access": "read"|"write"|"read_write"}; events show which value is written and from which PC
  sites         optional  breakpoint sites [{"address": n, "label": "text"}] (1..8): one capture_breakpoint per site
  sample        optional  memory sampled at every breakpoint hit: {"address": n, "size": 1|2|4} in the program space of `cpu`
  signatures    optional  {"<sampled value, hex>": "<entry label>"} naming which entry a sampled value identifies. It is
                          evidence you supply (e.g. from a ROM comparison); an unmatched value is reported as unmatched
  inputs        optional  press_sequence steps [{"port","field","start","frames"}] run once after warmup (e.g. coin, start)
  disassemble   optional  {"count": 1..16}: pause, disassemble that many instructions at up to 4 distinct select-writer PCs, resume
  bank_context  optional  true: after each capture read the selected entry of every memory bank (read_memory bank_context)
At least one of `select` and `sites` is required; `signatures` needs `sample`; `disassemble` needs `select`.

Reading the result: events carry LOGICAL CPU addresses. A select event says what was written to the select register and by which
instruction; it does not say which bank any later access used. A site/breakpoint event's entry is only as good as the signature
you configured (the sampled bytes are what the CPU saw at that hit). summary.json holds everything, including evidence paths.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

MAX_REGISTERS = 6
MAX_SITES = 8
MAX_EVENTS = 10000
MAX_SELECT_WRITER_PCS = 4
_TAG = re.compile(r"^:[A-Za-z0-9_:.\-]+$")
_SYSTEM = re.compile(r"^[a-z0-9_]{1,32}$")
_REG = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    pass


def _num(cfg: dict, key: str, lo: float, hi: float, where: str) -> float:
    v = cfg.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ConfigError(f"{where}{key} must be a number in [{lo}, {hi}]")
    return v


def _uint(v: Any, what: str, hi: int = 0xFFFFFFFF, lo: int = 0) -> int:
    if isinstance(v, str) and re.fullmatch(r"0[xX][0-9a-fA-F]+", v):
        v = int(v, 16)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ConfigError(f"{what} must be an integer in [{lo}, {hi}] (or a 0x hex string)")
    return v


def validate_config(cfg: Any) -> dict[str, Any]:
    """Validate and normalise a config. Every machine-specific value must be present: nothing is defaulted."""
    if not isinstance(cfg, dict):
        raise ConfigError("the config must be a JSON object")
    allowed = {"system", "cpu", "registers", "warmup_seconds", "limits", "select", "sites", "sample", "signatures", "inputs", "disassemble", "bank_context"}
    if extra := set(cfg) - allowed:
        raise ConfigError(f"unknown config key(s): {sorted(extra)}")
    for key in ("system", "cpu", "registers", "warmup_seconds", "limits"):
        if key not in cfg:
            raise ConfigError(f"missing required key '{key}' (there are no defaults)")
    if not isinstance(cfg["system"], str) or not _SYSTEM.fullmatch(cfg["system"]):
        raise ConfigError("system must be a MAME short name (lower-case letters, digits, underscore)")
    if not isinstance(cfg["cpu"], str) or not _TAG.fullmatch(cfg["cpu"]):
        raise ConfigError("cpu must be a full device tag such as ':maincpu'")
    regs = cfg["registers"]
    if not isinstance(regs, list) or len(regs) > MAX_REGISTERS or not all(isinstance(r, str) and _REG.fullmatch(r) for r in regs) or len(set(regs)) != len(regs):
        raise ConfigError(f"registers must be a list of at most {MAX_REGISTERS} distinct register names")
    out: dict[str, Any] = {"system": cfg["system"], "cpu": cfg["cpu"], "registers": list(regs)}
    out["warmup_seconds"] = _num(cfg, "warmup_seconds", 0, 30, "")
    lim = cfg["limits"]
    if not isinstance(lim, dict) or set(lim) != {"max_events", "wall_seconds", "total_wall_seconds"}:
        raise ConfigError("limits must be an object with exactly max_events, wall_seconds and total_wall_seconds")
    out["limits"] = {"max_events": _uint(lim["max_events"], "limits.max_events", MAX_EVENTS, 1),
                     "wall_seconds": _num(lim, "wall_seconds", 1, 120, "limits."), "total_wall_seconds": _num(lim, "total_wall_seconds", 10, 1800, "limits.")}
    if "select" not in cfg and "sites" not in cfg:
        raise ConfigError("give at least one of 'select' (watch the bank-select register) and 'sites' (breakpoint sites)")
    if "select" in cfg:
        s = cfg["select"]
        if not isinstance(s, dict) or set(s) != {"space", "address", "length", "access"}:
            raise ConfigError("select must be an object with exactly space, address, length and access")
        if s["space"] not in ("program", "data", "io") or s["access"] not in ("read", "write", "read_write"):
            raise ConfigError("select.space must be program|data|io and select.access read|write|read_write")
        out["select"] = {"space": s["space"], "address": _uint(s["address"], "select.address"), "length": _uint(s["length"], "select.length", 0x100000000, 1), "access": s["access"]}
    if "sites" in cfg:
        sites = cfg["sites"]
        if not isinstance(sites, list) or not 1 <= len(sites) <= MAX_SITES:
            raise ConfigError(f"sites must be a list of 1..{MAX_SITES} objects")
        norm, seen = [], set()
        for i, site in enumerate(sites):
            if not isinstance(site, dict) or not set(site) <= {"address", "label"} or "address" not in site:
                raise ConfigError(f"sites[{i}] must be an object with 'address' and an optional 'label'")
            a = _uint(site["address"], f"sites[{i}].address")
            if a in seen:
                raise ConfigError(f"duplicate site address {a:#x}")
            seen.add(a)
            label = site.get("label", "")
            if not isinstance(label, str) or len(label) > 120:
                raise ConfigError(f"sites[{i}].label must be a string of at most 120 characters")
            norm.append({"address": a, "label": label})
        out["sites"] = norm
    if "sample" in cfg:
        smp = cfg["sample"]
        if not isinstance(smp, dict) or set(smp) != {"address", "size"} or smp["size"] not in (1, 2, 4):
            raise ConfigError("sample must be an object with exactly address and size (1, 2 or 4)")
        out["sample"] = {"address": _uint(smp["address"], "sample.address"), "size": smp["size"]}
    if "sites" not in cfg and "sample" in cfg:
        raise ConfigError("sample is only used at breakpoint sites; give 'sites' too")
    if "signatures" in cfg:
        if "sample" not in out:
            raise ConfigError("signatures need a sample (they name what a sampled value identifies)")
        sig = cfg["signatures"]
        if not isinstance(sig, dict) or not sig:
            raise ConfigError("signatures must be a non-empty object {\"<hex value>\": \"<entry label>\"}")
        norm_sig = {}
        for k, v in sig.items():
            val = _uint(k if k.lower().startswith("0x") else "0x" + k, f"signatures key {k!r}", (1 << (8 * out["sample"]["size"])) - 1)
            if not isinstance(v, (str, int)) or isinstance(v, bool):
                raise ConfigError("a signature's entry label must be a string or integer")
            if val in norm_sig:
                raise ConfigError(f"duplicate signature value {val:#x}")
            norm_sig[val] = v
        out["signatures"] = norm_sig
    if "inputs" in cfg:
        steps = cfg["inputs"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
            raise ConfigError("inputs must be a list of 1..64 press_sequence steps {port, field, start, frames}")
        for i, st in enumerate(steps):
            if not isinstance(st, dict) or set(st) != {"port", "field", "start", "frames"}:
                raise ConfigError(f"inputs[{i}] must be an object with exactly port, field, start and frames")
        out["inputs"] = steps  # the server validates the whole sequence before pressing anything
    if "disassemble" in cfg:
        if "select" not in out:
            raise ConfigError("disassemble needs 'select' (it decodes the instructions that wrote the select register)")
        d = cfg["disassemble"]
        if not isinstance(d, dict) or set(d) != {"count"}:
            raise ConfigError("disassemble must be {\"count\": 1..16}")
        out["disassemble"] = {"count": _uint(d["count"], "disassemble.count", 16, 1)}
    if "bank_context" in cfg:
        if not isinstance(cfg["bank_context"], bool):
            raise ConfigError("bank_context must be true or false")
        if cfg["bank_context"] and "sample" not in out:
            raise ConfigError("bank_context reads memory next to the sample; give 'sample'")
        out["bank_context"] = cfg["bank_context"]
    return out


# ------------------------------------------------------------------------------------------------------------ MCP client
def _body(res) -> dict[str, Any]:
    if res.is_error:
        text = res.content[0].text if res.content else ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return {"_error": json.loads(m.group(0)) if m else {"message": text}}
        except ValueError:
            return {"_error": {"message": text}}
    return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)


def _read_events(status: dict[str, Any]) -> list[dict[str, Any]]:
    path = (status.get("evidence") or {}).get("events_jsonl")
    if not path or not Path(path).is_file():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


async def run(cfg: dict[str, Any], out_dir: Path, mame_dir: str | None, rompath: str | None) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    t_start = time.monotonic()
    deadline = t_start + cfg["limits"]["total_wall_seconds"]
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    if mame_dir:
        env["MAMEMCP_MAME_DIR"] = mame_dir
    if rompath:
        env["MAMEMCP_ROMPATH"] = rompath
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"], env=env)
    summary: dict[str, Any] = {"config": {k: (v if k != "signatures" else {hex(a): b for a, b in v.items()}) for k, v in cfg.items()},
                               "captures": [], "notes": [], "result": "incomplete"}
    errlog = open(out_dir / "server_stderr.txt", "w")
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()

                async def call(name: str, **args: Any) -> dict[str, Any]:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"total_wall_seconds ({cfg['limits']['total_wall_seconds']}) exceeded before {name}")
                    return _body(await s.call_tool(name, args))

                started = await call("start_session", system=cfg["system"], ready_timeout_s=90)
                if "_error" in started:
                    summary["result"] = "start_session failed"
                    summary["error"] = started["_error"]
                    return summary
                summary["session"] = {k: started.get(k) for k in ("session_id", "run_id", "system", "evidence_dir")}
                try:
                    info = await call("session_info")
                    cpus = {c["tag"]: c for c in info.get("cpus", [])}
                    if cfg["cpu"] not in cpus:
                        summary["result"] = "config error"
                        summary["error"] = f"cpu {cfg['cpu']} is not a CPU of {cfg['system']}: this machine has {sorted(cpus)}"
                        return summary
                    summary["cpu_address_spaces"] = [a.get("name") for a in cpus[cfg["cpu"]].get("address_spaces", [])]
                    await call("execution_control", action="resume")
                    await asyncio.sleep(cfg["warmup_seconds"])
                    if "inputs" in cfg:
                        seq = await call("press_sequence", steps=cfg["inputs"], wait=True)
                        summary["inputs"] = {k: seq.get(k) for k in ("state", "end_reason", "kind")} | {"sequence": seq.get("sequence"), "error": seq.get("_error")}

                    async def finish(label: str, cap: dict[str, Any]) -> dict[str, Any]:
                        if "_error" in cap:
                            return {"label": label, "started": False, "error": cap["_error"]}
                        end = time.monotonic() + cfg["limits"]["wall_seconds"] + 15
                        st: dict[str, Any] = {}
                        while time.monotonic() < end:
                            st = await call("experiment_status", experiment_id=cap["experiment_id"])
                            if st.get("state") != "running":
                                break
                            await asyncio.sleep(0.5)
                        return {"label": label, "started": True, "experiment_id": cap["experiment_id"], "state": st.get("state"),
                                "completion_reason": st.get("completion_reason"), "counts": st.get("counts"), "cleanup_verified": (st.get("cleanup") or {}).get("verified"),
                                "evidence": st.get("evidence"), "_status": st}

                    lim = cfg["limits"]
                    if "select" in cfg:
                        sel = cfg["select"]
                        cap = await call("capture_watchpoint", cpu=cfg["cpu"], space=sel["space"], address=sel["address"], length=sel["length"], access=sel["access"],
                                         max_events=lim["max_events"], max_wall_seconds=lim["wall_seconds"], registers=cfg["registers"], label="bank_capture select")
                        r = await finish("select", cap)
                        events = _read_events(r.pop("_status", {}))
                        values: dict[str, dict[str, int]] = {}  # access type -> data value -> count
                        writers: dict[str, int] = {}
                        for e in events:
                            per = values.setdefault(str(e.get("access")), {})
                            per[e["data"]] = per.get(e["data"], 0) + 1
                            writers[e["pc"]] = writers.get(e["pc"], 0) + 1
                        r.update(kind="select_watchpoint", watched=sel, events_read=len(events), data_values_by_access=values, accessing_pcs=writers,
                                 first_events=events[:5],
                                 note="logical addresses and values as the CPU issued them; no event is attributed to a bank")
                        summary["captures"].append(r)
                        if "disassemble" in cfg and writers:
                            await call("execution_control", action="pause")
                            await asyncio.sleep(0.5)
                            dis = []
                            for pc in sorted(writers, key=lambda p: -writers[p])[:MAX_SELECT_WRITER_PCS]:
                                d = await call("disassemble_memory", cpu=cfg["cpu"], address=int(pc, 16), count=cfg["disassemble"]["count"])
                                dis.append({"pc": pc, "error": d.get("_error"), "instructions": [(i["address"], i["text"]) for i in d.get("instructions", [])], "mapping_banked": (d.get("mapping") or {}).get("banked")})
                            r["writer_disassembly"] = dis
                            await call("execution_control", action="resume")
                    for site in cfg.get("sites", []):
                        args: dict[str, Any] = dict(cpu=cfg["cpu"], address=site["address"], max_events=lim["max_events"], max_wall_seconds=lim["wall_seconds"],
                                                    registers=cfg["registers"], label=f"bank_capture site {site['address']:#x}")
                        if "sample" in cfg:
                            # no "space" key: the field defaults to the program space (capture_breakpoint's advertised schema types every
                            # memory value as an integer, so a string space would be refused by the MCP argument validation)
                            args["memory"] = [{"address": cfg["sample"]["address"], "size": cfg["sample"]["size"]}]
                        r = await finish(f"site {site['address']:#x}", await call("capture_breakpoint", **args))
                        events = _read_events(r.pop("_status", {}))
                        rows = []
                        for e in events:
                            row = {"seq": e["seq"], "pc": e.get("pc"), "regs": e.get("regs")}
                            if "sample" in cfg and e.get("mem"):
                                v = int(e["mem"][0]["value"], 16)
                                row["sampled"] = e["mem"][0]["value"]
                                if "signatures" in cfg:
                                    row["entry_from_signature"] = cfg["signatures"].get(v)  # None = no configured signature matched
                            rows.append(row)
                        r.update(kind="site_breakpoint", site=site, events=rows)
                        if "signatures" in cfg:
                            r["entries_seen"] = sorted({str(x["entry_from_signature"]) for x in rows if x["entry_from_signature"] is not None})
                            r["unmatched_samples"] = sum(1 for x in rows if x.get("entry_from_signature") is None)
                        if cfg.get("bank_context") and "sample" in cfg:
                            bc = await call("read_memory", cpu=cfg["cpu"], address=cfg["sample"]["address"], length=cfg["sample"]["size"], bank_context=True)
                            r["bank_context_after"] = bc.get("bank_context") or bc.get("_error")
                        summary["captures"].append(r)
                    summary["result"] = "complete"
                finally:
                    summary["stop"] = await call_stop(s)
    finally:
        errlog.close()
    summary["wall_seconds"] = round(time.monotonic() - t_start, 1)
    return summary


async def call_stop(s) -> dict[str, Any]:
    r = _body(await s.call_tool("stop_session", {}))
    return {k: r.get(k) for k in ("state", "stopped", "process", "capture_cleanup", "input_cleanup", "_error")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bounded bank-select / bank-use capture for one machine described by an explicit config (MCP client example)")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="output directory (required unless --check); must not already contain summary.json")
    ap.add_argument("--check", action="store_true", help="validate the config and exit; starts nothing")
    ap.add_argument("--mame-dir")
    ap.add_argument("--rompath")
    a = ap.parse_args(argv)
    try:
        cfg = validate_config(json.loads(a.config.read_text()))
    except (OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if a.check:
        print(json.dumps({"config": "valid", "system": cfg["system"], "cpu": cfg["cpu"]}))
        return 0
    if a.out is None:
        print("--out is required (there is no default output location)", file=sys.stderr)
        return 2
    a.out.mkdir(parents=True, exist_ok=True)
    if (a.out / "summary.json").exists():
        print(f"{a.out / 'summary.json'} already exists; choose another --out (nothing is overwritten)", file=sys.stderr)
        return 2
    summary = asyncio.run(run(cfg, a.out, a.mame_dir, a.rompath))
    (a.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    for c in summary.get("captures", []):
        print(f"{c['label']}: {c.get('state')} {c.get('completion_reason')} counts={c.get('counts')} cleanup_verified={c.get('cleanup_verified')}")
    print(f"result={summary['result']} -> {a.out / 'summary.json'}")
    return 0 if summary["result"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
