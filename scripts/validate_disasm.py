"""disassemble_memory against real MAME 0.289 (direct bridge; the TEST-ONLY fixture plugin is the independent observer).

Independent observers, per instruction: MAME's own `dasm` command run by the fixture with the raw-bytes column (text, address
and therefore length), and the bytes read with the fixture's own normal-side-effect program-space reads (read_u8).

Sections: Z (shinobi Z80 + 68000: variable lengths, alignment, ranges, refusals) , B (1942 Z80 bank window, entry by entry,
checked against the fixture's bank list), O (1944 68000 opcodes-vs-program view, 1944 audio Z80 bank, device without an
interface).  Usage: .venv\\Scripts\\python -I scripts\\validate_disasm.py [Z B O]
Output: <runs>\\disasm\\results.json (<runs> = $MAMEMCP_HARNESS_RUNS or runs\\)
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Results  # noqa: E402
from _inst import H, Inst, code, profile, runs_root  # noqa: E402

OUT = runs_root() / "disasm"
P = profile()
R = Results("disasm")
check, note = R.check, R.note
RAW = re.compile(r"^([0-9A-F]+):\s+((?:[0-9A-F]+\s+)+)(.*)$")


def independent(m: Inst, cpu: str, address: int, length: int) -> list[dict]:
    """MAME's own dasm (raw bytes on) parsed into {address, bytes_hex (lowercase), text}; length from the next address."""
    out = []
    for line in m.fx.call("dasm_run", cpu=cpu, address=address, length=length, raw=True)["lines"]:
        mt = RAW.match(line)
        if not mt:
            continue
        out.append({"address": int(mt.group(1), 16), "bytes_hex": re.sub(r"\s+", "", mt.group(2)).lower(), "text": mt.group(3).strip()})
    for i, x in enumerate(out[:-1]):
        x["length"] = out[i + 1]["address"] - x["address"]
    return out


def fixture_bytes(m: Inst, cpu: str, space: str, address: int, n: int) -> str:
    return "".join(f"{m.fx.call('read_normal', cpu=cpu, space=space, address=address + i)['value'] & 0xFF:02x}" for i in range(n))


def compare(m: Inst, label: str, cpu: str, address: int, count: int, expect_bytes: list[str] | None = None, expect_lengths: list[int] | None = None, *, space: str = "program") -> dict:
    d = m.rq("disassemble", {"cpu": cpu, "address": address, "count": count})
    ins = d["instructions"]
    total = sum(i["length"] for i in ins)
    ind = independent(m, cpu, address, total + 1)
    ours_text = [(H(i["address"]), i["text"], i["length"]) for i in ins]
    ind_text = [(x["address"], x["text"], x.get("length")) for x in ind[:len(ins)]]
    check(f"{label}: {len(ins)} instructions; address, text and length equal MAME's own dasm (independent run)", len(ins) == count and ours_text == ind_text, ours=ours_text[:4], independent=ind_text[:4])
    wire = [bytes_hex for bytes_hex in (i["bytes_hex"] for i in ins)]
    ind_hex = [x["bytes_hex"] for x in ind[:len(ins)]]
    check(f"{label}: bytes_hex equals the raw bytes column of the independent dasm", wire == ind_hex, ours=wire[:4], independent=ind_hex[:4])
    read = [fixture_bytes(m, cpu, space, H(i["address"]), i["length"]) for i in ins]
    if d["instruction_view"]["used"] == "program":
        check(f"{label}: bytes_hex equals bytes read through the program space by an independent normal read", wire == read, ours=wire[:4], read=read[:4])
    if expect_bytes is not None:
        check(f"{label}: bytes and lengths are the ROM's (fixture expectation)", wire == expect_bytes and [i["length"] for i in ins] == expect_lengths, got=wire, lengths=[i["length"] for i in ins])
    check(f"{label}: next_address follows the last instruction", H(d["next_address"]) == address + total, next=d["next_address"], expect=hex(address + total))
    return d


