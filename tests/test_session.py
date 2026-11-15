"""Unit tests for the managed-session lifecycle and ownership logic (fake process, fake bridge client, fake
process table). They prove the decision logic only; scripts/mcp_session_e2e.py exercises real MAME."""
import subprocess
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
                 cancel_verified=True, cancel_error=None):
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
        self.session_id, self.connected = "s1-aaaaaa", True
        return {"machine": {"name": self.machine, "description": "d", "year": "1987", "manufacturer": "Sega"},
                "execution": {"state": "debugger_stopped"}, "mame_version": "0.289", "test_hooks": self.test_hooks,
                "bridge": {"session_id": self.session_id, "debugger_available": self.debugger}}

    def request(self, op, args=None, *, timeout=None, mutating=False, retry_safe=False):
        self.log.append(op)
        if op == "session_info":
            return {"active_experiment": self.active if "cancel_experiment" not in self.log else None,
                    "unresolved_instrumentation": []}
        if op == "cancel_experiment":
            if self.cancel_error:
                raise self.cancel_error
            return {"state": "cancelled", "completion_reason": "cancelled", "cleanup": {"verified": self.cancel_verified}}
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
    def __init__(self, tmp_path, **client_kw):
        (tmp_path / "roms").mkdir(exist_ok=True)
        (tmp_path / "roms" / "shinobi.zip").write_bytes(b"x")
        (tmp_path / "mame").mkdir(exist_ok=True)
        (tmp_path / "mame" / "mame.exe").write_bytes(b"stub")  # presence check only; nothing is executed
        self.cfg = SessionConfig(mame_dir=tmp_path / "mame", rompaths=[tmp_path / "roms"], runs_dir=tmp_path / "runs")
        self.ident, self.launches, self.procs, self.clients = FakeIdent(), [], [], []
        self.proc_kw, self.client_kw = {}, client_kw
        self.mgr = SessionManager(self.cfg, launch_fn=self._launch, client_factory=self._client, identity=self.ident,
                                  port_picker=lambda: 5555)

    def _launch(self, **kw):
        self.launches.append(kw)
        p = FakeProc(**self.proc_kw)
        self.procs.append(p)
        self.ident.images[p.pid] = str(self.cfg.mame_exe)
        self.ident.created_map[p.pid] = 777
        self.ident.owners[kw["port"]] = {p.pid}
        return p, {"pid_created_filetime": 777, "romset_zip": {"sha1": "abc"}, "verifyroms": {"output": "romset shinobi is good"}}

    def _client(self, port, pid, created):
        c = FakeClient(**self.client_kw)
        self.clients.append(c)
        return c


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(session, "MIN_READY_TIMEOUT", 0.05)


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
    assert set(kw) == {"system", "port", "run_dir", "mame_dir", "rompath", "debug"}
    assert kw["debug"] is True and kw["system"] == "shinobi" and kw["mame_dir"] == rig.cfg.mame_dir
    snap = rig.mgr.snapshot()
    assert snap["state"] == READY and snap["ownership"] == "managed" and snap["session_id"] == "s1-aaaaaa"


def test_second_start_is_rejected_while_active_and_launches_nothing(rig):
    rig.mgr.start("shinobi")
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "session_active" and len(rig.launches) == 1 and rig.procs[0].calls == []


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
    (lambda rig, p: rig.ident.images.__setitem__(p.pid, "C:/Windows/notepad.exe"), "not the configured MAME"),
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
    with pytest.raises(BridgeError) as e2:  # and a second start/stop cannot interleave
        rig.mgr.start("shinobi")
    assert e2.value.code == "session_active"
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
    rig.mgr._current.state = session.STOPPING
    with pytest.raises(BridgeError) as e:
        rig.mgr.stop()
    assert e.value.code == "session_stopping"


# ---------------------------------------------------------------------------------------------- shutdown
def test_shutdown_stops_owned_process_once_and_blocks_new_starts(rig):
    rig.mgr.start("shinobi")
    rep = rig.mgr.shutdown()
    assert rep["process"]["terminated"] is True and rig.procs[0].calls == ["terminate"]
    assert rig.mgr.shutdown() is None
    with pytest.raises(BridgeError) as e:
        rig.mgr.start("shinobi")
    assert e.value.code == "shutting_down"


def test_shutdown_with_no_session_is_a_noop(rig):
    assert rig.mgr.shutdown() is None


# ---------------------------------------------------------------------------------------------- configuration
def test_config_from_env_and_defaults(tmp_path):
    c = SessionConfig.from_env({"MAMEMCP_MAME_DIR": "D:/x/mame", "MAMEMCP_ROMPATH": "D:/a;D:/b;", "MAMEMCP_RUNS_DIR": str(tmp_path)})
    assert c.mame_exe == Path("D:/x/mame/mame.exe") and c.rompaths == [Path("D:/a"), Path("D:/b")] and c.runs_dir == tmp_path
    d = SessionConfig.from_env({})
    assert d.rompaths == [d.mame_dir / "roms"] and d.runs_dir.name == "sessions"


# ---------------------------------------------------------------------------------------------- launcher hygiene
def test_launcher_never_passes_test_hooks_or_fixture_into_production(monkeypatch, tmp_path):
    seen = {}

    class P:
        pid = 1

    def popen(cmd, **kw):
        seen["cmd"], seen["env"], seen["stdin"] = cmd, kw["env"], kw.get("stdin")
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
