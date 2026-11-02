import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from unittest.mock import patch

import playback
from music import (MusicPlayer, Queue, forget_session, load_sessions, queue_key,
                   remember_session, save_sessions)
from playback import Backlight


class QueueTests(unittest.TestCase):
    def tracks(self, count=5):
        return [{"Id": str(index)} for index in range(count)]

    def test_shuffle_keeps_current_track_and_every_track_once(self):
        queue = Queue(self.tracks(), start=2)
        queue.set_shuffle(True)
        self.assertEqual(queue.current["Id"], "2")
        self.assertEqual(sorted(t["Id"] for t in queue.upcoming()), ["0", "1", "2", "3", "4"])
        queue.advance(1)
        queue.set_shuffle(False)
        # Unshuffling resumes album order from whatever is playing now.
        self.assertEqual(queue.upcoming()[queue.position], queue.current)

    def test_end_of_queue_and_repeat_modes(self):
        queue = Queue(self.tracks(2), start=1)
        self.assertFalse(queue.advance(1, automatic=True))
        queue.cycle_repeat()
        self.assertEqual(queue.repeat, "all")
        self.assertTrue(queue.advance(1, automatic=True))
        self.assertEqual(queue.current["Id"], "0")
        queue.cycle_repeat()
        self.assertEqual(queue.repeat, "one")
        self.assertTrue(queue.advance(1, automatic=True))
        self.assertEqual(queue.current["Id"], "0")
        # A manual skip still moves on in repeat-one.
        self.assertTrue(queue.advance(1))
        self.assertEqual(queue.current["Id"], "1")

    def test_previous_at_start_does_not_wrap_without_repeat(self):
        queue = Queue(self.tracks(3))
        self.assertFalse(queue.advance(-1))
        self.assertEqual(queue.position, 0)

    def test_peek_matches_where_playback_goes(self):
        queue = Queue(self.tracks(2), start=1)
        self.assertIsNone(queue.peek())
        queue.repeat = "all"
        self.assertEqual(queue.peek()["Id"], "0")
        queue.repeat = "one"
        self.assertEqual(queue.peek()["Id"], "1")

    def test_snapshot_round_trip_keeps_order_and_drops_bulky_fields(self):
        tracks = [{"Id": str(i), "Name": "T%d" % i, "Overview": "long text"} for i in range(4)]
        queue = Queue(tracks, start=1)
        queue.set_shuffle(True)
        queue.repeat = "all"
        restored = Queue.restore(json.loads(json.dumps(queue.snapshot())))
        self.assertEqual(restored.upcoming(), [{k: v for k, v in t.items() if k != "Overview"} for t in queue.upcoming()])
        self.assertEqual((restored.current["Id"], restored.shuffle, restored.repeat), ("1", True, "all"))

    def test_corrupt_session_order_falls_back_to_album_order(self):
        restored = Queue.restore({"tracks": self.tracks(3), "order": [0, 0, 7], "position": 9, "repeat": "bogus"})
        self.assertEqual([t["Id"] for t in restored.upcoming()], ["0", "1", "2"])
        self.assertEqual((restored.position, restored.repeat), (0, "off"))

    def test_session_file_round_trip_and_old_single_queue_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "queue.json")
            self.assertEqual(load_sessions(path), [])
            save_sessions(path, [{"tracks": [{"Id": "1"}]}])
            self.assertEqual(load_sessions(path), [{"tracks": [{"Id": "1"}]}])
            # The earlier build stored one queue as a bare object.
            Path(path).write_text(json.dumps({"tracks": [{"Id": "9"}], "position": 0}))
            self.assertEqual([queue_key(s) for s in load_sessions(path)], [("9",)])
            Path(path).write_text("not json")
            self.assertEqual(load_sessions(path), [])


