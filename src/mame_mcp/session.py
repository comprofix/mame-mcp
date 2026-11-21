"""Managed MAME sessions for the MCP server: one server, several independent, server-owned MAME instances.

The MCP server starts with no MAME running. `start_session` launches one isolated MAME (through the existing
launcher, production plugin only, debugger enabled), connects a fresh BridgeClient to it and records the process
identity; `stop_session` finalises any capture and stops exactly that process. Everything the server owns is
reachable only through the Popen objects it created; no process is ever looked up by name.

Sessions (registry keyed by run id): every session owns its process and verified runtime identity, bridge client (port,
reconnect state), run/evidence directory, checkpoints and operation lock. Callers address a session by the `session_id`
returned from start_session. Omitting it selects the sole live session and is refused as ambiguous when there is more than
one; there is no mutable "current session". An unknown id, or the id of a session that has ended, never selects another one.
`MAMEMCP_PORT` (an externally attached MAME) is addressable as session_id "external" and is never started, stopped or
used by the managed-only operations.

States (``state`` of a session): starting, ready, stopping, exited, failed. starting/ready/stopping sessions are live and count
toward the session limit. ``exited`` (an UNEXPECTED MAME exit) and ``failed`` (a start that did not finish) stay visible until
stop_session acknowledges them or the next start_session; after that, or after a normal stop, a summary is kept in a short
history so a stale id is reported as ended rather than unknown.

Concurrency: ONE short lock (`_cond`) guards the registry and the lifecycle state and is never held across bridge I/O, process
launch, startup waits or shutdown waits (Condition waits release it). Per-session in-flight accounting lets stop wait for that
session's calls only; calls made during a lifecycle change are refused rather than queued. State-changing operations
(checkpoints, input presses/releases, RAM writes; operations.py) run one at a time per session under `exclusive()`; different
sessions never wait for each other.
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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from . import launcher, procinfo, winwake
from .bridge import BridgeClient
from .operations import StateOperations
from .protocol import BridgeError

NO_SESSION, STARTING, READY, STOPPING, EXITED, FAILED = "no_session", "starting", "ready", "stopping", "exited", "failed"
LIVE = (STARTING, READY, STOPPING)  # states that count toward the session limit and that omission may select
SYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")  # MAME short names; no path or option syntax can pass
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")
EXTERNAL_ID = "external"  # session_id of the optional externally attached MAME (MAMEMCP_PORT)
DEFAULT_READY_TIMEOUT = 60.0
STARTUP_POLL_S = 0.5  # while MAME answers but has not finished loading (no devices listed yet)
MIN_READY_TIMEOUT, MAX_READY_TIMEOUT = 5.0, 120.0
TERMINATE_WAIT, KILL_WAIT = 5.0, 3.0
DRAIN_WAIT = 20.0
DEFAULT_MAX_SESSIONS, MAX_SESSIONS_CEILING = 3, 16
PORT_PICK_ATTEMPTS = 20      # bound on picking a bridge port not reserved by another session
MAX_START_ATTEMPTS = 3       # bound on relaunching after a port collision (see COLLISION_CODES)
COLLISION_CODES = frozenset({"bridge_owner_mismatch", "duplicate_session_id"})
SHUTDOWN_BOUND_S = 20.0      # overall bound on the parallel teardown of every owned session at server shutdown
HISTORY_LIMIT = 16


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _same_file(a: str | Path, b: str | Path) -> bool:
    """Same path, literally or once symlinks are resolved (a Linux /proc image is always the resolved path)."""
    return _norm(a) == _norm(b) or _norm(os.path.realpath(a)) == _norm(os.path.realpath(b))


@dataclass
class SessionConfig:
    """Machine-local settings (from the MCP server's environment); never taken from tool arguments.

    The executable is `exe` when given, otherwise `mame_dir` + the platform's executable name when a folder is given,
    otherwise launcher.default_mame_exe() (Windows: the built-in folder; elsewhere: `mame` on PATH). MAME's own plugin
    folder (`plugins`) is separate from the executable's folder, because packaged Linux installations keep them apart."""
    mame_dir: Path | None = None
    rompaths: list[Path] = field(default_factory=list)
    runs_dir: Path = field(default_factory=lambda: launcher.PROJECT_ROOT / "runs" / "sessions")
    exe: Path | None = None
    plugins: Path | None = None
    runtime: Path | None = None  # MAMEMCP_MAME_RUNTIME: the real executable behind a wrapper-script launch command
    max_sessions: int = DEFAULT_MAX_SESSIONS  # MAMEMCP_MAX_SESSIONS: live (starting, ready, stopping) managed sessions at once
    config_error: str | None = None  # an invalid environment setting; start_session refuses to launch while it is set

    def __post_init__(self) -> None:
        if isinstance(self.max_sessions, bool) or not isinstance(self.max_sessions, int) or not 1 <= self.max_sessions <= MAX_SESSIONS_CEILING:
            raise ValueError(f"max_sessions must be an integer 1..{MAX_SESSIONS_CEILING}")
        if not self.rompaths:
            self.rompaths = launcher.default_rompaths(self.mame_exe)

    @property
    def mame_exe(self) -> Path:
        if self.exe is not None:
            return self.exe
        return self.mame_dir / launcher.MAME_EXE_NAME if self.mame_dir is not None else launcher.default_mame_exe()

    @property
    def mame_plugins(self) -> Path:
        return self.plugins or launcher.mame_plugins_dir(self.mame_exe)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SessionConfig":
        env = os.environ if env is None else env
        kw: dict[str, Any] = {}
        if env.get("MAMEMCP_MAME_EXE"):
            kw["exe"] = launcher.resolve_mame_exe(env["MAMEMCP_MAME_EXE"])
        if env.get("MAMEMCP_MAME_RUNTIME"):
            kw["runtime"] = Path(env["MAMEMCP_MAME_RUNTIME"])
        if env.get("MAMEMCP_MAME_DIR"):
            kw["mame_dir"] = Path(env["MAMEMCP_MAME_DIR"])
        if env.get("MAMEMCP_MAME_PLUGINS"):
            kw["plugins"] = Path(env["MAMEMCP_MAME_PLUGINS"])
        if env.get("MAMEMCP_ROMPATH"):
            kw["rompaths"] = [Path(x) for x in env["MAMEMCP_ROMPATH"].split(";") if x.strip()]
        if env.get("MAMEMCP_RUNS_DIR"):
            kw["runs_dir"] = Path(env["MAMEMCP_RUNS_DIR"])
        error = None
        raw = env.get("MAMEMCP_MAX_SESSIONS")
        if raw is not None and raw.strip():
            if re.fullmatch(r"[0-9]{1,3}", raw.strip()) and 1 <= int(raw.strip()) <= MAX_SESSIONS_CEILING:
                kw["max_sessions"] = int(raw.strip())
            else:  # never guess a limit: keep the default for display, refuse to launch until it is fixed
                error = f"MAMEMCP_MAX_SESSIONS must be an integer 1..{MAX_SESSIONS_CEILING}, got {raw!r}"
        cfg = cls(**kw)
        cfg.config_error = error
        return cfg

    def runtime_exe(self) -> Path:
        """The image the launched MAME process must have (see launcher.runtime_executable); raises RuntimeIdentityError."""
        return launcher.runtime_executable(self.mame_exe, self.runtime)

    def public(self) -> dict[str, Any]:
        return {"mame_exe": str(self.mame_exe), "mame_plugins": str(self.mame_plugins),
                "rompaths": [str(p) for p in self.rompaths], "runs_dir": str(self.runs_dir), "max_sessions": self.max_sessions,
                **({"config_error": self.config_error} if self.config_error else {})}


class ProcessIdentity:
    """Process facts (Windows or Linux, see procinfo) used to prove a pid is the MAME this server launched (replaceable in tests)."""

    def available(self) -> bool:
        return procinfo.available()

    def image(self, pid: int) -> str | None:
        return procinfo.process_image(pid)

    def created(self, pid: int) -> int | None:
        return procinfo.creation_time(pid)

    def port_owners(self, port: int) -> set[int]:
        return procinfo.port_owner_pids(port)


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
    runtime: Path | None = None  # the verified runtime image, established before launch
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
    inflight: int = 0  # bridge calls currently running on this session (guarded by SessionManager._cond)
    start_attempts: int = 0
    tried_ports: set[int] = field(default_factory=set)  # ports of discarded start attempts (never picked again for this session)
    op_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # one state-changing operation at a time, per session
    op_name: str | None = None

    def brief(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "run_id": self.run_id, "system": self.system, "state": self.state}

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
                 identity: ProcessIdentity | None = None, port_picker: Callable[[], int] = _free_port,
                 reserved_ports: frozenset[int] | set[int] = frozenset()):
        self.cfg = cfg
        self._launch, self._client_factory = launch_fn, client_factory
        self._ident = identity or ProcessIdentity()
        self._port_picker = port_picker
        self._extra_reserved = frozenset(reserved_ports)  # e.g. the externally attached MAME's bridge port
        self._cond = threading.Condition()  # guards everything below and Session.state/inflight; never held across I/O or waits
        self._sessions: dict[str, Session] = {}  # run_id -> session (live, plus exited/failed records not yet acknowledged)
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)  # summaries of ended sessions (stale-id reporting)
        self._shutting_down = False
        self._init_operations()

    # ------------------------------------------------------------------ registry (all *_locked helpers need self._cond)
    def _live_locked(self) -> list[Session]:
        return [s for s in self._sessions.values() if s.state in LIVE]

    def _reserved_ports_locked(self) -> set[int]:
        return {s.port for s in self._live_locked()} | set(self._extra_reserved)

    def _find_locked(self, sid: str) -> Session | None:
        for s in self._sessions.values():
            if sid == s.session_id or sid == s.run_id:
                return s
        return None

    def _ended_locked(self, sid: str) -> dict[str, Any] | None:
        for h in reversed(self._history):
            if sid == h.get("session_id") or sid == h.get("run_id"):
                return h
        return None

    def _prune_locked(self) -> None:
        """Exited/failed records move to the history (a new start replaces them, as before); live sessions are untouched."""
        for rid in [r for r, s in self._sessions.items() if s.state in (EXITED, FAILED)]:
            self._history.append(self._sessions.pop(rid).summary())

    def _ambiguous(self, candidates: list[Session]) -> BridgeError:
        return BridgeError(
            "session_ambiguous",
            f"{len(candidates)} sessions are present and no session_id was given; nothing was done. Pass the session_id of one of them "
            "(list_sessions shows them).", detail={"sessions": [s.brief() for s in candidates]})

    def _select_locked(self, selector: "str | Session | None", *, external_ok: bool, ended_ok: bool = False) -> "Session | dict | str | None":
        """Resolve which session a call is for. Returns the Session, EXTERNAL_ID (external attachment), a history summary (dict,
        only with ended_ok) or None (no managed session; the caller decides between external attachment and no_session).
        Explicit ids are never substituted: an unknown or ended id is an error. Omission selects the sole live session only."""
        if isinstance(selector, Session):
            return selector
        if selector is not None:
            if not isinstance(selector, str) or not SESSION_ID_RE.fullmatch(selector):
                raise BridgeError("bad_argument", "session_id must be a session id returned by start_session / list_sessions")
            if selector == EXTERNAL_ID:
                if not external_ok:
                    raise BridgeError("external_not_supported", "this operation exists only for sessions this server started; the externally "
                                      "attached MAME is never used for it")
                return EXTERNAL_ID
            s = self._find_locked(selector)
            if s is not None:
                return s
            h = self._ended_locked(selector)
            if h is not None:
                if ended_ok:
                    return h
                raise BridgeError("session_ended", f"session {selector} ended ({h.get('state')}, {h.get('exit_reason')}); it is not selected "
                                  "and no other session was used instead. list_sessions shows the live sessions.",
                                  detail={"session": {k: h.get(k) for k in ("session_id", "run_id", "system", "state", "exit_reason", "exit_code")}})
            raise BridgeError("unknown_session", f"no session {selector!r}; list_sessions shows the live sessions")
        live = self._live_locked()
        if len(live) == 1:
            return live[0]
        if len(live) > 1:
            raise self._ambiguous(live)
        ended = list(self._sessions.values())  # exited/failed records: reported (not skipped) so the cause stays visible
        if len(ended) == 1:
            return ended[0]
        if len(ended) > 1:
            raise self._ambiguous(ended)
        return None

    # ------------------------------------------------------------------ introspection
    def snapshot(self, session_id: "str | Session | None" = None, settle: float = 0.0) -> dict[str, Any]:
        """State of one session without touching MAME. Always works (no session, starting, ready, exited, ...). With several
        sessions the id is required (session_ambiguous otherwise). `settle` > 0: after a disconnect, give a READY process up
        to that many seconds to be reaped, because Popen.poll() can briefly still say "running" right after the process died."""
        if settle > 0:
            with self._cond:
                s = self._select_locked(session_id, external_ok=False, ended_ok=True)
                proc = s.proc if isinstance(s, Session) and s.state == READY else None
            if proc is not None:
                try:
                    proc.wait(settle)
                except subprocess.TimeoutExpired:
                    pass
        with self._cond:
            self._refresh_locked()
            s = self._select_locked(session_id, external_ok=False, ended_ok=True)
            last = self._history[-1] if self._history else None
            if isinstance(s, dict):  # an explicit id of a session that ended: its own summary, never another session's
                return {**s, "config": self.cfg.public()}
            if s is None:
                d: dict[str, Any] = {"state": NO_SESSION, "ownership": "none", "config": self.cfg.public()}
            else:
                d = {**s.summary(), "config": self.cfg.public()}
            if last is not None:
                d["last_session"] = last
            return d

    def list_sessions(self) -> dict[str, Any]:
        with self._cond:
            self._refresh_locked()
            sessions = [s.summary() for s in self._sessions.values()]
            return {"sessions": sessions, "max_sessions": self.cfg.max_sessions, "live_count": len(self._live_locked()),
                    "recent_ended": list(self._history)[-5:]}

    def launch_info(self, session_id: "str | Session | None" = None) -> dict[str, Any] | None:
        with self._cond:
            s = self._select_locked(session_id, external_ok=False)
            return s.launch_info if isinstance(s, Session) else None

    def _refresh_locked(self) -> None:
        for s in self._sessions.values():
            if s.state == READY and s.proc is not None:
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
    def use_session(self, external: BridgeClient | None = None, session_id: "str | Session | None" = None,
                    *, managed_only: bool = False) -> Iterator[tuple[BridgeClient, bool, Session | None]]:
        """Yield (client, managed, session) for one bridge call on the explicitly selected session (or the sole live one), or
        raise a coded BridgeError. A managed call counts as in-flight for ITS session only, so stop_session of that session can
        wait for it and nothing else is affected; calls during starting/stopping are refused. The externally attached MAME
        is used only when asked for explicitly ("external") or when no managed session exists at all."""
        with self._cond:
            self._refresh_locked()
            s = self._select_locked(session_id, external_ok=not managed_only)
            if s is EXTERNAL_ID or s is None:
                if managed_only or external is None:
                    if s is EXTERNAL_ID:
                        raise BridgeError("unknown_session", "no external attachment is configured (MAMEMCP_PORT)")
                    raise BridgeError("no_session", "no MAME session is active; call start_session(system=...) first")
                client, managed, s = external, False, None
            else:
                assert isinstance(s, Session)
                if s.state == READY and not self._shutting_down:
                    client, managed = s.client, True
                    s.inflight += 1
                elif s.state == STARTING:
                    raise BridgeError("session_starting", f"session {s.run_id} is still starting; wait for start_session to finish")
                elif s.state == STOPPING or self._shutting_down:
                    raise BridgeError("session_stopping", "the session is being stopped; nothing was sent")
                elif s.state == EXITED:
                    raise BridgeError("session_exited", f"the managed MAME exited ({s.exit_reason}, code {s.exit_code}); "
                                      "call stop_session to acknowledge or start_session to begin a new one",
                                      detail={"exit_code": s.exit_code, "evidence_dir": str(s.evidence_dir), "session_id": s.session_id})
                else:
                    raise BridgeError("session_failed", "the last start_session failed; see session_info.failure, "
                                      "then call start_session again", detail=s.failure)
        try:
            yield client, managed, s
        finally:
            if managed and s is not None:
                with self._cond:
                    s.inflight -= 1
                    self._cond.notify_all()

    @contextlib.contextmanager
    def use_client(self, external: BridgeClient | None = None, session_id: "str | Session | None" = None) -> Iterator[tuple[BridgeClient, bool]]:
        with self.use_session(external, session_id) as (client, managed, _s):
            yield client, managed

    @contextlib.contextmanager
    def use_managed(self, session_id: "str | Session | None" = None) -> Iterator[tuple[BridgeClient, Session]]:
        """Like use_client for the operations that exist only for a session this server owns (checkpoints, input
        presses, RAM writes): an external attachment is never used, so it has nothing to clean up after."""
        with self.use_session(None, session_id, managed_only=True) as (client, _managed, s):
            assert s is not None
            yield client, s

    def resolve_managed(self, session_id: "str | Session | None" = None) -> Session:
        """Pick the target session ONCE for a multi-step operation; later steps pass the returned object, never the omitted id."""
        with self._cond:
            self._refresh_locked()
            s = self._select_locked(session_id, external_ok=False)
        if not isinstance(s, Session):
            raise BridgeError("no_session", "no MAME session is active; call start_session(system=...) first")
        return s

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

    def _register(self, system: str, runtime: Path) -> Session:
        """Atomically enforce the session limit, reserve a bridge port no other session holds and add the session to the registry.
        Nothing is launched here, so an excess start is refused before any process exists."""
        picked: set[int] = set()
        for _ in range(PORT_PICK_ATTEMPTS):
            port = self._port_picker()  # may be taken by a concurrent start: decided under the lock below
            with self._cond:
                if self._shutting_down:
                    raise BridgeError("shutting_down", "the MCP server is shutting down")
                self._refresh_locked()
                self._prune_locked()
                live = self._live_locked()
                if len(live) >= self.cfg.max_sessions:
                    raise BridgeError("session_limit_reached", f"{len(live)} sessions are already live and the limit is {self.cfg.max_sessions} "
                                      "(MAMEMCP_MAX_SESSIONS); nothing was launched. Stop a session first.",
                                      detail={"max_sessions": self.cfg.max_sessions, "sessions": [s.brief() for s in live]})
                if port in self._reserved_ports_locked() or port in picked:
                    picked.add(port)
                    continue
                while True:
                    run_id = f"{system}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
                    run_dir = (self.cfg.runs_dir / run_id).resolve()
                    if run_id not in self._sessions and not run_dir.exists():
                        break
                s = Session(run_id=run_id, system=system, port=port, run_dir=run_dir, evidence_dir=run_dir / "evidence")
                s.runtime = runtime
                self._sessions[run_id] = s
                return s
        raise BridgeError("port_unavailable", f"no bridge port free of other sessions after {PORT_PICK_ATTEMPTS} attempts; nothing was launched")

    def _repick_port(self, s: Session) -> None:
        s.tried_ports.add(s.port)
        for _ in range(PORT_PICK_ATTEMPTS):
            port = self._port_picker()
            with self._cond:
                if self._shutting_down:
                    raise BridgeError("shutting_down", "the MCP server is shutting down")
                if port in s.tried_ports or port in {o.port for o in self._live_locked() if o is not s} or port in self._extra_reserved:
                    continue
                s.port = port
                return
        raise BridgeError("port_unavailable", f"no free bridge port after {PORT_PICK_ATTEMPTS} attempts")

    def start(self, system: str, ready_timeout: float | None = None) -> dict[str, Any]:
        if not isinstance(system, str) or not SYSTEM_RE.match(system):
            raise BridgeError("bad_argument", "system must be a MAME short name: lowercase letters, digits, underscore (e.g. 'shinobi')")
        timeout = DEFAULT_READY_TIMEOUT if ready_timeout is None else ready_timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (MIN_READY_TIMEOUT <= timeout <= MAX_READY_TIMEOUT):
            raise BridgeError("bad_argument", f"ready_timeout_s must be {MIN_READY_TIMEOUT:g}..{MAX_READY_TIMEOUT:g}")
        if self.cfg.config_error:
            raise BridgeError("bad_configuration", f"{self.cfg.config_error}; nothing was launched")
        if not self.cfg.mame_exe.is_file():
            raise BridgeError("mame_not_found", f"configured MAME executable does not exist: {self.cfg.mame_exe} "
                              "(set MAMEMCP_MAME_EXE or MAMEMCP_MAME_DIR, or put mame on PATH)")
        try:
            runtime = self.cfg.runtime_exe()  # before anything is launched; refusal leaves no process behind
        except launcher.RuntimeIdentityError as e:
            raise BridgeError("runtime_unverified", str(e)) from e
        rompath = self._find_rompath(system)
        s = self._register(system, runtime)
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
        """Bounded startup: a port collision (another process answering on the chosen port, or a duplicate bridge session id) is
        retried on a fresh port, at most MAX_START_ATTEMPTS times and within the one ready deadline. Another process is never
        attached to: every attempt proves pid, image, creation time and bridge-port ownership before the session becomes ready."""
        deadline = time.monotonic() + timeout
        for attempt in range(1, MAX_START_ATTEMPTS + 1):
            s.start_attempts = attempt
            try:
                return self._start_attempt(s, rompath, timeout, deadline)
            except BridgeError as e:
                if e.code not in COLLISION_CODES or attempt == MAX_START_ATTEMPTS or deadline - time.monotonic() < 2.0:
                    raise
                undone = self._discard_attempt(s)
                if undone.get("refused") or undone.get("still_running"):
                    raise  # the attempt's process could not be proven ours / stopped: never launch another beside it
                self._repick_port(s)
        raise AssertionError("unreachable")  # pragma: no cover

    def _discard_attempt(self, s: Session) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if s.proc is not None:
            out = self._terminate(s, check_port=False)
        if s.client is not None:
            s.client.close(graceful=False)
        if not (out.get("refused") or out.get("still_running")):
            s.proc, s.pid, s.created, s.client, s.launch_info, s.exit_code = None, None, None, None, None, None
        return out

    def _start_attempt(self, s: Session, rompath: Path, timeout: float, deadline: float) -> dict[str, Any]:
        try:  # production launch: debugger on, production plugin only, no fixture, no test_env, no extra arguments
            proc, info = self._launch(system=s.system, port=s.port, run_dir=s.run_dir, mame_exe=self.cfg.mame_exe,
                                      plugins_dir=self.cfg.mame_plugins, rompath=rompath, debug=True)
        except Exception as e:
            raise BridgeError("launch_failed", f"could not launch MAME: {type(e).__name__}: {e}") from e
        s.proc, s.pid, s.launch_info = proc, proc.pid, info
        s.created = info.get("pid_created_filetime")
        s.client = self._client_factory(s.port, s.pid, s.created)
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
        if self._ident.available():  # ready only when the process is the pre-established runtime image, same instance, owning the port
            ok, why = self._verify_owned(s, check_port=True)
            if not ok:
                raise BridgeError("process_identity_mismatch", f"the launched process is not the expected MAME runtime: {why}")
        with self._cond:
            if s.state != STARTING or self._shutting_down:
                raise BridgeError("shutting_down", "start was interrupted")
            sid = s.client.session_id
            if not sid or any(o is not s and o.session_id == sid for o in self._sessions.values()):
                # the id is the routing key: two sessions can never share it, and a bridge that answers with another live
                # session's id is not ours
                raise BridgeError("duplicate_session_id", f"bridge session id {sid!r} is empty or already belongs to another session")
            s.state, s.ready_utc = READY, _utc()
            s.session_id = sid
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
            "ownership": "managed", "start_attempts": s.start_attempts,
            "note": "MAME starts stopped in the debugger: use execution_control(action='resume', session_id=...) to run it. "
                    "Pass this session_id to every session-bound tool when more than one session is live.",
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
                         "cleanup": cleanup, "start_attempts": s.start_attempts,
                         "note": "only the process this attempt launched was stopped; run_dir is kept for diagnostics"}
            self._cond.notify_all()

    @staticmethod
    def _with_failure(e: BridgeError, s: Session) -> BridgeError:
        return BridgeError(e.code, e.message, detail=s.failure, uncertain=e.uncertain)

    # ------------------------------------------------------------------ stop
    def stop(self, session_id: "str | Session | None" = None, drain_timeout: float = DRAIN_WAIT) -> dict[str, Any]:
        """Stop exactly the explicitly resolved session (or the sole live one). Other sessions are not touched, and nothing is
        held while this waits for the session's own in-flight calls or for its process to exit."""
        with self._cond:
            self._refresh_locked()
            s = self._select_locked(session_id, external_ok=False, ended_ok=True)
            if s is None:
                last = self._history[-1] if self._history else None
                return {"state": NO_SESSION, "stopped": False, "message": "no managed session to stop", "last_session": last}
            if isinstance(s, dict):
                return {"state": s.get("state"), "stopped": False, "session_id": s.get("session_id"), "run_id": s.get("run_id"),
                        "message": "that session had already ended; nothing to stop", "last_session": s}
            if s.state == STARTING:
                raise BridgeError("session_starting", "the session is still starting; retry stop_session when it has finished")
            if s.state == STOPPING:
                raise BridgeError("session_stopping", "a stop is already in progress")
            prior = s.state
            s.state = STOPPING
            drained = self._cond.wait_for(lambda: s.inflight == 0, timeout=drain_timeout)
        report = self._teardown(s, prior, drained, cancel_timeout=10.0)
        with self._cond:
            s.ended_utc = _utc()
            s.exit_reason = s.exit_reason or "stopped_by_stop_session"
            s.state = EXITED
            s.stop_report = report
            self._history.append(s.summary())
            self._sessions.pop(s.run_id, None)
            remaining = [o.brief() for o in self._live_locked()]
            self._cond.notify_all()
        return {"state": "stopped", "stopped": True, "previous_state": prior, "session_id": s.session_id, "run_id": s.run_id,
                "exit_code": s.exit_code, "run_dir": str(s.run_dir), "evidence_dir": str(s.evidence_dir),
                "remaining_sessions": remaining, **report}

    def _teardown(self, s: Session, prior: str, drained: bool, *, cancel_timeout: float) -> dict[str, Any]:
        failures: list[str] = []
        report: dict[str, Any] = {"drained_in_flight_calls": drained, "input_cleanup": None, "capture_cleanup": None,
                                  "process": None, "cleanup_failures": failures}
        if not drained:
            failures.append("in-flight bridge calls did not finish before the stop; continuing (they fail when MAME is stopped)")
        if prior == READY and s.client is not None and s.proc is not None and s.proc.poll() is None:
            try:  # an unexpected error in the polite cleanup must never prevent stopping the process (or any other session's cleanup)
                inp = self._release_inputs(s.client, cancel_timeout)
                report["input_cleanup"] = inp
                if not inp.get("released"):
                    failures.append("held inputs were not confirmed released; stopping MAME releases them regardless")
                cap = self._cancel_active_capture(s.client, cancel_timeout)
                report["capture_cleanup"] = cap
                if not cap.get("verified"):
                    failures.append("capture cleanup was not verified; stopping MAME discards its breakpoints regardless")
            except Exception as e:  # noqa: BLE001
                failures.append(f"input/capture cleanup raised {type(e).__name__}: {e}; continuing to stop the process")
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
            return True, "ownership by Popen handle only (no process identity probes on this platform)"
        img = self._ident.image(s.pid)
        if not img:
            return False, "process image unreadable (process gone or access denied)"
        if s.runtime is None or not _same_file(img, s.runtime):
            return False, f"image '{img}' is not the expected MAME runtime executable '{s.runtime}'"
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
        """Stop every owned MAME with short, bounded waits, in parallel and independently: one session's cleanup failing,
        hanging or refusing never prevents the others from being cleaned up. Idempotent. Cannot run if the server is force-killed."""
        with self._cond:
            if self._shutting_down and not self._sessions:
                return None
            self._shutting_down = True
            todo: list[tuple[Session, str]] = []
            for s in self._sessions.values():
                prior = s.state
                if prior == READY:
                    s.state = STOPPING
                todo.append((s, prior))
            if not todo:
                return None
            self._cond.wait_for(lambda: all(s.inflight == 0 for s, _ in todo), timeout=2.0)
        reports: dict[str, dict[str, Any]] = {}

        def one(s: Session, prior: str) -> None:
            try:
                reports[s.run_id] = self._teardown(s, prior, True, cancel_timeout=2.0) if s.proc is not None else {"skipped": "no process was launched yet"}
            except Exception as e:  # noqa: BLE001 - isolation: never let one session's cleanup stop the others
                reports[s.run_id] = {"cleanup_failures": [f"teardown raised {type(e).__name__}: {e}"], "error": f"{type(e).__name__}: {e}"}

        threads = [threading.Thread(target=one, args=(s, prior), daemon=True, name=f"shutdown-{s.run_id}") for s, prior in todo]
        for t in threads:
            t.start()
        end = time.monotonic() + SHUTDOWN_BOUND_S
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        failures: list[str] = []
        with self._cond:
            for s, _prior in todo:
                rep = reports.get(s.run_id)
                if rep is None:
                    rep = reports[s.run_id] = {"cleanup_failures": [f"cleanup did not finish within {SHUTDOWN_BOUND_S:g} s"], "timed_out": True}
                s.state, s.ended_utc, s.exit_reason = EXITED, _utc(), "server_shutdown"
                s.stop_report = rep
                self._history.append(s.summary())
                self._sessions.pop(s.run_id, None)
                failures += [f"{s.session_id or s.run_id}: {f}" for f in rep.get("cleanup_failures") or []]
            self._cond.notify_all()
        return {"sessions": reports, "cleanup_failures": failures}
