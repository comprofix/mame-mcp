"""Managed MAME session lifecycle for the MCP server.

The MCP server starts with no MAME running. `start_session` launches ONE isolated MAME (through the existing
launcher, production plugin only, debugger enabled), connects a fresh BridgeClient to it and records the process
identity; `stop_session` finalises any capture and stops exactly that process. Everything the server owns is
reachable only through the Popen object it created; no process is ever looked up by name.

States (``session.state`` in session_info): no_session, starting, ready, stopping, exited, failed.
  * ``exited`` after an UNEXPECTED MAME exit stays visible until stop_session or the next start_session.
  * A normal stop_session returns to ``no_session`` and keeps a summary under ``last_session``.

Concurrency: bridge calls (captures, reads, execution control) register as in-flight; start/stop change the state
under one condition variable, so a lifecycle change never interleaves with a call that is already running, and
calls made during a lifecycle change are refused rather than queued. State-changing operations (checkpoints, input
presses/releases, RAM writes; operations.py) additionally run one at a time under `exclusive()`.
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import launcher, winwake
from .bridge import BridgeClient
from .operations import StateOperations
from .protocol import BridgeError

NO_SESSION, STARTING, READY, STOPPING, EXITED, FAILED = "no_session", "starting", "ready", "stopping", "exited", "failed"
SYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")  # MAME short names; no path or option syntax can pass
DEFAULT_READY_TIMEOUT = 60.0
STARTUP_POLL_S = 0.5  # while MAME answers but has not finished loading (no devices listed yet)
MIN_READY_TIMEOUT, MAX_READY_TIMEOUT = 5.0, 120.0
TERMINATE_WAIT, KILL_WAIT = 5.0, 3.0
DRAIN_WAIT = 20.0


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


@dataclass
class SessionConfig:
    """Machine-local settings (from the MCP server's environment); never taken from tool arguments."""
    mame_dir: Path = field(default_factory=lambda: launcher.DEFAULT_MAME_DIR)
    rompaths: list[Path] = field(default_factory=list)
    runs_dir: Path = field(default_factory=lambda: launcher.PROJECT_ROOT / "runs" / "sessions")

    def __post_init__(self) -> None:
        if not self.rompaths:
            self.rompaths = [self.mame_dir / "roms"]

    @property
    def mame_exe(self) -> Path:
        return self.mame_dir / "mame.exe"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SessionConfig":
        env = os.environ if env is None else env
        kw: dict[str, Any] = {}
        if env.get("MAMEMCP_MAME_DIR"):
            kw["mame_dir"] = Path(env["MAMEMCP_MAME_DIR"])
        if env.get("MAMEMCP_ROMPATH"):
            kw["rompaths"] = [Path(x) for x in env["MAMEMCP_ROMPATH"].split(";") if x.strip()]
        if env.get("MAMEMCP_RUNS_DIR"):
            kw["runs_dir"] = Path(env["MAMEMCP_RUNS_DIR"])
        return cls(**kw)

    def public(self) -> dict[str, Any]:
        return {"mame_exe": str(self.mame_exe), "rompaths": [str(p) for p in self.rompaths], "runs_dir": str(self.runs_dir)}


class ProcessIdentity:
    """Windows process facts used to prove a pid is the MAME this server launched (replaceable in tests)."""

    def available(self) -> bool:
        return winwake.available()

    def image(self, pid: int) -> str | None:
        return winwake.process_image(pid)

    def created(self, pid: int) -> int | None:
        return winwake.creation_time(pid)

    def port_owners(self, port: int) -> set[int]:
        return winwake.port_owner_pids(port)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _default_client(port: int, pid: int, created: int | None) -> BridgeClient:
    # The wake identity comes from the launch result: this pid, its creation time and THIS bridge port.
    return BridgeClient(port=port, wake=winwake.waker_for((pid, created), port=port))


@dataclass
class Session:
    run_id: str
    system: str
    port: int
    run_dir: Path
    evidence_dir: Path
    state: str = STARTING
    proc: Any = None
    pid: int | None = None
    created: int | None = None
    client: BridgeClient | None = None
    launch_info: dict[str, Any] | None = None
    session_id: str | None = None
    started_utc: str = field(default_factory=_utc)
    ready_utc: str | None = None
    ended_utc: str | None = None
    exit_code: int | None = None
    exit_reason: str | None = None
    failure: dict[str, Any] | None = None
    stop_report: dict[str, Any] | None = None
    mame_version: str | None = None
    checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)  # id -> metadata (operations.py)
    checkpoint_seq: int = 0

    def summary(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "state": self.state, "ownership": "managed", "run_id": self.run_id, "system": self.system,
            "session_id": self.session_id, "pid": self.pid, "port": self.port,
            "run_dir": str(self.run_dir), "evidence_dir": str(self.evidence_dir),
            "started_utc": self.started_utc, "ready_utc": self.ready_utc, "ended_utc": self.ended_utc,
            "checkpoints": sorted(self.checkpoints),
        }
        if self.state in (EXITED, FAILED) or self.exit_code is not None:
            d["exit_code"], d["exit_reason"] = self.exit_code, self.exit_reason
        if self.failure:
            d["failure"] = self.failure
        if self.stop_report:
            d["stop"] = self.stop_report
        return d


