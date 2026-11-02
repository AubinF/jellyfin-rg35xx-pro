"""Background music: one idle mpv process, a play queue, shuffle, repeat and sleep."""
import json
import logging
import os
import random
import subprocess
import sys
import time
import uuid

import playback
from playback import MpvIPC, PlaybackReporter, stop_process

LOG = logging.getLogger("jellyfin.music")
REPEAT_MODES = ("off", "all", "one")
# Minutes, or "track" to stop when the current track ends.
SLEEP_CHOICES = (0, 15, 30, 45, 60, 90, "track")
SLEEP_FADE = 15
# Enough of each track to play, report and draw it after a restart.
SAVED_FIELDS = ("Id", "Name", "Type", "Artists", "AlbumArtist", "Album", "AlbumId",
                "AlbumPrimaryImageTag", "ImageTags", "RunTimeTicks", "MediaSourceId", "UserData")


class Queue:
    """Play order over a fixed track list; toggling shuffle keeps the current track."""
    def __init__(self, tracks, start=0, title=None):
        self.tracks = list(tracks)
        self.order = list(range(len(self.tracks)))
        self.position = max(0, min(start, len(self.tracks) - 1))
        self.shuffle = False
        self.repeat = "off"
        # What the queue was started from (album, playlist, shelf), for the saved-queues list.
        self.title = title

    @property
    def current(self):
        return self.tracks[self.order[self.position]] if self.tracks else None

    def upcoming(self):
        """Tracks in the order they will play, for the queue view."""
        return [self.tracks[index] for index in self.order]

    def set_shuffle(self, enabled):
        playing = self.order[self.position]
        self.shuffle = enabled
        if enabled:
            rest = [index for index in range(len(self.tracks)) if index != playing]
            random.shuffle(rest)
            self.order, self.position = [playing] + rest, 0
        else:
            self.order, self.position = list(range(len(self.tracks))), playing

    def cycle_repeat(self):
        self.repeat = REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % len(REPEAT_MODES)]

    def advance(self, step, automatic=False):
        """Move step tracks. False means the queue ended (or there is no previous)."""
        if automatic and self.repeat == "one":
            return True
        target = self.position + step
        if 0 <= target < len(self.order):
            self.position = target
            return True
        if self.repeat == "all" and self.order:
            self.position = target % len(self.order)
            return True
        return False

    def peek(self):
        """The track uninterrupted playback reaches next, without moving."""
        if self.repeat == "one":
            return self.current
        if self.position + 1 < len(self.order):
            return self.tracks[self.order[self.position + 1]]
        return self.tracks[self.order[0]] if self.repeat == "all" and self.order else None

    def jump(self, position):
        if 0 <= position < len(self.order):
            self.position = position

    def snapshot(self):
        return {"tracks": [{key: track[key] for key in SAVED_FIELDS if key in track} for track in self.tracks],
                "order": self.order, "position": self.position,
                "shuffle": self.shuffle, "repeat": self.repeat, "title": self.title}

    @classmethod
    def restore(cls, value):
        queue = cls([track for track in value.get("tracks", []) if isinstance(track, dict) and track.get("Id")])
        order = value.get("order")
        if isinstance(order, list) and sorted(order) == queue.order:
            queue.order = order
        position = value.get("position")
        if isinstance(position, int):
            queue.jump(position)
        queue.shuffle = bool(value.get("shuffle"))
        queue.repeat = value.get("repeat") if value.get("repeat") in REPEAT_MODES else "off"
        queue.title = value.get("title") if isinstance(value.get("title"), str) else None
        return queue


SAVED_QUEUES = 10


def queue_key(session):
    """A saved queue is identified by its track list: replaying the same album or
    audiobook updates its entry instead of adding another."""
    return tuple(track.get("Id") for track in session.get("tracks", []))


