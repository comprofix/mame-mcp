# MAME MCP (proof of concept)

A local MAME Lua plugin, a small Python bridge client, and an MCP (stdio) server that let an agent run
bounded, repeatable debugger experiments against a **separately launched, isolated** MAME instance and
get structured evidence back, instead of writing ad hoc scripts or pulling huge traces into the conversation.

Status: POC. See [docs/VALIDATION.md](docs/VALIDATION.md) for what was proven, what failed along the way and
what is untested. Game-specific addresses live only in the experiment scripts, never in the server.

## Verified environment

| | |
|---|---|
| OS | Windows 11 Pro 10.0.26300 |
| MAME | `D:\arcade\mame\mame.exe`, **0.289 (mame0289)** |
| Validation set | `shinobi` (Shinobi set 6, System 16A, parent set, no extra ROM dependencies), `roms\shinobi.zip`, `-verifyroms`: "romset shinobi is good" |
| CPUs seen | `:maincpu` MC68000, `:soundcpu` Z80, `:upd7751` |
| Python / SDK | Python 3.14.7, `mcp` 2.3.0 (note: v2 API is `MCPServer`, not v1 `FastMCP`) |

## Install

```powershell
cd C:\Users\MatthewMcKinnon\git\mame-mcp
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
```

Nothing is installed into, or changed in, `D:\arcade\mame`. The plugin is loaded with
`-pluginspath <this repo>\plugin;D:\arcade\mame\plugins -plugin mamemcp` (MAME's own `plugins\` folder is only
read, for `boot.lua` and the bundled `json` library). All writable state (home, ini, cfg, nvram, snapshots,
evidence, `debug.log`) is under a run directory in `runs\` (git-ignored).

## Launch an isolated MAME

```powershell
.\.venv\Scripts\python -m mame_mcp.launcher --system shinobi --port 9411
```

It prints the run directory and evidence directory, writes `runs\<run>\launch.json` (MAME version, zip SHA-1,
`-verifyroms` output, expected ROM hashes from `-listxml`) and starts MAME with `-debug -video none -sound none`.
With `-debug` MAME starts stopped in the debugger; call `execution_control resume` (or `go` in the debugger).
`--no-debug` starts running but breakpoints/captures are then refused (`debugger_unavailable`).
Ctrl+C in that terminal stops *this* MAME instance only.

## Use it

Direct bridge smoke test (launches and kills its own MAME):
```powershell
.\.venv\Scripts\python -I scripts\bridge_smoke.py
```

MCP server (normally started by your MCP client; stdout = protocol, stderr = diagnostics):
```powershell
$env:MAMEMCP_PORT = "9411"; .\.venv\Scripts\python -I -m mame_mcp.server
```
Client configuration snippet (not applied anywhere): [docs/mcp-client-config.example.json](docs/mcp-client-config.example.json).

### Tools

| Tool | Purpose |
|---|---|
| `session_info` | MAME version, machine, CPU tags, address spaces, installed breakpoints (with ownership), execution state `running`/`paused`/`debugger_stopped`, ROM identity, bridge limits |
| `read_memory(cpu, address, length, space="program", mode="inspect")` | 1..4096 bytes, hex string, ascending order. `inspect` = MAME's side-effects-disabled debugger read (default); `direct` = program-space pointer read, no handler invoked (zeros where no pointer). Result states `read_mode`, `side_effects`, `limitations`. Addresses are CPU-visible **logical** addresses: on banked machines the bytes depend on the entry selected at that moment. `bank_context=true` (opt-in) adds the selected entry of every memory bank, sampled in the same plugin call as the bytes; it does **not** say which bank backs the range or which ROM offset an entry maps to |
| `read_registers(cpu, registers?)` | exact register names; `hex` and decimal-string values |
| `capture_breakpoint(cpu, max_events, max_wall_seconds, address? \| locations?, registers?, memory?, label?)` | asynchronous, both limits mandatory; give **exactly one** of `address` (single breakpoint, legacy form) or `locations` = 1..8 `{address, id?}` on the one named `cpu` (ids `[A-Za-z][A-Za-z0-9_]{0,23}`, default `L1..Ln`; duplicate ids/addresses rejected before anything is installed). One experiment = one CPU, one global event limit/wall limit/sequence. Every event carries `location`, `address` (logical) and `pc` (hit-time); memory fields `{space, address, size}` are read from `cpu`; never changes execution state (armed while paused/stopped = records nothing until resumed); borrows shared debugger temps (multi-location uses one extra temp for the hit-time PC, independent of the number of locations) and reports which; returns an experiment id, `all_armed` and the per-location arming outcome |
| `experiment_status(experiment_id?)` | progress, reason, counts (captured / observed / dropped), errors, cleanup, last 5 events, evidence paths |
| `cancel_experiment(experiment_id)` | removes only that experiment's breakpoint, verifies, finalises evidence |
| `execution_control(action)` | `pause` / `resume` only (needed to leave the initial debugger stop) |

Deliberately absent: Lua evaluation, shell, general debugger-command tool.

### Reproducible example experiment

```powershell
.\.venv\Scripts\python -I scripts\example_experiment.py
```
Reads the 68000 level-4 autovector at `0x70` (= `0x404` on this set), captures 30 entries to that handler with
`D0`, `SR`, `SP`, and the vector long. Expected last line: `RESULT: as expected`
(30 events, all at PC `0x404`, 0 dropped, breakpoint cleaned up, execution running).
Evidence: `runs\example\run\evidence\exp-*\{manifest.json,events.jsonl}`.

## Tests and validation

```powershell
.\.venv\Scripts\python -m pytest -q                              # 50 unit tests (fake sockets / fake process table; NOT MAME evidence)
.\.venv\Scripts\python -I scripts\validate.py                    # 51 regression checks vs real MAME (Shinobi)
.\.venv\Scripts\python -I scripts\validate_hardening.py          # 100 checks: temp ownership, execution-state matrix, Z80 (incl. hit-time register observation), read semantics
.\.venv\Scripts\python -I scripts\validate_latency.py --gaps 1,6,12 --reps 2   # debugger-stopped latency (measured), stale replies, retries
.\.venv\Scripts\python -I scripts\mcp_e2e.py                     # 13 checks through the official MCP SDK client
.\.venv\Scripts\python -I scripts\mcp_restart_e2e.py             # 22 checks: one MCP server across MAME restarts
.\.venv\Scripts\python -I scripts\validate_banked.py             # 42 checks: banked memory on 1942 (Revision B), Z80 :maincpu
.\.venv\Scripts\python -I scripts\mcp_bank_e2e.py                # 8 checks: bank-switch example through the MCP client
.\.venv\Scripts\python -I scripts\validate_multi.py             # 67 checks: multi-address capture (1942 Z80 + Shinobi 68000), unequal rates, cleanup-failure/recovery (test-only hooks)
.\.venv\Scripts\python -I scripts\mcp_multi_e2e.py              # 11 checks: multi-address capture through the MCP client (manifest structure asserted)
```
Results: `runs\<suite>\results.json` (banked runs: `runs\banked`, `runs\mcp_bank_e2e`). Each script launches and kills only its own isolated MAME
(distinct ports 9433/9441/9451/9452/9453/9455/9457/9459/9461/9463-9465/9475).
`tests\fixtures\` holds the TEST-ONLY fixture plugin and the machine profiles (`shinobi.json`, `1942.json`: CPU/bank-specific addresses); neither is used by the server.
The ROM collection is read only (`D:\arcade\ROMs\MAME 0.289 ROMs (merged)\1942.zip` for the banked suites).
See [docs/VALIDATION.md](docs/VALIDATION.md) for what passed, what is a limitation and what is untested.

## Behaviour you should know

* **Capture and execution state:** a capture never pauses, stops or resumes by itself; only its breakpoint action issues `g` after each recorded hit.
  Status/manifest report `execution`, `final_execution` and `execution_transitions`; a user's pause/stop is preserved, including through cancel.
* **Shared debugger state:** MAME's `temp0..temp9` are global. A capture borrows only temps that are zero, unreferenced by breakpoints and unclaimed, never
  overwrites a user's, releases them verified, and reports interference as a *failed* cleanup. Writing temps while a capture runs can still corrupt it.
* **Memory reads:** `inspect` suppresses side effects only for handlers that honour MAME's flag (not proven for any device); never use plain Lua reads for this.
* **Restart:** after MAME restarts, the first MCP call returns `session_changed` and sends nothing; old experiment ids are refused as `experiment_from_other_session`;
  a state-changing request whose outcome is unknown returns `uncertain_state: true` and is never retried. Only `session_info`, `read_registers` and `experiment_status` are retried (once, after a timeout).
* **Windows debugger-stopped stall:** while MAME is stopped in its Windows debugger, requests can go unanswered for a long time (measured: often > 30 s on an idle desktop).
  The default timeout is 15 s and is **not** a guarantee. Opt-in mitigation: set `MAMEMCP_MAME_PID` (or use `MAMEMCP_LAUNCH_INFO`, whose `launch.json` records the pid of a launcher-started MAME);
  the client then posts a no-op `WM_NULL` to that process while a request is unanswered. Before **every** post the target is re-verified: image name `mame*`, creation time
  (when recorded by the launcher), and ownership of a TCP socket on the bridge port, so a stale or recycled pid cannot reach a different MAME instance (refused, reason in `session_info.bridge_client.wake_last_refusal`).
  It is a demonstrated mitigation, not a timing guarantee; Windows and the `-debug` Windows front-end only.
* **Banked memory:** an address is a CPU-visible *logical* address, not a ROM offset and not a code identity: the same address returns different bytes under different selected banks.
  `read_memory` always follows the bank selected at the time of the read (`bank_context=true` reports the selected entries, atomic with that read). Capture memory fields are read **at the hit**
  and follow the bank selected then; a capture event carries **no** bank id (the debugger cannot read one at the hit, and a bank sampled later would describe a different moment). A breakpoint matches the logical
  address whatever bank is selected. Machine-specific bank mapping lives in test profiles (`tests/fixtures/profiles/1942.json`), never in the server. Validated on one machine only (1942, Z80); see the validation report.
* **Timestamps:** event time fields are harvest-time observations (not hit-time); hit order comes from exact sequence numbers; nothing is instruction-accurate.
* **Multi-address capture:** one experiment can watch up to 8 breakpoint locations on **one explicitly named CPU** (still one active experiment; no multi-CPU experiments).
  Example MCP arguments:
  ```json
  {"cpu": ":maincpu", "max_events": 100, "max_wall_seconds": 30, "registers": ["PC", "HL"], "memory": [{"address": 0, "size": 1}],
   "locations": [{"id": "frame", "address": "0x11"}, {"id": "irq8", "address": "0x08"}, {"id": "svc", "address": "0x166"}]}
  ```
  Event: `{"seq": 7, "location": "irq8", "address": "0x8", "pc": "0x8", "regs": {...}, "mem": [...], ...}`; `seq` is one global sequence across locations.
  `experiment_status` lists each location (`armed`, `breakpoint_index`, `events_captured`, `no_hits_observed`): *armed with 0 events* means "no hits observed", *not armed* means installation failed.
  All locations are installed inside one plugin callback before the machine runs again, so no temporary stop is needed even when armed while running (the emulated machine cannot execute between the installs). Any install failure removes everything that attempt installed, verifies, and returns `install_failed` with per-location outcomes and the rollback result (also written to a failed manifest).
  Completion, cancel, timeout and errors clear only this experiment's breakpoints (verified per location); other breakpoints, the user's pause/stop and the debugger temps policy are unchanged. A hit at one address does not suppress another; MAME evaluates one breakpoint per address, so two locations cannot share an address.
  **Cleanup failure:** if a breakpoint cannot be removed (or verified removed), the experiment ends `failed`, `cleanup.remaining_instrumentation` names it with a recovery text, its temporaries are kept (not reset), `session_info.unresolved_instrumentation` lists it, and `capture_breakpoint` is refused with `unresolved_instrumentation` until it is gone. Recovery: `bpclear <index>` on that CPU in the MAME debugger (or restart MAME); the next capture request re-checks, zeroes the retained temps and reports `recovered_instrumentation`. Validated only with test-only hooks that suppress the bridge's own clear (`MAMEMCP_TEST_FAIL_CLEAR`, `MAMEMCP_TEST_FAIL_INSTALL_AT`; off by default, stripped from the environment by `launcher.launch()` unless passed as `test_env`); a real MAME `bpclear` failure is untested.
  Events dropped by the bridge (rare buffer overflow) lose their location, since attribution is carried in the console line. The single `address` form behaves as before (its events additionally carry `location: "L1"`).

## Cleanup

```powershell
Get-CimInstance Win32_Process -Filter "Name='mame.exe'" | Select ProcessId,CommandLine   # find YOUR test instances (check the command line!)
Stop-Process -Id <pid-of-your-test-instance>
Remove-Item -Recurse -Force .\runs                                                          # isolated config + evidence
deactivate; Remove-Item -Recurse -Force .\.venv                                             # optional
```
Do not stop MAME processes you did not launch from `runs\`.

## How it works (short)

* **Transport:** newline-delimited JSON over a loopback TCP socket opened from Lua with
  `emu.file("", 7):open("socket.127.0.0.1:PORT")` (port via `MAMEMCP_PORT`).
  The socket accepts **one client at a time**; the plugin re-arms it after a `goodbye` or when a
  1-second heartbeat shows the client vanished.
* **Servicing:** `emu.register_periodic` handles requests. Verified to run while running, paused and
  stopped at a breakpoint (rates in the validation report). `add_machine_frame_notifier` was *not* used for
  requests: it keeps firing while paused but stops during a debugger stop, and
  `add_machine_frame_callback` does not exist on 0.289.
* **Capture:** one breakpoint. Its *condition* (compiled in that CPU's own symbol table) copies the selected registers into borrowed temps and tests `tempC < #N`; its action is
  `g;printf "MCP|tag|%d|...",(tempC=tempC+1),<temps>,<device-qualified side-effect-free memory reads>`. The count limit is enforced inside MAME,
  hits cost no round trips and execution resumes automatically. The plugin harvests the debugger console
  log, writes JSONL, and detects gaps from the sequence numbers.
* **Safety:** register names must exist in the CPU's `state` table; addresses/sizes are integers; the
  breakpoint index is stored and only that index is ever cleared (and verified with `bplist`).

See the validation report for the MAME 0.289 quirks this rests on (hex-by-default expressions, `bpset`
crashing without optional args, `eval` not assigning, etc.).
