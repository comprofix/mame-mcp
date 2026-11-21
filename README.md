# Description

mame-mcp is a local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives an AI agent bounded,
typed access to the MAME debugger. The agent asks for a game by its MAME short name (for example `shinobi`). The server
launches an isolated MAME of its own with a Lua plugin and the debugger enabled, connects to the plugin over a loopback TCP
socket and exposes a fixed set of tools. Results are structured and size-limited; detailed capture events are written to
JSONL evidence files in a per-session run directory instead of being returned into the conversation.

Tools (21):

* **Session:** `start_session` launches an isolated MAME for a system, stopped in the debugger; `stop_session` stops only the
  MAME this server started, after verifying its identity; `session_info` reports session state, machine, CPUs and execution state.
* **Inspection:** `read_memory`, `read_registers`, `disassemble_memory` (decoded by MAME's own disassembler).
* **Bounded captures:** `capture_breakpoint` and `capture_watchpoint` start asynchronous, limit-bounded experiments;
  `experiment_status` and `cancel_experiment` follow and stop them.
* **Execution:** `execution_control` pauses or resumes emulation.
* **Gameplay:** `list_inputs`, `press_inputs`, `press_sequence` and `release_inputs` drive digital inputs for a number of
  emulated frames; `save_checkpoint`, `load_checkpoint` and `list_checkpoints` manage machine-state checkpoints;
  `write_memory` writes one explicit RAM location.
* **Screens:** `list_screens` and `capture_screenshot` return the emulated game screen as a PNG.

There is deliberately no tool for arbitrary Lua, shell or debugger commands. The MAME executable, ROM paths and MAME arguments
come from the server's configuration and cannot be set through tool arguments. One managed session exists at a time. The MAME
installation is not modified: the plugin is loaded from this repository, and all writable state goes to a run directory.

Status: proof of concept, developed and tested against MAME 0.289 on Windows.

# Installation and usage

## Supported platforms

* **Windows:** supported (developed and tested on Windows 11).
* **Linux (and any other non-Windows platform):** not supported, and no Linux instructions are given because none have been
  verified. Concretely, managed sessions look for `<MAME folder>\mame.exe` (the executable name is fixed in the code); the
  process-identity checks used when stopping a session (image, creation time, bridge-port owner) and the wake-up that keeps a
  debugger-stopped MAME responsive use Windows APIs, and elsewhere the identity check falls back to the process handle alone;
  and the server, plugin and tests have only been run on Windows.

## Requirements

* Windows with Python 3.11 or newer (developed on 3.14).
* MAME 0.289 for Windows (`mame.exe`). The plugin and bridge rely on 0.289 behaviour; other versions are untested. MAME's own
  `plugins` folder must be intact (the plugin loads MAME's `boot.lua` and `json` library from it).
* ROM sets you are licensed to use for the systems you want to run (`<system>.zip`, `.7z` or an extracted folder). They are only read.
* An MCP client that can start a stdio server. The commands below use the Claude Code CLI.

## Install

From a PowerShell prompt:

```powershell
cd <path-to>\mame-mcp
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

This installs the package and its dependency (`mcp`) into the repository's virtual environment. Nothing is installed into or
changed in the MAME folder.

## Configure the MCP client

The server reads its machine-local settings from its environment; none of them can be set through tool arguments.

| Variable | Meaning |
|---|---|
| `MAMEMCP_MAME_DIR` | Folder that contains `mame.exe`. Always set it: the built-in default is only a developer-specific location. |
| `MAMEMCP_ROMPATH` | ROM directories separated by `;`. The first one holding `<system>.zip`, `<system>.7z` or a `<system>` folder is used (a clone's parent set must be in the same directory). Default: `<MAMEMCP_MAME_DIR>\roms`. |
| `MAMEMCP_RUNS_DIR` | Parent of the per-session run directories. Default: `runs\sessions` inside this repository (git-ignored). |

Register the server once with Claude Code (user scope):

```powershell
claude mcp add mame --scope user -e "MAMEMCP_MAME_DIR=<mame-folder>" -e "MAMEMCP_ROMPATH=<rom-folder>" -- <path-to>\mame-mcp\.venv\Scripts\python.exe -I -m mame_mcp.server
claude mcp get mame
```

Any other MCP client that starts stdio servers needs the same three things (command, arguments, environment). For a client
configured by JSON:

```json
{
  "mcpServers": {
    "mame": {
      "type": "stdio",
      "command": "<path-to>\\mame-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-I", "-m", "mame_mcp.server"],
      "env": {
        "MAMEMCP_MAME_DIR": "<mame-folder>",
        "MAMEMCP_ROMPATH": "<rom-folder>"
      }
    }
  }
}
```

Restart the client, or reconnect the server (in Claude Code, `/mcp`), so the tools are listed. The client starts the server
itself; nothing needs to be run by hand. The server starts with no emulator running, and no MAME is launched until
`start_session` is called.

## Basic usage

Ask the agent in plain language, for example:

> Start shinobi, resume it, capture 20 writes to 0xF800-0xFFFF on `:soundcpu` recording PC and SP, show me the summary, then stop the game.

The tool sequence behind that request:

1. `start_session` with `system` set to a MAME short name. MAME starts **stopped in the debugger**.
2. `execution_control` with `resume` to run it. Disassembly, checkpoints and RAM writes require stopped execution (paused or debugger-stopped).
3. `capture_breakpoint` or `capture_watchpoint` returns an experiment id immediately. Poll `experiment_status` until it completes;
   the events are written to `events.jsonl` under `<runs>\<system>-<date>-<time>-<id>\evidence\<experiment id>\`.
4. `session_info` works at any time. Its `execution.state` (`running`, `paused` or `debugger_stopped`) is the authoritative
   execution classification; after `execution_control`, confirm the result with a fresh `session_info`, because the reply only
   acknowledges the request.
5. `stop_session` when done: it cancels any active capture, releases held inputs and stops only the MAME this server launched.

# Tests

**Unit tests** (`tests\`, pytest) need no MAME and no ROMs. They use fake processes, fake bridge clients and a fake process
table to verify the Python side: argument validation; the bridge client's timeouts, stale replies, disconnects and the rule that
state-changing calls are never retried automatically; managed-session lifecycle, ownership and shutdown; the identity checks
that guard the wake-up nudge; checkpoint, input and RAM-write orchestration and cleanup; screenshot and PNG validation; the
watchpoint, disassembly and input-sequence tools and the advertised tool surface; and the configuration checks of the example
client.

**Real-MAME checks** (`scripts\`) start real MAME 0.289 through the production server using the official MCP client SDK. They
verify the session lifecycle, stop and reconnect behaviour, breakpoint, watchpoint and banked-memory captures, disassembly
against MAME's own disassembler, input sequences against an independent per-frame observer, checkpoints and RAM writes, and
screenshots. Together they need the ROM sets for `shinobi`, `1942`, `1944`, `1943kai` and `005`. The MAME and ROM locations are the
`MAME_DIR` and `ROMPATH` constants at the top of `scripts\_fx.py` (they are deliberately not read from the environment): edit
them to match your installation. The checks run one at a time, need no other `mame.exe` running, and start and stop only their
own MAME.

From the repository root:

```powershell
.\.venv\Scripts\python -m pip install -e ".[test]"                    # adds pytest
.\.venv\Scripts\python -m pytest -q                                   # unit tests
.\.venv\Scripts\python -I scripts\run_checks.py --plan lifecycle      # show what a set runs; nothing starts
.\.venv\Scripts\python -I scripts\run_checks.py lifecycle             # run a real-MAME set once
```

`run_checks.py` takes one or more sets: `unit`, `lifecycle` (session, stop and reconnect, restart, screenshots), `tools` (the
capture, disassembly, input, checkpoint and example-client checks), `legacy` (the older direct-bridge suites) and `all`
(`lifecycle` + `tools` + `legacy`). `latency` (about 15 minutes) is opt-in. A single suite can be named instead of a set; run
`--plan <set>` lists the suites in that set.
