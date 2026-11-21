"""Unit tests for the managed-session lifecycle and ownership logic (fake process, fake bridge client, fake
process table). They prove the decision logic only; scripts/mcp_session_e2e.py exercises real MAME."""
import itertools
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mame_mcp import launcher, session
from mame_mcp.protocol import BridgeError
from mame_mcp.session import EXITED, FAILED, NO_SESSION, READY, SessionConfig, SessionManager


class FakeProc:
    _next_pid = 4000

    def __init__(self, exit_on_terminate=True):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.rc = None
        self.exit_on_terminate = exit_on_terminate
        self.calls = []

    def poll(self):
        return self.rc

    def terminate(self):
        self.calls.append("terminate")
        if self.exit_on_terminate:
            self.rc = 1

    def kill(self):
        self.calls.append("kill")
        self.rc = 1

    def wait(self, timeout=None):
        if self.rc is None:
            raise subprocess.TimeoutExpired("mame", timeout)
        return self.rc


class FakeClient:
    def __init__(self, machine="shinobi", test_hooks=None, debugger=True, connect_errors=0, active="exp-s1-001",
                 cancel_verified=True, cancel_error=None, loading_polls=None, sid="s1-aaaaaa"):
        self.sid = sid
        self.loading_polls = loading_polls  # None: ready at once; N: the first connect + N-1 session_info replies list no devices / no debugger yet
        self.machine, self.test_hooks, self.debugger = machine, test_hooks or [], debugger
        self.connect_errors, self.active = connect_errors, active
        self.cancel_verified, self.cancel_error = cancel_verified, cancel_error
        self.session_id = None
        self.connected = False
        self.log = []

    def connect(self, timeout=15.0):
        if self.connect_errors:
            self.connect_errors -= 1
            time.sleep(0.01)
            raise BridgeError("connect_failed", "nobody listening")
        self.session_id, self.connected = self.sid, True
        return self._info()

    def _info(self):
        loading = self.loading_polls is not None and self.loading_polls > 0
        if loading:
            self.loading_polls -= 1
        return {"cpus": [] if loading else [{"tag": ":maincpu"}], **self._base_info(loading)}

    def _base_info(self, loading):
        return {"machine": {"name": self.machine, "description": "d", "year": "1987", "manufacturer": "Sega"},
                "execution": {"state": "debugger_stopped"}, "mame_version": "0.289", "test_hooks": self.test_hooks,
                "bridge": {"session_id": self.session_id, "debugger_available": False if loading else self.debugger}}

    def request(self, op, args=None, *, timeout=None, mutating=False, retry_safe=False):
        self.log.append(op)
        if op == "session_info" and self.loading_polls is not None:
            return {**self._info(), "active_experiment": None, "unresolved_instrumentation": []}
        if op == "session_info":
            return {"active_experiment": self.active if "cancel_experiment" not in self.log else None,
                    "unresolved_instrumentation": []}
        if op == "cancel_experiment":
            if self.cancel_error:
                raise self.cancel_error
            return {"state": "cancelled", "completion_reason": "cancelled", "cleanup": {"verified": self.cancel_verified}}
        if op == "input_cancel":  # teardown always releases held inputs first; nothing is held in these tests
            return {"state": "none", "was_holding": False, "release_errors": []}
        if op == "shutdown":  # the clean-exit request: accepted, but the fake process stays alive unless a test ends it
            return {"accepted": True}
        raise AssertionError(op)

    def close(self, graceful=True):
        self.connected = False
        self.log.append("close")


class FakeIdent(session.ProcessIdentity):
    def __init__(self):
        self.images, self.created_map, self.owners = {}, {}, {}

    def available(self):
        return True

    def image(self, pid):
        return self.images.get(pid)

    def created(self, pid):
        return self.created_map.get(pid)

    def port_owners(self, port):
        return self.owners.get(port, set())


class Rig:
    client_cls = FakeClient

    def __init__(self, tmp_path, max_sessions=3, unique_ids=False, **client_kw):
        (tmp_path / "roms").mkdir(exist_ok=True)
        (tmp_path / "roms" / "shinobi.zip").write_bytes(b"x")
        (tmp_path / "mame").mkdir(exist_ok=True)
        (tmp_path / "mame" / launcher.MAME_EXE_NAME).write_bytes(b"stub")  # presence check only; nothing is executed
        self.cfg = SessionConfig(mame_dir=tmp_path / "mame", rompaths=[tmp_path / "roms"], runs_dir=tmp_path / "runs",
                                 plugins=tmp_path / "mame" / "plugins", max_sessions=max_sessions)
        self.unique_ids, self._ids, self._ports = unique_ids, itertools.count(1), itertools.count(5555)
        self._launch_lock = threading.Lock()  # concurrent starts (multi-session tests) share these lists and the fake pid counter
        self.ident, self.launches, self.procs, self.clients = FakeIdent(), [], [], []
        self.proc_kw, self.client_kw, self.rt = {}, client_kw, None
        self.mgr = SessionManager(self.cfg, launch_fn=self._launch, client_factory=self._client, identity=self.ident,
                                  port_picker=lambda: next(self._ports))

    def _launch(self, **kw):
        with self._launch_lock:
            self.launches.append(kw)
            p = FakeProc(**self.proc_kw)
            self.procs.append(p)
            self.ident.images[p.pid] = str(self.rt or self.cfg.mame_exe)  # a wrapper rig reports its runtime, as /proc does after the exec
            self.ident.created_map[p.pid] = 777
            self.ident.owners[kw["port"]] = {p.pid}
        return p, {"pid_created_filetime": 777, "romset_zip": {"sha1": "abc"}, "verifyroms": {"output": "romset shinobi is good"}}

    def _client(self, port, pid, created):
        kw = dict(self.client_kw)
        if self.unique_ids:  # distinct bridge session ids, as real MAME processes have
            n = next(self._ids)
            kw["sid"] = f"s{1700000000 + n}-{n:06x}"
        c = self.client_cls(**kw)
        self.clients.append(c)
        return c


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(session, "MIN_READY_TIMEOUT", 0.05)
    monkeypatch.setattr(session, "EXIT_WAIT", 0.05)  # a fake process never exits on the clean-exit request unless a test makes it


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------------------------------------- arguments
@pytest.mark.parametrize("bad", ["", "Shinobi", "..\\x", "shinobi -debug", "a/b", "shinobi;calc", "x" * 40, "-video", None, 5])
def test_system_argument_is_validated_before_anything_happens(rig, bad):
    with pytest.raises(BridgeError) as e:
        rig.mgr.start(bad)
    assert e.value.code == "bad_argument"
    assert rig.launches == [] and rig.mgr.snapshot()["state"] == NO_SESSION


