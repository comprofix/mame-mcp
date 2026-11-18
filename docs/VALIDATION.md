# Validation report (MAME 0.289: Shinobi baseline + 1942 banked memory + checkpoints / RAM writes / inputs)

Environment: Windows 11, MAME 0.289 (mame0289) at `D:\arcade\mame`, `shinobi` (System 16A parent set; zip SHA-1
`a7a0ce96dedfa13669ff617a392fb3a0bbf43ef5`, `-verifyroms`: good), Python 3.14.7, `mcp` 2.3.0.
Every MAME run was a separate, isolated instance under `runs\` (own `-homepath`, `-inipath`, `-cfg_directory`, ...).
The ROM zip is only read. The MAME installation and configuration were not modified.

Status legend: **PASSED** = executed against real MAME and checked; **SOURCE** = established from the mame0289 source
only; **UNTESTED** = not exercised; **LIMITATION** = observed, not fixable here. Banked memory and games other than
Shinobi were out of scope for the hardening milestone; the banked-memory milestone (section 7) adds one machine, `1942`.

## Commands and evidence (all regenerate their files; `runs\` is git-ignored)

| Command (`.venv\Scripts\python`) | Result | Evidence file |
|---|---|---|
| `-m pytest -q` | 50 passed (client validation, lifecycle/retry logic on a fake socket; **not** MAME evidence) | - |
| `-I scripts\validate.py` | 51 passed, 0 failed (regression of the original POC) | `runs\validation\results.json` |
| `-I scripts\validate_hardening.py` | 100 passed, 0 failed (sections T, E, Z, R; Z4 corrected and Z5 added, see section 4) | `runs\hardening\results.json` |
| `-I scripts\validate_latency.py --gaps 1,6,12 --reps 2` | 10 passed, 0 failed (latency is *measured*, see below) | `runs\latency\results.json` |
| `-I scripts\mcp_e2e.py` | 13 passed, 0 failed | `runs\mcp_e2e\results.json` |
| `-I scripts\mcp_restart_e2e.py` | 22 passed, 0 failed (one MCP server process across MAME restarts) | `runs\mcp_restart_e2e\results.json` |
| `-I scripts\validate_banked.py` | 42 passed, 0 failed (sections BOOT, B, F, H, C, W, S, N on 1942) | `runs\banked\results.json` |
| `-I scripts\mcp_bank_e2e.py` | 8 passed, 0 failed (bank-switch example through the MCP client) | `runs\mcp_bank_e2e\results.json` |
| `-m pytest -q` (after the checkpoint/input/write milestone) | 168 passed (fakes only; **not** MAME evidence; 163 before the device-state correction, section 10c) | - |
| `-I scripts\validate_checkpoint_input.py` | 62 passed, 0 failed (sections I, W, C, G, D, U, M, S; direct bridge + test-only fixture observer, sections 10 and 10c; was 48 before section S) | `runs\checkpoint_input\results.json` |
| `-I scripts\mcp_checkpoint_e2e.py` | 46 passed, 0 failed (official MCP SDK client against the production server, sections 10 and 10c; was 38 before the 1944 part) | `runs\mcp_checkpoint_e2e\results.json` |
| `-I scripts\example_experiment.py` / `bridge_smoke.py` | ran; `RESULT: as expected` | `runs\example\...` |

Hardening/fixture design: `tests\fixtures\plugins\mamemcp_fixture` is a **test-only** plugin (file mailbox) that
emulates *user* actions (debugger stop, pause, own breakpoints, writing debugger temporaries) and observes MAME
independently of the bridge (MAME's own debugger `printf` with the visible CPU set, read taps). Game/CPU specific
addresses live in `tests\fixtures\profiles\shinobi.json`; the server contains none. The "manual debugger observation"
is MAME's own debugger driven by script, not a person at the debugger window.

## Feasibility gate: request handling per execution state: PASSED
Running (~61 periodic calls/s), normal pause (~20-30/s) and debugger-stopped all service requests through
`emu.register_periodic`; emulated time is frozen in the last two. See the debugger-stopped limitation below.

## 1. Debugger temporary ownership (hardening section T): PASSED
`temp0..temp9` are **shared global debugger state**; MAME 0.289 offers no private or allocatable variables.
Policy (`session_info`/manifest report it): a capture borrows 1 temp (hit counter) + 1 per captured register (max 8
registers), choosing from `temp9` downwards only temps that are **0, not referenced by any installed breakpoint's
condition/action, and not claimed by an experiment**. It never overwrites a non-zero temp. At release it checks the
counter (`undisturbed`), zeroes only its own temps and re-reads them (`verified`). No claim is made that
save/restore is safe, so none is attempted.
* T1 baseline all-zero; capture borrows (reported); completion leaves all temps 0, cleanup verified.
* T2 user's `temp9=5`, `temp8=7` are skipped, recorded in the manifest selection report and never modified.
* T3 temps referenced by a user breakpoint (`temp6` in a condition, `temp5` in an action) are avoided.
* T4 all ten temps in use: capture refused (`debug_state_unavailable`), nothing installed, no temp or breakpoint touched.
* T5 interference: counter lowered externally while paused, then cancel: run reported **failed** with
  `cleanup.debug_state.undisturbed=false, verified=false`; the disturbed temp is left as the user set it; breakpoint
  still removed and verified; emulation not resumed.
* T6 instrumentation removed externally: the experiment still ends (wall clock) and cleanup is verified.
* **LIMITATION:** disturbance is only detected when the counter ends *below* the bridge's observed hit count. A write that
  raises it looks the same as hits lost from the console log. A user who writes `temp0..temp9` while a capture runs can
  still corrupt that capture's data; the manifest records which temps were borrowed.

## 2. Capture execution-state handling (section E): PASSED
**Policy:** `capture_breakpoint`, `cancel_experiment`, timeouts and cleanup **never change execution state** (they only
*observe* and report it: `execution`, `final_execution`, `execution_transitions` in status and manifest). The only
resume is the breakpoint action's `g` after a recorded hit. A capture armed while paused or debugger-stopped records
nothing until the user (or `execution_control resume`) resumes. After the *last* hit the action's `g` leaves emulation
running (reported in `final_execution`).

| Start state | Wall-clock timeout | Cancel | Armed, then resume |
|---|---|---|---|
| running | PASSED (0 events, state unchanged) | PASSED | n/a |
| paused | PASSED, still paused | PASSED, still paused | PASSED, completes by event limit |
| debugger-stopped | PASSED, still stopped | PASSED, still stopped | PASSED, completes by event limit |

Also PASSED: user **stop** and user **pause** issued during a running capture (experiment keeps running, no hits while held,
wall-clock end preserves and reports the user's state, transition recorded in the manifest); 10/10 trials of a user pause
issued while hits were arriving survived, also through cancel; same-address capture refused (`address_in_use`:
MAME evaluates only the first breakpoint at an address, so a second one would shadow or be shadowed) without touching
breakpoints or temps.
* **UNTESTED:** a pause/stop requested in the same instant as the action's `g` is a theoretical race; 10 trials did not hit it.

## 3. Restart / reconnect through the MCP server (`mcp_restart_e2e.py`): PASSED
One MCP server process, real MAME restarted twice on the same port:
* MAME killed during an active experiment: next call fails at once with `mame_disconnected`; later calls `connect_failed`;
  cancel is not reported as done.
* Restart: first call returns **`session_changed`** (old and new session ids) and sends nothing; the next call works against
  the new session, which is clean (no experiment, no owned breakpoints, request counter exactly our own two calls).
* The old experiment id is refused as `experiment_from_other_session` for status and cancel; default status does not inherit it.
* A `capture_breakpoint` in flight when MAME (frozen, then killed) dies fails with `mame_disconnected` and
  **`uncertain_state=true`**; after the next restart the new session saw only its handshake and our call (no replay, no
  half-installed capture, all temps 0). State-changing requests are never retried (also unit-tested).
* The MCP server process killed mid-experiment (MAME untouched): a new server process re-attaches to the **same** session and
  reads the experiment that finished by wall clock inside MAME (361 events, cleanup verified, temps 0).

## 4. Second CPU: `:soundcpu` Z80 (section Z): PASSED, reported separately from the 68000
Profile: Z80 idle loop at `0xAE6` (found by sampling PC). The 68000 results are in `validate.py` and section E.
* MAME stopped at a user breakpoint on `:soundcpu`: `read_registers` (PC, SP, A, BC, HL, AF) equals MAME's own debugger `printf` with that CPU visible.
* `read_memory(:soundcpu, program)` equals the Z80's own debugger bytes; `:maincpu` at the same address equals the 68000's
  bytes; the two differ (so the explicit CPU is honoured). Address 0: Z80 `F3`, 68000 `FF`.
* Capture on `:soundcpu` armed while debugger-stopped on that CPU, then resumed: 3 events, every event's `PC` register is
  `0xAE6`, memory fields (`b@loop`, `b@0`, `w@loop`) equal
  the Z80 debugger values and `b@0` is the Z80's `F3`, not the 68000's. Registers are read in the breakpoint *condition* (compiled in the breakpoint
  CPU's own symbol table); memory fields use device-qualified operators (`:soundcpu.pb@...`).
* **Register coverage is two separate things (corrected 2026-11-15, see "Z4 correction" below):**
  * *Z2: `read_registers` vs MAME's debugger at the same stop* (PC, SP, A, BC, HL, AF). Unchanged.
  * *Z5: captured registers vs an independent observation of the same hit.* The capture is on `0xAE6`; an independent user breakpoint sits on the next
    instruction `0xAE8`, conditioned on the capture's own hit counter equalling a chosen number K (5, 40, 150). The machine therefore stops in the same loop
    iteration as hit K; MAME's own debugger (`printf`, `:soundcpu` visible) reads SP, BC, HL there and the result is compared with captured event `seq == K`.
    The counter read at each stop must equal K (it did). The premise is asserted from ROM: `0xAE6` is `CB 47` (`bit 0,a`), which writes only F, so SP, BC, HL
    at `0xAE8` equal their values at the hit; PC is checked as `0xAE6` in the event and `0xAE8` at the observer; A and AF are not compared.
* Negative control (observation): an *unqualified* `pc` in a `:soundcpu` breakpoint **action** printed `0x28E6` (a 68000-looking
  value), not the Z80's `0xAE6`: actions are not a safe place to read a non-visible CPU's registers, which is why they are not used.
* Direct mode refused for the `io` space. **UNTESTED:** Z80 `io` space reads, `:upd7751`, any other CPU/game.

## 5. Memory-read semantics (section R)
| Path | Basis | Status |
|---|---|---|
| `read_memory mode=inspect` (default) | `symbol_table:memory_value(..., disable_se=true)`, the primitive behind the debugger's `@` operator (`express.cpp`); one byte per call | SOURCE + PASSED: a read tap on a ROM range saw only *side-effects-disabled* accesses (4 disabled, 0 enabled); the tap saw no traffic from normal execution |
| capture memory fields | debugger `@` operator, device-qualified | PASSED: tap saw disabled accesses only; value equals the inspect read |
| `read_memory mode=direct` (program space only) | debugger `r` space, no handler invoked | PASSED: tap saw nothing; equals inspect over plain ROM. **Demonstrated limitation:** over a handler-backed (tapped) range it returns `00`s, indistinguishable from real zeros |
| plain Lua `space:read_u8`/`readv_u8` (**not used**) | `luaengine_mem.cpp` | PASSED as a control: tap saw side effects *enabled*. Hence not used |
| side effects of reads on I/O ports / specific devices | handlers must honour `machine.side_effects_disabled()` | **UNTESTED**; the result states the guarantee is not proven for any device |
Every `read_memory` result carries `read_mode`, `read_semantics`, `side_effects`, and (direct) `limitations`.

## 6. Debugger-stopped service latency and the retry policy
**Measured** (`validate_latency.py`, this host, quiet desktop, `-video none`, started with `-debug`, no request during the idle gap):
without the nudge, most first requests after an idle gap of 6 s or more were **not answered within 30 s** (3 gaps x 2 repetitions:
5 of 6 first requests unanswered; in an earlier run with 3 gaps x 4 repetitions, 10 of 12 first requests were unanswered, the answered ones took 0.06 s and 5.0 s, and one request made immediately after an answer took 16.8 s; a still earlier run measured 15.1 s). Requests made immediately after an answered one are usually fast. Delay is irregular and is not a
simple function of idle time. **A 15 s timeout is therefore a default, not a guarantee** of responsiveness.

**Source reading (not proven by the measurements):** while the emulation is debugger-stopped, `debugger_cpu::wait_for_debugger`
loops `periodic_check(); osd().wait_for_debugger()` and the Windows front-end's `wait_for_debugger` blocks in
`GetMessage(&message, nullptr, 0, 0)` (`debugwin.cpp`). Lua periodic callbacks (which service the socket) therefore only run when some window message
reaches that thread. A quiet desktop supplies none. This explanation is consistent with the observation below, but the
measurements alone do not demonstrate the mechanism.

**Observation supporting it:** posting `WM_NULL` to the MAME process' threads *after* the request is sent made every
unanswered request complete within ~0.6 s in 18/18 trials across two runs (0.58-0.61 s; the 0.6 s is the client's own 0.3 s wait + 0.25 s
re-post interval). **Mitigation (opt-in, Windows only):** `BridgeClient(wake=...)` / `MAMEMCP_MAME_PID` (or the pid the launcher records
in `launch.json`, followed across restarts) makes the client post `WM_NULL` while a request is unanswered for >0.3 s, only to a process whose image is named `mame*.exe`.
It sends no input and touches no memory; wake counts are reported in `session_info.bridge_client` and in the `transport`
note. Without a configured pid the limitation remains: **LIMITATION**, Windows debugger front-end only; other front-ends
(`-debugger qt/imgui`) and other OSes UNTESTED.

**Retry policy (`BridgeClient`):** nothing is retried unless the call is marked retry-safe, and only
`session_info`, `read_registers`, `experiment_status` are (no bus access, no state change, idempotent). **`read_memory` is not
retried** (a bus read may reach a handler that ignores the side-effects flag), nor are `capture_breakpoint`,
`cancel_experiment`, `execution_control`. A retry happens once, after `request_timeout`, on the same socket and session; a lost
connection or session change is raised, never retried (unit-tested, incl. session change between attempt and retry). Each attempt has its own
request id; the first attempt's late reply is discarded as stale by id (PASSED on real MAME: an orphaned `session_info`
reply never became the answer to the next `read_registers`; also for a timed-out attempt). Timeouts, retries and wake posts are recorded
in `BridgeClient.transport_events`, returned in a `transport` field of any tool result that needed one, and counted in
`session_info.bridge_client`. Experiment evidence files are written by the plugin and do not contain client transport events.

## 7. Banked memory: 1942 (Revision B), Z80 `:maincpu` (`validate_banked.py`, `mcp_bank_e2e.py`): PASSED for this one machine

**Target and identity.** MAME 0.289, set `1942` = "1942 (Revision B)", Capcom 1984, from the local merged collection
`D:\arcade\ROMs\MAME 0.289 ROMs (merged)\1942.zip` (read only), SHA-1 `20231872c77657a9b327877a046197b0dd59adf5`, `-verifyroms 1942`: good.
The five `:maincpu` ROM files (`srb-03.m3`, `srb-04.m4`, `srb-05.m5`, `srb-06.m6`, `srb-07.m7`) were hashed from the zip and equal the SHA-1s
in `src/mame/capcom/1942.cpp` of tag `mame0289` (source fetched from the mamedev repository; no ROM downloaded). Profile: `tests\fixtures\profiles\1942.json`.
Why this machine: one CPU, one obvious window, a tiny select register and three differently populated entries. The other local ROM sets were not surveyed beyond finding this one.

**Source facts (SOURCE, then confirmed by bytes):** `_1942_map`: `0x0000-0x7fff` ROM, `0x8000-0xbfff` `bankr("bank1")`, `0xc806` write handler `bankswitch_w`
= `membank("bank1")->set_entry(data & 3)`; `driver_init`: `configure_entries(0, 4, &ROM[0x10000], 0x4000)` over region `:maincpu` (0x20000 bytes, `ROMREGION_ERASEFF`).
`0xC806` is write-only: reading it returns the unmapped value (`ff`), so the entry is observable only through `bank.entry` (Lua) or by reading the window.

| Level | What it is | Established how |
|---|---|---|
| CPU-visible logical address | `0x8000-0xBFFF` on `:maincpu` program space | what the tools take and return |
| Selected bank entry | `bank1.entry` 0..3 | Lua `memory.banks[":bank1"].entry`, exposed by `read_memory(bank_context=true)` |
| Backing region offset | `0x10000 + 0x4000*entry + (addr-0x8000)` in `:maincpu` | **not** provided by the API; from the driver source and confirmed by byte equality with the ROM files in every probe |
| ROM file | entry 0 = `srb-05.m5`; 1 = `srb-06.m6` (first 0x2000 bytes only, rest is 0xFF erase fill); 2 = `srb-07.m7`; 3 = no file (all 0xFF) | profile + `-listxml` offsets |

Distinguishable expected bytes at `0x8000`: entry 0 `84b08431857685d3`, entry 1 `60800a81c4817e82`, entry 2 `dc98ba85c088dc98`, entry 3 `ff...`.
The upper half of entry 1 and all of entry 3 are **not** ROM-file backed, so a bank does not always map to an ordinary ROM offset.

**Results (all PASSED in `runs\banked\results.json` unless marked):**
* **Baseline (B):** for each entry 0-3 and three window addresses (`0x8000`, `0x9FF8` across entry 1's populated boundary, `0xBFF8`), the ROM-file expectation, MAME's own debugger (`printf` on the same CPU) and `read_memory` agree (16 cases) and `bank_context` names the entry. Unbanked `0x0` is identical across entries.
* **Follows the bank (F):** while the machine is *running* (attract mode; sampled PCs stayed below `0x1000`, so the window is not executed while held), `read_memory` returns the selected entry's bytes for entries 1,2,3,0,2.
* **Hit-time capture (H):** with entry 1 selected, a capture at `0x11` (frame handler) and an independent observer at the next instruction `0x12` that prints the window bytes and then switches 1 to 2 *inside the machine's own execution*:
  events = entry 1, entry 2, entry 2, each equal to the observer's hit-time value; a read after the experiment reports entry 2, so a later bank sample differs from event 1. Memory fields are hit-time; harvest happens later.
* **Bank changing during an experiment (C):** the fixture rewrote the select register `0,1,2,3,...` on every periodic callback (133 writes). 80 `read_memory(bank_context)` calls were each self-consistent (bytes match the entry reported *in that read*); a 24-event capture
  saw all four entries (sequence 1,2,3,0,1,2,3,0,...), each event's 1-byte and 4-byte fields came from one bank, and all 24 equalled the observer.
* **Breakpoint address semantics (W):** a capture at *logical* `0x8000` fired with PC `0x8000` for entries 0, 1 and 3 and recorded that entry's bytes. The breakpoint matches the address, not the bank; the event records PC and memory fields only. Entry 2 was not run in W (covered by H/C reads).
  To execute the window the harness wrote `JP 8000` to work RAM and set PC (recorded intervention), then soft-reset.
* **CPU / space (S):** `:audiocpu` at `0x8000` reads its own RAM (equals its debugger, differs from `:maincpu`'s bank); with the debugger's visible CPU set to `:audiocpu`, `:maincpu` reads and `:maincpu` capture memory fields still return `:maincpu`'s bank;
  `data` and `opcodes` are refused (`unknown_space`), an unknown CPU is refused. `io` is accepted for this Z80 and returned `00000000` (not interpreted).
* **Natural selection (BOOT):** a write watchpoint saw the game's own code write `0x00` to `0xC806` at PC `0x119F` after boot; the entry was 0 without any harness write (re-confirmed after each soft reset in W).
* **MCP end-to-end (`mcp_bank_e2e.py`):** through the official SDK client: `read_memory(bank_context)` under entries 1 and 2; a 3-event capture with the entry switching mid-experiment (entry 1, 2, 2); a later read reporting entry 2; manifest `bank_context.per_event = "unavailable"`. No memory-write or bank-select tool exists.

**Interventions (all by the harness, none by the server):** every entry selection other than the game's own boot write was (a) a normal CPU-space write to `0xC806` from the fixture plugin so the driver handler ran, (b) a *user breakpoint action* at `0x12` writing `0xC806=2`, (c) the fixture's per-callback cycler, or
(d) in W, a RAM trampoline + PC set + soft reset. Each is listed under `interventions` in the results files. The server gained **no** write tool.
**Natural switching:** only value 0 (the boot write) was observed. Non-zero entries need later gameplay; an exploratory probe that injected coin/start for about 60 s saw no non-zero write (and was not shown to be real gameplay); it is not part of the suite. Non-zero entries were therefore reached by interventions.

**What was added to the server (generic, API-supported only):** `read_memory(..., bank_context=true)` adds `bank_context.banks[] = {tag, entry}` (up to 32) sampled in the same plugin call as the bytes (emulation does not advance between them), so it is atomic **for that read**,
and states `covers_requested_range: "not determined"` and `backing_region_offset: "not available"` (MAME's Lua API reports neither). The capture manifest records `bank_context.per_event = "unavailable"` with the reason: the debugger expression language has no bank operator
and Lua runs only at harvest, so a bank id would describe a later moment than the hit. All mapping logic (which bank backs which window, region offsets) stays in the profile.

**Event attribution limits.** (1) Memory fields are hit-time and follow the selected bank; the bank itself is not recorded per event. (2) A breakpoint is a logical-address match: one address can mean different code or data, and a later `bank_context` must not be attributed to an earlier event.
(3) Timestamps are still harvest-time (`harvest_emu_time_s`, `harvest_frame`, `harvest_host_ms`). (4) **MAME behaviour found here:** a Lua-requested debugger stop landed at PC `0x10` (12 of 12 tries) and MAME does not re-trigger a breakpoint at the PC it resumes from, so a capture armed at that exact PC misses that instance
(check H0: first observation entry 1, first event entry 2). Arm at an address the stopped machine is not sitting on, or expect the first instance to be missed.

**Wake nudge (section N).** Enabled for every debugger-stopped part of these runs (`wake_configured: true`; posts are reported in `session_info.bridge_client` and in the results). In one run, first requests after 3/6/9 s idle while stopped took 0.21/0.60/0.15 s with 1 post: a demonstrated mitigation, **not** a timing guarantee.
In the MCP check a request after 8 s idle answered in 0.14 s without needing a post, so that sample does not demonstrate the nudge. **Identity binding (new):** before every post `winwake` re-verifies (a) image name `mame*`, (b) the creation time recorded in `launch.json` (when known), (c) that the pid owns a TCP socket on the bridge port.
PASSED on real MAME with a decoy second instance: the correct identity nudged exactly that pid; a different live MAME's pid (also with *its own* exact pid and creation time), a right pid with a wrong recorded creation time (the recycled-pid case), a non-MAME process and an exited MAME's pid were all refused with nothing posted; after killing the MAME and
restarting on the same port, the old identity was refused and the new one accepted. Six unit tests cover the logic with a fake process table. A pid-only setting (`MAMEMCP_MAME_PID`) relies on the port check alone. This is a posting guard, not process control. Windows only.

**Not claimed / unchanged limitations and their impact on banked evidence:**
* *Borrowed debugger-temp interference* is not resolved: it could corrupt a capture's register fields (memory fields are read by the same action). Temps ended at 0 and no bridge breakpoint remained in every banked capture (checks `H:` and `C:`).
* *Pause-versus-breakpoint-resume race* is theoretical and unchanged; not observed.
* *Real device I/O side effects* untested: the select register is write-only here and `inspect` reads of a ROM window have no side effects; a machine whose banked window overlays I/O would need its own check.
* *Other operating systems and debugger front-ends* untested.
* One machine, one CPU architecture (Z80), one 4-entry bank; `bank_context` lists tags and entries only and does not know which bank backs which range. Multi-bank machines, banked `opcodes` spaces, 68000 banking and other bank mechanisms (mapper chips, overlays, `data`-space banks) are **UNTESTED**.

## 8. Multi-address breakpoint capture (`validate_multi.py`, `mcp_multi_e2e.py`): PASSED for this one machine

```powershell
.\.venv\Scripts\python -I scripts\validate_multi.py   # 67 checks, five isolated MAME instances (1942 x4 including two fault-injection instances, Shinobi x1; ports 9457, 9459, 9463-9465); runs\multi\results.json
.\.venv\Scripts\python -I scripts\mcp_multi_e2e.py    # 11 checks through the official MCP SDK client (port 9461); runs\mcp_multi_e2e\results.json
```
Target: 1942 (Revision B), `:maincpu` Z80. Locations: `0x11` (per-frame handler), `0x08` (same per-frame rate here), `0x98` (frame-wait loop, very hot), `0x166` (service-mode path, never hit), `0xA000`-range addresses (never hit). Independent expectations: TEST-ONLY fixture user breakpoints at the instruction after each location print one console line per hit from MAME's own debugger; per-location order/attribution is compared with that observer, not with the bridge's own counters.

Design: one experiment = one CPU, 1..8 locations (`MAX_LOCATIONS = 8`, `limits.max_locations`), one shared hit-counter temp whose value is the global event sequence and which enforces the single event limit in every location's condition, the same register temps for all locations, per-location action differing only by a literal location id. A multi-location experiment borrows one extra temp (hit-time `curpc`); the temp count is independent of the number of locations (checked: 2 and 8 locations both use 4 temps with two registers). The single `address` form allocates the same temps as before.

| Check group | Result |
|---|---|
| M1 two locations, 30 events | one global seq 1..30; `location`/`address`/`pc` on every event; per-location counts equal counts from the events file; event location order equals the independent observer's order |
| M2 firing locations + never-firing location | the unhit location is armed with 0 events and reports `no_hits_observed`, distinct from a location that failed to arm. this M2 pair fired at equal rates (300/300); unequal rates are demonstrated in the closeout (M2b) |
| M3 global limit with a very hot location (`0x98`) | exactly 25 events, 0 dropped, counter 25 at release, events file did not grow in the following 2 s |
| M4 cancel with 3 locations | 3 bridge-owned breakpoints existed while running, all cleared and verified, user's unrelated breakpoint (same index/condition/action) untouched, state still `running` |
| M5 wall-clock timeout | `wall_clock_limit` at about 3.1 s, 0 events, both locations armed and unhit |
| M6 invalid requests | 9 invalid forms (duplicate address, duplicate id, > 8, empty, bad id, address out of range, both forms, neither, collision with a user breakpoint) rejected by the plugin with client validation bypassed; breakpoints and temps identical before/after; client validation rejects the same classes |
| M7 partial installation failure | with the TEST-ONLY env hook `MAMEMCP_TEST_FAIL_INSTALL_AT=2`: location 1 armed then rolled back, 2 failed with the reason, 3 not attempted; rollback verified; no bridge-owned breakpoint, user breakpoint and temps unchanged; failed manifest records installation + rollback; a following capture works |
| M8 single-address compatibility | legacy `address` form: `breakpoint_index`, `parameters.address`, same temps, events additionally carry `location: "L1"` |
| M9 temp ownership | all temps 0 and no bridge-owned breakpoints after every experiment |
| Unit tests | 14 new `test_protocol.py` cases (ids, duplicates, maximum, both/neither forms) |
| MCP end-to-end | schema advertises `locations`; duplicate rejected; 3-location capture, per-location counts, manifest, legacy call; no input/watch/command/eval/lua/write tool exists |

Regression after this change (same session, real MAME): pytest 50 passed; `validate.py` 51; `validate_hardening.py` 92; `validate_latency.py` 10; `mcp_e2e.py` 13; `mcp_restart_e2e.py` 22; `validate_banked.py` 42; `mcp_bank_e2e.py` 8; `example_experiment.py` "RESULT: as expected". A first hardening run failed one check because the manifest `instrumentation` field had changed from an array to an object; it is an array again (one entry, with a `locations` list) and hardening was rerun green. `mcp_multi_e2e.py` was last run before that one-line manifest-shape fix (8 passed); it does not read that field. The 1942 repeated-hit sites are `LDIR`-free here (0x11/0x08 are single instructions), so no hit is a bank-selection claim.

Evidence/format notes: manifest gains `parameters.locations`, `instrumentation[0].locations` (id, address, breakpoint index, action), `installation`, `per_location` counts and `cleanup.locations`; events keep logical-address semantics, `bank_context.per_event = "unavailable"` and harvest-time timestamps. Status adds a `locations` table; recent events stay at 5.

Limits and untested points:
* One CPU per experiment, one experiment at a time, at most 8 locations; MAME evaluates one breakpoint per address, so two locations cannot share an address.
* Arming while running without a stop relies on all installs occurring in one plugin callback, so the machine cannot run between them. This follows from the design and was observed to work (M1 to M6 arm while running), not separately proven.
* Rollback/clear failure: exercised through TEST-ONLY hooks (`MAMEMCP_TEST_FAIL_INSTALL_AT`, `MAMEMCP_TEST_FAIL_CLEAR`) that suppress the bridge's own call; a real failure of MAME's `bpclear` is untested (see Closeout).
* Hit-time PC uses the debugger symbol `curpc`; verified on the Z80 (section 8) and on the Shinobi 68000 (closeout, M10).
* Events dropped by the bridge lose their location along with the rest of the event. Per-event bank id remains unavailable. Observer comparison is by sequence and location only, not by timing.

### Closeout validation (final code)

Final-code results (same session, real MAME 0.289; run after the last plugin/launcher edit): `validate_multi.py` **67 passed, 0 failed**; `mcp_multi_e2e.py` **11 passed, 0 failed**; pytest 50; `validate.py` 51; `validate_banked.py` 42; `mcp_bank_e2e.py` 8; `mcp_e2e.py` 13; `mcp_restart_e2e.py` 22; `validate_latency.py` 10; `example_experiment.py` "RESULT: as expected"; `validate_hardening.py` 91 passed + **1 failed** (check Z4, see below).

* **Z4 (sound CPU) failed once in the full hardening run** (HL 0x1 in all three events, 0x4 at the earlier debugger stop) and passed in two reruns of section Z (17/17 each); the other 91 checks passed. *Resolved afterwards: the assertion was defective and has been replaced; see "Z4 correction" below.* The original text is kept here unchanged.
* **MCP, final code:** `instrumentation` is asserted to be an **array** (one `breakpoints` entry with `locations` [id, address, breakpoint_index, action] and one shared set of `debugger_temporaries` including `(hit-time pc)`), plus `installation.locations`, `per_location`, `cleanup.locations`, `final_state` and `bank_context.per_event`; the legacy single-address manifest is checked the same way (one location `L1`).
* **Unequal rates (M2b), 1942 `:maincpu`:** hot `0x11` (per-frame) beside less frequent `0x2BA2` (instruction after `ld (hl),1` at `0x2BA0`), independent MAME-debugger observers at `0x12` and `0x2BA5`, never-hit `0x166` kept as separate coverage. Observer lines equal the events' location sequence and per-location counts in both runs. **Limit 30:** the global limit ended collection *before* the rare location fired (30 hot, 0 rare; observers 33/0 over the whole run). **Limit 900:** 892 hot + 8 rare, observers 892/8. The bridge gives no fairness or coverage guarantee between locations; a small limit can be consumed entirely by the hot location. Addresses are in `tests/fixtures/profiles/1942.json` (`maincpu.unequal_rates`).
* **68000 (M10), Shinobi `:maincpu`:** locations `0x404` (level-4 vector `bra`) and `0x2870` (inside the handler it branches to; MAME `dasm`), registers D0 D1 A0 A1 SP SR + hit-time `curpc`, 40-event global limit. Verified against MAME's own debugger observers at the neighbouring instructions (`0x286C`, `0x2874`; an observer cannot share the location's address): location order of all 40 events equals the observer order (one leading observer line skipped for alignment because the stop landed mid-handler); D0/D1/A0/A1/SP (and SR for `0x404`) equal the observer values (SR is not compared for `0x2870` because `tst.b` changes the flags between them); `pc == address` via `curpc` on every event; exactly 40 events, counter 40 at release, 8 temps (1 counter + 6 registers + hit-time pc) released, both breakpoints cleared, all temps 0. Addresses are in `tests/fixtures/profiles/shinobi.json` (`maincpu.multi_location`). Observers are adjacent-instruction, so the match relies on every interrupt executing the whole path; it is not a same-instruction comparison.
* **Cleanup failure (M11, M12), real MAME with a TEST-ONLY hook.** The plugin's own `bpclear` of a chosen location (`MAMEMCP_TEST_FAIL_CLEAR=<ordinal>`) is suppressed and reported as failed, so the breakpoint **really remains installed in MAME** (confirmed from MAME's own breakpoint list). This exercises the bridge's bookkeeping and refusal behaviour against a real leftover breakpoint; it does **not** show how MAME behaves when its own `bpclear` fails, which is still untested.
  * *Finalize failure (M11: clear of location 2 suppressed, 3 locations, event limit):* experiment state `failed` (not `completed`); `cleanup.locations` cleared `[true, false, true]`; explicit `bpclear failed` error; `cleanup.remaining_instrumentation` names location, address and breakpoint index; `cleanup.recovery` text; temporaries are **retained, not reset** and the manifest says so (`debug_state.retained`, `verified: false`); `session_info.unresolved_instrumentation` lists it. A new capture (single and multi) is refused with `unresolved_instrumentation` (the bridge first retries the clear itself; the hook suppresses the retry too) and installs nothing.
  * *Recovery procedure (works):* remove the listed breakpoint (`bpclear <index>` on that CPU in the MAME debugger, or restart MAME). The next `capture_breakpoint` re-checks the breakpoint list, zeroes and releases the retained temporaries and reports `recovered_instrumentation`; that capture ran and completed, with all temps 0 and no bridge-owned breakpoint left. `session_info` keeps listing the leftover until a capture request re-checks (it never mutates state).
  * *Rollback failure after partial install (M12: `FAIL_INSTALL_AT=2` + clear of location 1 suppressed):* `install_failed` with "CLEANUP FAILED", `rollback.all_removed: false`, the stuck location identified with its error, recovery text; the failed manifest records it; MAME's list shows the leftover; a new capture is refused; after `bpclear` in the debugger the guard lets requests through again (checked with an invalid request that now fails on its own `bad_argument`) and the retained temps are zeroed.
* **Hooks default off:** `MAMEMCP_TEST_FAIL_INSTALL_AT` and `MAMEMCP_TEST_FAIL_CLEAR` exist only in the Lua plugin. `launcher.launch()` strips every ambient `MAMEMCP_TEST_*` variable and sets them only from an explicit `test_env=` argument (recorded in `launch.json`). M0 launches with `MAMEMCP_TEST_FAIL_CLEAR=1` set in the harness process and shows `session_info.test_hooks == []`; `session_info.test_hooks` lists any active hook; no server module other than the launcher mentions `MAMEMCP_TEST`.

Remaining limits after the closeout: the hook-injected failure is not a real MAME `bpclear` failure; the 68000 comparison uses adjacent-instruction observers; the Z4 register assertion was replaced (see "Z4 correction"); everything else is unchanged (one CPU per experiment, <= 8 locations, one extra temp for multi, dropped events lose their location, logical addresses only, no per-event bank id, harvest-time timestamps, shared debugger temps).

### Z4 correction (sound-CPU register check), 2026-11-15
**Cause (confirmed from evidence; assertion defect, not a capture defect).** The old Z4 read the Z80 registers at the debugger stop in Z1, then armed a capture, resumed, and
required every register that was constant over the capture's three events to equal the earlier stop. That equates two different times. The evidence:
* The retained capture manifests/events of earlier `z80-capture` runs (`runs\hardening\run\evidence\exp-*`, six of them) show HL constant *within* each capture
  (three identical events) but different *between* sessions: 0x76, 0x1, 0x1B, 0xC, 0x1, 0xFB38. SP (0xFFFE) and BC (0x0) were the same everywhere. So the captured values are stable and
  self-consistent; what varies is HL at the time the capture started. The original failing run's own evidence (HL 0x1 in the events, 0x4 at the stop) survives only as the text above:
  its `results.json` was overwritten by later runs and two retained captures both show HL 0x1, so I cannot say which directory it was.
* The hit address `0xAE6` is in the idle loop `0xAE4 in a,(1)` / `0xAE6 bit 0,a` / `0xAE8 jr z,0xAE4` (bytes read from ROM). The loop does not write HL; HL is whatever the last
  command handler left, and changes when the sound CPU processes a command from the main CPU. Between the earlier stop and the capture a command can be processed, so HL legitimately differs.
  Constant across three adjacent hits proves nothing about an earlier moment. (Observed here: in full runs HL at the capture was 0x2F and 0xF; in six isolated section-Z runs it was 0xFB38 every time,
  which is why the old check usually passed.) No evidence suggests a capture defect: no production code was changed.

**Replacement.** The cross-time register comparison was removed from Z4 (Z4 keeps: armed-while-stopped, event limit, PC, memory fields, Z80-not-68000 byte, manifest; plus an INFO line, not judged, printing both HL values).
New Z5 (method in section 4) compares captured SP/BC/HL with MAME's debugger at the same occurrence, one instruction later, using a controlled occurrence and an instruction whose register effects are asserted from ROM. No majority
values, loosened equality, skipped registers or retries. Z2 stays as the separate "register reads vs debugger at the same stop" check. Profile data (`observer_pc`, `hit_instruction_bytes`, `observer_hit_counts` ...) is in `tests/fixtures/profiles/shinobi.json`.
Production code and the multi-address behaviour and limits are untouched.

**Results (real MAME 0.289).** Section Z alone in six fresh MAME sessions: 25 passed, 0 failed each (HL 0xFB38 each time). Full `validate_hardening.py` twice, each a fresh session: **100 passed, 0 failed** both times (Z5 HL 0x2F in one,
0xF in the other, so the comparison did see a value that differs between sessions). No other suite was re-run: no implementation changed.

**Remaining uncertainty.**
* HL was constant *within* every session, so Z5 has not been seen to compare a value that changed between hits; it demonstrates agreement at three occurrences per session, not tracking of a varying register. A, AF were never compared in capture.
* The observation is one instruction after the hit, not the same instruction (an observer cannot share the capture's address); correctness relies on `bit 0,a` preserving SP/BC/HL and on no interrupt between the two instructions (an interrupt would
  change PC/SP at the observer, which the PC and SP checks would show).
* README.md and this file now both give 100 checks for hardening (the earlier 92 and the 91+1 in the closeout are historical results of earlier code).

## 9. Managed sessions: `start_session` / `stop_session` (`mcp_session_e2e.py`, `tests/test_session.py`): PASSED for Shinobi on Windows
Run through the official MCP SDK client against **one** server process (production entry point, no `MAMEMCP_PORT`, no `launch.json`, no chosen run directory; the server environment deliberately carried `MAMEMCP_TEST_FAIL_CLEAR=1`).
40 checks, 0 failed (`runs\mcp_session_e2eesults.json`):
* **A** server initialises with no MAME; `session_info` = `no_session`; bridge tools = `no_session` (an unrelated MAME on its own port is not attached); `stop_session` no-op; bad/unknown system refused before any launch; `start_session` exposes only `system` and `ready_timeout_s`.
* **B** `start_session(shinobi)` ready in ~1-4 s: machine identity, `debugger_stopped`, unique run/evidence dirs, ROM set verified good; second start refused (`session_active`); test-only hooks not inherited (`test_hooks == []`).
* **C** stopped-state `session_info`/`read_registers`, `resume`, bounded capture of 30 events at the validated address `0x404`: completed, cleanup verified, evidence files present, execution still running.
* **D** `stop_session` with a capture still running: capture cancelled and verified, evidence finalised, owned process gone, unrelated MAME untouched; afterwards `no_session` with `last_session`.
* **E** second start in the same server: new session id and run dir; old experiment ids refused (`experiment_from_other_session`; default status does not inherit).
* **F** unexpected exit (owned MAME killed from outside during a capture): `exited` with exit code, calls refused as `session_exited` (no reconnect to the dead process), new `start_session` replaces the record.
* **G** startup failure (second server, corrupt `shinobi.zip`): `mame_exited_during_startup` with exit code, MAME output tail and run dir; state `failed`; unrelated MAME untouched.
* **H** normal server shutdown (stdin closed) with an active capture: the server's own cleanup ran (capture verified, process terminated) and the MAME was gone.

Unit tests (47 added, fakes only, not MAME evidence): argument validation, production-only launch arguments, start failures (exit during startup, timeout, machine mismatch, test hooks, port squatter, launch exception), ownership refusal
(wrong image / recycled pid / foreign port owner / unreadable image: never terminated), kill escalation, unexpected exit, serialisation (stop waits for in-flight calls, new calls and starts refused during stop, drain timeout reported), external fallback, shutdown, configuration, launcher env hygiene.
Regression after the change: `pytest` 97 passed; `validate.py` 51, `mcp_e2e.py` 13, `mcp_restart_e2e.py` 22, `mcp_bank_e2e.py` 8, `mcp_multi_e2e.py` 11, `validate_hardening.py` 100, `validate_banked.py` 42, `validate_multi.py` 67, `validate_latency.py` 10, all 0 failed.
(One run of `validate.py` failed its two short-timeout checks once, passed on rerun: timing-sensitive; and the first restart run exposed that external-mode `session_info` swallowed `session_changed`, fixed.)

**Limitations / untested:** a forced kill of the MCP server (no cleanup, MAME may be left running); the MCP SDK client's own shutdown kills the process tree after 2 s, so the graceful path is verified by the server's stderr line, not by timing alone; only Shinobi and Windows; free-port selection has a small race
(a squatter on the chosen port is detected by the bridge-port ownership check and fails the start); a hung MAME is stopped after the 20 s drain/timeouts, not instantly; `-verifyroms` failure does not by itself block a start (MAME's own error shows as startup failure).
Live Claude Code tool use of these tools is a separate step (see README / hand-over); its first result is recorded in 9a below.

### 9a. Live Claude Code smoke test (2026-11-15): PASSED, live-client validation (separate from the harness results above)
Evidence class: Claude Code (Sonnet 5.5) called the registered `mame` MCP tools directly (no shell, no harness script) against the real MAME 0.289 and Shinobi romset. The harness sections above (`mcp_session_e2e.py` and the others) drive the server with the official MCP SDK client; this is the first run through Claude Code itself. It is one run, not a repeatable script, and the outputs below are as the tools reported them.
* `session_info` = `no_session`; `start_session(shinobi)` ready (pid 53960, run `shinobi-20261115-141611-8111a3`), `debugger_stopped`, `test_hooks == []`, `unresolved_instrumentation == []`, `-verifyroms` good.
* Stopped `:maincpu` registers: PC `0x402`, SP `0xFFFFFF00`, SR `0x2700`.
* `capture_breakpoint` at `:maincpu` `0x404` (30 events, 15 s, D0 and SR), then `resume`; the first `experiment_status` poll already showed `completed`: 30 captured, 30 observed, 0 dropped, reason `event_limit`, 2227 ms wall, cleanup verified (breakpoint cleared, temporaries reset). Last five events: D0 `0x0`, SR `0x2404` at pc `0x404`. Evidence: `runs\sessions\shinobi-20261115-141611-8111a3\evidence\exp-s1794716171-9253df-001\` (`manifest.json`, `events.jsonl`).
* `stop_session`: process identity verified, terminated, no cleanup failures; `session_info` afterwards `no_session` with `last_session.state == exited`, `exit_reason == stopped_by_stop_session`, `exit_code == 1`.
* Not covered by this run: any machine other than Shinobi, error paths, stop with a capture active, or repeated use from the live client.

**Why `exit_code` is 1 after `stop_session` (source inspection plus one focused check):**
* `Sessions._terminate` (`session.py`) first calls `proc.poll()`; only if MAME is still running and its identity is verified does it call `proc.terminate()`, then reads `proc.poll()` into `exit_code`. The reply also had `was_running: true`, no `escalated_to_kill` and `still_running: false`, so the graceful `terminate()` path was the one taken.
* On Windows, CPython's `Popen.terminate()` is `_winapi.TerminateProcess(handle, 1)` (shown by `inspect.getsource` on Python 3.14.7). A throwaway Python child terminated that way returned `returncode == 1`. `Popen.kill()` is an alias on Windows, so the escalation path would give 1 as well.
* Conclusion: the code 1 is supplied by our termination mechanism. It is not an emulator-reported status and does not by itself indicate an emulator failure. No production change was made; none was warranted.
* **Remaining uncertainty:** the exit code cannot distinguish "MAME was terminated by us" from "MAME exited by itself with 1" at the moment of stopping. The distinguishing evidence is `was_running: true` together with the identity check in the same `stop_session` reply and the `stopped_by_stop_session` reason that this code path sets. I did not run MAME with a clean exit (for example its own quit) to see what code it returns, and I did not inspect MAME's stderr for this run. Consumers should treat `exit_code` as meaningful only for `unexpected_exit` and `startup_failed`, not for `stopped_by_stop_session` or `server_shutdown`.

## 10. Checkpoints, RAM writes and held inputs (`validate_checkpoint_input.py`, `mcp_checkpoint_e2e.py`, `tests/test_operations.py`): PASSED for Shinobi on MAME 0.289 (harness evidence, plus one live Claude Code smoke test in 10a)
New tools: `list_inputs`, `press_inputs`, `release_inputs`, `save_checkpoint`, `load_checkpoint`, `list_checkpoints`, `write_memory`. The state-changing ones are managed-session only (an externally attached MAME is refused with `no_session`; external attachment otherwise behaves as before). Machine-specific facts (RAM / ROM / unmapped / device addresses, the press target, the expected field counts, the machine without save-state support) are in `tests\fixtures\profiles\shinobi.json` under `checkpoint_input`; the server contains none. **No Side Fighter addresses were researched or guessed; MAME was not switched.**

**Evidence class: harness.** The direct-bridge suite drives the plugin with the test-only fixture plugin as an independent observer (its own frame-by-frame port watcher, its own RAM reads through MAME's space API, its own debugger stop). The SDK suite drives the production server over stdio with the official MCP client, no fixture plugin and no test hooks. The one live Claude Code run is a separate test, recorded in 10a below and kept apart from these harness results.

**API choice (checked in the mame0289 source first).** Lua `machine:save()/load()` are asynchronous (they only schedule; the file appears at the next safe point), report no failure and do not complete while the machine is stopped in the debugger. The debugger's `statesave`/`stateload` run synchronously. The plugin therefore uses the debugger commands as the primary path, with the scheduled Lua call as a fallback (used when MAME cannot save at that instant, or forced by the test-only hook `MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT`). Neither is trusted to have worked: a save is complete only when the file exists with a valid `MAMESAVE` header for this machine, and a load only when the post-load fingerprint (frame counter, every CPU's non-`CUR*` registers, and a digest of MAME's memory shares) matches what was recorded at save time.

**Direct-bridge suite, 48 checks (`runs\checkpoint_input\results.json`):**
* **I inputs (14)**: `list_inputs` reports the machine's real fields (20 supported, 8 unsupported = DIP/toggle); presses of 1, 5, 30 frames and two fields together are seen by the independent watcher for **exactly** the requested number of frames, then idle again with release verified; a press made while debugger-stopped or paused is armed, nothing elapses (0 frames after 1.5 s), blocks write/save/second press (`input_active`), and its frames count only after the resume (watcher: exactly 5); cancel while stopped, cancel mid-press and idempotent cancel; **client disconnect mid-press releases the input** (watcher: press ended long before 600 frames, status `client_disconnected`); unsupported / unknown port / unknown field / 0 / 601 frames / duplicates / empty are refused with nothing held.
* **W RAM writes**: 1/2/4-byte writes report before / requested / actual and agree with the fixture's own read; a 16-bit write is big-endian in memory; a write through the map's mirror lands in the same RAM; ROM, unmapped, device-handled (tile RAM) and memory-mapped I/O targets are refused with `rom_target` / `unmapped_target` / `unsupported_target` and ROM is unchanged; bad width / alignment / range / space / cpu are refused; refused while running, accepted while paused.
* **C checkpoints (immediate path)**: save while debugger-stopped and while paused (valid `MAMESAVE` header, size, exact fingerprint, execution unchanged, zero frames run); ids are never overwritten; after changing RAM (including a second RAM share) a load restores both (independent reads), the frame counter returns to the checkpoint frame after ~1 s of emulation, paused/stopped state is preserved, the state epoch is bumped; hostile ids rejected before any debugger command is built; a missing file and files for another machine / truncated / not a state file are refused **before** the machine is touched; a load without the recorded fingerprint is refused.
* **G guards**: an active capture refuses save, load and write (`capture_active`) and cancelling it re-enables them; experiment ids recorded before a load are refused as `experiment_stale_after_load` (status and cancel), the default status does not inherit them, their evidence files stay on disk, and a capture started after the load completes normally with 3 events.
* **D scheduled path (test hook)**: save from a debugger stop completes via polling, ran at most a few frames, restores the stopped state, and reports `fingerprint_exact: false` (frame window); load verified by frame window with RAM restored; a **corrupt** state file is reported `checkpoint_not_applied`, never as loaded, emulation bounded (<= 95 frames), stopped state restored, epoch not bumped.
* **U unresolved instrumentation (test hook `MAMEMCP_TEST_FAIL_CLEAR`)**: with a breakpoint that could not be removed, save, load and write are all refused `unresolved_instrumentation` and nothing is written.
* **M**: machine `005` (`savestate="unsupported"`): `supports_save` is false, save and load are refused `unsupported_machine`, no file written.

**SDK suite, 38 checks (`runs\mcp_checkpoint_e2e\results.json`)**: one server process; its environment deliberately carried `MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT`, `MAMEMCP_TEST_FAIL_CLEAR` and `MAMEMCP_STATE_DIR`, and the session inherited none of them.
* **A** the tool surface is exactly the 16 typed tools (no Lua / shell / debugger-command tool); `write_memory` requires cpu, space, address, value and width (enum 1/2/4); every new tool reports `no_session` with no MAME.
* **B the target flow**: `start_session` -> resume -> `press_inputs` 20 frames (waits; machine saw exactly 20; release verified) -> pause -> `write_memory` 0x5A -> `save_checkpoint` (real state file under the session run dir, id, SHA-256, frame, label) -> change RAM and run ~1.2 s -> `load_checkpoint` (verified; frame and RAM back to the saved values; still paused; same session id; epoch 1) -> `capture_breakpoint` after the restore (20 events, cleanup verified, evidence on disk) -> `stop_session`.
* **C** 1/2/4-byte writes agree with an independent `read_memory`; ROM / unmapped / device / I/O refusals; bad value, width 3 (refused by the tool schema itself), misalignment, bad tag, unknown cpu, negative value; write and save refused while running.
* **D** unsupported/unknown/out-of-range press requests refused; a press while paused is armed and held; a held input blocks write/save/second press; `release_inputs` (idempotent); `wait=false`; pausing mid-press freezes the count; **`stop_session` with a held input releases it first** (`input_cleanup.released`, `was_holding`) and the owned MAME is gone.
* **E** active capture refuses save/load/write; stale experiment ids after a load; `list_checkpoints`; hostile / malformed / unknown ids.
* **F cross-session**: a new session lists the earlier session's checkpoint (not current), earlier experiment ids are refused, and loading the earlier checkpoint **imports** it, verifies it (frame, registers, RAM) and restores RAM; a checkpoint file modified afterwards is refused by SHA-256 before the machine is touched.
* **G** normal server shutdown (stdin closed) with a held input stops the owned MAME.

**Unit tests (`tests\test_operations.py`, 66 added, fakes only - not MAME evidence):** argument validation (`write_memory_args`, `press_args`, labels, checkpoint ids incl. a trailing-newline id), save/load registry and sidecar metadata, import from an earlier session (system / MAME version / SHA-256 / id mismatches; a bad import is not left behind), pending-poll completion and failure mapping, plugin refusals passed through with nothing registered, `operation_in_progress` serialisation, `stop_session` abandoning a pending wait, input wait / timeout / error-path release (a failed release never masks the original error), release on `stop` and on shutdown, external attachment never reaching the managed-only operations, launcher state-dir and test-hook isolation.

**Regression after the change** (all 0 failed): `pytest` 163; `validate.py` 51 (its two short-timeout checks failed on the first run and passed on rerun: the known timing sensitivity above); `validate_hardening.py` 100; `validate_banked.py` 46; `validate_multi.py` 95; `mcp_e2e.py` 13; `mcp_session_e2e.py` 40; `mcp_restart_e2e.py` 22; `mcp_bank_e2e.py` 8; `mcp_multi_e2e.py` 11. Not rerun: `validate_latency.py`. Three harnesses asserted the old tool list or "no input / no write tool"; those assertions were updated (the invariant "no Lua / shell / debugger-command / bank-select / poke tool" is kept; the only writer is the typed, RAM-only `write_memory`).

**Found by validation and fixed:** (1) a press armed while debugger-stopped was counted for one frame the machine never saw it (bridge `frames_elapsed` 5, watcher 4); the first frame(s) that still read idle after arming are now not counted (bounded by `input_latency_frames = 3`, reported as `latency_frames`), after which bridge and watcher agree exactly; (2) frame + registers cannot tell a paused state from the same state with different RAM (`load_effect_observed` was false after a RAM-only change), so the fingerprint includes a RAM-share digest; (3) `$`-anchored id / field-name regexes accepted a trailing newline (now `fullmatch`); (4) the new fixture watcher used `manager.machine` at plugin load time (nil). Harness mistakes, not server bugs: restarting a watcher while the input was held, `pause` requested from a debugger stop, a ROM path mangled by shell escaping.

**Limitations (observed or by design):**
* **Execution state.** Save, load and RAM write are refused while emulation is running (`execution_running`); pause or stop first. Input frames elapse only while emulation runs, so a press made while paused/stopped is armed and held until the resume (and blocks the other state-changing operations meanwhile; `release_inputs` ends it).
* **Immediate vs scheduled.** The default immediate path is exact. The scheduled fallback (exercised here only through the test hook; no real MAME 0.289 case that needed it was observed) briefly runs emulation, so its fingerprint is a frame window (`fingerprint_exact: false`, `frames_run_during_operation` reported) and a load is bounded to 90 frames.
* **Verification coverage.** The RAM digest covers MAME's memory *shares* (RAM pools drivers expose by tag), bounded to 256 KiB per fingerprint; anonymous RAM and device-internal state are not covered by the digest, although the state file contains them. A load that restores frame, registers and shares is reported as verified: strong evidence, not proof of every byte.
* **RAM writes** classify targets from the CPU's *declared* address map (plain `ram` read and write handlers only); a handler installed at run time is not visible, so the read-back is the final arbiter (a mismatch is rolled back and reported). Tile/sprite/palette RAM behind device handlers and I/O are refused even though they are RAM-like. Only the program and data spaces; widths 1/2/4; the value uses the space's endianness (big-endian 68000 verified; little-endian CPUs untested for writes).
* **Inputs** are plain digital fields only; duplicate field names on one port collapse to one entry; no analog, toggle, DIP or configuration fields; at most 8 fields and 600 frames per press; one press group at a time.
* **Checkpoints** load only for the same machine and MAME version; ids from earlier sessions import from that session's run directory (which must still exist); there is no delete tool.
* Validated on **Shinobi (68000)** and one machine without save-state support (`005`) only, on Windows. Other machines, CPUs and games, and real MAME save/load failures other than the corrupt-file case, are untested.

### 10a. Live Claude Code smoke test (2026-11-18): PASSED, live-client validation (separate from the harness results above)
Evidence class: Claude Code (Sonnet 5.5) called the registered `mame` MCP tools directly (no shell, no harness script) against real MAME 0.289 and the Shinobi romset, after reconnecting the server with `/mcp` and with both ROM paths registered. One run, not a repeatable script; values below are as the tools reported them. This is not harness evidence and does not replace it.
* **Start:** no prior session (`no_session`). `start_session(shinobi)` ready: Shinobi (set 6, System 16A), MAME 0.289, `debugger_stopped`, `debugger_available: true`, `test_hooks == []`, `unresolved_instrumentation == []`, `-verifyroms` good, run `shinobi-20261118-222413-6b81a0`, session `s1795004654-5c2f5a`. `list_inputs` showed 20 supported fields and `:P1` / `P1 Button 1`.
* **Input:** after `resume`, `press_inputs(:P1 / P1 Button 1, 20 frames)` returned `released`: 20 requested, 20 elapsed, 20 observed pressed, armed at frame 96, ended at frame 116, `end_reason: completed`, `latency_frames: 0`, no release errors; `session_info` afterwards showed `release_verified: true`.
* **Checkpoint, write, load** (paused at frame 233): original byte at `:maincpu` program `0xC70010` = `0x02`. `save_checkpoint("smoke")` completed: id `shinobi-20261118-222413-6b81a0-cp001`, mode `immediate`, `fingerprint_exact: true`, 14,802-byte file, SHA-256 `cfd6c26be74d5ddd5352efb4cb4a86b14ea3eb9552083f3f533b8bf3b40ed9a4`, execution stayed paused, 0 frames run. `write_memory` of `0x5A` (width 1): before `0x02`, requested `0x5A`, after `0x5A`, `verified: true`; an independent `read_memory` returned `0x5A`. `load_checkpoint` completed (immediate, state epoch 1, no experiments invalidated); `read_memory` then returned `0x02`, equal to the original.
* **Load verification coverage:** `method: frame_and_cpu_registers_exact`, `exact: true`, frame 233 before and after (expected 233), 64 registers compared (debugger-derived `CUR*` pseudo-registers ignored), `load_effect_observed: true`, RAM digest `eabd83dc` over 6 memory shares / 59,456 bytes (`partial: false`). Anonymous RAM is not covered by the digest.
* **Capture:** `capture_breakpoint` at `:maincpu` `0x404` (10 events, 15 s wall limit, D0 and SR) armed while paused, then `resume`; the first `experiment_status` poll already showed `completed`: 10 captured, 10 observed, 0 dropped, reason `event_limit`, 1530 ms wall; every event D0 `0x0`, SR `0x2404` at pc `0x404`. Cleanup verified (breakpoint cleared, temporaries reset, `undisturbed: true`). Evidence: `runs\sessions\shinobi-20261118-222413-6b81a0\evidence\exp-s1795004654-5c2f5a-001\` (`manifest.json`, `events.jsonl`).
* **Stop:** `stop_session` reported `input_cleanup` released (`was_holding: false`, no release errors), capture cleanup verified with no unresolved instrumentation, process identity verified (image, creation time, bridge port) and terminated, `cleanup_failures == []`; `session_info` afterwards `no_session` with `last_session.exit_reason == stopped_by_stop_session`. `exit_code` 1 is the Windows termination code (see 9a), not an emulator status.

**Limitations of the live run:** one Shinobi run and one save/write/load cycle; anonymous RAM is outside the verification digest; inputs armed while stopped or paused (the held-until-resume case) were **not** exercised live (harness only, above); no error path, cross-session load, scheduled-save path or machine other than Shinobi was run live.

### 10b. Live Claude Code compatibility smoke test on 1944 (2026-11-18): save FAILED, defect found (live-client evidence; separate from the harness results)
Target selected by the user for this test: MAME 0.289 (`D:\arcade\mame\mame.exe`), ROM directory `D:\arcade\mame\roms`, system `1944` (parent set, "1944: The Loop Master (Europe 000620)", `capcom/cps2.cpp`, MC68000 `:maincpu` + Z80 `:audiocpu`; `-verifyroms 1944` good; `1944.zip` SHA-256 `9186f400aa9cfcdf2720c16bf9801da0e782c5ca6e4bfcdac70c3dc30bbe508d`, SHA-1 `bcb9f0846ce8acd97e697e4596b8403c136d65ba`; the archive hash identifies the local package, the individual ROM CRC/SHA-1 definitions from `-listxml` identify the contents). One run through the registered `mame` MCP tools:
* **Startup PASS**, **inputs PASS as tool mechanics** (Coin 1 and 1 Player Start on `:IN2`, P1 Button 1 on `:IN0`, each 10 requested / 10 elapsed / 10 observed, release verified), **cleanup PASS** (held-input cleanup, no unresolved instrumentation, process terminated, `no_session`).
* **Gameplay was not established.** No screen and no known game-state variable were available; input presses and release do not show that the game accepted a coin or started.
* **Save FAILED:** `save_checkpoint` while paused returned `internal_error: ...init.lua:1662: bad argument #1 to 'for iterator' (table expected, got nil)`. No file was created; `list_checkpoints` was empty. Load was therefore BLOCKED.
* `session_info` had already shown the same defect in its device list: `:qsound` (QSound HLE) reported `breakpoints` and `register_count` as unavailable with the Lua errors `init.lua:249: attempt to index a nil value (field 'debug')` and `init.lua:285: bad argument #1 to 'for iterator' (table expected, got nil)`.
* A `write_memory` to `0xFF0000` (width 1, `0x00` -> `0x01` -> `0x00`) was accepted as plain RAM and read back, with a manual revert; `0x900012` was refused as `unsupported_target` (read handler `ram`, write handler `delegate`). That address was a guess and is not a justified mutation target.
* The PC was observed around `0x7F4`/`0x7F6` after the coin/start presses; its meaning was not established in that run (see 10c).

