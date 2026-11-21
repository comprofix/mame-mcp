"""Unit tests for screenshot capture: argument validation, session requirements, output ownership, PNG validation, error
passing, serialisation, and the MCP tool's image result. A fake bridge client stands in for the plugin; the real plugin and
real MAME are exercised by scripts/mcp_screenshot_e2e.py."""
import asyncio
import base64
import json
import struct
import threading
import time
import zlib
from pathlib import Path

import pytest

from mame_mcp import operations, png, protocol, server
from mame_mcp.protocol import BridgeError
from test_operations import OpsRig, rig  # noqa: F401  (rig fixture)


# ------------------------------------------------------------------------------------------------ PNG helpers
def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def make_png(width, height, pixel, filter_type=0, color_type=2, corrupt=None):
    """pixel(x, y) -> tuple of channel values. Rows are encoded with `filter_type` (0..4)."""
    bpp = {2: 3, 6: 4, 0: 1}[color_type]
    rows = [bytes(v for x in range(width) for v in pixel(x, y)) for y in range(height)]
    raw, prev = bytearray(), bytes(width * bpp)
    for row in rows:
        out = bytearray()
        for i, v in enumerate(row):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pred = {0: 0, 1: a, 2: b, 3: (a + b) >> 1, 4: paeth(a, b, c)}[filter_type]
            out.append((v - pred) & 255)
        raw += bytes([filter_type]) + bytes(out)
        prev = row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    data = png.SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    if corrupt == "crc":
        data = data[:30] + bytes([data[30] ^ 0xFF]) + data[31:]
    return data, rows


def gradient(x, y):
    return (x * 7 % 256, y * 11 % 256, (x * y) % 256)


# ------------------------------------------------------------------------------------------------ png module
@pytest.mark.parametrize("ft", [0, 1, 2, 3, 4])
def test_png_decodes_every_filter_type(ft):
    data, rows = make_png(17, 9, gradient, filter_type=ft)
    info, out = png.decode_pixels(data)
    assert (info.width, info.height, info.color_type) == (17, 9, 2)
    assert out == rows


def test_png_summary_distinguishes_blank_from_drawn():
    blank, _ = make_png(32, 8, lambda x, y: (0, 0, 0))
    drawn, _ = make_png(32, 8, gradient)
    assert png.summarise_pixels(blank)["single_colour"] is True
    s = png.summarise_pixels(drawn)
    assert s["single_colour"] is False and s["distinct_colours"] > 10
    assert png.summarise_pixels(blank)["pixel_sha256"] != s["pixel_sha256"]


@pytest.mark.parametrize("mutate", [
    lambda d: d[:7], lambda d: b"\x00" * len(d), lambda d: d[:-5], lambda d: d + b"junk",
    lambda d: d[:8] + chunk(b"IDAT", b"x") + d[8:],  # IHDR not first
])
def test_png_rejects_malformed_input(mutate):
    data, _ = make_png(8, 8, gradient)
    with pytest.raises(png.PngError):
        png.inspect_png(mutate(data))


def test_png_rejects_bad_crc_and_wrong_data_length():
    bad, _ = make_png(8, 8, gradient, corrupt="crc")
    with pytest.raises(png.PngError, match="CRC"):
        png.inspect_png(bad)
    ihdr = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    short = png.SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b"")
    with pytest.raises(png.PngError, match="inflates"):
        png.inspect_png(short)


# ------------------------------------------------------------------------------------------------ protocol
def test_screenshot_args():
    assert protocol.screenshot_args(None) == {}
    assert protocol.screenshot_args(":screen") == {"screen": ":screen"}
    assert protocol.screenshot_args(":lscreen") == {"screen": ":lscreen"}
    for bad in ["", "screen", ":scr een", ":screen/../x", "..\\x", ":screen\n", ":" + "a" * 70, 5, True, [":screen"], {"screen": 1}, "C:\\x.png"]:
        with pytest.raises(BridgeError) as e:
            protocol.screenshot_args(bad)
        assert e.value.code == "bad_argument", bad


def test_there_is_no_way_to_choose_an_output_path():
    import inspect
    assert list(inspect.signature(server.capture_screenshot).parameters) == ["screen", "session_id"]
    assert list(inspect.signature(server.list_screens).parameters) == ["session_id"]
    assert list(inspect.signature(operations.StateOperations.capture_screenshot).parameters) == ["self", "screen", "session_id"]


# ------------------------------------------------------------------------------------------------ session operation
PNG_OK, _ROWS = make_png(24, 16, gradient)