@pytest.mark.parametrize("bad", [0, 0.01, 500, True, "60"])
def test_ready_timeout_is_bounded(rig, bad):
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi", ready_timeout=bad)
    assert e.value.code == "bad_argument" and rig.launches == []


def test_missing_rom_set_and_missing_mame_are_reported(tmp_path, rig):
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("pacman")
    assert e.value.code == "rom_not_found" and str(tmp_path / "roms") in e.value.detail["searched"][0]
    rig.cfg.mame_dir = tmp_path / "nowhere"  # exe check is real again for this path
    with pytest.raises(BridgeError) as e2:
        rig.mgr.start("shinobi")
    assert e2.value.code == "mame_not_found"


# ---------------------------------------------------------------------------------------------- start
def test_start_launches_production_session_and_reports_identity(rig, tmp_path):
    r = rig.mgr.start("shinobi")
    assert r["state"] == READY and r["session_id"] == "s1-aaaaaa" and r["machine"]["name"] == "shinobi"
    assert r["execution"]["state"] == "debugger_stopped" and r["port"] == 5555
    assert Path(r["run_dir"]).parent == (tmp_path / "runs").resolve() and r["evidence_dir"].endswith("evidence")
    kw = rig.launches[0]
    # production only: no fixture plugin, no test env, no extra MAME arguments, debugger on, only fixed paths
    assert set(kw) == {"system", "port", "run_dir", "mame_exe", "plugins_dir", "rompath", "debug"}
    assert kw["debug"] is True and kw["system"] == "shinobi" and kw["mame_exe"] == rig.cfg.mame_exe
    assert kw["plugins_dir"] == rig.cfg.mame_plugins
    snap = rig.mgr.snapshot()
    assert snap["state"] == READY and snap["ownership"] == "managed" and snap["session_id"] == "s1-aaaaaa"


def test_second_start_beyond_the_limit_is_rejected_and_launches_nothing(tmp_path):
    rig = Rig(tmp_path, max_sessions=1)
    rig.mgr.start("shinobi")
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "session_limit_reached" and len(rig.launches) == 1 and rig.procs[0].calls == []


def test_start_stop_start_in_one_manager(rig):
    first = rig.mgr.start("shinobi")
    rig.mgr.stop()
    second = rig.mgr.start("shinobi")
    assert second["run_id"] != first["run_id"] and len(rig.launches) == 2
    assert rig.mgr.snapshot()["last_session"]["run_id"] == first["run_id"]


def test_process_exit_during_startup_is_failed_with_diagnostics(rig, tmp_path):
    rig.client_kw["connect_errors"] = 10**6
    orig = rig._launch

    def dying(**kw):
        p, info = orig(**kw)
        p.rc = 3
        Path(kw["run_dir"]).mkdir(parents=True, exist_ok=True)
        (Path(kw["run_dir"]) / "mame_stdout.txt").write_text("missing ROM: foo.bin")
        return p, info

    rig.mgr._launch = dying
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "mame_exited_during_startup"
    assert e.value.detail["exit_code"] == 3 and "missing ROM" in e.value.detail["stdout_tail"]
    snap = rig.mgr.snapshot()
    assert snap["state"] == FAILED and snap["failure"]["code"] == "mame_exited_during_startup"
    assert rig.procs[0].calls == []  # already dead: nothing to terminate
    assert rig.clients[0].log.count("close") >= 1


def test_startup_timeout_stops_only_the_launched_process(rig):
    rig.client_kw["connect_errors"] = 10**6
    other = FakeProc()  # an unrelated process: must never be touched
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi", ready_timeout=0.2)
    assert e.value.code == "startup_timeout"
    assert rig.procs[0].calls == ["terminate"] and other.calls == []
    assert e.value.detail["cleanup"]["terminated"] is True
    assert rig.mgr.snapshot()["state"] == FAILED


@pytest.mark.parametrize("kw,code", [({"machine": "1942"}, "machine_mismatch"),
                                     ({"test_hooks": ["MAMEMCP_TEST_FAIL_CLEAR=1"]}, "test_hooks_active"),
                                     ({"debugger": False}, "debugger_unavailable")])
