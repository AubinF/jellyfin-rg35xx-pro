"""Offline copies of albums, playlists and audiobooks.

A single background worker fetches original files through Jellyfin's
/Items/{id}/Download endpoint. Partial files resume with HTTP ranges, so a
dropped Wi-Fi connection or a restart continues where it stopped. The index
(what was downloaded, and where) lives in the app's data folder, so files in
an older location stay playable after the download location changes.
"""
import collections
import json
import logging
import os
import re
import threading
import time

import requests

from music import SAVED_FIELDS

LOG = logging.getLogger("jellyfin.downloads")
FOLDER_NAME = "Jellyfin"
# Never fill a card to the brim: the system and saves need room too.
SPACE_MARGIN = 200 * 1024 * 1024
CHUNK = 256 * 1024
RETRIES = 3


def free_bytes(path):
    """Free space where path is (or would be created)."""
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    try:
        stat = os.statvfs(path or "/")
    except OSError:
        return 0
    return stat.f_bavail * stat.f_frsize


MEMORY_FILESYSTEMS = ("tmpfs", "ramfs", "devtmpfs", "overlay", "squashfs")


def mount_type(path, mounts="/proc/mounts"):
    """Filesystem type of the mount that holds path (the longest matching mount point)."""
    path = os.path.realpath(path)
    best, kind = "", ""
    try:
        with open(mounts, encoding="utf-8") as source:
            for line in source:
                fields = line.split()
                if len(fields) < 3:
                    continue
                point = fields[1].replace("\\040", " ")
                inside = path == point or path.startswith(point.rstrip("/") + "/")
                if inside and len(point) >= len(best):
                    best, kind = point, fields[2]
    except OSError:
        pass
    return kind


def on_real_storage(path, mounts="/proc/mounts"):
    """False when path is only in memory, e.g. /media/<card> while that card is not
    mounted: KNULLI's /media is a tmpfs, so files there vanish and space is tiny."""
    return mount_type(path, mounts) not in MEMORY_FILESYSTEMS


def storage_choices(mounts="/proc/mounts"):
    """Places to keep downloads: the userdata partition plus any extra SD card or
    USB drive (KNULLI mounts those under /media). Returns label/path/free dicts."""
    choices, devices = [], set()
    try:
        with open(mounts, encoding="utf-8") as source:
            lines = source.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        device, point = fields[0], fields[1].replace("\\040", " ")
        if not device.startswith("/dev/") or device.startswith("/dev/loop") or device in devices:
            continue
        if not (point == "/userdata" or point.startswith("/media/") or point.startswith("/mnt/")):
            continue
        devices.add(device)
        if point == "/userdata":
            label = "Internal storage"
        elif "/mmcblk" in device:
            label = "SD card · " + os.path.basename(point)
        else:
            label = "USB drive · " + os.path.basename(point)
        choices.append({"label": label, "path": os.path.join(point, FOLDER_NAME), "free": free_bytes(point)})
    # Removable storage first: on the RG35XX H the system card is the internal one.
    choices.sort(key=lambda choice: choice["label"] == "Internal storage")
    return choices


def safe_name(value, fallback):
    """A file or folder name that FAT/exFAT cards accept."""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(value or "")).strip().rstrip(". ")
    return re.sub(r"\s+", " ", value)[:80] or fallback


def file_extension(response, track):
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    if match and "." in match.group(1):
        extension = match.group(1).rsplit(".", 1)[1].lower()
        if re.fullmatch(r"[a-z0-9]{1,5}", extension):
            return "." + extension
    container = str(track.get("Container") or "").split(",")[0].strip().lower()
    return "." + container if re.fullmatch(r"[a-z0-9]{1,5}", container) else ".audio"


