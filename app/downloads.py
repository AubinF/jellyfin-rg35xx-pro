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
VIDEO_TYPES = ("Movie", "Episode", "Video", "MusicVideo")
DOWNLOADABLE = ("Audio", "Book") + VIDEO_TYPES
# Streaming profiles shared with playback: quality -> (width, height, video bitrate, max bitrate).
VIDEO_PROFILES = {"360p": (640, 360, 900000, 1200000), "480p": (854, 480, 1400000, 1800000),
                  "720p": (1280, 720, 2800000, 3400000), "1080p": (1920, 1080, 5000000, 6000000)}
AUDIO_BITRATE = 128000
VIDEO_FIELDS = ("Id", "Name", "Type", "SeriesName", "SeriesId", "SeasonId", "SeasonName",
                "ParentIndexNumber", "IndexNumber", "RunTimeTicks", "ProductionYear", "Overview",
                "ImageTags", "ParentThumbItemId", "ParentThumbImageTag", "SeriesPrimaryImageTag",
                "ParentBackdropItemId", "BackdropImageTags", "UserData", "MediaSourceId")
TEXT_SUBTITLES = ("srt", "subrip", "ass", "ssa", "vtt", "webvtt", "mov_text", "text")


def video_plan(item, options):
    """What to fetch for a film or episode: its media source, the text subtitles to
    save next to it, and the expected size (for the space check and progress)."""
    sources = item.get("MediaSources") or [{}]
    source = sources[0]
    streams = source.get("MediaStreams") or item.get("MediaStreams") or []
    subtitles = [{"index": s["Index"], "lang": safe_name(s.get("Language") or "", "") or str(s["Index"])}
                 for s in streams if s.get("Type") == "Subtitle" and s.get("Index") is not None
                 and (s.get("IsTextSubtitleStream") or str(s.get("Codec", "")).lower() in TEXT_SUBTITLES)]
    seconds = (item.get("RunTimeTicks") or 0) / 10000000
    if options.get("mode") == "original":
        size = source.get("Size") or 0
    else:
        _, _, video_bitrate, _ = VIDEO_PROFILES.get(options.get("quality"), VIDEO_PROFILES["480p"])
        size = int((video_bitrate + AUDIO_BITRATE) / 8 * seconds * 1.1)  # 10% container overhead
    return {"_source": source.get("Id") or item["Id"], "_subs": subtitles, "_size": size}


def slim(item, options):
    """The fields kept for offline use (playing, drawing, reporting)."""
    if item.get("Type") in VIDEO_TYPES:
        kept = {k: item[k] for k in VIDEO_FIELDS if k in item}
        kept.update(video_plan(item, options))
        return kept
    if item.get("Type") == "Book":
        return {k: item[k] for k in ("Id", "Name", "Type", "ImageTags", "Overview", "ProductionYear", "UserData")
                if k in item}
    return {k: item[k] for k in SAVED_FIELDS + ("Container", "IndexNumber") if k in item}
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


class OfflineProgress:
    """Positions and finished plays recorded without a server, so Jellyfin's resume
    points, watched marks and Next Up catch up the next time the app is online."""
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as source:
                self.entries = dict(json.load(source))
        except (OSError, ValueError, TypeError):
            self.entries = {}

    def _save(self):
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as target:
                json.dump(self.entries, target)
            os.replace(self.path + ".tmp", self.path)
        except OSError as exc:
            LOG.warning("Could not save offline progress (%s)", type(exc).__name__)

    def record(self, item, ticks, played):
        keep = ("Id", "Name", "Type", "RunTimeTicks", "MediaSourceId")
        self.entries[item["Id"]] = {"item": {k: item[k] for k in keep if k in item},
                                    "ticks": int(ticks or 0), "played": bool(played), "at": int(time.time())}
        self._save()

    def position(self, item_id):
        """The last offline position, for resuming while still offline."""
        entry = self.entries.get(item_id)
        return None if not entry else (0 if entry["played"] else entry["ticks"])

    def pending(self):
        return [dict(entry, id=item_id) for item_id, entry in self.entries.items()]

    def done(self, item_id):
        if self.entries.pop(item_id, None) is not None:
            self._save()