def test_post_connect_checks_refuse_and_clean_up(tmp_path, kw, code):
    rig = Rig(tmp_path, **kw)
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == code and rig.procs[0].calls == ["terminate"]


def test_bridge_port_not_owned_by_launched_pid_is_refused(rig):
    orig = rig._launch

    def squatter(**kw):
        p, info = orig(**kw)
        rig.ident.owners[kw["port"]] = {99999}  # someone else answers on our port
        return p, info

    rig.mgr._launch = squatter
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "bridge_owner_mismatch" and rig.mgr.snapshot()["state"] == FAILED


def test_failed_start_does_not_block_a_new_start(rig):
    rig.client_kw["connect_errors"] = 10**6
    with pytest.raises(BridgeError):
        rig.mgr.start("shinobi", ready_timeout=0.1)
    rig.client_kw["connect_errors"] = 0
    assert rig.mgr.start("shinobi")["state"] == READY
    assert rig.mgr.snapshot()["last_session"]["state"] == FAILED


def test_launch_exception_is_reported_and_state_failed(rig):
    def boom(**kw):
        raise OSError("mame.exe: access denied")

    rig.mgr._launch = boom
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "launch_failed" and rig.mgr.snapshot()["state"] == FAILED


# ---------------------------------------------------------------------------------------------- stop / ownership
def test_stop_cancels_capture_then_stops_owned_process(rig):
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    log = rig.clients[0].log
    assert log.index("cancel_experiment") < log.index("close")
    assert r["capture_cleanup"]["verified"] is True and r["capture_cleanup"]["active_experiment"] == "exp-s1-001"
    assert r["process"]["terminated"] is True and r["exit_code"] == 1 and r["cleanup_failures"] == []
    assert rig.procs[0].calls == ["terminate"]
    snap = rig.mgr.snapshot()
    assert snap["state"] == NO_SESSION and snap["last_session"]["state"] == EXITED


def test_stop_without_capture_and_without_session(rig):
    assert rig.mgr.stop()["stopped"] is False
    rig.client_kw["active"] = None
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    assert r["capture_cleanup"]["active_experiment"] is None and "cancel_experiment" not in rig.clients[0].log


def test_capture_cleanup_failure_is_reported_but_process_still_stopped(tmp_path):
    rig = Rig(tmp_path, cancel_verified=False)
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    assert r["capture_cleanup"]["verified"] is False and any("capture cleanup" in f for f in r["cleanup_failures"])
    assert r["process"]["terminated"] is True


def test_capture_cancel_error_is_reported(tmp_path):
    rig = Rig(tmp_path, cancel_error=BridgeError("request_timeout", "slow", uncertain=True))
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    assert r["capture_cleanup"]["error"]["code"] == "request_timeout" and r["process"]["terminated"] is True


@pytest.mark.parametrize("mutate,why", [
    (lambda rig, p: rig.ident.images.__setitem__(p.pid, "C:/Windows/notepad.exe"), "not the expected MAME runtime"),
    (lambda rig, p: rig.ident.created_map.__setitem__(p.pid, 1), "recycled pid"),
    (lambda rig, p: rig.ident.owners.__setitem__(5555, {1}), "bridge port"),
    (lambda rig, p: rig.ident.images.pop(p.pid), "unreadable"),
])
def test_unverified_process_is_never_terminated(rig, mutate, why):
    rig.mgr.start("shinobi")
    p = rig.procs[0]
    mutate(rig, p)
    r = rig.mgr.stop()
    assert p.calls == [] and why in r["process"]["refused"]
    assert any("NOT stopped" in f for f in r["cleanup_failures"])


def test_unresponsive_process_escalates_to_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TERMINATE_WAIT", 0.05)
    rig = Rig(tmp_path)
    rig.proc_kw["exit_on_terminate"] = False
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    assert rig.procs[0].calls == ["terminate", "kill"] and r["process"]["escalated_to_kill"] is True


# ---------------------------------------------------------------------------------------------- unexpected exit
def test_unexpected_exit_is_detected_and_blocks_calls_until_acknowledged(rig):
    rig.mgr.start("shinobi")
    rig.procs[0].rc = -1073741819
    snap = rig.mgr.snapshot()
    assert snap["state"] == EXITED and snap["exit_code"] == -1073741819 and snap["exit_reason"] == "unexpected_exit"
    with pytest.raises(BridgeError) as e:
        with rig.mgr.use_client(None):
            pass
    assert e.value.code == "session_exited"
    r = rig.mgr.stop()  # acknowledges and clears; must not try to kill an already dead process
    assert r["stopped"] is True and rig.procs[0].calls == [] and rig.mgr.snapshot()["state"] == NO_SESSION


def test_new_start_replaces_an_exited_session(rig):
    rig.mgr.start("shinobi")
    rig.procs[0].rc = 1
    again = rig.mgr.start("shinobi")
    assert again["state"] == READY and rig.mgr.snapshot()["last_session"]["state"] == EXITED


# ---------------------------------------------------------------------------------------------- routing / serialisation
def test_no_session_without_external_attachment(rig):
    with pytest.raises(BridgeError) as e:
        with rig.mgr.use_client(None):
            pass
    assert e.value.code == "no_session"


def test_external_client_is_used_only_when_no_managed_session(rig):
    ext = object()
    with rig.mgr.use_client(ext) as (c, managed):
        assert c is ext and managed is False
    rig.mgr.start("shinobi")
    with rig.mgr.use_client(ext) as (c, managed):
        assert c is rig.clients[0] and managed is True
    rig.mgr.stop()
    with rig.mgr.use_client(ext) as (c, managed):
        assert c is ext