def section_z() -> None:
    m = Inst(OUT, "shinobi", 9641, "shinobi")
    try:
        z, k = P["shinobi"]["z80"], P["shinobi"]["m68k"]
        zd, kd = z["disasm"], k["disasm"]
        m.stop_debugger()
        d = compare(m, "Z1 Z80 (shinobi :soundcpu)", z["cpu"], H(zd["address"]), zd["count"], zd["expected_bytes"], zd["expected_lengths"])
        check("Z1 the instruction view is the program space (no opcodes space) and the result says so", d["instruction_view"]["used"] == "program" and d["instruction_view"]["separate_opcode_space"] is False and d["instruction_view"]["selectable"] is False,
              view=d["instruction_view"]["used"])
        check("Z1 mapping context: declared map run is ROM, not banked, with the logical-address caveat",
              d["mapping"]["banked"] is False and d["mapping"]["runs"][0]["read_handler"] == "rom" and "not ROM file offsets" in d["address_units"], mapping=d["mapping"]["runs"][:1])
        d = compare(m, "Z2 68000 (shinobi :maincpu)", k["cpu"], H(kd["address"]), kd["count"], kd["expected_bytes"], kd["expected_lengths"])
        check("Z2 variable lengths (4,4,2 bytes) and big-endian byte order are reported as stored", [i["length"] for i in d["instructions"]] == [4, 4, 2] and d["instructions"][0]["bytes_hex"].startswith("6000"), lengths=[i["length"] for i in d["instructions"]])
        # a longer run: lengths and addresses stay consistent with the independent decoder over 40 instructions of each CPU
        for label, cpu, addr in (("Z3 Z80 40 instructions", z["cpu"], 0x100), ("Z3 68000 40 instructions", k["cpu"], 0x1000)):
            d = m.rq("disassemble", {"cpu": cpu, "address": addr, "count": 40})
            total = sum(i["length"] for i in d["instructions"])
            ind = independent(m, cpu, addr, total + 1)
            same = [(H(i["address"]), i["text"], i["length"]) for i in d["instructions"]] == [(x["address"], x["text"], x.get("length")) for x in ind[:40]]
            check(f"{label}: addresses, text and lengths equal the independent dasm", same and d["count_returned"] == 40, count=d["count_returned"], lengths=sorted({i["length"] for i in d["instructions"]}))
        # alignment warning on the 68000 and the decoding still being MAME's
        d = m.rq("disassemble", {"cpu": k["cpu"], "address": H(kd["odd_address"]), "count": 2})
        check("Z4 an odd 68000 start address is decoded but flagged (the decoding may be meaningless)", any("aligned" in w for w in d["warnings"]) and d["count_returned"] == 2, warnings=d["warnings"])
        # end of the address space: fewer instructions than requested is reported, not hidden
        d = m.rq("disassemble", {"cpu": z["cpu"], "address": 0xFFFC, "count": 8})
        check("Z5 near the end of the 16-bit address space fewer instructions come back and a warning says so", d["count_returned"] < 8 and any("fit before the end" in w for w in d["warnings"]), returned=d["count_returned"], warnings=d["warnings"])
        # execution state: the paused and the debugger-stopped states both work; running is refused and the machine is not paused for the caller
        m.resume()
        e = m.err("disassemble", {"cpu": z["cpu"], "address": H(zd["address"]), "count": 1})
        check("Z6 running execution is refused with execution_running and the machine keeps running", code(e) == "execution_running" and m.state() == "running", code=code(e), state=m.state())
        m.pause()
        d = m.rq("disassemble", {"cpu": z["cpu"], "address": H(zd["address"]), "count": 2})
        check("Z6 paused execution is accepted, and the call neither resumed nor advanced emulation", d["execution"]["state"] == "paused" and m.state() == "paused", state=d["execution"]["state"])
        f0 = m.info_()["clock"]["frame"]
        for _ in range(3):
            m.rq("disassemble", {"cpu": k["cpu"], "address": H(kd["address"]), "count": 3})
        check("Z6 repeated disassembly while paused leaves the frame counter unchanged", m.info_()["clock"]["frame"] == f0, frame=f0)
        # refusals
        base = {"cpu": z["cpu"], "address": 0x100, "count": 2}
        cases = [("count 0", {**base, "count": 0}, "bad_argument"), ("count 65", {**base, "count": 65}, "bad_argument"), ("address past the mask", {**base, "address": 0x10000}, "out_of_range"),
                 ("unknown cpu", {**base, "cpu": ":nocpu"}, "unknown_cpu"), ("device without spaces", {**base, "cpu": P["shinobi"]["no_spaces_device"]}, "not_a_cpu"),
                 ("tag with a separator (never reaches the disassembler)", {**base, "cpu": ":soundcpu,1"}, "unknown_cpu")]
        got = {n: code(m.err("disassemble", a)) for n, a, _ in cases}
        check("Z7 invalid arguments and unsupported devices are refused with typed errors", all(got[n] == want for n, _, want in cases), got=got)
        from mame_mcp import protocol
        from mame_mcp.protocol import BridgeError
        refused = 0
        for bad in ({"count": 0}, {"count": 65}, {"address": -1}, {"cpu": "soundcpu"}, {"count": True}):
            try:
                protocol.disassemble_args(**{**{"cpu": ":soundcpu", "address": 0, "count": 1}, **bad})
            except BridgeError:
                refused += 1
        check("Z7 the client-side validator refuses the same classes of input before any I/O", refused == 5, refused=refused)
        check("Z7 nothing was installed or left running by any disassembly (no experiment, no leftover dasm file)",
              m.info_().get("active_experiment") in (None, {}) and not list((Path(m.info["run_dir"]) / "evidence").rglob("dasm-*.txt")), active=m.info_().get("active_experiment"))
        note("Z finished")
    finally:
        m.close()