def remember_session(sessions, session, account, limit=SAVED_QUEUES):
    """Put session (with its progress) first, replacing the entry for the same
    track list; keep at most limit per account. Returns the new list."""
    session = dict(session, **account)
    key = queue_key(session)
    mine = [s for s in sessions if all(s.get(k) == v for k, v in account.items())]
    old = next((s for s in mine if queue_key(s) == key), None)
    if mine and old is mine[0] and {k: v for k, v in old.items() if k != "played_at"} == session:
        return sessions  # already first and unchanged: avoid rewriting the file
    session["played_at"] = int(time.time())
    dropped = [s for s in mine if s is not old][limit - 1:]
    return [session] + [s for s in sessions if s is not old and not any(s is d for d in dropped)]


def session_queue(session):
    """The validated Queue a saved session describes (tolerates a corrupt file)."""
    return Queue.restore(session)


def forget_session(sessions, key, account):
    return [s for s in sessions if not (queue_key(s) == key and all(s.get(k) == v for k, v in account.items()))]


def save_sessions(path, sessions):
    """Atomically keep the saved queues so they survive a restart."""
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as target:
            json.dump({"sessions": sessions}, target)
        os.replace(path + ".tmp", path)
    except OSError as exc:
        LOG.warning("Could not save the music queues (%s)", type(exc).__name__)


def load_sessions(path):
    try:
        with open(path, encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, ValueError):
        return []
    if isinstance(value, dict) and value.get("tracks"):
        return [value]  # the earlier single-queue file
    sessions = value.get("sessions") if isinstance(value, dict) else None
    return [s for s in sessions if isinstance(s, dict) and s.get("tracks")] if isinstance(sessions, list) else []