def test_stop_waits_for_in_flight_call_and_refuses_new_ones(rig):
    rig.mgr.start("shinobi")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def long_call():
        with rig.mgr.use_client(None):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=long_call)
    t.start()
    assert entered.wait(2)
    result = {}

    def stopper():
        result["r"] = rig.mgr.stop()
        done.set()

    s = threading.Thread(target=stopper)
    s.start()
    time.sleep(0.2)
    assert not done.is_set()  # blocked on the in-flight call
    with pytest.raises(BridgeError) as e:  # new calls during the stop are refused, not queued
        with rig.mgr.use_client(None):
            pass
    assert e.value.code == "session_stopping"
    with pytest.raises(BridgeError) as e2:  # and a second stop cannot interleave
        rig.mgr.stop()
    assert e2.value.code == "session_stopping"
    release.set()
    assert done.wait(3)
    t.join()
    s.join()
    assert result["r"]["drained_in_flight_calls"] is True and rig.mgr.snapshot()["state"] == NO_SESSION


def test_stop_continues_after_drain_timeout_and_says_so(rig):
    rig.mgr.start("shinobi")
    hold = threading.Event()
    def holder():
        with rig.mgr.use_client(None):
            hold.wait(3)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    time.sleep(0.1)
    r = rig.mgr.stop(drain_timeout=0.2)
    hold.set()
    assert r["drained_in_flight_calls"] is False and r["process"]["terminated"] is True
    assert any("in-flight" in f for f in r["cleanup_failures"])


def test_concurrent_stop_is_rejected(rig):
    rig.mgr.start("shinobi")
    next(iter(rig.mgr._sessions.values())).state = session.STOPPING
    with pytest.raises(BridgeError) as e:
        rig.mgr.stop()
    assert e.value.code == "session_stopping"


# ---------------------------------------------------------------------------------------------- shutdown
def test_shutdown_stops_owned_process_once_and_blocks_new_starts(rig):
    rig.mgr.start("shinobi")
    run_id = rig.mgr.snapshot()["run_id"]
    rep = rig.mgr.shutdown()["sessions"][run_id]
    assert rep["process"]["terminated"] is True and rig.procs[0].calls == ["terminate"]
    assert rig.mgr.shutdown() is None
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "shutting_down"


def test_shutdown_with_no_session_is_a_noop(rig):
    assert rig.mgr.shutdown() is None


# ---------------------------------------------------------------------------------------------- configuration
def test_config_from_env_and_defaults(tmp_path, monkeypatch):
    c = SessionConfig.from_env({"MAMEMCP_MAME_DIR": "D:/x/mame", "MAMEMCP_ROMPATH": "D:/a;D:/b;", "MAMEMCP_RUNS_DIR": str(tmp_path)})
    assert c.mame_exe == Path("D:/x/mame") / launcher.MAME_EXE_NAME and c.rompaths == [Path("D:/a"), Path("D:/b")] and c.runs_dir == tmp_path
    monkeypatch.setattr(launcher, "default_mame_exe", lambda: tmp_path / "nowhere" / launcher.MAME_EXE_NAME)
    d = SessionConfig.from_env({})
    assert d.mame_exe == tmp_path / "nowhere" / launcher.MAME_EXE_NAME
    assert d.rompaths == [tmp_path / "nowhere" / "roms"] and d.runs_dir.name == "sessions"


def test_config_executable_from_env_takes_precedence_and_bare_names_use_path(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "mame"
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(exe) if name == "mame" else None)
    c = SessionConfig.from_env({"MAMEMCP_MAME_EXE": "mame", "MAMEMCP_MAME_DIR": "D:/x/mame", "MAMEMCP_ROMPATH": "/r",
                                "MAMEMCP_MAME_PLUGINS": "/usr/share/games/mame/plugins"})
    assert c.mame_exe == exe.resolve() and c.mame_plugins == Path("/usr/share/games/mame/plugins")
    assert c.public()["mame_plugins"] == str(Path("/usr/share/games/mame/plugins"))
    assert SessionConfig.from_env({"MAMEMCP_MAME_EXE": str(tmp_path / "m"), "MAMEMCP_ROMPATH": "/r"}).mame_exe == tmp_path / "m"


def test_resource_folders_come_from_next_to_the_executable_without_asking_mame(tmp_path, monkeypatch):
    """The Windows layout: plugins and roms next to the executable; MAME's configuration is never consulted."""
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "boot.lua").write_text("")
    (tmp_path / "roms").mkdir()
    exe = tmp_path / launcher.MAME_EXE_NAME
    exe.write_bytes(b"stub")
    monkeypatch.setattr(launcher, "_SHOWCONFIG", {})
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: pytest.fail("MAME was run"))
    assert launcher.mame_plugins_dir(exe) == tmp_path / "plugins" and launcher.default_rompaths(exe) == [tmp_path / "roms"]


