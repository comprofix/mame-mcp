# Validation report (MAME 0.289: Shinobi baseline + 1942 banked memory)

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