class SavedQueuesTests(unittest.TestCase):
    ME = {"server": "http://jf", "user": "me"}

    def book(self, name, position=0, elapsed=0):
        return {"tracks": [{"Id": name + str(i)} for i in range(3)], "order": [0, 1, 2],
                "position": position, "elapsed": elapsed, "title": name}

    def test_same_track_list_updates_its_entry_and_moves_first(self):
        sessions = remember_session([], self.book("a"), self.ME)
        sessions = remember_session(sessions, self.book("b"), self.ME)
        sessions = remember_session(sessions, self.book("a", position=2, elapsed=90), self.ME)
        self.assertEqual([(s["title"], s["position"]) for s in sessions], [("a", 2), ("b", 0)])

    def test_unchanged_first_queue_is_left_alone_but_a_resumed_one_moves_first(self):
        sessions = remember_session([], self.book("a"), self.ME)
        sessions = remember_session(sessions, self.book("b"), self.ME)
        self.assertIs(remember_session(sessions, self.book("b"), self.ME), sessions)
        # Resuming "a" with no new progress still makes it the most recent.
        sessions = remember_session(sessions, self.book("a"), self.ME)
        self.assertEqual([s["title"] for s in sessions], ["a", "b"])

    def test_keeps_ten_per_account_and_leaves_other_accounts_alone(self):
        other = dict(self.book("theirs"), server="http://jf", user="them")
        sessions = [other]
        for index in range(12):
            sessions = remember_session(sessions, self.book("b%d" % index), self.ME)
        mine = [s["title"] for s in sessions if s["user"] == "me"]
        self.assertEqual(mine, ["b%d" % i for i in range(11, 1, -1)])
        self.assertIn(other, sessions)

    def test_forget_removes_only_that_queue_for_this_account(self):
        sessions = remember_session([], self.book("a"), self.ME)
        sessions = remember_session(sessions, self.book("b"), self.ME)
        theirs = remember_session([], self.book("a"), {"server": "http://jf", "user": "them"})
        sessions = forget_session(sessions + theirs, queue_key(self.book("a")), self.ME)
        self.assertEqual([(s["title"], s["user"]) for s in sessions], [("b", "me"), ("a", "them")])


class BacklightTests(unittest.TestCase):
    def panel(self, root, name, brightness="180"):
        path = Path(root) / name
        path.mkdir()
        (path / "brightness").write_text(brightness)
        (path / "bl_power").write_text("0")
        return path

    def test_off_zeroes_every_device_and_on_restores_each(self):
        with tempfile.TemporaryDirectory() as root:
            first, second = self.panel(root, "a", "180"), self.panel(root, "b", "40")
            light = Backlight(root)
            light.turn_off()
            for path in (first, second):
                # Brightness 0 as well: some drivers accept bl_power but ignore it.
                self.assertEqual(((path / "bl_power").read_text(), (path / "brightness").read_text()), ("4", "0"))
            light.turn_on()
            self.assertEqual([(p / "bl_power").read_text() + "/" + (p / "brightness").read_text()
                              for p in (first, second)], ["0/180", "0/40"])

    def fake_disp(self, start):
        state = {"value": start, "calls": []}
        def brightness(value=None, path=None):
            state["calls"].append(value)
            if value is None:
                return state["value"]
            state["value"] = value
            return 0
        return state, patch.object(playback, "disp_brightness", brightness)

    def test_allwinner_disp_dims_and_restores(self):
        with tempfile.TemporaryDirectory() as root:
            disp = Path(root) / "disp"
            disp.touch()
            state, fake = self.fake_disp(150)
            with fake:
                light = Backlight(str(Path(root) / "no-sysfs"), str(disp))
                light.turn_off()
                self.assertEqual(state["value"], 0)
                light.turn_on()
                self.assertEqual(state["value"], 150)

    def test_implausible_disp_reading_never_dims(self):
        with tempfile.TemporaryDirectory() as root:
            disp = Path(root) / "disp"
            disp.touch()
            state, fake = self.fake_disp(0)
            with fake:
                light = Backlight(str(Path(root) / "no-sysfs"), str(disp))
                light.turn_off()
                self.assertEqual(state["calls"], [None])
                self.assertFalse(light.off)

    def test_missing_backlight_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as root:
            light = Backlight(root)
            light.turn_off()
            light.turn_on()
            self.assertFalse(light.off)


