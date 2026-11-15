# Research report: does 1942 (Revision B) select a non-zero `:maincpu` bank?

Target: MAME 0.289, `1942` = "1942 (Revision B)", ROM zip SHA-1 `20231872c77657a9b327877a046197b0dd59adf5` (`-verifyroms` good), Windows, launcher-started with `-debug`.
All addresses are **logical `:maincpu` addresses**. Tooling changes for this task: **none** (new files only: `scripts/research_1942_banks.py`, `scripts/research_1942_input.lua`, this report).

## Question and hypotheses
Q: during ordinary attract mode, or a bounded coin/start sequence, does the game select a non-zero bank for the `0x8000-0xBFFF` window? If so, which code writes the selector, and is code executed inside the window under that selection?

* **H1:** the game selects non-zero entries from short data-copy routines (interrupts disabled), including in attract mode. *Confirm:* a capture at the instruction after a `ld (C806),a` shows PC there, the expected `A`, and window bytes (read at the hit) that match that entry's ROM bytes and not entry 0's. *Reject:* none of the candidate sites hit with a non-zero entry in the coverage below.
* **H2:** ordinary play does not execute code inside the window; the only static transfer into it is the service-mode path at `0x0166`. *Confirm/reject:* PC at the service site and any PC >= `0x8000` observed.

## Static basis
* `src/mame/capcom/1942.cpp` (tag `mame0289`, fetched for the earlier milestone): `0x8000-0xbfff` = `bankr("bank1")`; `0xc806` write = `set_entry(data & 3)`; entries map `:maincpu` region `0x10000 + 0x4000*n`. Bytes at `0x8000`: entry 0 `84`, 1 `60`, 2 `dc`, 3 `ff` (profile `tests/fixtures/profiles/1942.json`, hash-checked against the zip).
* Disassembly of `:maincpu` `0x0000-0x7fff` by MAME's own debugger `dasm` command (`runs/research1942/dasm/main.asm`, generated with the launcher and a `-debugscript` file `dasm C:/.../main.asm,0,8000,1,:maincpu`). It is a *linear* disassembly: data can be misread and indirect jumps are not resolved.
* Writers of `0xC806` found (byte patterns `32 06 C8`, `22 05 C8`, `21 06 C8`; no `LD DE/BC,C806`):

| Writer | Selects | Followed by | Restores 0 at |
|---|---|---|---|
| `0x0163` `LD (C806),A` | 2 (`LD A,2`) | `JP $ACFE` (code in the window); reached only when `($C004) bit 3` is clear = DSWB service switch (`PORT_SERVICE_DIPLOC 0x08`) | never (jumps) |
| `0x0D15` | 2 | `LD B,(HL)` / `LD C,(HL+1)`, HL=`($EBC0)` (data read) | `0x0D1C` |
| `0x0E4F` | 2 | `LDIR` 8 bytes from HL | `0x0E55` |
| `0x152A` | 1 | data reads | `0x1556` |
| `0x19AC` | 2 | `CALL $1938` with HL=`$8010`, `$8020`... | `0x19C5` |
| `0x1A6A` / `0x1A7A` | 2 / `($E10A)` | `LDIR` `0x80` bytes from `($EBD6)` / `($EBD8)` | `0x1A80` |
| `0x2BA0` `LD (HL),1`, HL=`$C806` | 1 | `LD D,(IX+0)` / `LD B,(IX+1)` | `0x2BA8` |
| `0x119C` `LD ($C805),HL` (HL=0) | 0 (boot clear; the earlier watchpoint saw the write at `0x119F`) | - | - |

* Only one **direct** call/jump into `0x8000-0xBFFF` exists (`0x0166`). Four `jp (hl)` exist (`0x00C0`, `0x05F8`, `0x4685`, `0x6701`); their targets were not resolved, so indirect entry into the window is **not excluded**.

## Method, inputs and commands
Observation sites were the instruction **after** each write (so a hit proves the write executed with no branch between). Every capture: `:maincpu`, `max_events=4`, `max_wall_seconds=10`, registers `PC, A, HL`, memory `b@0x8000` and `d@0x8000`, **armed while running** (MAME starts debugger-stopped at PC 0; the run resumes first, so the stop-PC first-instance limitation never applied). Sites run sequentially. After the captures, 60 `read_registers(PC)` samples (0.25 s apart).

```powershell
.\.venv\Scripts\python -I scripts\research_1942_banks.py A     # attract mode, no input
.\.venv\Scripts\python -I scripts\research_1942_banks.py B     # coin + 1P start + ordinary controls
```
Both use `launcher.launch()` (isolated run dir, ports 9491/9492) and the production MCP server over stdio with `MAMEMCP_LAUNCH_INFO` (pid + creation time + port-bound Windows nudge; `wake_configured: true`, no timeouts, no refusals in either run).
**Inputs (run B only, no MCP tool exists for input):** an external MAME `-autoboot_script` (`scripts/research_1942_input.lua`, `-autoboot_delay 0`) presses the machine's own fields at fixed emulated times: Coin 1 at t=3.001 s (frame 178), 1 Player Start at t=4.511 s (frame 268), then from t=9.0 s a Fire pulse every 0.5 s and Left/Right alternating every 2 s. Log: `runs/research1942/B/input.log` (every 20th pulse logged). It writes no memory and selects no bank. Whether the coin/start were *accepted* was not independently checked.
Emulated time: run A about 68 s of captures + 15 s sampling (final frame 4050 at the end of the captures); run B about 60 s + 15 s. Total about 2.6 min, within the 5 min bound.