def test_packaged_layout_takes_resource_folders_from_mames_own_configuration(tmp_path, monkeypatch):
    """A packaged Linux MAME: /usr/games/mame with plugins and roms elsewhere, found through -showconfig."""
    exe = tmp_path / "games" / "mame"
    exe.parent.mkdir()
    exe.write_bytes(b"stub")
    share = tmp_path / "share" / "plugins"
    share.mkdir(parents=True)
    (share / "boot.lua").write_text("")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen = []

    class R:
        stdout = (f"homepath                  .\nrompath                   $HOME/mame/roms;{tmp_path}/share/roms\n"
                  f"pluginspath               {tmp_path}/empty;{share}\n")

    def run(cmd, **kw):
        seen.append(cmd)
        return R()

    monkeypatch.setattr(launcher, "_SHOWCONFIG", {})
    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.mame_plugins_dir(exe) == share
    assert launcher.default_rompaths(exe) == [tmp_path / "home" / "mame" / "roms", tmp_path / "share" / "roms"]
    assert seen == [[str(exe), "-showconfig"]]  # read once per executable
    cmd = launcher.build_command(mame_exe=exe, system="shinobi", rompath=tmp_path, run_dir=tmp_path / "run", port=1,
                                 plugins_dir=share)
    assert cmd[cmd.index("-pluginspath") + 1] == f"{launcher._fwd(launcher.PLUGIN_DIR)};{launcher._fwd(share)}"


def test_ownership_accepts_a_configured_executable_reached_through_a_symlink(tmp_path):
    real = tmp_path / "real" / "mame"
    real.parent.mkdir()
    real.write_bytes(b"stub")
    link = tmp_path / "mame-link"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable")
    r = Rig(tmp_path)
    r.cfg.exe = link
    r.mgr.start("shinobi")
    r.ident.images[r.procs[0].pid] = str(real)  # what /proc/<pid>/exe reports: the resolved image
    rep = r.mgr.stop()
    assert rep["process"]["terminated"] is True and "refused" not in rep["process"]


# ---------------------------------------------------------------------------------------------- wrapper / runtime identity
ELF_STUB = b"\x7fELF" + b"\0" * 12
# Wrapper scripts and ELF runtimes are a POSIX contract: launcher.runtime_executable deliberately returns the launch executable itself
# on Windows (a .exe is never a `#!` script, and MAMEMCP_MAME_RUNTIME does not apply). The Windows side is asserted separately below.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="wrapper-script / ELF runtime contract; Windows runs the launch executable itself")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime-identity contract")


def _wrapper_rig(tmp_path, *, runtime="configured"):
    """A launch command that is a `#!` wrapper script and a separate ELF-looking runtime (stub files; nothing is executed)."""
    r = Rig(tmp_path)
    wrapper = tmp_path / "bin" / "mame"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\nexec /somewhere/else \"$@\"\n")
    rt = tmp_path / "lib" / "mame-real"
    rt.parent.mkdir()
    rt.write_bytes(ELF_STUB)
    r.cfg.exe = wrapper
    if runtime == "configured":
        r.cfg.runtime = rt
    r.rt = rt
    return r


@posix_only
def test_wrapper_launch_with_configured_runtime_is_owned_by_the_runtime_image_only(tmp_path):
    r = _wrapper_rig(tmp_path)
    r.mgr.start("shinobi")
    pid = r.procs[0].pid
    assert r.launches[0]["mame_exe"] == r.cfg.exe  # launched through the wrapper, which keeps its own setup behaviour
    rep = r.mgr.stop()
    assert rep["process"]["terminated"] is True and "refused" not in rep["process"]


@posix_only
def test_wrapper_image_is_not_accepted_as_the_runtime(tmp_path):
    r = _wrapper_rig(tmp_path)
    r.mgr.start("shinobi")
    r.ident.images[r.procs[0].pid] = str(r.cfg.exe)  # still the wrapper: not the established runtime
    rep = r.mgr.stop()
    assert rep["process"]["terminated"] is False and "not the expected MAME runtime" in rep["process"]["refused"]


def test_unrelated_executable_is_refused_at_readiness_and_the_launched_process_is_stopped(tmp_path):
    r = _wrapper_rig(tmp_path)
    r.ident.image = lambda pid: "/bin/sh"
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi")
    assert e.value.code == "process_identity_mismatch"


def test_unrelated_port_owner_is_refused_at_readiness(tmp_path):
    r = Rig(tmp_path)
    orig = r._launch

    def launch(**kw):
        p, info = orig(**kw)
        r.ident.owners[kw["port"]] = {p.pid + 1000}
        return p, info
    r.mgr._launch = launch
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi")
    assert e.value.code == "bridge_owner_mismatch"


def test_creation_time_mismatch_is_refused_at_readiness(tmp_path):
    r = Rig(tmp_path)
    orig = r._launch

    def launch(**kw):
        p, info = orig(**kw)
        r.ident.created_map[p.pid] = 1
        return p, info
    r.mgr._launch = launch
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi")
    assert e.value.code == "process_identity_mismatch"


@posix_only
def test_wrapper_without_an_established_runtime_is_refused_before_anything_is_launched(tmp_path):
    r = _wrapper_rig(tmp_path, runtime=None)
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi")
    assert e.value.code == "runtime_unverified" and "MAMEMCP_MAME_RUNTIME" in e.value.message and r.launches == []


@posix_only
def test_configured_runtime_must_be_an_elf_file_and_cannot_redirect_a_plain_executable(tmp_path):
    r = _wrapper_rig(tmp_path)
    (tmp_path / "lib" / "notelf").write_bytes(b"hello")
    with pytest.raises(launcher.RuntimeIdentityError):
        launcher.runtime_executable(r.cfg.exe, tmp_path / "lib" / "notelf")
    with pytest.raises(launcher.RuntimeIdentityError):
        launcher.runtime_executable(r.rt, tmp_path / "bin" / "mame")  # r.rt is not a script: a different runtime is a mismatch