def write_select_and_read(m: Inst, cpu: str, select: int, value: int) -> None:
    m.fx.call("space_write_u8", cpu=cpu, space="program", address=select, value=value)


def section_b() -> None:
    m = Inst(OUT, "1942", 9642, "1942")
    try:
        z, bk = P["1942"]["z80"], P["1942"]["bank"]
        cpu, window, select = z["cpu"], H(bk["window"]), H(bk["select"])
        m.stop_debugger()
        zd = z["disasm"]
        compare(m, "B1 Z80 variable lengths (1942 RST 10h vector)", cpu, H(zd["address"]), zd["count"], zd["expected_bytes"], zd["expected_lengths"])
        texts = {}
        for entry in range(4):
            write_select_and_read(m, cpu, select, entry)
            listed = {b["tag"]: b["entry"] for b in m.fx.call("bank_list")["banks"]}
            d = m.rq("disassemble", {"cpu": cpu, "address": window, "count": 4})
            run = d["mapping"]["runs"][0]
            bank = run.get("bank") or {}
            ind = independent(m, cpu, window, sum(i["length"] for i in d["instructions"]) + 1)
            same = [(H(i["address"]), i["text"], i["length"], i["bytes_hex"]) for i in d["instructions"]] == [(x["address"], x["text"], x.get("length"), x["bytes_hex"]) for x in ind[:4]]
            first8 = fixture_bytes(m, cpu, "program", window, 8)
            check(f"B2 entry {entry}: window decodes what the independent dasm decodes, and the bytes are that entry's ({first8})", same and first8 == bk["first8"][str(entry)] and "".join(i["bytes_hex"] for i in d["instructions"]).startswith(first8[:4]), first8=first8, expected=bk["first8"][str(entry)])
            check(f"B2 entry {entry}: mapping reports a banked window with the selected entry equal to the independent bank list",
                  d["mapping"]["banked"] is True and bank.get("tag") == bk["bank_tag"] and bank.get("selected_entry") == listed.get(bk["bank_tag"]) == entry and "bank_note" in d["mapping"],
                  bank=bank, listed=listed.get(bk["bank_tag"]))
            texts[entry] = [i["text"] for i in d["instructions"]]
        check("B3 different bank entries give different code at the same logical address (so the entry must be reported, and is)", len({tuple(v) for v in texts.values()}) >= 3, entries={k: v[:2] for k, v in texts.items()})
        d = m.rq("disassemble", {"cpu": cpu, "address": window - 4, "count": 8})   # >= 8 bytes: reaches into the window
        check("B4 a range straddling the window start lists the ROM run and the bank run separately", len(d["mapping"]["runs"]) >= 2 and any("bank" in r for r in d["mapping"]["runs"]) and any(r["read_handler"] == "rom" for r in d["mapping"]["runs"]),
              runs=[(r["first"], r["last"], r["read_handler"]) for r in d["mapping"]["runs"]])
        note("B finished")
    finally:
        m.close()


