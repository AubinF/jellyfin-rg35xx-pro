from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import downloads
from downloads import Downloads, safe_name, storage_choices

FILES = {"t1": b"A" * 300000, "t2": b"B" * 200000, "t3": b"C" * 1000}


class FakeJellyfin(BaseHTTPRequestHandler):
    """/Items/<id>/Download with Range support; ids in `broken` answer 500."""
    broken = set()
    requests = []
    queries = []

    def do_GET(self):
        path, _, query = self.path.partition("?")
        item = path.split("/")[2]
        FakeJellyfin.requests.append((item, self.headers.get("Range"), self.headers.get("X-Emby-Token")))
        FakeJellyfin.queries.append((path, dict(pair.split("=", 1) for pair in query.split("&") if pair)))
        if "/Subtitles/" in path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")
            return
        if path.endswith("/stream.ts"):
            # A server conversion: no Content-Length, the body ends when encoding does.
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            self.wfile.write(FILES[item])
            return
        if item in self.broken or item not in FILES:
            self.send_response(500)
            self.end_headers()
            return
        body, start = FILES[item], 0
        if self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].rstrip("-"))
            self.send_response(206)
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body) - start))
        self.send_header("Content-Disposition", 'attachment; filename="%s.flac"' % item)
        self.end_headers()
        self.wfile.write(body[start:])

    def log_message(self, *args):
        pass


class DownloadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeJellyfin)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.index = os.path.join(self.root.name, "index.json")
        self.card = os.path.join(self.root.name, "card", "Jellyfin")
        FakeJellyfin.broken, FakeJellyfin.requests, FakeJellyfin.queries = set(), [], []
        self.managers = []
        # /tmp is RAM on some machines; treat the test folder as a real card.
        real = patch.object(downloads, "on_real_storage", return_value=True)
        real.start()
        self.addCleanup(real.stop)

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.root.cleanup()

    def manager(self):
        manager = Downloads(self.index, self.url, {"X-Emby-Token": "secret"}, True)
        self.managers.append(manager)
        return manager

    def tracks(self, *ids):
        return [{"Id": i, "Type": "Audio", "Name": "Song " + i, "IndexNumber": n + 1} for n, i in enumerate(ids)]

    def wait(self, manager, collection, state, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if manager.status(collection)["state"] == state:
                return True
            time.sleep(0.05)
        return False

    def test_album_downloads_with_readable_names_and_plays_from_disk(self):
        manager = self.manager()
        manager.add("alb", "Dune: Part 1", {"Id": "alb", "Name": "Dune: Part 1"}, self.tracks("t1", "t2"), self.card)
        self.assertTrue(self.wait(manager, "alb", "done"))
        path = manager.local_path("t1")
        # ":" is not allowed on FAT/exFAT cards.
        self.assertEqual(Path(path).relative_to(self.card).as_posix(), "Dune Part 1/01 Song t1 [t1].flac")
        self.assertEqual(Path(path).read_bytes(), FILES["t1"])
        self.assertEqual(manager.status("alb")["size"], 500000)
        self.assertEqual({token for _, _, token in FakeJellyfin.requests}, {"secret"})

    def test_partial_file_resumes_with_a_range_request(self):
        manager = self.manager()
        manager.close()  # keep the worker idle while we plant a partial file
        target = manager._target({"title": "Alb", "folder": self.card}, self.tracks("t1")[0]) + ".part"
        os.makedirs(os.path.dirname(target))
        Path(target).write_bytes(FILES["t1"][:120000])
        manager = self.manager()
        manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1"), self.card)
        self.assertTrue(self.wait(manager, "alb", "done"))
        self.assertIn(("t1", "bytes=120000-", "secret"), FakeJellyfin.requests)
        self.assertEqual(Path(manager.local_path("t1")).read_bytes(), FILES["t1"])

    def test_failure_is_reported_and_retry_finishes(self):
        FakeJellyfin.broken = {"t2"}
        manager = self.manager()
        with patch.object(downloads.time, "sleep"):  # skip retry back-off
            manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1", "t2"), self.card)
            self.assertTrue(self.wait(manager, "alb", "failed"))
        self.assertEqual(manager.status("alb")["done"], 1)
        FakeJellyfin.broken = set()
        manager.retry("alb")
        self.assertTrue(self.wait(manager, "alb", "done"))

    def test_full_card_pauses_the_queue(self):
        manager = self.manager()
        with patch.object(downloads, "free_bytes", return_value=0):
            manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1"), self.card)
            self.assertTrue(self.wait(manager, "alb", "paused"))
            self.assertIn("Not enough space", manager.summary())
        manager.retry("alb")
        self.assertTrue(self.wait(manager, "alb", "done"))

    def test_unfinished_downloads_continue_after_a_restart(self):
        FakeJellyfin.broken = {"t1"}
        manager = self.manager()
        with patch.object(downloads.time, "sleep"):
            manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1"), self.card)
            self.assertTrue(self.wait(manager, "alb", "failed"))
        manager.close()
        FakeJellyfin.broken = set()
        restarted = self.manager()
        self.assertTrue(self.wait(restarted, "alb", "done"))

    def test_remove_deletes_files_but_keeps_tracks_another_download_uses(self):
        manager = self.manager()
        manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1", "t3"), self.card)
        manager.add("pl", "Mix", {"Id": "pl"}, self.tracks("t3"), self.card)
        self.assertTrue(self.wait(manager, "alb", "done") and self.wait(manager, "pl", "done"))
        shared, own = manager.local_path("t3"), manager.local_path("t1")
        manager.remove("alb")
        self.assertFalse(os.path.exists(own))
        self.assertTrue(os.path.exists(shared))
        self.assertIsNone(manager.status("alb"))
        self.assertEqual([entry["id"] for entry in self.manager().listing()], ["pl"])


    def test_missing_files_are_fetched_again(self):
        manager = self.manager()
        manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1", "t3"), self.card)
        self.assertTrue(self.wait(manager, "alb", "done"))
        os.remove(manager.local_path("t1"))  # e.g. a card mounted over the folder
        manager.retry("alb")
        self.assertTrue(self.wait(manager, "alb", "done"))
        self.assertTrue(os.path.exists(manager.local_path("t1")))

    def test_folder_in_memory_pauses_instead_of_filling_ram(self):
        manager = self.manager()
        with patch.object(downloads, "on_real_storage", return_value=False):
            manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1"), self.card)
            self.assertTrue(self.wait(manager, "alb", "paused"))
            self.assertIn("is the card in?", manager.summary())
        self.assertFalse(os.path.exists(self.card))
        manager.retry("alb")
        self.assertTrue(self.wait(manager, "alb", "done"))


    def film(self, item_id="t1"):
        return {"Id": item_id, "Type": "Movie", "Name": "Dune: Part Two", "RunTimeTicks": 60 * 10000000,
                "MediaSources": [{"Id": "src-" + item_id, "Size": 9 * 1024 ** 3, "MediaStreams": [
                    {"Type": "Video", "Index": 0}, {"Type": "Audio", "Index": 1},
                    {"Type": "Subtitle", "Index": 2, "Language": "eng", "Codec": "subrip", "IsTextSubtitleStream": True},
                    {"Type": "Subtitle", "Index": 3, "Language": "fre", "Codec": "PGSSUB", "IsTextSubtitleStream": False}]}]}

    def test_converted_film_with_text_subtitles(self):
        manager = self.manager()
        options = {"mode": "converted", "quality": "480p", "audio_index": 1, "subtitle_index": 3}
        manager.add("film", "Dune: Part Two", self.film(), [self.film()], self.card, options)
        self.assertTrue(self.wait(manager, "film", "done"))
        path = manager.local_path("t1")
        self.assertTrue(path.endswith(".ts"))
        self.assertEqual(Path(path).read_bytes(), FILES["t1"])
        stream = next(q for p, q in FakeJellyfin.queries if p.endswith("/stream.ts"))
        self.assertEqual((stream["VideoCodec"], stream["MaxWidth"], stream["AudioStreamIndex"],
                          stream["SubtitleStreamIndex"], stream["SubtitleMethod"], stream["MediaSourceId"]),
                         ("h264", "854", "1", "3", "Encode", "src-t1"))
        # Only the text subtitle is saved, named so mpv loads it with the video.
        subs = manager.files["t1"]["subs"]
        self.assertEqual([Path(s).name for s in subs], [Path(path).stem + ".eng.srt"])
        self.assertIn(b"Hello", Path(subs[0]).read_bytes())
        manager.remove("film")
        self.assertFalse(os.path.exists(path) or os.path.exists(subs[0]))

    def test_original_film_uses_the_resumable_download(self):
        manager = self.manager()
        manager.add("film", "Film", self.film(), [self.film()], self.card, {"mode": "original"})
        self.assertTrue(self.wait(manager, "film", "done"))
        self.assertTrue(any(p.endswith("/t1/Download") for p, _ in FakeJellyfin.queries))
        self.assertTrue(manager.local_path("t1").endswith(".flac"))  # the server's own file name

    def test_video_plan_estimates_size_and_picks_text_subtitles(self):
        film = self.film()
        converted = downloads.video_plan(film, {"mode": "converted", "quality": "480p"})
        # (1.4 Mb/s video + 128 kb/s audio) for 60 s, plus 10%.
        self.assertEqual(converted["_size"], int((1400000 + 128000) / 8 * 60 * 1.1))
        self.assertEqual([s["index"] for s in converted["_subs"]], [2])
        self.assertEqual(downloads.video_plan(film, {"mode": "original"})["_size"], 9 * 1024 ** 3)

    def test_offline_progress_survives_a_restart_until_synced(self):
        path = os.path.join(self.root.name, "progress.json")
        progress = downloads.OfflineProgress(path)
        progress.record({"Id": "ep1", "Name": "Pilot", "Type": "Episode"}, 12345, False)
        progress.record({"Id": "ep2", "Type": "Episode"}, 999, True)
        again = downloads.OfflineProgress(path)
        self.assertEqual((again.position("ep1"), again.position("ep2")), (12345, 0))
        again.done("ep1")
        self.assertEqual([e["id"] for e in downloads.OfflineProgress(path).pending()], ["ep2"])


    def test_files_deleted_by_hand_are_not_downloaded_again_until_asked(self):
        manager = self.manager()
        manager.add("alb", "Alb", {"Id": "alb"}, self.tracks("t1", "t3"), self.card)
        self.assertTrue(self.wait(manager, "alb", "done"))
        os.remove(manager.local_path("t1"))
        manager.close()
        restarted = self.manager()
        time.sleep(0.3)
        status = restarted.status("alb")
        self.assertEqual((status["state"], status["missing"]), ("missing", 1))
        self.assertEqual(sum(1 for item, _, _ in FakeJellyfin.requests if item == "t1"), 1)  # not fetched again
        restarted.retry("alb")  # Y: get it back on purpose
        self.assertTrue(self.wait(restarted, "alb", "done"))

    def test_set_items_swaps_watched_episodes_for_the_next_ones(self):
        manager = self.manager()
        episodes = [dict(t, Type="Episode") for t in self.tracks("t1", "t2", "t3")]
        manager.set_items("series-x", "Show · next", {"Id": "x"}, episodes[:2], self.card, {"mode": "original"})
        self.assertTrue(self.wait(manager, "series-x", "done"))
        first = manager.local_path("t1")
        manager.set_items("series-x", "Show · next", {"Id": "x"}, episodes[1:], self.card)
        self.assertTrue(self.wait(manager, "series-x", "done"))
        self.assertFalse(os.path.exists(first))           # watched one deleted
        self.assertTrue(manager.local_path("t2") and manager.local_path("t3"))
        self.assertEqual(sum(1 for item, _, _ in FakeJellyfin.requests if item == "t2"), 1)  # kept, not refetched

    def test_next_episodes_start_at_the_first_unwatched(self):
        episodes = [{"Id": str(i), "UserData": {"Played": i < 2}} for i in range(6)]
        self.assertEqual([e["Id"] for e in downloads.next_episodes(episodes, 3)], ["2", "3", "4"])
        self.assertEqual(downloads.next_episodes([{"Id": "a", "UserData": {"Played": True}}], 3), [])

    def test_followed_series_are_remembered(self):
        manager = self.manager()
        manager.follow_series({"Id": "x", "Name": "Show", "Type": "Series"}, 3)
        self.assertEqual(self.manager().series["x"]["count"], 3)
        manager.unfollow_series("x")
        self.assertNotIn("x", self.manager().series)


class StorageTests(unittest.TestCase):
    def test_lists_userdata_and_extra_cards_but_not_system_mounts(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as mounts:
            mounts.write("\n".join([
                "/dev/mmcblk0p3 /boot vfat rw 0 0",
                "/dev/loop0 /overlay/base squashfs ro 0 0",
                "overlay / overlay rw 0 0",
                "/dev/mmcblk0p4 /userdata ext4 rw 0 0",
                "/dev/mmcblk1p1 /media/MUSIC\\040CARD exfat rw 0 0",
                "/dev/mmcblk0p4 /media/SHARE ext4 rw 0 0",   # same partition again
                "/dev/sda1 /media/USB vfat rw 0 0"]))
        try:
            with patch.object(downloads, "free_bytes", return_value=5):
                choices = storage_choices(mounts.name)
        finally:
            os.remove(mounts.name)
        self.assertEqual([(c["label"], c["path"]) for c in choices],
                         [("SD card · MUSIC CARD", "/media/MUSIC CARD/Jellyfin"),
                          ("USB drive · USB", "/media/USB/Jellyfin"),
                          ("Internal storage", "/userdata/Jellyfin")])

    def test_unmounted_card_folder_is_recognised_as_memory(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as mounts:
            mounts.write("overlay / overlay rw 0 0\n/dev/mmcblk0p4 /userdata exfat rw 0 0\n"
                         "tmpfs /media tmpfs rw 0 0\n")
        try:
            self.assertFalse(downloads.on_real_storage("/media/MEDIA/Jellyfin", mounts.name))
            self.assertTrue(downloads.on_real_storage("/userdata/Jellyfin", mounts.name))
            with open(mounts.name, "a") as more:
                more.write("/dev/mmcblk1p1 /media/MEDIA ext4 rw 0 0\n")
            self.assertTrue(downloads.on_real_storage("/media/MEDIA/Jellyfin", mounts.name))
        finally:
            os.remove(mounts.name)

    def test_safe_name(self):
        self.assertEqual(safe_name('AC/DC: "Live"?', "x"), "AC DC Live")
        self.assertEqual(safe_name("...", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