## Results (evidence: `runs/research1942/{A,B}/summary.json`; per-experiment `run/evidence/exp-*/{manifest.json,events.jsonl}`)
Entry = from the window byte read **at the hit** (`b@8000`: `84`/`60`/`dc`/`ff` = 0/1/2/3), with `d@8000` equal to entry 2's `0x85BA98DC` or entry 1's `0x810A8060`.

| Site (after write at) | Run A (attract) | Run B (coin/start) |
|---|---|---|
| `0x0D18` (`0x0D15`, A=2) | 1 event, A=2, entry 2, HL `0x80FA` | 4 events, A=2, entry 2, HL `0x80FA..0x8112` |
| `0x0E52` (`0x0E4F`, A=2) | 4 events, A=2, entry 2, HL `0x8102..0x8105` | 4 events, A=2, entry 2, HL `0x811A..0x811D` |
| `0x152D` (`0x152A`, A=1) | not observed in 10 s | not observed in 10 s |
| `0x19AF` (`0x19AC`, A=2) | not observed | 1 event, A=2, entry 2, HL `0x8010` |
| `0x1A6D` (`0x1A6A`, A=2) | not observed | not observed |
| `0x1A7D` (`0x1A7A`, A=`$E10A`) | not observed | 4 events, A=2, entry 2, HL `0x8134..0x8137` |
| `0x2BA2` (`0x2BA0`, HL=`C806`) | 4 events, HL=`0xC806`, entry 1 | 4 events, HL=`0xC806`, entry 1 |
| `0x0166` (service path) | not observed | not observed |

* At `0x0E52` and `0x1A7D` the instruction is `LDIR`, which re-executes in place, so consecutive events (HL incrementing) are **iterations of one copy**, not separate selections. Event counts there overstate the number of selects.
* All 16 experiments: bridge-owned breakpoint cleared, borrowed temps verified and reset, `undisturbed: true`, no cleanup errors, 0 dropped events.
* PC samples: 120 samples (both runs), none >= `0x8000`; they cluster in the frame-wait loop (`0x87-0x96`) with a few at `0x1E5` / up to `0x485`. Sampling happens in the plugin's periodic callback, so it is biased toward the idle loop and is weak evidence.

## Confirmed facts, inferences, unresolved
**Confirmed (observed):**
* In attract mode, with no input, the game selects **entry 2** (writer `0x0D15`, `0x0E4F`) and **entry 1** (writer `0x2BA0`), each shown by PC at the following instruction, the constant `A=2` (or `HL=0xC806` for the `(HL)` writer), and window bytes read at the hit that equal that entry's ROM bytes and differ from entry 0's. H1 is confirmed. Run B (with the logged inputs) also observed `0x19AC` and `0x1A7A` selecting entry 2.
* The `HL` values at the hits are pointers into the window (`0x80FA`..`0x8137`, `0x8010`), consistent with the static code reading the selected bank's data next.

**Inferred (static code + the above, not directly observed):** the reads that follow each hit (`ld b,(hl)`, `ldir`) come from the selected entry; the routines restore entry 0 at the listed addresses (restore sites were not instrumented); the selecting code runs with interrupts disabled for the copy.

**Not observed within this coverage:** the `0x152A` (entry 1) and `0x1A6A` writers; the service-mode path `0x0166`/`0xACFE`; **any execution inside the banked window** (no PC >= `0x8000` in 120 samples). The only static direct entry is service-mode-gated. H2 is consistent with the data but **not confirmed**: indirect jumps (`jp (hl)`) and returns into the window were not resolved, and one breakpoint at a time cannot cover a 16 KB window. The "execution within the banked window under that selection" part is therefore **unresolved**; this is not a claim that non-zero banks hold no executed code anywhere.
**Run A vs B:** `0x19AF` and `0x1A7D` hit only in B, but sites were sampled at different emulated times in each run, so this does not show that the inputs caused them.

## Limitations
Sites armed one after another for 10 s each (not simultaneous), so absence at a site is "not observed in that 10 s". Linear disassembly. Event timestamps are harvest-time (`harvest_frame` only; not used for conclusions). Events carry no bank id; the entry above was derived at hit time from the captured window bytes plus the static writer, never from a later read. Borrowed temps: all experiments undisturbed, and `A`/`PC` matched the static code. One machine, Z80, 0.289, Windows, `-debug` front-end.

## Practical usefulness
* **Production MCP tools did:** resume, every capture (with registers and hit-time memory), status/cleanup/`undisturbed`, `read_registers` sampling, `session_info` clocks. The server+nudge ran unattended (no timeouts).
* **Needed outside the tools:** static disassembly (MAME debugger `dasm` through a `-debugscript`, plus Python byte-pattern search); ordinary input (external Lua `-autoboot_script`; MCP has no input tool and none was added); a driver script to sequence eight captures per run; reading `events.jsonl` for the rows (I did not test whether the status tool's compact "last events" alone would have sufficed).
* **Compact responses:** `summary`, `counts`, `cleanup.debug_state.undisturbed` and the evidence paths were enough to judge each experiment quickly.
* **Single improvement that would save the most repeated work:** capture on **several breakpoint addresses in one experiment** (each event tagged with its PC). Coverage questions like this one cost eight sequential single-address experiments, most of them empty after a full 10 s wall limit, and sites could not be observed simultaneously. A close second: de-duplicating or flagging repeated hits of a self-repeating instruction such as `LDIR`.