def shot_reply(rig, args=None, *, name="shot-0001-screen-f120.png", content=PNG_OK, **over):
    folder = rig.run_dir / "evidence" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(content)
    r = {"screen": ":screen", "path": str(p).replace("\\", "/"), "file": name, "file_size": len(content), "sequence": 1,
         "image_frame": 120, "current_frame": 120, "frame_after_capture": 120, "frames_behind": 0, "freshness": "fresh",
         "freshness_note": "n", "execution": {"state": "paused"}, "execution_after": {"state": "paused"},
         "clock": {"frame": 120, "emu_time_s": 2.0}, "state_epoch": 0, "native_width": 24, "native_height": 16, "orientation_degrees": 0,
         "frameskip": 0}
    r.update(over)
    return r


def test_capture_returns_png_bytes_and_complete_metadata(rig):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a)
    meta, data = rig.mgr.capture_screenshot()
    assert data == PNG_OK
    assert meta["state"] == "captured" and meta["system"] == "shinobi" and meta["session_id"] == "s1-aaaaaa"
    assert meta["screen"] == ":screen" and meta["mime_type"] == "image/png"
    assert meta["image"]["width"] == 24 and meta["image"]["height"] == 16 and meta["image"]["single_colour"] is False
    assert meta["frame"]["image_frame"] == 120 and meta["frame"]["freshness"] == "fresh"
    assert meta["execution"] == {"state": "paused"} and meta["emulation_unchanged_by_capture"] is True
    assert meta["geometry"]["image_matches_native_after_rotation"] is True
    assert {"rotation", "scaling", "cropping"} <= set(meta["geometry"])
    path = Path(meta["evidence_path"])
    assert path.is_relative_to((rig.run_dir / "evidence" / "screenshots").resolve()) and path.read_bytes() == PNG_OK
    side = json.loads(Path(meta["sidecar_path"]).read_text())
    assert side["sha256"] == meta["sha256"] and side["frame"]["image_frame"] == 120


def test_capture_sends_only_the_validated_screen(rig):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a)
    rig.mgr.capture_screenshot(":screen")
    rig.mgr.capture_screenshot()
    assert [a for o, a in rig.client.calls if o == "screenshot"] == [{"screen": ":screen"}, {}]


def test_rotated_screens_expect_swapped_dimensions(rig):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a, native_width=16, native_height=24, orientation_degrees=270)
    meta, _ = rig.mgr.capture_screenshot()
    assert meta["geometry"]["image_matches_native_after_rotation"] is True
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a, native_width=24, native_height=16, orientation_degrees=270, name="shot-0002-screen-f1.png")
    meta, _ = rig.mgr.capture_screenshot()
    assert meta["geometry"]["image_matches_native_after_rotation"] is False  # reported, not hidden


def test_a_moving_emulation_is_reported_not_assumed_unchanged(rig):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a, frame_after_capture=121, execution={"state": "running"}, execution_after={"state": "running"})
    meta, _ = rig.mgr.capture_screenshot()
    assert meta["emulation_unchanged_by_capture"] is False


def test_bad_arguments_never_reach_the_bridge(rig):
    for bad in ["", "screen", ":a b", 5, "../x"]:
        with pytest.raises(BridgeError) as e:
            rig.mgr.capture_screenshot(bad)
        assert e.value.code == "bad_argument"
    assert rig.count("screenshot") == 0


def test_capture_needs_a_managed_session(tmp_path):
    r = OpsRig(tmp_path)
    with pytest.raises(BridgeError) as e:
        r.mgr.capture_screenshot()
    assert e.value.code == "no_session"


def test_capture_after_stop_is_refused(rig):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a)
    rig.mgr.stop()
    with pytest.raises(BridgeError) as e:
        rig.mgr.capture_screenshot()
    assert e.value.code == "no_session"


@pytest.mark.parametrize("where", ["outside", "sibling_dir", "wrong_ext", "traversal", "subdir"])
def test_a_screenshot_outside_the_session_screenshot_directory_is_refused(rig, tmp_path, where):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    ev = rig.run_dir / "evidence"
    (ev / "screenshots" / "sub").mkdir(parents=True, exist_ok=True)
    (ev / "other").mkdir(parents=True, exist_ok=True)
    paths = {
        "outside": elsewhere / "x.png", "sibling_dir": ev / "other" / "x.png", "wrong_ext": ev / "screenshots" / "x.txt",
        "traversal": ev / "screenshots" / ".." / "other" / "x.png", "subdir": ev / "screenshots" / "sub" / "x.png",
    }
    p = paths[where]
    p.write_bytes(PNG_OK)
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a, path=str(p))
    with pytest.raises(BridgeError) as e:
        rig.mgr.capture_screenshot()
    assert e.value.code == "screenshot_path_outside_session" and e.value.uncertain is True


