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

    def do_GET(self):
        item = self.path.split("/")[2]
        FakeJellyfin.requests.append((item, self.headers.get("Range"), self.headers.get("X-Emby-Token")))
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
        FakeJellyfin.broken, FakeJellyfin.requests = set(), []
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