@unittest.skipUnless(shutil.which("mpv") and shutil.which("ffmpeg"), "needs mpv and ffmpeg")
class MusicPlayerTests(unittest.TestCase):
    """Drives a real windowless mpv through short generated tones."""
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.files = {}
        for index in range(3):
            path = Path(cls.directory.name) / ("tone%d.wav" % index)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                            "sine=frequency=%d:duration=1" % (440 + index * 100), str(path)], check=True)
            cls.files[str(index)] = str(path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.reports = []
        self.lock = threading.Lock()
        def report(event, track, session_id, ticks, paused):
            with self.lock:
                self.reports.append((event, track["Id"]))
        self.player = MusicPlayer(lambda track: self.files[track["Id"]], report, None, guard=False)

    def tearDown(self):
        self.player.stop()

    def run_until(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.player.update()
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_queue_plays_through_then_stops(self):
        self.player.play([{"Id": "0"}, {"Id": "1"}, {"Id": "2"}], start=1)
        self.assertTrue(self.run_until(lambda: not self.player.active))
        self.assertEqual(self.player.status, "Finished playing the queue")
        # A finished queue is reported so the saved queues can drop it.
        self.assertIsNone(self.player.last_session)
        self.assertEqual(queue_key(self.player.finished_session), ("0", "1", "2"))
        time.sleep(0.2)
        with self.lock:
            started = [track for event, track in self.reports if event == "started"]
            stopped = [track for event, track in self.reports if event == "stopped"]
        self.assertEqual(started, ["1", "2"])
        self.assertEqual(stopped, ["1", "2"])

    def test_skip_and_previous(self):
        self.player.play([{"Id": "0"}, {"Id": "1"}, {"Id": "2"}])
        self.assertTrue(self.run_until(lambda: self.player.started))
        self.player.next()
        self.assertEqual(self.player.current["Id"], "1")
        self.assertTrue(self.run_until(lambda: self.player.started))
        self.player.previous()
        self.assertEqual(self.player.current["Id"], "0")
        self.assertTrue(self.run_until(lambda: self.player.started))
        # A skip must not also count the previous track's eof as a second skip.
        self.assertEqual(self.player.queue.position, 0)

    def test_natural_track_changes_are_gapless_inside_mpv(self):
        replaces = []
        load = self.player._load
        self.player._load = lambda: (replaces.append(self.player.current["Id"]), load())
        tracks = [{"Id": str(i), "RunTimeTicks": 10000000} for i in range(3)]
        self.player.play(tracks)
        self.assertTrue(self.run_until(lambda: not self.player.active))
        # Only the first track was loaded by us; mpv moved on to the lined-up ones itself.
        self.assertEqual(replaces, ["0"])
        time.sleep(0.2)
        with self.lock:
            self.assertEqual([t for e, t in self.reports if e == "started"], ["0", "1", "2"])

    def test_sleep_at_end_of_track_stops_and_resumes_with_next(self):
        self.player.play([{"Id": "0"}, {"Id": "1"}, {"Id": "2"}])
        self.player.cycle_sleep(-1)
        self.assertEqual(self.player.sleep, "track")
        self.assertTrue(self.run_until(lambda: not self.player.active))
        self.assertEqual(self.player.status, "Sleep timer stopped the music")
        self.assertEqual(self.player.last_session["position"], 1)
        self.assertEqual(self.player.sleep, 0)

    def test_timed_sleep_stops_music(self):
        self.player.play([{"Id": "0"}, {"Id": "1"}, {"Id": "2"}])
        self.assertTrue(self.run_until(lambda: self.player.started))
        self.player.cycle_sleep(1)
        self.player.sleep_at = time.monotonic() + 0.3
        self.assertTrue(self.run_until(lambda: not self.player.active, timeout=3))
        self.assertEqual(self.player.status, "Sleep timer stopped the music")

    def test_resume_restores_track_and_position(self):
        long_tone = Path(self.directory.name) / "long.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=30",
                        str(long_tone)], check=True)
        self.files["long"] = str(long_tone)
        self.player.resume({"tracks": [{"Id": "0"}, {"Id": "long"}], "position": 1, "elapsed": 20})
        self.assertTrue(self.run_until(lambda: self.player.started and self.player.position()[0] >= 19))
        self.assertEqual(self.player.current["Id"], "long")

    def test_missing_file_is_skipped(self):
        self.files["bad"] = str(Path(self.directory.name) / "missing.wav")
        self.player.play([{"Id": "bad", "Name": "Broken"}, {"Id": "0"}])
        self.assertTrue(self.run_until(lambda: self.player.active and self.player.current["Id"] == "0"
                                       and self.player.started))
        self.assertIn("Broken", self.player.status)


if __name__ == "__main__":
    unittest.main()
