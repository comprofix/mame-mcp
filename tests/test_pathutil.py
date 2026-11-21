"""Containment by filesystem identity: accepted equivalent spellings of the session's own directories, rejected paths that
only look similar or escape through `..`, links or junctions. Windows spellings are exercised on Windows only."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mame_mcp import operations, pathutil
from mame_mcp.protocol import BridgeError
from test_operations import rig  # noqa: F401  (rig fixture)
from test_screenshot import PNG_OK, shot_reply

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows path spellings")
EXT_PREFIX = "\\\\?\\"


def _short(p: Path) -> str | None:
    """The 8.3 short form of an existing path, or None where the volume has short names disabled."""
    if sys.platform != "win32":
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    return buf.value if 0 < n < 1024 else None


def _link_dir(link: Path, target: Path) -> bool:
    """Create a directory symlink (junction on Windows, which needs no privilege). False when the platform refuses."""
    try:
        if sys.platform == "win32":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            return r.returncode == 0
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def _capture_with(rig, reported: str):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a, path=reported)
    return rig.mgr.capture_screenshot()


def _folder(rig) -> Path:
    return rig.run_dir / "evidence" / "screenshots"


# ------------------------------------------------------------------------------------------------ helper level
def test_same_directory_and_containment_by_identity(tmp_path):
    a, b = tmp_path / "run", tmp_path / "run-2"
    (a / "x" / "y").mkdir(parents=True)
    b.mkdir()
    assert pathutil.same_directory(a, a / "x" / "..")
    assert not pathutil.same_directory(a, b) and not pathutil.same_directory(a, tmp_path / "missing")
    assert pathutil.is_within(a / "x" / "y" / "f.sta", a) and pathutil.is_within(a, a)
    assert not pathutil.is_within(b / "f.sta", a), "a sibling whose name starts with the folder's name is not inside it"
    assert pathutil.is_directly_in(a / "x" / "f.png", a / "x") and not pathutil.is_directly_in(a / "x" / "y" / "f.png", a / "x")


# ------------------------------------------------------------------------------------------------ accepted spellings
@win
def test_equivalent_windows_spellings_of_the_screenshot_file_are_accepted(rig):
    folder = _folder(rig)
    shot_reply(rig)  # creates the folder and the default file
    real = str(folder / "shot-0001-screen-f120.png")
    spellings = {
        "extended_prefix": EXT_PREFIX + real,
        "forward_slashes": real.replace("\\", "/"),
        "upper_case": real.upper().replace("SHOT-", "shot-").replace(".PNG", ".png"),
        "dot_segments": str(folder / "." / "shot-0001-screen-f120.png"),
    }
    short = _short(folder / "shot-0001-screen-f120.png")
    if short:
        spellings["short_name"] = short
    for label, reported in spellings.items():
        rig.handlers["screenshot"] = lambda a, r=reported: shot_reply(rig, a, path=r)
        meta, data = rig.mgr.capture_screenshot()
        assert data == PNG_OK and meta["state"] == "captured", label


@win
def test_the_extended_prefix_alone_does_not_defeat_the_ownership_check(rig):
    """The regression this file exists for: Path.resolve() leaves the prefix on, so a raw comparison of the folder and the
    reported file's parent failed for the very same directory."""
    reported = EXT_PREFIX + str(_folder(rig) / "shot-0001-screen-f120.png")
    assert Path(reported).resolve().parent != _folder(rig).resolve(), "premise: the spellings differ as strings"
    shot_reply(rig)
    meta, _ = _capture_with(rig, reported)
    assert meta["state"] == "captured"


# ------------------------------------------------------------------------------------------------ rejected paths
@win
def test_extended_prefix_paths_outside_the_session_are_still_refused(rig, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.png").write_bytes(PNG_OK)
    sibling = rig.run_dir / "evidence" / "screenshots-extra"  # shares the folder's name as a prefix
    sibling.mkdir(parents=True)
    (sibling / "x.png").write_bytes(PNG_OK)
    for p in (outside / "x.png", sibling / "x.png", _folder(rig).parent / "x.png"):
        (_folder(rig)).mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(PNG_OK)
        with pytest.raises(BridgeError) as e:
            _capture_with(rig, EXT_PREFIX + str(p))
        assert e.value.code == "screenshot_path_outside_session" and e.value.uncertain is True


def test_a_sibling_directory_with_the_folders_name_as_a_prefix_is_refused(rig):
    sibling = rig.run_dir / "evidence" / "screenshots-extra"
    sibling.mkdir(parents=True)
    (sibling / "x.png").write_bytes(PNG_OK)
    with pytest.raises(BridgeError) as e:
        _capture_with(rig, str(sibling / "x.png"))
    assert e.value.code == "screenshot_path_outside_session"


def test_a_directory_link_inside_the_session_that_points_outside_is_refused(rig, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.png").write_bytes(PNG_OK)
    folder = _folder(rig)
    folder.mkdir(parents=True, exist_ok=True)
    if not _link_dir(folder / "escape", outside):
        pytest.skip("this platform or account cannot create directory links")
    with pytest.raises(BridgeError) as e:
        _capture_with(rig, str(folder / "escape" / "x.png"))
    assert e.value.code == "screenshot_path_outside_session"


def test_a_file_link_inside_the_session_that_points_outside_is_refused(rig, tmp_path):
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG_OK)
    folder = _folder(rig)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, folder / "link.png")
    except OSError:
        pytest.skip("this platform or account cannot create file symlinks")
    with pytest.raises(BridgeError) as e:
        _capture_with(rig, str(folder / "link.png"))
    assert e.value.code == "screenshot_path_outside_session"


def test_a_link_to_the_session_folder_from_elsewhere_is_the_same_directory(rig, tmp_path):
    """Identity, not spelling: reaching the session's own screenshot folder through a link is still inside it."""
    folder = _folder(rig)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shot-0001-screen-f120.png").write_bytes(PNG_OK)
    if not _link_dir(tmp_path / "alias", folder):
        pytest.skip("this platform or account cannot create directory links")
    meta, data = _capture_with(rig, str(tmp_path / "alias" / "shot-0001-screen-f120.png"))
    assert data == PNG_OK and meta["state"] == "captured"


def test_a_checkpoint_state_file_is_judged_by_the_same_identity_rule(rig, tmp_path):
    inside, sibling = rig.run_dir / "a.sta", Path(str(rig.run_dir) + "-2") / "a.sta"
    sibling.parent.mkdir(parents=True)
    rig.run_dir.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"x" * 8)
    sibling.write_bytes(b"x" * 8)
    s = rig.mgr.resolve_managed()
    assert rig.mgr._owned_path(s, str(inside)) == inside.resolve()
    if sys.platform == "win32":
        assert rig.mgr._owned_path(s, EXT_PREFIX + str(inside)).name == "a.sta"
    with pytest.raises(BridgeError) as e:
        rig.mgr._owned_path(s, str(sibling))
    assert e.value.code == "checkpoint_path_outside_session"
