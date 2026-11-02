import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from playback import (InputDecoder, JsonLines, MediaKeys, MpvIPC, PlaybackReporter,
                      PlaybackSession)


class ProtocolTests(unittest.TestCase):
    def test_batched_and_fragmented_messages_with_utf8(self):
        expected = [{"event": "file-loaded"}, {"data": 123.5, "request_id": 9},
                    {"event": "property-change", "name": "media-title", "data": "é"}]
        raw = b"".join(json.dumps(v, ensure_ascii=False).encode() + b"\n" for v in expected)
        self.assertEqual(JsonLines().feed(raw), expected)
        for chunk_size in (1, 2, 7, 31, len(raw) - 1):
            parser = JsonLines()
            result = []
            for offset in range(0, len(raw), chunk_size):
                result.extend(parser.feed(raw[offset:offset + chunk_size]))
            self.assertEqual(result, expected)

    def test_bad_line_does_not_discard_following_reply(self):
        self.assertEqual(JsonLines().feed(b'not-json\n[]\n{"data":3}\n'), [{"data": 3}])

    def test_unsolicited_event_and_seek_reply_cannot_become_position(self):
        client = MpvIPC("unused")
        local, peer = socket.socketpair()
        local.setblocking(False)
        client.sock = local
        try:
            seek_id = client.command("seek", 10, "relative")
            position_id = client.command("get_property", "time-pos", property_name="time-pos")
            client.poll()
            peer.recv(4096)
            replies = [{"request_id": seek_id, "error": "success", "data": None},
                       {"event": "property-change", "name": "pause", "data": True},
                       {"request_id": position_id, "error": "success", "data": 321.75},
                       {"event": "property-change", "name": "time-pos", "data": None}]
            peer.sendall(b"".join(json.dumps(v).encode() + b"\n" for v in replies))
            client.poll()
            self.assertEqual(client.position_ticks(0), 3217500000)
            self.assertTrue(client.properties["pause"])
            self.assertEqual(client.position_ticks(0), 3217500000)
            client.properties["time-pos"] = float("nan")
            self.assertEqual(client.position_ticks(999), 999)
        finally:
            peer.close()
            client.close()


class InputTests(unittest.TestCase):
    def event(self, kind, code, value):
        return InputDecoder.EVENT.pack(0, 0, kind, code, value)

    def test_hardware_back_start_and_select_stop(self):
        for code in (305, 310, 311, 312):
            self.assertEqual(InputDecoder().feed(self.event(1, code, 1)), ["stop"])

    def test_signed_dpad_and_shoulders(self):
        raw = b"".join(self.event(*event) for event in
                       [(3, 16, -1), (3, 16, 0), (3, 16, 1),
                        (1, 308, 1), (1, 309, 1), (1, 304, 1)])
        self.assertEqual(InputDecoder().feed(raw),
                         ["seek:-10", "seek:10", "seek:-30", "seek:30", "pause"])

    def test_x_and_y_cycle_audio_and_subtitle_tracks(self):
        raw = self.event(1, 306, 1) + self.event(1, 307, 1)
        self.assertEqual(InputDecoder().feed(raw), ["audio", "subtitles"])

    def test_volume_release_and_repeat_are_ignored(self):
        decoder = InputDecoder()
        raw = b"".join(self.event(*event) for event in
                       [(1, 114, 1), (1, 115, 1), (1, 305, 0), (1, 305, 2)])
        self.assertEqual(decoder.feed(raw), [])

    def test_partial_input_record_and_no_duplicate_direction(self):
        decoder = InputDecoder()
        raw = self.event(3, 16, -1)
        self.assertEqual(decoder.feed(raw[:5]), [])
        self.assertEqual(decoder.feed(raw[5:] + raw), ["seek:-10"])


class ReporterTests(unittest.TestCase):
    def test_network_delay_does_not_block_controls_and_order_is_preserved(self):
        gate = threading.Event()
        entered = threading.Event()
        sent = []
        def send(event, ticks, paused):
            if event == "started":
                entered.set()
                gate.wait(2)
            sent.append((event, ticks))
        reporter = PlaybackReporter(send)
        reporter.submit("started", 100)
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        reporter.submit("progress", 200)
        reporter.submit("progress", 300)
        reporter.submit("stopped", 400)
        self.assertLess(time.monotonic() - before, 0.1)
        gate.set()
        self.assertTrue(reporter.finished.wait(1))
        self.assertEqual(sent, [("started", 100), ("stopped", 400)])
        self.assertTrue(reporter.stop_ok)

    def test_report_failure_is_visible_without_killing_worker(self):
        def send(*args):
            raise OSError("offline")
        reporter = PlaybackReporter(send)
        reporter.submit("started", 100)
        reporter.submit("stopped", 200)
        self.assertTrue(reporter.finished.wait(1))
        self.assertFalse(reporter.stop_ok)
        self.assertTrue(reporter.error)


class CleanupTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux parent-death guard")
    def test_kernel_stops_child_if_app_is_killed(self):
        module = str(Path(__file__).resolve().parents[1] / "app" / "playback.py")
        if not Path(module).exists():
            module = str(Path(__file__).with_name("playback.py"))
        parent_code = (
            "import os,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,sys.argv[1],'--guard-parent',"
            "str(os.getpid()),sys.executable,'-c','import time; time.sleep(30)']); "
            "print(child.pid,flush=True); time.sleep(30)"
        )
        parent = subprocess.Popen([sys.executable, "-c", parent_code, module],
                                  stdout=subprocess.PIPE, text=True)
        child_pid = int(parent.stdout.readline())
        try:
            time.sleep(0.15)
            parent.kill()
            parent.wait(timeout=1)
            deadline = time.monotonic() + 2
            alive = True
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{child_pid}/stat").read_text().split(")", 1)[1].split()[0]
                    alive = state not in ("Z", "X")
                except FileNotFoundError:
                    alive = False
                if not alive:
                    break
                time.sleep(0.02)
            self.assertFalse(alive, "Player survived an uncatchable app crash")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=1)
            parent.stdout.close()
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_input_exception_reaps_real_child_and_reports_last_position(self):
        class BrokenControls:
            closed = False
            def poll(self):
                raise ValueError("regression: unexpected input failure")
            def close(self):
                self.closed = True
        controls = BrokenControls()
        sent = []
        reporter = PlaybackReporter(lambda *value: sent.append(value))
        with tempfile.TemporaryDirectory() as directory:
            session = PlaybackSession([sys.executable, "-c", "import time; time.sleep(30)"],
                                      os.path.join(directory, "ipc"), 123000000,
                                      reporter, controls, guard=False)
            with self.assertRaises(ValueError):
                session.run()
            self.assertIsNotNone(session.player.poll(), "video child was orphaned")
            self.assertTrue(controls.closed)
            self.assertTrue(reporter.finished.wait(1))
            self.assertEqual(sent[-1], ("stopped", 123000000, False))

    def test_parser_exception_also_cleans_up_player(self):
        class Controls:
            def poll(self):
                return []
            def close(self):
                pass
        reporter = PlaybackReporter(lambda *args: None)
        with tempfile.TemporaryDirectory() as directory:
            session = PlaybackSession([sys.executable, "-c", "import time; time.sleep(30)"],
                                      os.path.join(directory, "ipc"), 0,
                                      reporter, Controls(), guard=False)
            with patch.object(session.ipc, "connect", return_value=True), \
                 patch.object(session.ipc, "poll", side_effect=RuntimeError("broken IPC")):
                with self.assertRaises(RuntimeError):
                    session.run()
            self.assertIsNotNone(session.player.poll())


if __name__ == "__main__":
    unittest.main()


class MediaKeysTests(unittest.TestCase):
    """Fake evdev devices: a FIFO per event node plus its sysfs capability file."""
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.inputs = Path(self.root.name) / "input"
        self.sys = Path(self.root.name) / "sys"
        self.inputs.mkdir()
        self.writers = []

    def tearDown(self):
        for writer in self.writers:
            os.close(writer)
        self.root.cleanup()

    def device(self, name, codes):
        node = self.inputs / name
        os.mkfifo(node)
        info = self.sys / name / "device"
        (info / "capabilities").mkdir(parents=True)
        (info / "name").write_text("Test " + name)
        bits = sum(1 << code for code in codes)
        (info / "capabilities" / "key").write_text("%x" % bits)
        return node

    def keys(self):
        return MediaKeys(str(self.inputs), str(self.sys), rescan=0)

    def connect(self, node):
        writer = os.open(node, os.O_WRONLY | os.O_NONBLOCK)
        self.writers.append(writer)
        return writer

    def press(self, writer, code):
        os.write(writer, InputDecoder.EVENT.pack(0, 0, 1, code, 1) + InputDecoder.EVENT.pack(0, 0, 1, code, 0))

    def test_dedicated_keys_and_devices_without_media_keys_are_ignored(self):
        headset = self.device("event0", [163, 164, 165])
        self.device("event1", [304, 305])  # a gamepad: not opened
        keys = self.keys()
        keys.poll()
        writer = self.connect(headset)
        self.assertEqual(len(keys.devices), 1)
        for code in (164, 163, 165, 200):
            self.press(writer, code)
        self.assertEqual(keys.poll(), ["playpause", "next", "previous", "play"])
        keys.close()

    def test_single_button_remote_counts_presses_like_a_phone(self):
        remote = self.device("event2", [226])
        keys = self.keys()
        keys.poll()
        writer = self.connect(remote)
        results = []
        for presses in (1, 2, 3):
            for _ in range(presses):
                self.press(writer, 226)
            self.assertEqual(keys.poll(), [])
            time.sleep(MediaKeys.CLICK_GAP + 0.05)
            results += keys.poll()
        self.assertEqual(results, ["playpause", "next", "previous"])
        keys.close()

    def test_disconnected_device_is_dropped_and_found_again(self):
        node = self.device("event3", [164])
        keys = self.keys()
        keys.poll()
        os.close(self.connect(node))
        self.writers.clear()
        keys.poll()  # EOF, like a Bluetooth headset going away
        self.assertEqual(keys.devices, {})
        keys.poll()  # the rescan opens it again
        self.assertEqual(len(keys.devices), 1)
        keys.close()