class MusicPlayer:
    """Plays a Queue in a windowless mpv the UI polls once per frame.

    The library UI keeps the display, so music continues while browsing.
    Gapless: mpv's playlist always holds the playing track plus the next one,
    which --prefetch-playlist opens early. Natural track changes happen inside
    mpv; skips replace the playlist. Each track gets its own Jellyfin play
    session so the server sees ordinary per-track playback reports.
    """
    def __init__(self, stream_url, report, token, guard=True):
        self.stream_url, self.report, self.token, self.guard = stream_url, report, token, guard
        self.queue = None
        self.player = self.ipc = self.reporter = None
        self.status = ""
        self.started = False
        self.awaiting_start = False
        self.queued = False
        self.failures = 0
        self.next_report = 0
        self.connect_deadline = 0
        self.last_ticks = 0
        self.resume_at = None
        self.sleep = 0
        self.sleep_at = None
        self.volume = 100
        # The final state when playback stops, for the UI to save; None after the queue finished.
        self.last_session = None
        # A queue that played to the end, so the UI can drop it from the saved queues.
        self.finished_session = None

    @property
    def active(self):
        return self.player is not None

    @property
    def current(self):
        return self.queue.current if self.queue else None

    @property
    def paused(self):
        return bool(self.ipc and self.ipc.properties.get("pause", False))

    def position(self):
        """(elapsed, duration) in seconds; duration falls back to Jellyfin's runtime."""
        if not self.ipc:
            return 0, 0
        elapsed = self.ipc.position_ticks(0) / 10000000
        duration = self.ipc.properties.get("duration")
        if not isinstance(duration, (int, float)) or duration <= 0:
            duration = (self.current or {}).get("RunTimeTicks", 0) / 10000000
        return elapsed, duration

    def session(self):
        """The queue to persist: the live one, or the last one after a stop."""
        if not self.queue:
            return self.last_session
        value = self.queue.snapshot()
        value["elapsed"] = round(self.position()[0]) if self.started else 0
        return value

    def play(self, tracks, start=0, title=None):
        if not tracks:
            return
        queue = Queue(tracks, start, title)
        if self.queue:
            # A new album keeps the listener's shuffle/repeat choice.
            queue.repeat = self.queue.repeat
            if self.queue.shuffle:
                queue.set_shuffle(True)
        self._start(queue)

    def resume(self, session):
        queue = Queue.restore(session or {})
        if not queue.tracks:
            return
        elapsed = session.get("elapsed")
        self._start(queue, elapsed if isinstance(elapsed, (int, float)) and elapsed > 5 else None)

    def _start(self, queue, resume_at=None):
        self._finish_track()
        self.queue, self.resume_at = queue, resume_at
        self.failures = 0
        self.status = ""
        if not self.active:
            self._spawn()
        self._load()

    def _spawn(self):
        path = "/tmp/jellyfin-music-" + str(uuid.uuid4()) + ".sock"
        args = ["mpv", "--idle=yes", "--no-video", "--force-window=no", "--no-terminal",
                "--input-default-bindings=no", "--input-ipc-server=" + path,
                "--cache=yes", "--cache-secs=30", "--audio-display=no",
                "--gapless-audio=weak", "--prefetch-playlist=yes"]
        if self.token:
            args.append("--http-header-fields=X-Emby-Token: " + self.token)
        if self.guard and sys.platform.startswith("linux"):
            args = [sys.executable, os.path.abspath(playback.__file__), "--guard-parent",
                    str(os.getpid())] + args
        self.ipc = MpvIPC(path)
        self.player = subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.connect_deadline = time.monotonic() + 8
        LOG.info("Music player started pid=%d", self.player.pid)

    def _load(self):
        """Replace whatever mpv holds with the queue's current track (skips, jumps)."""
        self.reporter = None
        self.started = False
        self.queued = False
        # Ignore end-file events until mpv confirms this file started: an eof
        # for the previous track can still be in flight when we skip.
        self.awaiting_start = True
        self.ipc.command("loadfile", self.stream_url(self.current), "replace")
        self.ipc.command("set_property", "pause", False)
        LOG.info("Music track %d/%d", self.queue.position + 1, len(self.queue.order))

    def _queue_next(self):
        """Line up the following track so mpv moves on with no gap.
        playlist-clear drops everything except the playing file."""
        self.ipc.command("playlist-clear")
        upcoming = self.queue.peek()
        self.queued = upcoming is not None
        if upcoming:
            self.ipc.command("loadfile", self.stream_url(upcoming), "append")

    def _finish_track(self, ticks=None):
        if self.reporter:
            if ticks is None:
                ticks = self.ipc.position_ticks(self.last_ticks) if self.ipc else self.last_ticks
            self.reporter.submit("stopped", ticks, self.paused)
            self.reporter = None
        self.started = False
        self.last_ticks = 0

    def update(self):
        """Called every UI frame: pumps IPC, reports progress, follows the queue."""
        if not self.active:
            return
        if self.player.poll() is not None:
            LOG.warning("Music player exited unexpectedly")
            self.stop()
            self.status = "Music stopped unexpectedly"
            return
        if not self.ipc.sock:
            if not self.ipc.connect():
                if time.monotonic() > self.connect_deadline:
                    self.stop()
                    self.status = "Music player did not start"
                return
        self.ipc.poll()
        if self.ipc.eof:
            self.stop()
            self.status = "Music stopped unexpectedly"
            return
        if self.started:
            self.last_ticks = self.ipc.position_ticks(self.last_ticks)
        while self.ipc.events:
            self._handle(self.ipc.events.popleft())
            if not self.active:
                return
        if self.started and time.monotonic() >= self.next_report:
            self.reporter.submit("progress", self.ipc.position_ticks(0), self.paused)
            self.next_report = time.monotonic() + 5
        if self.sleep_at:
            remaining = self.sleep_at - time.monotonic()
            if remaining <= 0:
                self._sleep_now()
            elif remaining < SLEEP_FADE:
                # Fade out instead of cutting off mid-phrase.
                self._set_volume(round(100 * remaining / SLEEP_FADE))

    def _handle(self, event):
        name = event.get("event")
        if name == "start-file":
            self.awaiting_start = False
        elif name == "file-loaded" and not self.started:
            # The reporter (and its thread) exists only once a track really plays,
            # so it always ends with a "stopped" report.
            track, session_id = self.current, str(uuid.uuid4())
            self.reporter = PlaybackReporter(
                lambda event, ticks, paused: self.report(event, track, session_id, ticks, paused))
            self.started = True
            self.failures = 0
            self.reporter.submit("started", 0, False)
            self.next_report = time.monotonic() + 5
            if self.resume_at:
                self.ipc.command("seek", self.resume_at, "absolute+exact")
                self.resume_at = None
            self._queue_next()
        elif name == "end-file" and not self.awaiting_start:
            reason = event.get("reason")
            if reason == "eof":
                # Report the full length: Jellyfin counts a play from the final position.
                self._finish_track((self.current or {}).get("RunTimeTicks") or self.last_ticks)
                if self.sleep == "track":
                    # Resuming later should start with the track after this one.
                    self.queue.advance(1, automatic=True)
                    self._sleep_now()
                elif self.queued:
                    # mpv is already starting the lined-up track; just follow it.
                    self.queue.advance(1, automatic=True)
                    self.queued = False
                else:
                    self.stop(keep_session=False)
                    self.status = "Finished playing the queue"
            elif reason == "error":
                self.failures += 1
                self.status = "Could not play " + (self.current or {}).get("Name", "track")
                LOG.warning("Music track failed to play")
                self._finish_track()
                if self.failures >= len(self.queue.order) or not self.queue.advance(1):
                    self.stop()
                    if self.failures > 1:
                        self.status = "None of these tracks could be played"
                else:
                    # Replace mpv's own move to the lined-up file to keep one code path.
                    self._load()

    def _sleep_now(self):
        self.stop()
        self.status = "Sleep timer stopped the music"
        LOG.info("Sleep timer stopped the music")

    def _set_volume(self, volume):
        if volume != self.volume and self.ipc:
            self.ipc.command("set_property", "volume", volume)
            self.volume = volume

    def cycle_sleep(self, step=1):
        self.sleep = SLEEP_CHOICES[(SLEEP_CHOICES.index(self.sleep) + step) % len(SLEEP_CHOICES)]
        minutes = self.sleep if self.sleep != "track" else 0
        self.sleep_at = time.monotonic() + minutes * 60 if minutes else None
        self._set_volume(100)

    def sleep_remaining(self):
        return max(0, self.sleep_at - time.monotonic()) if self.sleep_at else None

    def _skip(self, step):
        self._finish_track()
        if self.queue.advance(step):
            self._load()

    def toggle_pause(self):
        if self.ipc and self.ipc.sock:
            self.ipc.command("cycle", "pause")

    def seek(self, seconds):
        if self.ipc and self.ipc.sock and self.started:
            self.ipc.command("seek", seconds, "relative+exact")

    def next(self):
        if self.active:
            if self.queue.position + 1 >= len(self.queue.order) and self.queue.repeat != "all":
                self.status = "Last track in the queue"
                return
            self._skip(1)

    def previous(self):
        """Like most players: restart the track unless it only just began."""
        if not self.active:
            return
        elapsed, _ = self.position()
        if elapsed > 3 or (self.queue.position == 0 and self.queue.repeat != "all"):
            self.ipc.command("seek", 0, "absolute+exact")
        else:
            self._skip(-1)

    def jump(self, position):
        if self.active and position != self.queue.position:
            self._finish_track()
            self.queue.jump(position)
            self._load()

    def toggle_shuffle(self):
        if self.queue:
            self.queue.set_shuffle(not self.queue.shuffle)
            if self.started:
                self._queue_next()

    def cycle_repeat(self):
        if self.queue:
            self.queue.cycle_repeat()
            if self.started:
                self._queue_next()

    def stop(self, keep_session=True):
        """Stop and reap mpv. Returns the last track's reporter so callers can wait on it."""
        if self.queue:
            snapshot = self.session()
            self.last_session = snapshot if keep_session else None
            if not keep_session:
                self.finished_session = snapshot
        reporter = self.reporter
        self._finish_track()
        if self.player:
            try:
                stop_process(self.player, self.ipc)
            finally:
                self.ipc.close()
                try:
                    os.unlink(self.ipc.path)
                except FileNotFoundError:
                    pass
                self.player = self.ipc = None
                LOG.info("Music player closed")
        self.queue = None
        self.queued = False
        # A timer belongs to one listening session; a fresh mpv starts at full volume.
        self.sleep, self.sleep_at, self.volume = 0, None, 100
        return reporter