### 10c. Correction: devices without a state interface (harness evidence; live retests are in 10d and 10e)
**Cause (confirmed from the plugin source and the retained error text):** the plugin treated "has an address space" as "has registers". `:qsound` (`qsound_hle`) exposes a ROM address space but no `state` interface (and no `debug` interface), so `dev.state` / `dev.debug` is nil. `fingerprint()` (checkpoints), `session_info` (`register_count`, `breakpoints`), `read_registers`, and the capture register/breakpoint path all dereferenced it. Other consequences found while inspecting these paths: `fingerprint()` silently skipped registers that failed to read (and a failed RAM digest), which would have weakened verification without saying so.

**Correction (`plugin/mamemcp/init.lua`):**
* An interface probe separates **absent** (nil, no error) from a **real read error** (an error string); the two are reported differently everywhere.
* The fingerprint records coverage per device: `covered` with its register count; `no_state_interface`; or `state_read_error` with the error. Registers that cannot be read or are not integers are listed as excluded (count and examples) instead of being dropped. A failed RAM digest is recorded (`ram_error`), not omitted. Save and load both build the fingerprint with the same function, and the load compares coverage (device set, covered flag, register counts) as well as values, so a coverage change is a verification difference. Fingerprints stored before this field existed carry no `devices` and are compared on values only.
* `save_checkpoint` returns `fingerprint_coverage` and `load_checkpoint` returns `verification.coverage`: registers compared per device, devices without a state interface, read errors, excluded registers, the RAM-share digest (shares, bytes hashed vs total, `partial`) and the statement that this is **not** whole-machine equality. The coverage is also stored in the checkpoint's metadata file and returned by `list_checkpoints`.
* `session_info` reports `state_interface` (`present` / `absent` / `read_error`) per device; an absent interface gives `register_count: 0` with a note, and breakpoints on a device without a debug interface are `{unavailable, interface: "absent"}`.
* `read_registers` on such a device returns `no_state_interface` (or `state_read_error`); `capture_breakpoint` returns `no_debug_interface` / `no_state_interface` / `debug_interface_error` / `state_read_error`, not `internal_error`.
* **Startup readiness (adjacent defect, found while validating this):** the bridge can answer while MAME is still loading ROMs; that `session_info` lists no devices and the debugger is not initialised yet. `start_session` judged it immediately (`debugger_unavailable` after 91.6 s in the SDK harness on 1944), and a `resume` sent in that window crashed MAME 0.289 in `running_machine::resume()` (access violation, seen once in a scratch probe). `SessionManager.start` now waits, inside the same wall-clock deadline, while the reply explicitly lists an empty device set (a MAME that exits meanwhile or never finishes is reported as before). The harness `Inst` helper does the same. This was not part of the requested change; it is recorded here so it can be reviewed separately.