def test_a_plain_executable_is_its_own_runtime(tmp_path):
    """Portable half of the former combined test: a non-script launch executable is its own (resolved) runtime everywhere."""
    exe = tmp_path / "bin" / launcher.MAME_EXE_NAME
    exe.parent.mkdir()
    exe.write_bytes(ELF_STUB)
    assert launcher.runtime_executable(exe) == exe.resolve()


@windows_only
def test_windows_runtime_is_the_launch_executable_whatever_is_configured(tmp_path):
    """No wrapper concept on Windows: even a `#!`-looking file is the runtime itself, and MAMEMCP_MAME_RUNTIME is not consulted."""
    exe = tmp_path / "mame.exe"
    exe.write_text("#!/bin/sh\n")
    other = tmp_path / "other.exe"
    other.write_bytes(b"MZ")
    assert launcher.runtime_executable(exe) == exe.resolve()
    assert launcher.runtime_executable(exe, other) == exe.resolve()


@windows_only
def test_windows_ownership_expects_the_launch_executable_even_with_a_configured_runtime(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r = _wrapper_rig(tmp_path / "a")  # a configured runtime that would be authoritative on Linux
    r.rt = None  # the process image is the launch executable, as winwake reports it
    r.mgr.start("shinobi")
    rep = r.mgr.stop()
    assert rep["process"]["terminated"] is True and "refused" not in rep["process"]
    r2 = _wrapper_rig(tmp_path / "b")  # the process image is the configured runtime: not accepted on Windows
    with pytest.raises(BridgeError) as e:
        r2.mgr.start("shinobi")
    assert e.value.code == "process_identity_mismatch" and "not the expected MAME runtime" in e.value.message


def test_runtime_from_env_and_symlinked_wrapper(tmp_path):
    c = SessionConfig.from_env({"MAMEMCP_MAME_RUNTIME": "/usr/lib/mame/mame", "MAMEMCP_MAME_EXE": str(tmp_path / "m")})
    assert c.runtime == Path("/usr/lib/mame/mame")


def test_debian_discovery_requires_the_same_dpkg_package_and_root_protection(tmp_path, monkeypatch):
    wrapper = tmp_path / "usr-games" / "mame"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\n")
    cand = Path("/usr/lib/mame/mame")
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher, "_root_protected", lambda p: True)
    monkeypatch.setattr(Path, "is_file", lambda self: True if self == cand else Path.stat(self) is not None)
    monkeypatch.setattr(launcher, "_is_elf", lambda p: True)
    monkeypatch.setattr(launcher.os.path, "realpath", lambda p: str(p))
    monkeypatch.setattr(launcher, "_dpkg_packages", lambda *p: {"mame"})
    assert launcher.runtime_executable(wrapper) == cand
    monkeypatch.setattr(launcher, "_dpkg_packages", lambda *p: {"mame"} if p[0] == wrapper else {"other"})
    with pytest.raises(launcher.RuntimeIdentityError):
        launcher.runtime_executable(wrapper)
    monkeypatch.setattr(launcher, "_dpkg_packages", lambda *p: None)
    with pytest.raises(launcher.RuntimeIdentityError):
        launcher.runtime_executable(wrapper)
    monkeypatch.setattr(launcher, "_dpkg_packages", lambda *p: {"mame"})
    monkeypatch.setattr(launcher, "_root_protected", lambda p: p != cand)  # a runtime others can write is not trusted
    with pytest.raises(launcher.RuntimeIdentityError):
        launcher.runtime_executable(wrapper)


# ---------------------------------------------------------------------------------------------- launcher hygiene
def test_launcher_never_passes_test_hooks_or_fixture_into_production(monkeypatch, tmp_path):
    seen = {}

    class P:
        pid = 1

    def popen(cmd, **kw):
        seen["cmd"], seen["env"], seen["stdin"], seen["kw"] = cmd, kw["env"], kw.get("stdin"), kw
        return P()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "mame_version", lambda e: "0.289")
    monkeypatch.setattr(launcher, "verify_roms", lambda *a: {"returncode": 0, "output": "good"})
    monkeypatch.setattr(launcher, "listxml_roms", lambda *a: {})
    monkeypatch.setenv("MAMEMCP_TEST_FAIL_CLEAR", "1")
    monkeypatch.setenv("MAMEMCP_FIXTURE_DIR", "C:/evil")
    _, info = launcher.launch(system="shinobi", port=5555, run_dir=tmp_path / "run", mame_dir=tmp_path, rompath=tmp_path)
    assert not any(k.startswith("MAMEMCP_TEST_") for k in seen["env"]) and "MAMEMCP_FIXTURE_DIR" not in seen["env"]
    assert "mamemcp_fixture" not in " ".join(seen["cmd"]) and "-debug" in seen["cmd"]
    assert seen["env"]["MAMEMCP_PORT"] == "5555" and seen["stdin"] == subprocess.DEVNULL
    assert info["test_env"] == {}
    # POSIX keeps the inherited SIG_IGN for SIGPIPE/SIGXFSZ (see the regression test below); Windows passes nothing extra
    assert ("restore_signals" in seen["kw"]) is (sys.platform != "win32")
    assert seen["kw"].get("restore_signals", True) is (sys.platform == "win32")