def section_o() -> None:
    m = Inst(OUT, "1944", 9643, "1944")
    try:
        k, a = P["1944"]["m68k"], P["1944"]["z80"]
        kd = k["disasm"]
        m.stop_debugger()
        d = m.rq("disassemble", {"cpu": k["cpu"], "address": H(kd["address"]), "count": kd["count"]})
        wire = [i["bytes_hex"] for i in d["instructions"]]
        iv = d["instruction_view"]
        check("O1 a CPU with an opcodes space: the decoded view is the opcodes view, flagged as separate and different from the program view",
              iv["used"] == "opcodes" and iv["separate_opcode_space"] is True and iv["differs_from_program_view"] is True and iv["selectable"] is False, view=iv)
        check("O1 bytes and lengths are the decrypted instruction stream's (fixture expectation)", wire == kd["expected_opcode_bytes"] and [i["length"] for i in d["instructions"]] == kd["expected_lengths"], got=wire)
        prog = [fixture_bytes(m, k["cpu"], "program", H(i["address"]), i["length"]) for i in d["instructions"]]
        shown = [i.get("program_view_bytes_hex") for i in d["instructions"]]
        check("O1 where the program (data) view holds other bytes, program_view_bytes_hex gives them, equal to an independent program-space read",
              any(p != w for p, w in zip(prog, wire)) and all((s == p) if p != w else (s is None) for s, p, w in zip(shown, prog, wire)), opcodes=wire, program=prog, shown=shown)
        ind = independent(m, k["cpu"], H(kd["address"]), sum(i["length"] for i in d["instructions"]) + 1)
        check("O1 text, address and length equal MAME's own dasm (which also decodes the opcodes view) and the independent raw bytes equal the opcode bytes",
              [(H(i["address"]), i["text"], i["length"], i["bytes_hex"]) for i in d["instructions"]] == [(x["address"], x["text"], x.get("length"), x["bytes_hex"]) for x in ind[:kd["count"]]], text=[i["text"] for i in d["instructions"]][:2])
        # banked audio Z80 (bank register written through the driver's own handler, a TEST-ONLY intervention)
        bk = a["bank"]
        cpu, window, select = a["cpu"], H(bk["window"]), H(bk["select"])
        seen = {}
        for value in (0, 1, 2, 3):
            write_select_and_read(m, cpu, select, value)
            listed = {b["tag"]: b["entry"] for b in m.fx.call("bank_list")["banks"]}
            d = m.rq("disassemble", {"cpu": cpu, "address": window, "count": 3})
            bank = (d["mapping"]["runs"][0].get("bank") or {})
            ind = independent(m, cpu, window, sum(i["length"] for i in d["instructions"]) + 1)
            same = [(i["text"], i["bytes_hex"]) for i in d["instructions"]] == [(x["text"], x["bytes_hex"]) for x in ind[:3]]
            check(f"O2 1944 audio Z80, select value {value}: banked window, selected_entry equals the independent bank list, text and bytes equal the independent dasm",
                  d["mapping"]["banked"] is True and bank.get("selected_entry") == listed.get(bk["bank_tag"]) and bank.get("tag") == bk["bank_tag"] and same, bank=bank, listed=listed.get(bk["bank_tag"]))
            seen[value] = (bank.get("selected_entry"), d["instructions"][0]["bytes_hex"])
        check("O2 different select values select different entries and different code", len({v[0] for v in seen.values()}) >= 2 and len({v[1] for v in seen.values()}) >= 2, seen=seen)
        # a device with address spaces but no debug interface
        e = m.err("disassemble", {"cpu": P["1944"]["no_debug_device"], "address": 0, "count": 1})
        check("O3 a device without a debug interface (no disassembler) is refused with no_debug_interface", code(e) == "no_debug_interface", code=code(e), message=(e.message if e else None))
        note("O finished")
    finally:
        m.close()


def main() -> int:
    want = set(sys.argv[1:]) or {"Z", "B", "O"}
    for name, fn in (("Z", section_z), ("B", section_b), ("O", section_o)):
        if name in want:
            print(f"\n=== section {name}", flush=True)
            try:
                fn()
            except Exception as e:
                import traceback
                check(f"{name} section completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-800:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