class Downloads:
    def __init__(self, index_path, server, headers, verify, session=None, offline=False):
        self.index_path = index_path
        self.server, self.headers, self.verify = server, headers, verify
        self.http = session or requests
        self.lock = threading.Condition()
        self.collections = {}   # id -> title, parent, tracks, folder, added_at
        self.files = {}         # track id -> path, size (complete files only)
        self.pending = collections.deque()  # (collection id, track) waiting to download
        self.errors = {}        # collection id -> message
        self.current = None     # collection id, track id, received, total
        self.paused = ""        # why the whole queue stopped (e.g. no space)
        self.cancelled = set()  # collection ids the worker must abandon
        self.closed = False
        self.offline = offline  # no server: keep the queue, wait instead of failing
        self._load()
        for collection_id, entry in self.collections.items():
            self._enqueue(collection_id, entry)  # resume after a restart
        self.worker = threading.Thread(target=self._run, name="jellyfin-downloads", daemon=True)
        self.worker.start()

    # ---- index -----------------------------------------------------------
    def _load(self):
        try:
            with open(self.index_path, encoding="utf-8") as source:
                value = json.load(source)
            self.collections = dict(value.get("collections") or {})
            self.files = dict(value.get("files") or {})
        except (OSError, ValueError, AttributeError):
            self.collections, self.files = {}, {}

    def _save(self):
        value = {"collections": self.collections, "files": self.files}
        try:
            with open(self.index_path + ".tmp", "w", encoding="utf-8") as target:
                json.dump(value, target)
            os.replace(self.index_path + ".tmp", self.index_path)
        except OSError as exc:
            LOG.warning("Could not save the downloads index (%s)", type(exc).__name__)

    # ---- queries (UI thread) ---------------------------------------------
    def local_path(self, track_id):
        """The downloaded file for a track, if it is still on the card."""
        entry = self.files.get(track_id)
        return entry["path"] if entry and os.path.exists(entry["path"]) else None

    def listing(self):
        with self.lock:
            entries = sorted(self.collections.items(), key=lambda kv: -kv[1].get("added_at", 0))
            return [dict(entry, id=collection_id, **self._status(collection_id, entry))
                    for collection_id, entry in entries]

    def status(self, collection_id):
        with self.lock:
            entry = self.collections.get(collection_id)
            return self._status(collection_id, entry) if entry else None

    def _status(self, collection_id, entry):
        ids = [track["Id"] for track in entry["tracks"]]
        done = [i for i in ids if i in self.files]
        size = sum(self.files[i].get("size", 0) for i in done)
        progress = 0
        if self.current and self.current[0] == collection_id and self.current[3]:
            progress = self.current[2] / self.current[3]
        waiting = any(cid == collection_id for cid, _ in self.pending)
        active = bool(self.current and self.current[0] == collection_id)
        if len(done) == len(ids):
            state = "done"
        elif collection_id in self.errors:
            state = "failed"
        elif self.paused and (active or waiting):
            state = "paused"
        elif active:
            state = "downloading"
        elif waiting:
            state = "queued"
        else:
            state = "incomplete"
        return {"done": len(done), "total": len(ids), "size": size, "state": state,
                "progress": progress, "error": self.errors.get(collection_id, self.paused)}

    def summary(self):
        """One line for the Downloads screen header."""
        with self.lock:
            if self.paused:
                return self.paused
            if self.offline and self.pending:
                return "Offline: downloads continue when the server is back"
            if self.current:
                entry = self.collections.get(self.current[0], {})
                ids = [t["Id"] for t in entry.get("tracks", [])]
                done = sum(1 for i in ids if i in self.files)
                percent = int(100 * self.current[2] / self.current[3]) if self.current[3] else 0
                return "Downloading %s · %d/%d · %d%%" % (entry.get("title", ""), done + 1, len(ids), percent)
            return "Queued" if self.pending else ""

    # ---- changes (UI thread) ---------------------------------------------
    def add(self, collection_id, title, parent, tracks, folder):
        """Download every track of an album/playlist into folder."""
        tracks = [{k: t[k] for k in SAVED_FIELDS + ("Container", "IndexNumber") if k in t}
                  for t in tracks if t.get("Id") and t.get("Type") == "Audio"]
        with self.lock:
            entry = self.collections.get(collection_id)
            if entry is None:
                entry = {"title": title, "parent": {k: parent[k] for k in ("Id", "Name", "Type", "ImageTags") if k in parent},
                         "tracks": tracks, "folder": folder, "added_at": int(time.time())}
                self.collections[collection_id] = entry
            self.errors.pop(collection_id, None)
            self.cancelled.discard(collection_id)
            self.paused = ""
            self._enqueue(collection_id, entry)
            self._save()
            self.lock.notify()

    def retry(self, collection_id):
        """Retry failures and re-fetch any file that is no longer on the card."""
        with self.lock:
            entry = self.collections.get(collection_id)
            if entry:
                self.errors.pop(collection_id, None)
                self.paused = ""
                self._enqueue(collection_id, entry)
                self._save()
                self.lock.notify()

    def remove(self, collection_id):
        """Forget a collection and delete its files that no other download uses."""
        with self.lock:
            entry = self.collections.pop(collection_id, None)
            if not entry:
                return
            self.pending = collections.deque((c, t) for c, t in self.pending if c != collection_id)
            self.errors.pop(collection_id, None)
            if self.current and self.current[0] == collection_id:
                self.cancelled.add(collection_id)
            still_used = {t["Id"] for e in self.collections.values() for t in e["tracks"]}
            for track in entry["tracks"]:
                if track["Id"] in still_used:
                    continue
                path = self.files.pop(track["Id"], {}).get("path")
                for leftover in (path, self._target(entry, track) + ".part"):
                    if leftover:
                        try:
                            os.remove(leftover)
                        except OSError:
                            pass
            folder = self._folder(entry)
            try:
                os.rmdir(folder)  # only when empty
            except OSError:
                pass
            self._save()

    def set_offline(self, offline):
        """Pause the worker without a server; going online continues the queue."""
        with self.lock:
            self.offline = offline
            self.lock.notify()

    def connect(self, server, headers):
        """New login (fresh token) for the same account: keep the index and queue."""
        with self.lock:
            self.server, self.headers = server, headers
        self.set_offline(False)

    def close(self):
        with self.lock:
            self.closed = True
            self.lock.notify()

    # ---- worker ------------------------------------------------------------
    def _enqueue(self, collection_id, entry):
        # Files can disappear behind the index's back: a card swapped or mounted over
        # the folder they were written to. Fetch those again rather than trust the index.
        for track in entry["tracks"]:
            known = self.files.get(track["Id"])
            if known and not os.path.exists(known["path"]):
                del self.files[track["Id"]]
        queued = {t["Id"] for c, t in self.pending if c == collection_id}
        for track in entry["tracks"]:
            if track["Id"] not in self.files and track["Id"] not in queued:
                self.pending.append((collection_id, track))

    def _folder(self, entry):
        return os.path.join(entry["folder"], safe_name(entry["title"], "Download"))

    def _target(self, entry, track):
        """Readable names on the card: <folder>/<Album>/<NN> <Title>.<ext> (ext added later)."""
        number = track.get("IndexNumber")
        prefix = "%02d " % number if isinstance(number, int) else ""
        name = safe_name(prefix + (track.get("Name") or ""), track["Id"])
        return os.path.join(self._folder(entry), name + " [" + track["Id"][:8] + "]")

    def _run(self):
        while True:
            with self.lock:
                self.lock.wait_for(lambda: self.closed or (self.pending and not self.paused and not self.offline))
                if self.closed:
                    return
                collection_id, track = self.pending[0]
                entry = self.collections.get(collection_id)
                if entry is None or track["Id"] in self.files:
                    self.pending.popleft()
                    continue
                self.current = [collection_id, track["Id"], 0, 0]
            outcome = self._fetch(collection_id, entry, track)
            with self.lock:
                self.current = None
                if self.pending and self.pending[0] == (collection_id, track):
                    if outcome != "paused":
                        self.pending.popleft()
                if outcome == "failed":
                    # Leave the rest of this collection for a retry; move on to others.
                    self.pending = collections.deque((c, t) for c, t in self.pending if c != collection_id)
                self.cancelled.discard(collection_id)
                self._save()

    def _fetch(self, collection_id, entry, track):
        """Download one track. Returns done, failed, paused or cancelled."""
        base = self._target(entry, track)
        part = base + ".part"
        url = self.server + "/Items/" + track["Id"] + "/Download"
        if not on_real_storage(entry["folder"]):
            # The card is missing (removed, or not mounted after a reboot): don't fill memory.
            with self.lock:
                self.paused = "Storage not available: is the card in? (" + entry["folder"] + ")"
            LOG.warning("Downloads paused: %s is not on a card", entry["folder"])
            return "paused"
        for attempt in range(1, RETRIES + 1):
            try:
                os.makedirs(os.path.dirname(part), exist_ok=True)
                have = os.path.getsize(part) if os.path.exists(part) else 0
                headers = dict(self.headers, **({"Range": "bytes=%d-" % have} if have else {}))
                with self.http.get(url, headers=headers, stream=True, timeout=(6, 30),
                                   verify=self.verify) as response:
                    if response.status_code == 416:
                        have = 0  # the server no longer matches our partial file
                        os.remove(part)
                        continue
                    response.raise_for_status()
                    if response.status_code != 206:
                        have = 0
                    remaining = int(response.headers.get("Content-Length") or 0)
                    total = have + remaining
                    if remaining and free_bytes(part) < remaining + SPACE_MARGIN:
                        with self.lock:
                            self.paused = "Not enough space in " + entry["folder"]
                        LOG.warning("Downloads paused: not enough space")
                        return "paused"
                    with self.lock:
                        self.current[2:] = [have, total]
                    with open(part, "ab" if have else "wb") as target:
                        for chunk in response.iter_content(CHUNK):
                            if self.closed or collection_id in self.cancelled:
                                return "cancelled"
                            target.write(chunk)
                            with self.lock:
                                self.current[2] += len(chunk)
                    path = base + file_extension(response, track)
                os.replace(part, path)
                with self.lock:
                    self.files[track["Id"]] = {"path": path, "size": os.path.getsize(path)}
                LOG.info("Downloaded track %s", track["Id"][:8])
                return "done"
            except (requests.RequestException, OSError) as exc:
                # requests errors may include a URL/token: only log the class.
                LOG.warning("Download attempt %d failed (%s)", attempt, type(exc).__name__)
                if self.closed:
                    return "cancelled"
                time.sleep(min(30, 3 * attempt))
        with self.lock:
            self.errors[collection_id] = "Download failed (network or server error)"
        return "failed"