def next_episodes(episodes, count):
    """The `count` episodes from the first unwatched one on, in series order."""
    first = next((i for i, e in enumerate(episodes) if not (e.get("UserData") or {}).get("Played")), None)
    return [] if first is None else episodes[first:first + count]


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
        self.missing = set()     # downloaded files no longer on the card (deleted by hand?)
        self.missing_checked = 0
        self._load()
        self._scan_missing()
        for collection_id, entry in self.collections.items():
            # Resume unfinished downloads after a restart, but don't bring back files
            # the user deleted: those show as missing until Y asks for them again.
            self._enqueue(collection_id, entry, refetch=False)
        self.worker = threading.Thread(target=self._run, name="jellyfin-downloads", daemon=True)
        self.worker.start()

    # ---- index -----------------------------------------------------------
    def _load(self):
        try:
            with open(self.index_path, encoding="utf-8") as source:
                value = json.load(source)
            self.collections = dict(value.get("collections") or {})
            self.files = dict(value.get("files") or {})
            self.series = dict(value.get("series") or {})
        except (OSError, ValueError, AttributeError):
            self.collections, self.files, self.series = {}, {}, {}

    def _scan_missing(self):
        self.missing = {i for i, f in self.files.items() if not os.path.exists(f["path"])}
        self.missing_checked = time.monotonic()

    def _save(self):
        value = {"collections": self.collections, "files": self.files, "series": self.series}
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
            if time.monotonic() - self.missing_checked > 5:
                self._scan_missing()  # notices a card mounted or files deleted meanwhile
            entries = sorted(self.collections.items(), key=lambda kv: -kv[1].get("added_at", 0))
            return [dict(entry, id=collection_id, **self._status(collection_id, entry))
                    for collection_id, entry in entries]

    def status(self, collection_id):
        with self.lock:
            entry = self.collections.get(collection_id)
            return self._status(collection_id, entry) if entry else None

    def _status(self, collection_id, entry):
        ids = [track["Id"] for track in entry["tracks"]]
        missing = [i for i in ids if i in self.missing]
        done = [i for i in ids if i in self.files and i not in self.missing]
        size = sum(self.files[i].get("size", 0) for i in done)
        progress = 0
        if self.current and self.current[0] == collection_id and self.current[3]:
            progress = min(1, self.current[2] / self.current[3])  # conversions only have an estimate
        waiting = any(cid == collection_id for cid, _ in self.pending)
        active = bool(self.current and self.current[0] == collection_id)
        if len(done) == len(ids):
            state = "done"
        elif missing and not (active or waiting):
            state = "missing"
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
        return {"done": len(done), "total": len(ids), "size": size, "state": state, "missing": len(missing),
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
                percent = min(99, int(100 * self.current[2] / self.current[3])) if self.current[3] else 0
                return "Downloading %s · %d/%d · %d%%" % (entry.get("title", ""), done + 1, len(ids), percent)
            return "Queued" if self.pending else ""

    # ---- changes (UI thread) ---------------------------------------------
    def add(self, collection_id, title, parent, tracks, folder, options=None):
        """Download every item of an album, playlist, film or season into folder.
        options (video): mode converted/original, quality, audio_index, subtitle_index."""
        options = dict(options or {})
        tracks = [slim(t, options) for t in tracks if t.get("Id") and t.get("Type") in DOWNLOADABLE]
        with self.lock:
            entry = self.collections.get(collection_id)
            if entry is None:
                entry = {"title": title, "parent": {k: parent[k] for k in ("Id", "Name", "Type", "ImageTags", "SeriesId")
                                                    if k in parent},
                         "tracks": tracks, "folder": folder, "added_at": int(time.time()), "options": options}
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
            self._delete_unused(entry, entry["tracks"])
            self._save()

    def _delete_unused(self, entry, tracks):
        """Delete the files of tracks that no remaining collection still lists."""
        still_used = {t["Id"] for e in self.collections.values() for t in e["tracks"]}
        for track in tracks:
            if track["Id"] in still_used:
                continue
            known = self.files.pop(track["Id"], {})
            self.missing.discard(track["Id"])
            for leftover in [known.get("path"), self._target(entry, track) + ".part"] + known.get("subs", []):
                if leftover:
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
        try:
            os.rmdir(self._folder(entry))  # only when empty
        except OSError:
            pass

    def set_items(self, collection_id, title, parent, items, folder, options=None):
        """Make a collection hold exactly these items (e.g. a series' next episodes):
        new ones are queued, ones that dropped out are deleted unless used elsewhere."""
        with self.lock:
            entry = self.collections.get(collection_id)
        if entry is None:
            self.add(collection_id, title, parent, items, folder, options)
            return
        options = dict(options or entry.get("options") or {})
        fresh = [slim(t, options) for t in items if t.get("Id") and t.get("Type") in DOWNLOADABLE]
        with self.lock:
            keep = {t["Id"] for t in fresh}
            dropped = [t for t in entry["tracks"] if t["Id"] not in keep]
            entry.update(title=title, tracks=fresh, options=options)
            self.pending = collections.deque((c, t) for c, t in self.pending
                                             if c != collection_id or t["Id"] in keep)
            if dropped and self.current and self.current[0] == collection_id and self.current[1] not in keep:
                self.cancelled.add(collection_id)
            self._delete_unused(entry, dropped)
            self._enqueue(collection_id, entry)
            self._save()
            self.lock.notify()

    def follow_series(self, series, count):
        """Remember to keep the next `count` episodes of series downloaded."""
        with self.lock:
            self.series[series["Id"]] = {"name": series.get("Name") or "Series", "count": count,
                                         "parent": {k: series[k] for k in ("Id", "Name", "Type", "ImageTags") if k in series}}
            self._save()

    def unfollow_series(self, series_id):
        with self.lock:
            self.series.pop(series_id, None)
            self._save()
        self.remove("series-" + series_id)

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
    def _enqueue(self, collection_id, entry, refetch=True):
        # Files can disappear behind the index's back: deleted by hand, or a card
        # swapped. When asked (add, Y), fetch them again rather than trust the index.
        for track in entry["tracks"]:
            known = self.files.get(track["Id"])
            if refetch and known and not os.path.exists(known["path"]):
                del self.files[track["Id"]]
                self.missing.discard(track["Id"])
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

    def _source(self, entry, track):
        """(url, params, resumable) for one item. Music and "original" video use the
        resumable download endpoint; converted video is encoded by the server like a
        stream (H.264/AAC in MPEG-TS, which plays and seeks well without an index)."""
        options = entry.get("options") or {}
        if track.get("Type") not in VIDEO_TYPES or options.get("mode") == "original":
            return self.server + "/Items/" + track["Id"] + "/Download", None, True
        width, height, video_bitrate, _ = VIDEO_PROFILES.get(options.get("quality"), VIDEO_PROFILES["480p"])
        params = {"Static": "false", "MediaSourceId": track.get("_source") or track["Id"],
                  "DeviceId": "rg35xxh-download", "PlaySessionId": "download-" + track["Id"],
                  "VideoCodec": "h264", "AudioCodec": "aac", "MaxWidth": width, "MaxHeight": height,
                  "VideoBitrate": video_bitrate, "AudioBitrate": AUDIO_BITRATE,
                  "MaxAudioChannels": 2, "TranscodingMaxAudioChannels": 2}
        if options.get("audio_index") is not None:
            params["AudioStreamIndex"] = options["audio_index"]
        if options.get("subtitle_index") is not None:
            # Only image subtitles are burned in; text ones are saved as files instead.
            params.update(SubtitleStreamIndex=options["subtitle_index"], SubtitleMethod="Encode")
        return self.server + "/Videos/" + track["Id"] + "/stream.ts", params, False

    def _fetch_subtitles(self, track, base):
        """Save text subtitles as <video name>.<language>.srt; mpv loads them by name."""
        saved = []
        for sub in track.get("_subs", []):
            path = "%s.%s.srt" % (base, sub["lang"])
            if path in saved:
                path = "%s.%s-%d.srt" % (base, sub["lang"], sub["index"])
            url = "%s/Videos/%s/%s/Subtitles/%d/0/Stream.srt" % (
                self.server, track["Id"], track.get("_source") or track["Id"], sub["index"])
            try:
                with self.http.get(url, headers=self.headers, timeout=(6, 30), verify=self.verify) as response:
                    response.raise_for_status()
                    with open(path, "wb") as target:
                        target.write(response.content)
                saved.append(path)
            except (requests.RequestException, OSError) as exc:
                LOG.warning("Subtitle download failed (%s)", type(exc).__name__)  # the video still plays
        return saved

    def has_file(self, track_id):
        """Cheap check (no disk access) for drawing lists every frame."""
        return track_id in self.files

    def _fetch(self, collection_id, entry, track):
        """Download one track. Returns done, failed, paused or cancelled."""
        base = self._target(entry, track)
        part = base + ".part"
        url, params, resumable = self._source(entry, track)
        if not on_real_storage(entry["folder"]):
            # The card is missing (removed, or not mounted after a reboot): don't fill memory.
            with self.lock:
                self.paused = "Storage not available: is the card in? (" + entry["folder"] + ")"
            LOG.warning("Downloads paused: %s is not on a card", entry["folder"])
            return "paused"
        for attempt in range(1, RETRIES + 1):
            try:
                os.makedirs(os.path.dirname(part), exist_ok=True)
                if not resumable and os.path.exists(part):
                    os.remove(part)  # a server conversion can't continue mid-file: start over
                have = os.path.getsize(part) if os.path.exists(part) else 0
                headers = dict(self.headers, **({"Range": "bytes=%d-" % have} if have else {}))
                # Conversions stream as the server encodes: allow long gaps between chunks.
                with self.http.get(url, params=params, headers=headers, stream=True,
                                   timeout=(6, 30 if resumable else 120), verify=self.verify) as response:
                    if response.status_code == 416:
                        have = 0  # the server no longer matches our partial file
                        os.remove(part)
                        continue
                    response.raise_for_status()
                    if response.status_code != 206:
                        have = 0
                    if "application/json" in response.headers.get("Content-Type", ""):
                        raise requests.HTTPError("server sent an error instead of media")
                    # A conversion has no Content-Length: use the estimate for space and progress.
                    remaining = int(response.headers.get("Content-Length") or 0) or track.get("_size", 0)
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
                    path = base + (file_extension(response, track) if resumable else ".ts")
                os.replace(part, path)
                subs = self._fetch_subtitles(track, base)
                with self.lock:
                    self.files[track["Id"]] = {"path": path, "size": os.path.getsize(path), "subs": subs}
                LOG.info("Downloaded %s %s", track.get("Type", "item").lower(), track["Id"][:8])
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