# A stand-in for MAME (launched exactly like it, through launcher.launch): it reports the SIGPIPE disposition it inherited
# and then writes 1 MB into a pipe whose reader has already exited. It changes no signal handling itself, so it keeps whatever
# disposition it was given, which is what a MAME build that does not ignore SIGPIPE does.
_CLOSED_PIPE_WRITER = """#!/bin/sh
grep -E '^SigIgn' /proc/self/status
exec 3>&1
{ head -c 1048576 /dev/zero; echo "writer_status=$?" >&3; } | true
"""


def _sigpipe_ignored(sigign_line: str) -> bool:
    return bool(int(sigign_line.split(":")[1].strip(), 16) & (1 << 12))  # SIGPIPE = 13 -> bit 12


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX signal inheritance, read from /proc")
def test_launched_child_survives_a_closed_pipe_write_with_a_write_error(monkeypatch, tmp_path):
    writer = tmp_path / "fake-mame"
    writer.write_text(_CLOSED_PIPE_WRITER)
    writer.chmod(0o755)
    import signal
    assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN  # the launching Python ignores SIGPIPE (CPython startup)

    # control: the default Popen policy hands the same child SIGPIPE at SIG_DFL, and the write kills the writer (128 + 13)
    ctl = subprocess.run([str(writer)], capture_output=True, text=True, timeout=30)
    ctl_sig = next(l for l in ctl.stdout.splitlines() if l.startswith("SigIgn"))
    assert not _sigpipe_ignored(ctl_sig) and "writer_status=141" in ctl.stdout, ctl.stdout + ctl.stderr

    # corrected launch policy: launcher.launch with a real Popen
    monkeypatch.setattr(launcher, "mame_version", lambda e: "0.289")
    monkeypatch.setattr(launcher, "verify_roms", lambda *a: {"returncode": 0, "output": "good"})
    monkeypatch.setattr(launcher, "listxml_roms", lambda *a: {})
    proc, info = launcher.launch(system="shinobi", port=5555, run_dir=tmp_path / "run", mame_exe=writer,
                                 plugins_dir=tmp_path, rompath=tmp_path)
    assert proc.wait(timeout=30) == 0
    out = (Path(info["run_dir"]) / "mame_stdout.txt").read_text()
    sig = next(l for l in out.splitlines() if l.startswith("SigIgn"))
    assert _sigpipe_ignored(sig), out
    assert "writer_status=1" in out.splitlines() and "Broken pipe" in out, out  # EPIPE write error, process still running


# ------------------------------------------------------------------------------------------------ startup readiness
def test_start_waits_while_mame_answers_but_has_not_finished_loading(tmp_path, monkeypatch):
    """The bridge can answer during ROM loading with no devices and no debugger yet; that must not be judged (debugger_unavailable)
    and no request other than session_info may be sent before the machine has finished starting."""
    monkeypatch.setattr(session, "STARTUP_POLL_S", 0.01)
    r = Rig(tmp_path, loading_polls=4)
    info = r.mgr.start("shinobi", ready_timeout=5)
    assert info["state"] == "ready"
    log = r.clients[0].log
    assert log.count("session_info") >= 3 and set(log) == {"session_info"}
    assert r.procs[0].rc is None  # the process was never touched


def test_start_times_out_if_mame_never_finishes_loading_and_stops_only_its_process(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "STARTUP_POLL_S", 0.01)
    r = Rig(tmp_path, loading_polls=10_000)
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi", ready_timeout=0.3)
    assert e.value.code == "startup_timeout" and "no devices" in e.value.message
    assert r.procs[0].rc is not None and r.mgr.snapshot()["state"] in ("failed", "no_session")


def test_start_reports_a_real_missing_debugger_only_once_devices_are_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "STARTUP_POLL_S", 0.01)
    r = Rig(tmp_path, loading_polls=2, debugger=False)
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi", ready_timeout=5)
    assert e.value.code == "debugger_unavailable" and r.clients[0].log.count("session_info") >= 2


def test_start_exits_the_wait_if_mame_dies_while_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "STARTUP_POLL_S", 0.01)
    r = Rig(tmp_path, loading_polls=10_000)
    r.proc_kw = {}
    orig = r._launch

    def launch(**kw):
        p, info = orig(**kw)
        threading.Timer(0.1, lambda: setattr(p, "rc", 3)).start()
        return p, info

    r.mgr._launch = launch
    with pytest.raises(BridgeError) as e:
        r.mgr.start("shinobi", ready_timeout=5)
    assert e.value.code == "mame_exited_during_startup"


# ---------------------------------------------------------------------------------------------- shutdown ordering and identity races
class ResetOnCloseClient(FakeClient):
    """Models what was measured on Windows with real MAME 0.289: when the client closes while a plugin heartbeat byte is still
    unread, the connection is RESET and MAME's socket on the bridge port disappears within about a millisecond. The fake process
    table therefore loses the port owner the instant the client closes."""

    def __init__(self, ident, port, **kw):
        super().__init__(**kw)
        self.ident, self.port = ident, port
        self.owned_when_asked = []

    def close(self, graceful=True):
        self.ident.owners.pop(self.port, None)
        super().close(graceful)


class ResetRig(Rig):
    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        orig = self.ident.port_owners
        self.ident.port_owners = lambda port: (self.checks.append(bool(self.clients and self.clients[-1].connected)), orig(port))[1]
        self.checks = []  # for each port-ownership lookup: was the bridge connection still open?

    def _client(self, port, pid, created):
        c = ResetOnCloseClient(self.ident, port, **self.client_kw)
        self.clients.append(c)
        return c