**Harness results (real MAME 0.289, all 0 failed):**
* `pytest` 168 passed (+5: the readiness wait x4, coverage persisted/returned for a machine with a device lacking a state interface). Fakes only; not MAME evidence.
* `validate_checkpoint_input.py` 62 passed (48 + section **S** on 1944, 13 checks, + a Shinobi coverage check). Section S: `session_info` reports `:qsound` as `state_interface: absent`, `register_count: 0`, breakpoints interface absent (no raw Lua text); `read_registers` / capture on it return `no_state_interface` / `no_debug_interface` while an unknown register is still `unknown_register`; **save while paused completes** (immediate, exact) with `:qsound` recorded as not covered and registers compared on `:maincpu` (20) and `:audiocpu` (27); after ~2 s of emulation the **load verifies exactly** (frame 566 -> 439, 47 registers compared, effect observed) and its coverage equals the save's; a byte written to RAM (`0xFF0000`, accepted by `write_memory` as plain RAM, independent read agrees, emulation **not** resumed) is restored by the load (independent read). Shinobi: coverage reported for `:maincpu`, `:soundcpu`, `:upd7751`, none lacking a state interface.
* `mcp_checkpoint_e2e.py` 46 passed (38 + coverage checks in the Shinobi flow + a 1944 part through the production server and the official SDK client: `start_session(1944)`, device reporting, save, advance, exact load, RAM mutation restored, input discovery on 1944's own fields, server shutdown with a held input on the 1944 session).
* Regression, same run: `validate.py` 51, `validate_hardening.py` 100, `validate_banked.py` 46, `validate_multi.py` 95, `mcp_e2e.py` 13, `mcp_session_e2e.py` 40, `mcp_restart_e2e.py` 22, `mcp_bank_e2e.py` 8, `mcp_multi_e2e.py` 11. `validate_latency.py` was not rerun. Four of those scripts (`validate_banked`, `validate_multi`, `mcp_multi_e2e`, `mcp_session_e2e`) pointed at `D:\arcade\ROMs\MAME 0.289 ROMs (merged)`, which now holds no sets; their ROM-directory constant was changed to `D:\arcade\mame\roms` so they could run. A harness flake was also fixed: `press_inputs` can return before the machine's next frame sets `release_verified`, so the harness polls for it (wall clock).
* A harness check that wanted `input_status == "none"` after a completed press was wrong (`released` is correct) and was fixed.

**The PC loop around `0x7F4`/`0x7F6` (investigated; harness evidence):** after the same coin/start sequence and running to frame ~1500, 40 of 40 `CURPC` samples were at `0x7EE` or `0x7F4`. The bytes at `0x7EE` in the `opcodes` space are `4a79 00921cc4 6700 fff8`, which I decoded by hand as `TST.W $00921CC4 ; BEQ` back to the test - a poll of a flag at `0x921CC4`. The instructions after it include `ADDQ.W #1,$00921CD0`; that word advanced by exactly one per frame (967 -> 1049 over frames 1513 -> 1595), so the loop is left once per frame: a frame-synchronisation wait, not a hang. The flag itself read 0 at every sample. **Not established:** what sets the flag (presumably an interrupt handler; not checked), whether the game is in attract mode or gameplay, and whether the coin or start was accepted. Earlier in the boot (before the presses, ~6 s) the PC was in a different loop (`0x6A8`/`0x6AE`).

**Remaining limits and what is not claimed:**
* **Live validation:** this section is harness evidence only. The live retests that followed are recorded in 10d (intermediate, with a stale server and a disregarded checkpoint) and 10e (completed). 10b remains the original live failure.
* **RAM-mutation target.** `0xFF0000` is accepted by `write_memory` as plain RAM, but mapped RAM and zero bytes do not show a location is unused. The test is valid because no emulation runs between the write and the load, so the machine cannot observe the value; restoration is verified by an independent read of that byte. It does not show the location is unused, and it says nothing about Side Fighter or any game field.
* **The RAM-share digest is partial on 1944** (9 shares, 262,144 of 4,415,628 bytes hashed), so on this machine the digest is weak evidence and the independent read of the mutated byte is the restoration evidence. Load verification here means: frame counter, registers of the CPUs, and the digest above - not whole-machine equality. Device-internal state of devices without a register interface (QSound), anonymous RAM and the rest of the state file are not compared.
* Gameplay establishment and checkpoint compatibility are separate questions; input presses and release do not prove the game accepted a coin or started. No observable game-state variable has been established for 1944.
* Wall-clock limits are enforced in the new harness (monotonic deadlines for startup, frame waits and sampling); emulated time is only reported.
* Startup of 1944 is slow when MAME loads the ROMs cold (up to ~90 s observed); `start_session` accepts `ready_timeout_s` up to 120.
* Validated on Shinobi, 1944 and one machine without save-state support (`005`) only, on Windows.

### 10d. Live 1944 retest, first attempt (2026-11-18): save and load worked, save-side coverage NOT shown (live-client evidence; historical)
Same target as 10e, through the registered `mame` tools after the plugin fix. Recorded as it happened, including two problems:
* **Stale Python server.** The MCP server process had not been restarted, so it still ran the old `operations.py`. The plugin is loaded afresh with every MAME start, so `session_info` already showed the new fields (`state_interface: absent` for `:qsound`, no Lua error text) and the load reply carried `verification.coverage`; but the **save reply carried no `fingerprint_coverage`**, because the old server dropped the field. The save-side coverage was therefore not observed in this run.
* **Disregarded frame-zero checkpoint.** The first "run for about 3 s" did not run: resume, a sleep and pause were issued in one parallel batch (an operator sequencing error, not a product defect), so they executed back-to-back and `…-a15969-cp001` was saved at **frame 0** (emulated 0.017 s). It is disregarded as evidence. The run was redone sequentially and `…-a15969-cp002` was saved at frame 383.
* Results otherwise: start ready (about 43 s cold), `:qsound` reported absent, save `completed` / `immediate` / `fingerprint_exact` true, advance to frame 713, **load restored frame 383**, `exact` true, 47 registers compared, load coverage reported (`:qsound` without a state interface, no read errors, partial RAM digest), cleanup verified, `no_session`.
* Conclusion at the time: the original `save_checkpoint` failure was fixed, but save-side coverage and save/load coverage agreement were still unshown live. The MCP server was then reconnected (`/mcp`) to load the updated Python.

### 10e. Live 1944 retest, completed (2026-11-18): CLOSES the save-side coverage gap (live-client evidence)
Run through the registered `mame` tools only, each dependent step sequential, with the updated server loaded (checked by requiring `fingerprint_coverage` in the save reply). Run `1944-20261118-233210-e6431c`.

**Target:** system `1944` (parent set, `parent: "0"`), "1944: The Loop Master (Europe 000620)", MAME 0.289 (`D:\arcade\mame\mame.exe`), ROM directory `D:\arcade\mame\roms`, `1944.zip` SHA-1 `bcb9f0846ce8acd97e697e4596b8403c136d65ba`, `-verifyroms` good. No test hooks, no unresolved instrumentation, debugger available, CPUs populated on the first `session_info` (`:maincpu` 22 registers, `:audiocpu` 29, `:qsound` absent state interface with no Lua error text). No session was active beforehand.

**Sequence and results:**
| Step | Result |
|---|---|
| Resume ~4 s wall, pause | running, then paused |
| `save_checkpoint` | `…-e6431c-cp001`: `completed`, mode `immediate`, `fingerprint_exact` true, **frame 464** (emulated 7.80 s), 0 frames run during the operation; **`fingerprint_coverage` present in the reply** |
| Resume ~3 s wall, pause | frame advanced **464 -> 864** |
| `load_checkpoint` | `completed`, mode `immediate`, no error; **frame 864 -> 464** (`expected_frame` 464, `frame_after_load` 464), `load_effect_observed` true, method `frame_and_cpu_registers_exact`, `exact` true, **47 registers compared** |
| `stop_session` | input cleanup released, no active experiment, unresolved instrumentation `[]`, process identity verified and terminated, no cleanup failures; `session_info` then `no_session` |

**Save-side `fingerprint_coverage` and load-side `verification.coverage` were identical (no differences):**
* registers compared: `:maincpu` 20, `:audiocpu` 27 (47 in total);
* no state interface: `:qsound` (QSound HLE; nothing to compare or verify);
* read errors: none (`state_read_error: []`); excluded registers: none;
* RAM digest: **partial**, 262,144 of 4,415,628 bytes hashed over 9 memory shares (digest `69b1fa47`); anonymous RAM is not covered;
* the reply's own caveat: device-internal state of devices without a register interface, anonymous RAM and any other state in the file are not compared; this is not a whole-machine equality check.

**What this establishes:** on real MAME 0.289 through the production server and a live Claude Code client, `save_checkpoint` on 1944 now completes (10b's failure is not reproduced), and the checkpoint restores the saved frame with the saved CPU registers. **Exactness applies only to the reported coverage** (the 47 registers, the frame counter and the partial RAM digest). It is not whole-machine equality; QSound internal state is not checked.

**What it does not establish (unresolved):**
* **Gameplay.** No coin, start or input was sent in this run, and a responsive frame loop plus a successful restore do not show attract mode or gameplay. Gameplay on 1944 remains unestablished (10b's coin/start presses proved only the tool mechanics; the `0x7EE`-area PC loop in 10c is a frame-synchronisation poll and says nothing about the game state).
* **Side Fighter / game-specific research.** Not done and not part of this validation; no game-state addresses are known.
* RAM mutation was not repeated live (it is covered by the harness, 10c). The RAM digest is partial on this machine.

## Failed during development (found by validation, fixed)
Original POC: `bpset(addr)` without condition/action crashes MAME 0.289; expressions are hex by default (`#` for decimal);
counter reset needs a bare `temp9=0`; the single-accept socket needs `goodbye` + heartbeat re-arm; a debugger stop also sets
`machine.paused` (stop takes precedence). This milestone: `address_in_use` guard exposed a test that shared 0x404 with a manual breakpoint;
`focus` was assumed to set the visible CPU (it sets ignore flags), so registers are captured in the condition; the fixture's `set_temp` wrote hex
by default; raw-socket test tripped over heartbeat blank lines; 15 s was assumed sufficient for stopped-state requests (it is not).
Banked milestone: events looked one hit late against the independent observer, which exposed that a Lua stop lands at the breakpoint PC and MAME does not re-trigger it on resume (capture moved to `0x11`; kept as check H0);
a freshly started game's own boot write of entry 0 overwrote an intervention made during start-up (added the BOOT pre-roll and natural-selection check); the fixture used a non-existent Lua method for setting the PC (`state[...].value` is the API);
a first draft of the profile contained wrongly transcribed SHA-1s and was regenerated from the MAME source and the zip; earlier README text had lost characters to shell escaping (fixed).

## Known limitations
* One client, one active experiment, 1..8 breakpoint locations on one CPU per experiment (section 8), program space only, <= 10000 events, <= 600 s, <= 8 captured registers, <= 8 memory fields.
* **Event time fields are harvest-time observations** (emulated time/frame and host ms when the bridge read the event from the console log), **not hit-time**;
  hits are ordered by exact in-MAME sequence numbers. Nothing here is instruction-accurate timing.
* Shared `temp0..temp9` (section 1); console log lines stay in MAME's memory (about one line per hit).
* Addresses are CPU-visible logical addresses, not ROM offsets; banked or overlay memory means one address can be different code. Validated for one Z80 bank (section 7); no per-event bank id exists.
* `execution_control` is requested, not confirmed. 0.289-specific behaviour is relied on; re-validate after a MAME upgrade.
* Debugger-stopped latency (section 6).

## Readiness for one bounded real research task
Ready for one bounded task on a banked Z80-style machine, with conditions: keep the nudge configured on Windows (identity-bound; launch via the launcher so `launch.json` carries pid and creation time);
treat every captured address as logical and pair captures with a profile or static analysis that names the bank and its backing offset, because events carry no bank id; use `read_memory(bank_context=true)` to record the selected entry at read time only;
arm captures at an address the stopped machine is not sitting on; expect harvest-time timestamps. Not validated: multi-bank or non-Z80 banking, device I/O side effects, other OSes and debugger front-ends.
