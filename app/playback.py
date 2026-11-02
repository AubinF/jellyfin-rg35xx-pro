"""mpv transport and RG35XX-H controls; no UI or network calls in the input loop."""
import collections
import glob
import json
import logging
import math
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

LOG = logging.getLogger("jellyfin.playback")


class JsonLines:
    """A socket read can contain half a message, many messages, or both."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        messages = []
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            try:
                message = json.loads(line)
                if isinstance(message, dict):
                    messages.append(message)
            except (ValueError, UnicodeError):
                LOG.warning("Ignoring malformed mpv message")
        if len(self.buffer) > 1024 * 1024:
            raise RuntimeError("mpv reply exceeded the message limit")
        return messages


class MpvIPC:
    def __init__(self, path):
        self.path = path
        self.sock = None
        self.decoder = JsonLines()
        self.outgoing = bytearray()
        self.serial = 0
        self.pending = {}
        self.properties = {}
        self.loaded = False
        self.eof = False
        self.failed = False
        self.connected_once = False

    def connect(self):
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        candidate.settimeout(0.05)
        try:
            candidate.connect(self.path)
        except OSError:
            candidate.close()
            return False
        candidate.setblocking(False)
        self.sock = candidate
        self.connected_once = True
        for index, name in enumerate(("time-pos", "pause", "duration"), 1):
            self.command("observe_property", index, name)
        return True

    def command(self, *args, property_name=None):
        self.serial += 1
        message = {"command": list(args), "request_id": self.serial}
        self.outgoing.extend(json.dumps(message).encode("utf-8") + b"\n")
        if property_name:
            self.pending[self.serial] = property_name
        if len(self.outgoing) > 65536:
            raise RuntimeError("mpv is not accepting controls")
        return self.serial

    def poll(self):
        if not self.sock:
            return
        if self.outgoing:
            try:
                sent = self.sock.send(self.outgoing)
                del self.outgoing[:sent]
            except BlockingIOError:
                pass
            except (BrokenPipeError, ConnectionResetError):
                self.eof = True
                return
        # Bound work per frame so even a flood of events cannot starve controls.
        for _ in range(32):
            try:
                data = self.sock.recv(8192)
            except BlockingIOError:
                break
            if not data:
                self.eof = True
                break
            for message in self.decoder.feed(data):
                name = self.pending.pop(message.get("request_id"), None)
                if message.get("event") == "property-change":
                    name = message.get("name")
                if name and message.get("error", "success") == "success":
                    value = message.get("data")
                    # mpv sends null when unloading. Keep the last real position.
                    if value is not None:
                        self.properties[name] = value
                if message.get("event") == "file-loaded":
                    self.loaded = True
                if message.get("event") == "end-file":
                    self.failed = message.get("reason") == "error"

    def position_ticks(self, fallback):
        value = self.properties.get("time-pos")
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            if math.isfinite(value) and value >= 0:
                return int(value * 10000000)
        return fallback

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


class InputDecoder:
    # Native Linux input_event: timeval, unsigned type/code, SIGNED value.
    EVENT = struct.Struct("@llHHi")
    # Verified against KNULLI's es_input.cfg for Anbernic RG35XX-H Controller.
    BUTTONS = {304: "pause", 305: "stop", 310: "stop", 311: "stop",
               312: "stop", 306: "audio", 307: "subtitles",
               308: "seek:-30", 309: "seek:30", 314: "seek:-60",
               315: "seek:60"}

    def __init__(self):
        self.buffer = bytearray()
        self.hat_x = 0

    def feed(self, data):
        self.buffer.extend(data)
        actions = []
        while len(self.buffer) >= self.EVENT.size:
            _, _, kind, code, value = self.EVENT.unpack_from(self.buffer)
            del self.buffer[:self.EVENT.size]
            if kind == 1 and value == 1 and code in self.BUTTONS:
                actions.append(self.BUTTONS[code])
            elif kind == 3 and code == 16:
                if value and value != self.hat_x:
                    actions.append("seek:-10" if value < 0 else "seek:10")
                self.hat_x = value
        return actions


def is_gamepad(name, name_path):
    """Other RG35XX models name their pad differently; accept any device with face buttons."""
    if any(word in name.lower() for word in ("keyboard", "mouse", "touch", "power", "headset", "lid")):
        return False
    try:
        with open(os.path.join(os.path.dirname(name_path), "capabilities", "key"), encoding="utf-8") as source:
            words = [int(word, 16) for word in source.read().split()]
    except (OSError, ValueError):
        return False
    bits = sum(word << (64 * index) for index, word in enumerate(reversed(words)))
    # BTN_SOUTH (304) and BTN_EAST (305) are the A/B buttons.
    return bool(bits >> 304 & 1 and bits >> 305 & 1)


class ControllerInput:
    def __init__(self):
        self.devices = {}
        for path in sorted(glob.glob("/dev/input/event*")):
            name_path = "/sys/class/input/" + os.path.basename(path) + "/device/name"
            try:
                with open(name_path, encoding="utf-8") as source:
                    name = source.read().strip()
                if not (name == "Anbernic RG35XX-H Controller" or is_gamepad(name, name_path)):
                    continue
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                self.devices[fd] = InputDecoder()
                LOG.info("Controller ready: %s (%s)", name, path)
            except OSError:
                continue
        if not self.devices:
            raise RuntimeError("Handheld controller unavailable; playback was not started")

    def poll(self):
        actions = []
        ready, _, _ = select.select(list(self.devices), [], [], 0)
        for fd in ready:
            for _ in range(8):
                try:
                    data = os.read(fd, InputDecoder.EVENT.size * 64)
                except BlockingIOError:
                    break
                if not data:
                    raise RuntimeError("Handheld controller disconnected")
                actions.extend(self.devices[fd].feed(data))
        return actions

    def close(self):
        for fd in self.devices:
            os.close(fd)
        self.devices.clear()


class PlaybackReporter:
    """One ordered worker keeps HTTP latency out of playback controls."""
    def __init__(self, send):
        self.send = send
        self.condition = threading.Condition()
        self.queue = collections.deque()
        self.finished = threading.Event()
        self.error = False
        self.stop_ok = False
        threading.Thread(target=self._run, name="jellyfin-reports", daemon=True).start()

    def submit(self, event, ticks, paused=False):
        with self.condition:
            if event in ("progress", "stopped"):
                self.queue = collections.deque(v for v in self.queue if v[0] != "progress")
            self.queue.append((event, int(ticks), paused))
            self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: bool(self.queue))
                event, ticks, paused = self.queue.popleft()
            ok = False
            for _ in range(2 if event in ("started", "stopped") else 1):
                try:
                    self.send(event, ticks, paused)
                    ok = True
                    break
                except Exception as exc:
                    # requests errors may include a URL/token: only log the class.
                    LOG.warning("Playback %s report failed (%s)", event, type(exc).__name__)
            self.error = self.error or not ok
            if event == "stopped":
                self.stop_ok = ok
                self.finished.set()
                return


def stop_process(player, ipc=None):
    """Always reap our exact child. IPC quit, then bounded TERM/KILL fallbacks."""
    if player.poll() is None and ipc and ipc.sock:
        try:
            ipc.command("quit")
            ipc.poll()
        except (OSError, RuntimeError):
            pass
        try:
            player.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    if player.poll() is None:
        player.terminate()
        try:
            player.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait(timeout=1)
    else:
        player.wait()


class PlaybackSession:
    def __init__(self, args, path, resume_ticks, reporter, controls, guard=True):
        self.args, self.path = args, path
        self.position_ticks = int(resume_ticks)
        self.reporter, self.controls, self.guard = reporter, controls, guard
        self.player = None
        self.ipc = MpvIPC(path)
        self.started = False
        self.paused = False
        self.next_report = 0
        self.completed = False

    def _receive(self):
        self.ipc.poll()
        self.position_ticks = self.ipc.position_ticks(self.position_ticks)
        self.paused = bool(self.ipc.properties.get("pause", False))

    def run(self):
        stop_requested = False
        try:
            args = self.args
            if self.guard and sys.platform.startswith("linux"):
                args = [sys.executable, os.path.abspath(__file__), "--guard-parent",
                        str(os.getpid())] + args
            try:
                errlog = open(os.path.join(os.path.dirname(os.environ.get("JELLYFIN_CONFIG", "/userdata/roms/ports/jellyfinrg35xx/config.json")), "mpv-stderr.txt"), "wb")
            except OSError:
                errlog = subprocess.DEVNULL
            self.player = subprocess.Popen(args, start_new_session=True,
                                           stdin=subprocess.DEVNULL,
                                           stdout=errlog,
                                           stderr=errlog)
            LOG.info("Player started pid=%d", self.player.pid)
            connect_deadline = time.monotonic() + 8
            load_deadline = time.monotonic() + 45
            while self.player.poll() is None:
                # Exactly one controller path; no duplicate SDL + evdev actions.
                actions = self.controls.poll()
                if "stop" in actions:
                    stop_requested = True
                    LOG.info("Controller requested stop")
                    break
                if not self.ipc.sock:
                    if not self.ipc.connect():
                        if time.monotonic() > connect_deadline:
                            raise RuntimeError("Player controls did not connect")
                        time.sleep(0.02)
                        continue
                    self.ipc.command(
                        "show-text",
                        "A: Pause   B / Start: Back\n"
                        "Left/Right: 10s   L/R: 30s\n"
                        "X: Audio track   Y: Subtitles",
                        5000)
                self._receive()
                if self.ipc.eof:
                    break
                for action in actions:
                    if action.startswith("seek:"):
                        seconds = int(action.split(":", 1)[1])
                        self.ipc.command("seek", seconds, "relative+exact")
                        self.ipc.command("show-progress")
                        LOG.info("Controller seek %+ds", seconds)
                    elif action == "pause":
                        self.ipc.command("cycle", "pause")
                        self.ipc.command("show-progress")
                    elif action == "audio":
                        # mpv's audio property cycles every embedded audio stream.
                        self.ipc.command("cycle", "audio")
                        self.ipc.command("show-text", "Audio track changed", 1800)
                    elif action == "subtitles":
                        # "sub" includes Off, then each embedded subtitle stream.
                        self.ipc.command("cycle", "sub")
                        self.ipc.command("show-text", "Subtitle track changed", 1800)
                if self.ipc.loaded or "time-pos" in self.ipc.properties:
                    if not self.started:
                        self.reporter.submit("started", self.position_ticks, self.paused)
                        self.started = True
                        self.next_report = time.monotonic() + 5
                    elif time.monotonic() >= self.next_report:
                        self.reporter.submit("progress", self.position_ticks, self.paused)
                        self.next_report = time.monotonic() + 5
                elif time.monotonic() > load_deadline:
                    raise RuntimeError("Video did not load; returned to the library")
                time.sleep(0.02)
            # Read final property messages before quit, including a seek just made.
            if self.ipc.sock and not self.ipc.eof:
                request = self.ipc.command("get_property", "time-pos", property_name="time-pos")
                deadline = time.monotonic() + 0.15
                while time.monotonic() < deadline:
                    self._receive()
                    if request not in self.ipc.pending or self.ipc.eof:
                        break
                    time.sleep(0.005)
            if not stop_requested and (self.ipc.failed or not self.started):
                raise RuntimeError("Video playback failed; returned to the library")
            self.completed = not stop_requested and self.started and not self.ipc.failed
        finally:
            # This also runs if the controller, IPC parser, or UI raises an error.
            try:
                if self.player:
                    stop_process(self.player, self.ipc)
            finally:
                self.position_ticks = self.ipc.position_ticks(self.position_ticks)
                self.reporter.submit("stopped", self.position_ticks, self.paused)
                self.controls.close()
                self.ipc.close()
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
                LOG.info("Player closed; position_ticks=%d", self.position_ticks)
        return self.position_ticks


def guarded_exec(parent_pid, args):
    # A fresh Python process sets this before exec; no unsafe preexec_fn with threads.
    # The kernel kills this mpv if its Python parent dies, including SIGKILL/OOM.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot set player parent-death signal")
    if os.getppid() != parent_pid:
        raise SystemExit(1)
    os.execvp(args[0], args)


if __name__ == "__main__" and len(sys.argv) > 3 and sys.argv[1] == "--guard-parent":
    guarded_exec(int(sys.argv[2]), sys.argv[3:])