def test_stop_proves_ownership_while_the_bridge_is_still_open_and_survives_the_reset(tmp_path):
    rig = ResetRig(tmp_path)
    rig.mgr.start("shinobi")
    rig.checks.clear()
    r = rig.mgr.stop()
    assert r["process"]["terminated"] is True and "refused" not in r["process"] and r["cleanup_failures"] == []
    assert r["process"]["identity_check"] == "image, creation time and bridge port verified"
    assert rig.checks == [True]  # the one stop-time lookup happened before the client closed
    assert rig.clients[0].log[-1] == "close" and rig.procs[0].calls == ["terminate"]


def test_server_shutdown_uses_the_same_ordering(tmp_path):
    rig = ResetRig(tmp_path)
    rig.mgr.start("shinobi")
    rig.checks.clear()
    rep = next(iter(rig.mgr.shutdown()["sessions"].values()))
    assert rep["process"]["terminated"] is True and rep["cleanup_failures"] == []
    assert rep["process"]["identity_check"] == "image, creation time and bridge port verified" and rig.checks == [True]


def test_another_process_holding_the_port_still_refuses_with_the_holder_named(rig):
    rig.mgr.start("shinobi")
    rig.ident.owners[5555] = {1234}
    r = rig.mgr.stop()
    assert rig.procs[0].calls == [] and "held by pid [1234]" in r["process"]["refused"]


def test_already_reset_connection_falls_back_on_exact_identity_and_says_so(rig):
    rig.mgr.start("shinobi")
    rig.ident.owners.pop(5555)  # the connection was reset earlier and nobody holds the port now
    r = rig.mgr.stop()
    assert r["process"]["terminated"] is True and "no process currently holds the bridge port" in r["process"]["identity_check"]


def test_no_owner_and_no_recorded_creation_time_is_refused(rig):
    orig = rig._launch
    rig.mgr._launch = lambda **kw: (lambda p_i: (p_i[0], {**p_i[1], "pid_created_filetime": None}))(orig(**kw))
    rig.mgr.start("shinobi")
    rig.ident.owners.pop(5555)
    r = rig.mgr.stop()
    assert rig.procs[0].calls == [] and "no creation time was recorded" in r["process"]["refused"]


def test_process_exiting_while_it_is_verified_is_reported_as_exited_not_refused(rig):
    rig.mgr.start("shinobi")
    p = rig.procs[0]

    def vanishing_image(pid):
        p.rc = 3  # exits between poll() and the image lookup
        return None

    rig.ident.image = vanishing_image
    r = rig.mgr.stop()
    assert r["process"].get("exited_before_termination") is True and r["process"]["was_running"] is True
    assert "refused" not in r["process"] and r["process"]["exit_code"] == 3
    assert p.calls == [] and not any("NOT stopped" in f for f in r["cleanup_failures"]) and r["stopped"] is True


def test_identity_change_between_verification_and_termination_is_refused(rig):
    rig.mgr.start("shinobi")
    asked = []

    def created(pid):
        asked.append(pid)
        return 777 if len(asked) == 1 else 999  # the second look (just before terminate) sees a different process

    rig.ident.created = created
    r = rig.mgr.stop()
    assert rig.procs[0].calls == [] and "identity changed between verification and termination" in r["process"]["refused"]
    assert any("NOT stopped" in f for f in r["cleanup_failures"])


def test_process_exit_between_verification_and_the_terminate_call_is_handled(tmp_path):
    class ExitsBeforeTerminate(FakeProc):
        def terminate(self):
            self.calls.append("terminate")
            self.rc = 0
            raise PermissionError("access denied: the process had already exited")

    rig = Rig(tmp_path)
    rig.mgr._launch = lambda **kw: (lambda p_i: (p_i[0], p_i[1]))(_launch_with(rig, ExitsBeforeTerminate, kw))
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    assert r["stopped"] is True and r["process"]["terminated"] is True and r["process"]["exit_code"] == 0 and not r["process"].get("still_running")


def _launch_with(rig, proc_cls, kw):
    rig.launches.append(kw)
    p = proc_cls()
    rig.procs.append(p)
    rig.ident.images[p.pid] = str(rig.cfg.mame_exe)
    rig.ident.created_map[p.pid] = 777
    rig.ident.owners[kw["port"]] = {p.pid}
    return p, {"pid_created_filetime": 777, "romset_zip": {"sha1": "abc"}, "verifyroms": {"output": "romset shinobi is good"}}


def test_input_cleanup_failure_is_reported_and_the_process_is_still_stopped(tmp_path):
    class InputTimeout(ResetOnCloseClient):
        def request(self, op, *a, **kw):
            if op == "input_cancel":
                raise BridgeError("request_timeout", "slow", uncertain=True)
            return super().request(op, *a, **kw)

    rig = ResetRig(tmp_path)
    rig.mgr._client_factory = lambda port, pid, created: (rig.clients.append(InputTimeout(rig.ident, port)), rig.clients[-1])[1]
    rig.mgr.start("shinobi")
    r = rig.mgr.stop()
    assert r["input_cleanup"]["released"] is False and r["input_cleanup"]["error"]["code"] == "request_timeout"
    assert any("held inputs were not confirmed released" in f for f in r["cleanup_failures"])
    assert r["process"]["terminated"] is True and r["process"]["identity_check"] == "image, creation time and bridge port verified"