def test_missing_or_absent_path_and_invalid_images_are_errors(rig):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a, path=str(rig.run_dir / "evidence" / "screenshots" / "gone.png"))
    with pytest.raises(BridgeError) as e:
        rig.mgr.capture_screenshot()
    assert e.value.code == "screenshot_failed"
    rig.handlers["screenshot"] = lambda a: {k: v for k, v in shot_reply(rig, a).items() if k != "path"}
    with pytest.raises(BridgeError) as e:
        rig.mgr.capture_screenshot()
    assert e.value.code == "screenshot_failed"
    for i, bad in enumerate([b"", b"not a png", PNG_OK[:-8], make_png(8, 8, gradient, corrupt="crc")[0]]):
        rig.handlers["screenshot"] = lambda a, i=i, bad=bad: shot_reply(rig, a, name=f"shot-bad{i}.png", content=bad)
        with pytest.raises(BridgeError) as e:
            rig.mgr.capture_screenshot()
        assert e.value.code == "screenshot_invalid"


def test_oversized_output_is_refused(rig, monkeypatch):
    monkeypatch.setattr(operations, "MAX_SCREENSHOT_BYTES", 100)
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a)
    with pytest.raises(BridgeError) as e:
        rig.mgr.capture_screenshot()
    assert e.value.code == "screenshot_invalid"


def test_plugin_refusals_and_failures_pass_through(rig):
    for code in ("screen_required", "unknown_screen", "no_screen", "checkpoint_busy", "screenshot_failed", "evidence_unavailable"):
        rig.handlers["screenshot"] = lambda a, code=code: BridgeError(code, f"plugin says {code}", detail={"screens": [":screen"]})
        with pytest.raises(BridgeError) as e:
            rig.mgr.capture_screenshot(":nope")
        assert e.value.code == code and e.value.detail == {"screens": [":screen"]}


def test_capture_is_serialised_with_checkpoint_operations(rig, monkeypatch):
    """A capture never overlaps a save/load: while one runs the other waits, then is refused with a clear error."""
    monkeypatch.setattr(operations, "EXCLUSIVE_WAIT_S", 0.2)
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a)
    inside, release = threading.Event(), threading.Event()

    def slow_save(a):
        inside.set()
        release.wait(5)
        from test_operations import save_reply
        return save_reply(rig, a)

    rig.handlers["checkpoint_save"] = slow_save
    t = threading.Thread(target=lambda: rig.mgr.save_checkpoint())
    t.start()
    assert inside.wait(2)
    with pytest.raises(BridgeError) as e:
        rig.mgr.capture_screenshot()
    assert e.value.code == "operation_in_progress" and "save_checkpoint" in e.value.message
    assert rig.count("screenshot") == 0
    release.set()
    t.join(5)
    meta, _ = rig.mgr.capture_screenshot()  # free again afterwards
    assert meta["state"] == "captured"


def test_stop_session_waits_for_an_in_flight_capture(rig):
    inside, release = threading.Event(), threading.Event()

    def slow(a):
        inside.set()
        release.wait(5)
        return shot_reply(rig, a)

    rig.handlers["screenshot"] = slow
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", rig.mgr.capture_screenshot()))
    t.start()
    assert inside.wait(2)
    stopper = threading.Thread(target=lambda: box.setdefault("stop", rig.mgr.stop(drain_timeout=5)))
    stopper.start()
    time.sleep(0.2)
    assert "stop" not in box  # stop is still draining the in-flight capture
    release.set()
    t.join(5)
    stopper.join(5)
    assert box["r"][0]["state"] == "captured" and box["stop"]["stopped"] is True and box["stop"]["drained_in_flight_calls"] is True


# ------------------------------------------------------------------------------------------------ MCP tool result
def test_tool_returns_image_content_with_metadata(rig, monkeypatch):
    rig.handlers["screenshot"] = lambda a: shot_reply(rig, a)
    monkeypatch.setattr(server, "manager", rig.mgr)
    res = asyncio.run(server.capture_screenshot())
    kinds = [c.type for c in res.content]
    assert kinds == ["text", "image"] and not res.is_error
    img = res.content[1]
    assert img.mime_type == "image/png" and base64.b64decode(img.data) == PNG_OK
    assert json.loads(res.content[0].text)["evidence_path"] == res.structured_content["evidence_path"]
    assert res.structured_content["frame"]["image_frame"] == 120


def test_tool_errors_are_typed_tool_errors(rig, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    monkeypatch.setattr(server, "manager", rig.mgr)
    rig.handlers["screenshot"] = lambda a: BridgeError("screen_required", "several screens", detail={"screens": [":a", ":b"]})
    with pytest.raises(ToolError) as e:
        asyncio.run(server.capture_screenshot())
    assert json.loads(str(e.value))["code"] == "screen_required"
    with pytest.raises(ToolError) as e:
        asyncio.run(server.capture_screenshot("bad tag"))
    assert json.loads(str(e.value))["code"] == "bad_argument"