class SessionManager(StateOperations):
    def __init__(self, cfg: SessionConfig, *, launch_fn: Callable[..., Any] = launcher.launch,
                 client_factory: Callable[[int, int, int | None], BridgeClient] = _default_client,
                 identity: ProcessIdentity | None = None, port_picker: Callable[[], int] = _free_port):
        self.cfg = cfg
        self._launch, self._client_factory = launch_fn, client_factory
        self._ident = identity or ProcessIdentity()
        self._port_picker = port_picker
        self._cond = threading.Condition()
        self._current: Session | None = None
        self._last: dict[str, Any] | None = None
        self._inflight = 0
        self._shutting_down = False
        self._init_operations()

    # ------------------------------------------------------------------ introspection
    def snapshot(self, settle: float = 0.0) -> dict[str, Any]:
        """Session state without touching MAME. Always works (no session, starting, ready, exited, ...).
        `settle` > 0: after a disconnect, give a READY process up to that many seconds to be reaped, because
        Popen.poll() can briefly still say "running" right after the process died."""
        if settle > 0:
            with self._cond:
                s = self._current
                proc = s.proc if s is not None and s.state == READY else None
            if proc is not None:
                try:
                    proc.wait(settle)
                except subprocess.TimeoutExpired:
                    pass
        with self._cond:
            self._refresh_locked()
            if self._current is None:
                d: dict[str, Any] = {"state": NO_SESSION, "ownership": "none", "config": self.cfg.public()}
            else:
                d = {**self._current.summary(), "config": self.cfg.public()}
            if self._last is not None:
                d["last_session"] = self._last
            return d

    def launch_info(self) -> dict[str, Any] | None:
        with self._cond:
            return self._current.launch_info if self._current is not None else None

    def _refresh_locked(self) -> None:
        s = self._current
        if s is not None and s.state == READY and s.proc is not None:
            rc = s.proc.poll()
            if rc is not None:
                s.state, s.exit_code, s.exit_reason = EXITED, rc, "unexpected_exit"
                s.ended_utc = _utc()
                s.failure = {"code": "mame_exited", "message": f"MAME (pid {s.pid}) exited unexpectedly with code {rc}",
                             "stdout_tail": self._tail(s), "evidence_dir": str(s.evidence_dir),
                             "note": "an experiment that was running may have unfinalised evidence in evidence_dir"}
                client = s.client
                self._cond.notify_all()
                if client is not None:
                    threading.Thread(target=client.close, kwargs={"graceful": False}, daemon=True).start()

    @staticmethod
    def _tail(s: Session, limit: int = 4000) -> str:
        try:
            data = (s.run_dir / "mame_stdout.txt").read_bytes()[-limit:]
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    # ------------------------------------------------------------------ routing for bridge tools
    @contextlib.contextmanager
    def _use(self, external: BridgeClient | None) -> Iterator[tuple[BridgeClient, bool, Session | None]]:
        with self._cond:
            self._refresh_locked()
            s = self._current
            managed = False
            if s is None:
                if external is None:
                    raise BridgeError("no_session", "no MAME session is active; call start_session(system=...) first")
                client = external
            elif s.state == READY and not self._shutting_down:
                client, managed = s.client, True
                self._inflight += 1
            elif s.state == STARTING:
                raise BridgeError("session_starting", "a session is still starting; wait for start_session to finish")
            elif s.state == STOPPING or self._shutting_down:
                raise BridgeError("session_stopping", "the session is being stopped; nothing was sent")
            elif s.state == EXITED:
                raise BridgeError("session_exited", f"the managed MAME exited ({s.exit_reason}, code {s.exit_code}); "
                                  "call stop_session to acknowledge or start_session to begin a new one",
                                  detail={"exit_code": s.exit_code, "evidence_dir": str(s.evidence_dir)})
            else:
                raise BridgeError("session_failed", "the last start_session failed; see session_info.failure, "
                                  "then call start_session again", detail=s.failure)
        try:
            yield client, managed, (s if managed else None)
        finally:
            if managed:
                with self._cond:
                    self._inflight -= 1
                    self._cond.notify_all()

    @contextlib.contextmanager
    def use_client(self, external: BridgeClient | None = None) -> Iterator[tuple[BridgeClient, bool]]:
        """Yield (client, managed) for one bridge call, or raise a coded BridgeError. A managed call counts as
        in-flight so stop_session can wait for it; calls during starting/stopping are refused."""
        with self._use(external) as (client, managed, _s):
            yield client, managed

    @contextlib.contextmanager
    def use_managed(self) -> Iterator[tuple[BridgeClient, Session]]:
        """Like use_client for the operations that exist only for a session this server owns (checkpoints, input
        presses, RAM writes): an external attachment is never used, so it has nothing to clean up after."""
        with self._use(None) as (client, _managed, s):
            assert s is not None
            yield client, s

    # ------------------------------------------------------------------ start
    def _find_rompath(self, system: str) -> Path:
        for p in self.cfg.rompaths:
            if (p / f"{system}.zip").is_file() or (p / f"{system}.7z").is_file() or (p / system).is_dir():
                return p
        raise BridgeError("rom_not_found", f"no ROM set named '{system}' (.zip/.7z/folder) in the configured ROM paths",
                          detail={"searched": [str(p) for p in self.cfg.rompaths]})

    @staticmethod
    def _still_loading(info: dict[str, Any]) -> bool:
        """True when the reply explicitly lists an empty device set (MAME is up but has not finished starting)."""
        cpus = info.get("cpus")
        return isinstance(cpus, (list, dict)) and len(cpus) == 0

    def start(self, system: str, ready_timeout: float | None = None) -> dict[str, Any]:
        if not isinstance(system, str) or not SYSTEM_RE.match(system):
            raise BridgeError("bad_argument", "system must be a MAME short name: lowercase letters, digits, underscore (e.g. 'shinobi')")
        timeout = DEFAULT_READY_TIMEOUT if ready_timeout is None else ready_timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (MIN_READY_TIMEOUT <= timeout <= MAX_READY_TIMEOUT):
            raise BridgeError("bad_argument", f"ready_timeout_s must be {MIN_READY_TIMEOUT:g}..{MAX_READY_TIMEOUT:g}")
        if not self.cfg.mame_exe.is_file():
            raise BridgeError("mame_not_found", f"configured MAME executable does not exist: {self.cfg.mame_exe} (set MAMEMCP_MAME_DIR)")
        rompath = self._find_rompath(system)
        with self._cond:
            if self._shutting_down:
                raise BridgeError("shutting_down", "the MCP server is shutting down")
            self._refresh_locked()
            cur = self._current
            if cur is not None and cur.state in (STARTING, READY, STOPPING):
                raise BridgeError("session_active", f"a managed session is already {cur.state} (system {cur.system}, "
                                  f"session {cur.session_id or cur.run_id}); call stop_session first")
            if cur is not None:  # an exited/failed record is replaced; keep its summary
                self._last = cur.summary()
            run_id = f"{system}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
            run_dir = (self.cfg.runs_dir / run_id).resolve()
            s = Session(run_id=run_id, system=system, port=self._port_picker(), run_dir=run_dir, evidence_dir=run_dir / "evidence")
            self._current = s
        try:
            return self._start(s, rompath, float(timeout))
        except BridgeError as e:
            self._fail(s, e)
            raise self._with_failure(e, s) from e
        except Exception as e:  # never leave a half-started session behind
            err = BridgeError("start_failed", f"{type(e).__name__}: {e}")
            self._fail(s, err)
            raise self._with_failure(err, s) from e

    def _start(self, s: Session, rompath: Path, timeout: float) -> dict[str, Any]:
        try:  # production launch: debugger on, production plugin only, no fixture, no test_env, no extra arguments
            proc, info = self._launch(system=s.system, port=s.port, run_dir=s.run_dir, mame_dir=self.cfg.mame_dir,
                                      rompath=rompath, debug=True)
        except Exception as e:
            raise BridgeError("launch_failed", f"could not launch MAME: {type(e).__name__}: {e}") from e
        s.proc, s.pid, s.launch_info = proc, proc.pid, info
        s.created = info.get("pid_created_filetime")
        s.client = self._client_factory(s.port, s.pid, s.created)
        deadline = time.monotonic() + timeout
        while True:
            if self._shutting_down:
                raise BridgeError("shutting_down", "the MCP server is shutting down")
            rc = proc.poll()
            if rc is not None:
                s.exit_code = rc
                raise BridgeError("mame_exited_during_startup", f"MAME exited with code {rc} before the bridge became ready")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("startup_timeout", f"the bridge was not ready within {timeout:g} s")
            try:
                bridge_info = s.client.connect(timeout=min(3.0, remaining))
                break
            except BridgeError as e:
                if e.code != "connect_failed":
                    raise
        # The bridge can answer while MAME is still loading ROMs: that session_info lists no devices and the debugger is not
        # initialised yet, so judging it (debugger_unavailable) or sending requests then is wrong (a resume sent in that window
        # crashed MAME 0.289 in running_machine::resume). Wait, inside the same deadline, until devices are listed.
        while self._still_loading(bridge_info):
            if self._shutting_down:
                raise BridgeError("shutting_down", "the MCP server is shutting down")
            rc = proc.poll()
            if rc is not None:
                s.exit_code = rc
                raise BridgeError("mame_exited_during_startup", f"MAME exited with code {rc} before it finished starting")
            if time.monotonic() >= deadline:
                raise BridgeError("startup_timeout", f"MAME answered but had not finished starting (no devices listed) within {timeout:g} s")
            time.sleep(STARTUP_POLL_S)
            bridge_info = s.client.request("session_info", {}, retry_safe=True)
        machine = (bridge_info.get("machine") or {}).get("name")
        if machine != s.system:
            raise BridgeError("machine_mismatch", f"MAME reports machine '{machine}', expected '{s.system}'")
        if bridge_info.get("test_hooks"):
            raise BridgeError("test_hooks_active", f"test-only hooks are active in a production session: {bridge_info['test_hooks']}")
        if not (bridge_info.get("bridge") or {}).get("debugger_available"):
            raise BridgeError("debugger_unavailable", "MAME started without the debugger; captures would be refused")
        if self._ident.available() and s.pid not in self._ident.port_owners(s.port):
            raise BridgeError("bridge_owner_mismatch", f"pid {s.pid} does not own bridge port {s.port}; refusing to use it")
        with self._cond:
            if s.state != STARTING or self._shutting_down:
                raise BridgeError("shutting_down", "start was interrupted")
            s.state, s.ready_utc = READY, _utc()
            s.session_id = s.client.session_id
            s.mame_version = bridge_info.get("mame_version")
            self._cond.notify_all()
        m = bridge_info["machine"]
        return {
            "state": READY, "session_id": s.session_id, "run_id": s.run_id, "system": s.system,
            "machine": {k: m.get(k) for k in ("name", "description", "year", "manufacturer", "parent")},
            "execution": bridge_info.get("execution"), "mame_version": bridge_info.get("mame_version"),
            "pid": s.pid, "port": s.port, "run_dir": str(s.run_dir), "evidence_dir": str(s.evidence_dir),
            "launch_info": str(s.run_dir / "launch.json"),
            "romset": {"rompath": str(rompath), "zip_sha1": (info.get("romset_zip") or {}).get("sha1"),
                       "verifyroms": (info.get("verifyroms") or {}).get("output")},
            "ownership": "managed",
            "note": "MAME starts stopped in the debugger: use execution_control(action='resume') to run it.",
        }

    def _fail(self, s: Session, e: BridgeError) -> None:
        cleanup = None
        if s.proc is not None:
            cleanup = self._terminate(s, check_port=False)
        if s.client is not None:
            s.client.close(graceful=False)
        with self._cond:
            s.state, s.ended_utc = FAILED, _utc()
            if s.exit_code is None and s.proc is not None:
                s.exit_code = s.proc.poll()
            s.exit_reason = "startup_failed"
            s.failure = {"code": e.code, "message": e.message, "detail": e.detail, "exit_code": s.exit_code,
                         "stdout_tail": self._tail(s), "run_dir": str(s.run_dir), "evidence_dir": str(s.evidence_dir),
                         "cleanup": cleanup,
                         "note": "only the process this attempt launched was stopped; run_dir is kept for diagnostics"}
            self._cond.notify_all()

    @staticmethod
    def _with_failure(e: BridgeError, s: Session) -> BridgeError:
        return BridgeError(e.code, e.message, detail=s.failure, uncertain=e.uncertain)

    # ------------------------------------------------------------------ stop
    def stop(self, drain_timeout: float = DRAIN_WAIT) -> dict[str, Any]:
        with self._cond:
            self._refresh_locked()
            s = self._current
            if s is None:
                return {"state": NO_SESSION, "stopped": False, "message": "no managed session to stop", "last_session": self._last}
            if s.state == STARTING:
                raise BridgeError("session_starting", "the session is still starting; retry stop_session when it has finished")
            if s.state == STOPPING:
                raise BridgeError("session_stopping", "a stop is already in progress")
            prior = s.state
            s.state = STOPPING
            drained = self._cond.wait_for(lambda: self._inflight == 0, timeout=drain_timeout)
        report = self._teardown(s, prior, drained, cancel_timeout=10.0)
        with self._cond:
            s.ended_utc = _utc()
            s.exit_reason = s.exit_reason or "stopped_by_stop_session"
            s.state = EXITED
            s.stop_report = report
            self._last = s.summary()
            self._current = None
            self._cond.notify_all()
        return {"state": NO_SESSION, "stopped": True, "previous_state": prior, "session_id": s.session_id, "run_id": s.run_id,
                "exit_code": s.exit_code, "run_dir": str(s.run_dir), "evidence_dir": str(s.evidence_dir), **report}

    def _teardown(self, s: Session, prior: str, drained: bool, *, cancel_timeout: float) -> dict[str, Any]:
        failures: list[str] = []
        report: dict[str, Any] = {"drained_in_flight_calls": drained, "input_cleanup": None, "capture_cleanup": None,
                                  "process": None, "cleanup_failures": failures}
        if not drained:
            failures.append("in-flight bridge calls did not finish before the stop; continuing (they fail when MAME is stopped)")
        if prior == READY and s.client is not None and s.proc is not None and s.proc.poll() is None:
            inp = self._release_inputs(s.client, cancel_timeout)
            report["input_cleanup"] = inp
            if not inp.get("released"):
                failures.append("held inputs were not confirmed released; stopping MAME releases them regardless")
            cap = self._cancel_active_capture(s.client, cancel_timeout)
            report["capture_cleanup"] = cap
            if not cap.get("verified"):
                failures.append("capture cleanup was not verified; stopping MAME discards its breakpoints regardless")
        elif prior == READY:
            report["input_cleanup"] = report["capture_cleanup"] = {"skipped": "MAME was not running"}
        # Prove ownership BEFORE the bridge connection is closed. MAME's socket on the bridge port is the evidence of who owns the
        # port, and when the client closes while a plugin heartbeat byte is still unread, Windows resets the connection and that
        # socket disappears within about a millisecond; checking afterwards made stop_session refuse to stop its own MAME.
        verdict = None
        if s.proc is not None and s.proc.poll() is None:
            verdict = self._verify_owned(s, check_port=(prior == READY))
        if s.client is not None:
            s.client.close(graceful=False)
        if s.proc is not None:
            report["process"] = self._terminate(s, check_port=(prior == READY), preverified=verdict)
            if report["process"].get("refused"):
                failures.append("process was NOT stopped: " + report["process"]["refused"])
            elif report["process"].get("still_running"):
                failures.append("process did not exit after kill")
        return report

    @staticmethod
    def _cancel_active_capture(client: BridgeClient, timeout: float) -> dict[str, Any]:
        """Established cleanup policy: cancel_experiment removes only that experiment's breakpoints, verifies and
        finalises evidence, and never changes execution state."""
        out: dict[str, Any] = {"active_experiment": None, "verified": False}
        try:
            si = client.request("session_info", {}, timeout=min(timeout, 5.0), retry_safe=True)
            exp = si.get("active_experiment")
            out["active_experiment"] = exp
            if exp:
                r = client.request("cancel_experiment", {"experiment_id": exp}, timeout=timeout, mutating=True)
                cleanup = r.get("cleanup") or {}
                out.update(cancel_state=r.get("state"), completion_reason=r.get("completion_reason"), cleanup=cleanup,
                           verified=bool(cleanup.get("verified")))
            else:
                out["verified"] = True
            after = client.request("session_info", {}, timeout=min(timeout, 5.0), retry_safe=True)
            out["unresolved_instrumentation"] = after.get("unresolved_instrumentation")
            if out["unresolved_instrumentation"]:
                out["verified"] = False
        except BridgeError as e:
            out["error"] = e.to_dict()
            out["verified"] = False
        return out

    # ------------------------------------------------------------------ process control (owned process only)
    def _verify_owned(self, s: Session, *, check_port: bool) -> tuple[bool, str]:
        if s.proc is None or s.pid is None:
            return False, "no process"
        if not self._ident.available():
            return True, "ownership by Popen handle only (non-Windows)"
        img = self._ident.image(s.pid)
        if not img:
            return False, "process image unreadable (process gone or access denied)"
        if _norm(img) != _norm(self.cfg.mame_exe):
            return False, f"image '{img}' is not the configured MAME executable"
        if s.created is not None and self._ident.created(s.pid) != s.created:
            return False, "creation time differs from the recorded one (recycled pid)"
        if not check_port:
            return True, "image and creation time verified (bridge port not checked)"
        owners = self._ident.port_owners(s.port)
        if s.pid in owners:
            return True, "image, creation time and bridge port verified"
        if owners:  # another process holds the bridge port: this pid is not (or no longer) what the bridge talks to
            return False, f"pid does not own bridge port {s.port} (held by pid {sorted(owners)})"
        if s.created is None:
            return False, f"pid does not own bridge port {s.port} (nothing holds it, and no creation time was recorded to identify the process exactly)"
        # Nothing holds the port: the bridge connection was already reset and the plugin has not re-listened yet. The process is
        # still identified exactly (Popen handle, configured image, recorded creation time) and nothing else owns the port.
        return True, ("image and creation time verified; no process currently holds the bridge port (the connection was reset "
                      "and the plugin has not re-listened yet), and no other process owns it")

    def _terminate(self, s: Session, *, check_port: bool, preverified: tuple[bool, str] | None = None) -> dict[str, Any]:
        """Stop the owned process after verifying it is the one this session launched.

        `preverified` is the verdict of `_verify_owned` taken EARLIER, while the bridge connection was still open (the only time
        bridge-port ownership can be observed reliably); it is then re-checked here for what is still observable (image and
        creation time) so an identity change in between is caught. Termination goes through the Popen handle, which on Windows
        keeps the pid from being reused while it is open. A process that exits at any point before the terminate call is
        reported as exited (exited_before_termination), not as a refusal and not as something this server terminated."""
        proc = s.proc
        out: dict[str, Any] = {"pid": s.pid, "was_running": False, "terminated": False, "exit_code": None}

        def _exited() -> bool:
            rc_now = proc.poll()
            if rc_now is None:
                return False
            out["exit_code"] = s.exit_code = rc_now
            return True

        if _exited():
            if preverified is not None:  # it was alive when its identity was checked and has exited since: nothing was terminated by us
                out["was_running"], out["exited_before_termination"] = True, True
            return out
        out["was_running"] = True
        if preverified is None:
            ok, why = self._verify_owned(s, check_port=check_port)
        else:
            ok, why = preverified
            if ok:
                ok2, why2 = self._verify_owned(s, check_port=False)
                if not ok2:
                    ok, why = False, f"identity changed between verification and termination: {why2} (verified earlier: {why})"
        if not ok:
            if _exited():  # it went away while it was being verified (an unreadable image is then expected)
                out["exited_before_termination"] = True
                out["identity_check"] = f"not checked to completion: the process exited meanwhile ({why})"
                return out
            out["identity_check"] = why
            out["refused"] = why  # an unverified process is never terminated
            return out
        out["identity_check"] = why
        try:
            proc.terminate()
            try:
                proc.wait(TERMINATE_WAIT)
            except Exception:
                out["escalated_to_kill"] = True
                proc.kill()
                proc.wait(KILL_WAIT)
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
        rc = proc.poll()
        out["terminated"] = rc is not None
        out["still_running"] = rc is None
        out["exit_code"] = s.exit_code = rc
        return out

    # ------------------------------------------------------------------ server shutdown
    def shutdown(self) -> dict[str, Any] | None:
        """Stop the owned MAME with short, bounded waits. Idempotent. Cannot run if the server is force-killed."""
        with self._cond:
            if self._shutting_down and self._current is None:
                return None
            self._shutting_down = True
            s = self._current
            if s is None:
                return None
            prior = s.state
            if prior == READY:
                s.state = STOPPING
            self._cond.wait_for(lambda: self._inflight == 0, timeout=2.0)
        if s.proc is None:
            return None
        report = self._teardown(s, prior, True, cancel_timeout=2.0)
        with self._cond:
            s.state, s.ended_utc, s.exit_reason = EXITED, _utc(), "server_shutdown"
            s.stop_report = report
            self._current = None
            self._last = s.summary()
        return report
