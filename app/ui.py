"""640x480 controller UI with Jellyfin home shelves and asynchronous artwork."""
import atexit
import hashlib
import json
import logging
import math
import os
import time

import pygame

from downloads import Downloads, free_bytes, on_real_storage, safe_name, storage_choices
from library import Artwork, Jobs, LibraryAPI, OfflineLibrary, Rows
from music import (SAVED_QUEUES, MusicPlayer, forget_session, load_sessions, queue_key,
                   remember_session, save_sessions, session_queue)
from playback import Backlight, MediaKeys

# SDL joystick index of R2 on the RG35XX H/Pro (Linux BTN_TR2, code 315). Found with a
# button probe on the Pro: SDL numbers there are the evdev order shifted by 3
# (A=3, B=4, R1=8, Select/Start/Menu=9/10/11, L1=7, L2=13, R2=14).
R2_BUTTON = 14
MEDIA_KEYS = {getattr(pygame, name) for name in ("K_AUDIOPLAY", "K_AUDIOSTOP", "K_AUDIONEXT", "K_AUDIOPREV",
                                                 "K_AUDIOPAUSE", "K_MEDIASELECT", "K_UNKNOWN") if hasattr(pygame, name)}
LOG = logging.getLogger("jellyfin.ui")
DATA_DIR = os.path.dirname(os.environ.get("JELLYFIN_CONFIG", "/userdata/roms/ports/jellyfinrg35xx/config.json"))

BG = (16, 16, 20)
PANEL = (28, 29, 36)
TEXT = (244, 244, 248)
MUTED = (161, 165, 180)
ACCENT = (0, 164, 220)
BODY = pygame.Rect(0, 66, 640, 370)
ROW_HEIGHT = 182


def clock(seconds):
    minutes, seconds = divmod(int(max(0, seconds)), 60)
    return "%d:%02d" % (minutes, seconds)


def sleep_label(music):
    if music.sleep == "track":
        return "End of track"
    remaining = music.sleep_remaining()
    return "%d min" % math.ceil(remaining / 60) if remaining is not None else "Off"


SCREEN_OFF_CHOICES = (30, 60, 120, 300, 0)


def folder_rows(page):
    """Rows of the folder browser: actions first, then the subfolders."""
    rows = [("use", ""), ("new", "")]
    if page["path"] != "/":
        rows.append(("up", ""))
    return rows + [("dir", name) for name in page["dirs"]]


def list_folders(path):
    """Visible subfolders of path, sorted like a file manager; (names, error)."""
    try:
        with os.scandir(path) as entries:
            names = [e.name for e in entries if not e.name.startswith(".") and e.is_dir()]
    except OSError as exc:
        return [], "Can't open this folder (%s)" % type(exc).__name__
    return sorted(names, key=str.lower)[:500], ""


def size_label(size):
    if size >= 10 * 1024 ** 3:
        return "%d GB" % (size // 1024 ** 3)
    if size >= 1024 ** 3:
        return "%.1f GB" % (size / 1024 ** 3)
    return "%d MB" % round(size / 1024 ** 2)


def screen_off_label(seconds):
    if not seconds:
        return "Never"
    return "After %d s" % seconds if seconds < 60 else "After %d min" % (seconds // 60)


class Renderer:
    def __init__(self, screen, build):
        self.screen, self.build = screen, build
        bundled_font = os.path.join(os.path.dirname(__file__), "assets", "DejaVuSans.ttf")
        font = bundled_font if os.path.exists(bundled_font) else "/usr/share/fonts/dejavu/DejaVuSans.ttf"
        self.small = pygame.font.Font(font, 14)
        self.font = pygame.font.Font(font, 17)
        self.heading = pygame.font.Font(font, 21)
        self.title = pygame.font.Font(font, 25)
        self.logo = pygame.transform.smoothscale(pygame.image.load(
            os.path.join(os.path.dirname(__file__), "assets", "jellyfin-icon.png")).convert_alpha(), (35, 35))
        self.scroll = 0.0
        self.now_playing = ""
        self.music_hint = ""
        self.offline = False

    def text(self, value, x, y, width, font=None, color=TEXT, center=False, right=False):
        font = font or self.font
        value = str(value or "").replace("\n", " ")
        while value and font.size(value)[0] > width:
            value = value[:-2].rstrip("…") + "…" if len(value) > 2 else ""
        image = font.render(value, True, color)
        offset = (width - image.get_width()) // 2 if center else (width - image.get_width() if right else 0)
        self.screen.blit(image, (x + offset, y))

    def wrap(self, value, x, y, width, lines, font=None, color=MUTED):
        font = font or self.small
        words = str(value or "").split()
        height = font.get_linesize() + 3
        for line in range(lines):
            current = ""
            while words and (not current or font.size(current + " " + words[0])[0] <= width):
                current = (current + " " + words.pop(0)).strip()
            if line == lines - 1 and words:
                current += "…"
            self.text(current, x, y + line * height, width, font, color)
            if not words:
                break

    def spinner(self, x, y, radius=10):
        angle = time.monotonic() * 4.5
        for index in range(10):
            light = 65 + index * 19
            point = (round(x + math.cos(angle + index * 0.6) * radius),
                     round(y + math.sin(angle + index * 0.6) * radius))
            pygame.draw.circle(self.screen, (0, min(light, 164), min(light + 35, 255)), point, 2)

    def header(self, home=True):
        self.screen.set_clip(None)
        self.screen.fill(BG)
        self.screen.blit(self.logo, (16, 13))
        self.text("Jellyfin", 59, 15, 122, self.title)
        self.text("Home" if home else "Library", 222, 24, 130, self.font, TEXT)
        pygame.draw.rect(self.screen, ACCENT, (222, 57, 48 if home else 62, 3), border_radius=1)
        if self.now_playing:
            self.text(("Offline · ♪ " if self.offline else "♪ ") + self.now_playing, 380, 24, 170, self.small, ACCENT)
        elif self.offline:
            self.text("Offline", 418, 24, 121, self.small, ACCENT)
        else:
            self.text("RG35XX H", 418, 24, 121, self.small, MUTED)
        self.text(self.build, 560, 24, 62, self.small, MUTED)
        pygame.draw.line(self.screen, (43, 44, 52), (18, 63), (622, 63))

    def footer(self, message):
        self.screen.set_clip(None)
        pygame.draw.rect(self.screen, BG, (0, 438, 640, 42))
        pygame.draw.line(self.screen, (43, 44, 52), (18, 438), (622, 438))
        self.text(message, 18, 450, 607, self.small, MUTED)

    def card(self, item, rect, artwork, selected, landscape=True, subtitle=True):
        image_height = rect.height - (42 if subtitle else 25)
        image_rect = pygame.Rect(rect.x, rect.y, rect.width, image_height)
        pygame.draw.rect(self.screen, PANEL, image_rect, border_radius=5)
        surface, loading = artwork.get(item, (image_rect.width, image_rect.height), landscape) if artwork else (None, True)
        if surface:
            self.screen.blit(surface, (image_rect.centerx - surface.get_width() // 2,
                                       image_rect.centery - surface.get_height() // 2))
        elif loading:
            self.spinner(*image_rect.center)
        else:
            cx, cy = image_rect.center
            pygame.draw.rect(self.screen, (86, 97, 123), (cx - 19, cy - 9, 38, 24), 2, border_radius=3)
            pygame.draw.line(self.screen, (86, 97, 123), (cx - 17, cy - 12), (cx - 3, cy - 12), 3)
        if selected:
            pygame.draw.rect(self.screen, ACCENT, image_rect.inflate(6, 6), 3, border_radius=7)
        data = item.get("UserData") or {}
        if data.get("Played"):
            # Match Jellyfin's familiar watched badge without obscuring poster art.
            badge = (image_rect.right - 15, image_rect.y + 15)
            pygame.draw.circle(self.screen, (24, 143, 89), badge, 11)
            pygame.draw.lines(self.screen, (255, 255, 255), False,
                              [(badge[0] - 5, badge[1]), (badge[0] - 1, badge[1] + 4),
                               (badge[0] + 6, badge[1] - 5)], 2)
        ticks, duration = data.get("PlaybackPositionTicks", 0), item.get("RunTimeTicks", 0)
        if ticks and duration:
            fraction = max(0, min(1, ticks / duration))
            pygame.draw.rect(self.screen, (35, 37, 46), (image_rect.x, image_rect.bottom - 4, image_rect.width, 4))
            pygame.draw.rect(self.screen, ACCENT, (image_rect.x, image_rect.bottom - 4, round(image_rect.width * fraction), 4))
        name = item.get("SeriesName") if item.get("Type") == "Episode" else item.get("Name")
        self.text(name or item.get("Name", "Untitled"), rect.x, image_rect.bottom + 7, rect.width,
                  self.font, TEXT if selected else (221, 223, 232), center=True)
        if subtitle:
            if item.get("Type") == "Episode":
                extra = "S%s E%s · %s" % (item.get("ParentIndexNumber", "?"), item.get("IndexNumber", "?"), item.get("Name", ""))
            else:
                extra = str(item.get("ProductionYear") or "")
            self.text(extra, rect.x, image_rect.bottom + 28, rect.width, self.small, MUTED, center=True)

    def home(self, rows, artwork, load_row):
        self.header()
        top = max(0, rows.row - 1)
        target = top * ROW_HEIGHT
        self.scroll += (target - self.scroll) * 0.35
        if abs(target - self.scroll) < 1:
            self.scroll = target
        self.screen.set_clip(BODY)
        for index, row in enumerate(rows.values):
            y = round(76 + index * ROW_HEIGHT - self.scroll)
            if y + ROW_HEIGHT < BODY.top or y > BODY.bottom:
                continue
            load_row(index)
            self.text(row["title"], 18, y, 485, self.heading)
            items = row.get("items", [])
            col = min(rows.columns[index], max(0, len(items) - 1))
            first = max(0, col - 2)
            if items:
                self.text(f"{col + 1} / {len(items)}", 527, y + 5, 92, self.small, MUTED)
            if row.get("loading"):
                self.spinner(314, y + 78)
                self.text("Loading...", 254, y + 102, 120, self.small, MUTED, center=True)
            elif not items:
                self.text(row.get("error") or row.get("empty", "Nothing here yet"), 20, y + 61, 600, self.font, MUTED)
            for offset, item in enumerate(items[first:first + 3]):
                self.card(item, pygame.Rect(18 + offset * 204, y + 31, 188, 144), artwork,
                          index == rows.row and first + offset == col)
        if self.offline:
            hints = (["D-pad  Browse", "A  Play", "X  Go online"] + (["B  " + self.music_hint] if self.music_hint else [])
                     + ["R2  Queues", "R1  Settings", "Start  Exit"])
        else:
            hints = (["D-pad  Browse", "A  Open", "Y  Search"] + (["B  " + self.music_hint] if self.music_hint else [])
                     + ["R2  Queues", "R1  Settings", "Start  Exit"])
        # Six or seven hints only fit with tighter spacing.
        self.footer("   ".join(hints))

    def keyboard(self, page, loading=False):
        """Controller keyboard modelled on OD Commander's proven 12-by-4 layout."""
        self.header(False)
        parent = page.get("parent")
        title = page.get("title") or ("Search all libraries" if not parent else "Search in " + parent.get("Name", "folder"))
        self.text(title, 18, 78, 420, self.heading)
        query = page.get("query", "")
        pygame.draw.rect(self.screen, PANEL, (18, 105, 604, 31), border_radius=4)
        shown = ("•" * len(query)) if page.get("secret") else query
        self.text(shown or page.get("placeholder", "Enter search text"), 29, 112, 570, self.font, TEXT if query else MUTED)
        layer_name = "CAPS + SYMBOLS" if page["layer"] else "lowercase"
        self.text("R1/L1  " + layer_name, 430, 81, 190, self.small, ACCENT, center=True)
        keys = page["layers"][page["layer"]]
        width, height, x0, y0 = 48, 42, 32, 146
        for row_index, row in enumerate(keys):
            for column, label in enumerate(row):
                rect = pygame.Rect(x0 + column * width, y0 + row_index * height, width - 2, height - 2)
                selected = page["focus"] == "keys" and page["key_y"] == row_index and page["key_x"] == column
                pygame.draw.rect(self.screen, ACCENT if selected else PANEL, rect, border_radius=3)
                display = "SPACE" if label == " " else ("DEL" if label == "←" else label)
                self.text(display, rect.x, rect.y + 12, rect.width, self.small, TEXT, center=True)
        items = page.get("items", [])
        if loading:
            self.spinner(606, 345, 7)
        elif page.get("searched") and not items:
            self.text("No matches", 18, 326, 604, self.font, MUTED, center=True)
        elif items:
            first = max(0, min(page["selection"] - 1, max(0, len(items) - 3)))
            for offset, item in enumerate(items[first:first + 3]):
                y = 320 + offset * 28
                selected = page["focus"] == "results" and first + offset == page["selection"]
                if selected:
                    pygame.draw.rect(self.screen, ACCENT, (18, y - 2, 604, 24), border_radius=3)
                label = item.get("SeriesName") + " · " + item.get("Name", "") if item.get("Type") == "Episode" else item.get("Name", "Untitled")
                self.text(label, 28, y + 2, 570, self.font, TEXT)
        if page["kind"] == "input":
            help_text = "A  Type     X  Space     Y  Delete     Start  Save     B  Cancel"
        elif page["focus"] == "results":
            help_text = "D-pad  Choose result     A  Open     B  Keyboard"
        else:
            help_text = "A  Type     X  Space     Y  Delete     Start  Search     B  Cancel"
        self.footer(help_text)

    def setup(self, page):
        self.header(False)
        self.text("Connect to Jellyfin", 18, 84, 604, self.heading)
        self.wrap("Enter your server details once. They stay on this handheld.", 18, 115, 604, 2, self.font)
        fields = [("Server URL", page["values"].get("serverUrl", "")), ("Username", page["values"].get("username", "")),
                  ("Password", "•" * len(page["values"].get("password", ""))), ("Quality", page["values"].get("quality", "480p")),
                  ("Connect", "Save and sign in")]
        for index, (label, value) in enumerate(fields):
            y = 175 + index * 45
            selected = index == page["selection"]
            pygame.draw.rect(self.screen, ACCENT if selected else PANEL, (18, y, 604, 36), border_radius=5)
            self.text(label, 31, y + 9, 160, self.font, TEXT)
            self.text(value or "Not set", 210, y + 9, 392, self.font, TEXT if value else MUTED)
        self.footer("D-pad  Choose     A  Edit / Connect     Start  Exit")

    def settings(self, page):
        self.header(False)
        self.text("Settings", 18, 84, 604, self.heading)
        values = page["values"]
        fields = [("Quality", values.get("quality", "480p")),
                  ("Music screen off", screen_off_label(values.get("screenOff", 30))),
                  ("Downloads", values.get("downloadPath") or "Choose where to keep them"),
                  ("Server URL", values.get("serverUrl", "")),
                  ("Username", values.get("username", "")), ("Password", "Saved"), ("Logout", "Forget saved login"), ("Back", "Return home")]
        for index, (label, value) in enumerate(fields):
            y = 122 + index * 39
            selected = index == page["selection"]
            pygame.draw.rect(self.screen, ACCENT if selected else PANEL, (18, y, 604, 33), border_radius=5)
            self.text(label, 31, y + 7, 170, self.font, TEXT)
            self.text(value or "Not set", 210, y + 7, 392, self.font, TEXT if value else MUTED)
        self.footer("D-pad  Choose     A  Edit     B  Back     Start  Exit")
        if page.get("confirm_logout"):
            pygame.draw.rect(self.screen, (30, 32, 41), (82, 184, 476, 112), border_radius=8)
            self.text("Log out?", 104, 202, 432, self.heading, center=True)
            self.text("Server and username will be kept.", 104, 238, 432, self.small, MUTED, center=True)
            self.text("A  Confirm       B  Cancel", 104, 266, 432, self.small, TEXT, center=True)

    def listing(self, page, artwork, loading=False):
        self.header(False)
        self.text(page["title"], 18, 79, 480, self.heading)
        items = page["items"]
        selected = page["selection"]
        portrait = page.get("portrait", True)
        capacity = 4 if portrait else 6
        first = selected // capacity * capacity
        total = page.get("total")
        counter = f"{selected + 1 if items else 0} / {total if total is not None else len(items)}"
        self.text(counter, 510, 83, 112, self.small, MUTED)
        if not items and not loading:
            self.text("No items in this folder", 70, 220, 500, self.heading, MUTED, center=True)
        for offset, item in enumerate(items[first:first + capacity]):
            if portrait:
                rect = pygame.Rect(18 + offset * 155, 121, 140, 262)
            else:
                rect = pygame.Rect(18 + (offset % 3) * 204, 121 + (offset // 3) * 153, 188, 139)
            self.card(item, rect, artwork, first + offset == selected, not portrait)
        # The download status sits in the strip between the last card row (its
        # subtitle ends near y=416) and the footer, so it never covers card text.
        if page.get("download") is not None:
            # A music page: show where its offline copy stands instead of Watched.
            self.text(page["download"], 306, 420, 316, self.small, ACCENT, right=True)
            self.footer("D-pad  Browse     A  Play     R1  Download     Y  Search     B  Back     Start  Exit")
        else:
            self.footer("D-pad  Browse     A  Open     X  Watched     Y  Search     B  Back     Start  Exit")

    def downloads(self, page, entries, location, free, summary, status=""):
        self.header(False)
        self.text("Downloads", 18, 79, 200, self.heading)
        message = "Press X again to remove it" if page.get("confirm") is not None else (status or summary)
        self.text(message, 222, 84, 400, self.small, ACCENT if message else MUTED, right=True)
        rows = [None] + entries
        first = max(0, min(page["selection"] - 4, len(rows) - 9))
        for offset, entry in enumerate(rows[first:first + 9]):
            index, y = first + offset, 114 + offset * 34
            selected = index == page["selection"]
            if selected:
                pygame.draw.rect(self.screen, ACCENT, (18, y, 604, 30), border_radius=4)
            muted = TEXT if selected else MUTED
            if entry is None:
                self.text("Location", 28, y + 5, 120, self.font, TEXT)
                self.text(location or "Not chosen yet", 130, y + 7, 380, self.small, muted)
                if location:
                    self.text(size_label(free) + " free", 482, y + 7, 130, self.small, muted, right=True)
                continue
            self.text(entry["title"], 28, y + 5, 300, self.font, TEXT)
            state = {"done": "✓ %d tracks" % entry["total"],
                     "downloading": "%d/%d · %d%%" % (entry["done"] + 1, entry["total"], entry["progress"] * 100),
                     "queued": "Queued · %d/%d" % (entry["done"], entry["total"]),
                     "paused": "Paused · %d/%d" % (entry["done"], entry["total"]),
                     "failed": "Failed · %d/%d · Y retry" % (entry["done"], entry["total"]),
                     "incomplete": "%d/%d · Y resume" % (entry["done"], entry["total"])}[entry["state"]]
            self.text(state, 336, y + 7, 180, self.small, TEXT if selected else (ACCENT if entry["state"] == "downloading" else MUTED))
            self.text(size_label(entry["size"]), 516, y + 7, 96, self.small, muted, right=True)
        if not entries:
            self.text("Open an album, playlist or audiobook and press R1", 18, 250, 604, self.font, MUTED, center=True)
        self.footer("▲ ▼  Choose     A  Play / Change     X  Remove     Y  Retry     B  Back")

    def storage(self, page, choices, current):
        self.header(False)
        self.text("Download location", 18, 79, 300, self.heading)
        self.text("Current: " + (current or "not chosen"), 18, 106, 604, self.small, MUTED)
        rows = choices + [{"label": "Browse folders...", "path": "", "free": None},
                          {"label": "Type a folder...", "path": "", "free": None}]
        for index, choice in enumerate(rows):
            y, selected = 136 + index * 48, index == page["selection"]
            pygame.draw.rect(self.screen, ACCENT if selected else PANEL, (18, y, 604, 42), border_radius=5)
            self.text(choice["label"], 31, y + (3 if choice["path"] else 11), 380, self.font, TEXT)
            if choice["path"]:
                self.text(choice["path"], 31, y + 23, 420, self.small, TEXT if selected else MUTED)
            if choice["free"] is not None:
                self.text(size_label(choice["free"]) + " free", 440, y + 12, 170, self.small, TEXT, right=True)
        if not choices:
            self.text("No storage found; browse or type a folder instead", 18, 136 + len(rows) * 48, 604, self.small, MUTED)
        self.footer("▲ ▼  Choose     A  Use this location     B  Back")

    def folders(self, page, free, status=""):
        """Folder browser for the download location."""
        self.header(False)
        self.text("Choose a folder", 18, 79, 300, self.heading)
        if page.get("memory"):
            # Before a card is mounted its /media folder is only RAM: say so up front.
            self.text("In memory only, not on a card", 322, 84, 300, self.small, ACCENT, right=True)
        else:
            self.text(size_label(free) + " free" if free else "", 422, 84, 200, self.small, MUTED, right=True)
        path = page["path"]
        while len(path) > 1 and self.small.size(path)[0] > 604:
            path = "…" + path[2:]  # keep the end of long paths: that is the folder you are in
        self.text(path, 18, 106, 604, self.small, ACCENT)
        rows = folder_rows(page)
        first = max(0, min(page["selection"] - 4, len(rows) - 9))
        for offset, (kind, name) in enumerate(rows[first:first + 9]):
            index, y = first + offset, 130 + offset * 33
            selected = index == page["selection"]
            if selected:
                pygame.draw.rect(self.screen, ACCENT, (18, y, 604, 29), border_radius=4)
            label = {"use": "✓  Use this folder", "new": "+  New folder...", "up": "..  Up one level"}.get(kind, "▸  " + name)
            self.text(label, 30, y + 5, 580, self.font, TEXT if selected or kind != "dir" else (221, 223, 232))
        if page.get("error"):
            self.text(page["error"], 18, 130 + min(len(rows), 9) * 33 + 4, 604, self.small, MUTED)
        elif status:
            self.text(status, 18, 418, 604, self.small, MUTED)
        self.footer("▲ ▼  Choose     A  Open / Use     B  Back")

    def detail(self, item, artwork, status, options=None):
        self.header(False)
        self.card(item, pygame.Rect(18, 110, 144, 240), artwork, False, False, False)
        self.wrap(item.get("SeriesName") or item.get("Name", "Untitled"), 185, 82, 434, 2, self.heading, TEXT)
        extra = [item.get("Type", ""), str(item.get("ProductionYear") or "")]
        if item.get("RunTimeTicks"):
            extra.append(str(round(item["RunTimeTicks"] / 600000000)) + " min")
        self.text(" · ".join(v for v in extra if v), 185, 143, 434, self.small, MUTED)
        if item.get("Type") == "Episode":
            self.text(item.get("Name"), 185, 166, 434, self.font, TEXT)
        self.wrap(item.get("Overview") or "No description available.", 185, 195, 432, 7)
        ticks = (item.get("UserData") or {}).get("PlaybackPositionTicks", 0)
        label = "Resume %d:%02d" % (ticks // 600000000, (ticks // 10000000) % 60) if ticks > 30000000 else "Play"
        if item.get("Type") in ("Movie", "Episode", "Video", "MusicVideo", "Audio"):
            pygame.draw.rect(self.screen, ACCENT, (185, 362, 212, 39), border_radius=5)
            self.text("A   " + label, 195, 371, 192, self.font, TEXT, center=True)
        else:
            self.text("This media type is not playable yet", 185, 370, 429, self.small, MUTED)
        self.text(status, 18, 414, 600, self.small, MUTED)
        self.footer("A  Play / Resume     X  Play options     Y  Watched     B  Back     Start  Exit")
        if options:
            pygame.draw.rect(self.screen, (30, 32, 41), (174, 268, 452, 150), border_radius=8)
            self.text("Play options", 192, 282, 400, self.heading)
            values = [("Play", "Start video"), ("Audio", options["audio"][options["audio_at"]]["label"]),
                      ("Subtitles", options["subtitles"][options["subtitle_at"]]["label"])]
            for index, (label, value) in enumerate(values):
                y = 315 + index * 30
                if index == options["row"]:
                    pygame.draw.rect(self.screen, ACCENT, (188, y - 3, 420, 25), border_radius=4)
                self.text(label, 201, y, 105, self.small, TEXT)
                self.text(value, 315, y, 280, self.small, TEXT)
            self.footer("D-pad  Choose     A  Change / Play     B  Save and return")

    def loading(self, label, modal=True):
        if modal:
            shade = pygame.Surface((640, 374), pygame.SRCALPHA)
            shade.fill((10, 10, 15, 218))
            self.screen.blit(shade, (0, 64))
        pygame.draw.rect(self.screen, (30, 32, 41), (116, 175, 408, 127), border_radius=9)
        self.spinner(320, 210, 13)
        self.text(label, 136, 244, 368, self.font, TEXT, center=True)

    def next_episode_splash(self, item):
        self.header(False)
        self.screen.blit(pygame.transform.smoothscale(self.logo, (70, 70)), (285, 105))
        self.text("Episode complete", 18, 205, 604, self.heading, center=True)
        self.text("Playing next episode", 18, 244, 604, self.font, ACCENT, center=True)
        self.text(item.get("SeriesName") or "Next episode", 18, 285, 604, self.font, TEXT, center=True)
        self.text("S%s E%s · %s" % (item.get("ParentIndexNumber", "?"), item.get("IndexNumber", "?"), item.get("Name", "")), 18, 315, 604, self.small, MUTED, center=True)
        self.spinner(320, 365, 12)
        self.footer("Preparing next episode...")

    def player(self, page, music, artwork, status=""):
        self.header(False)
        if page["view"] == "queue":
            self.queue_view(page, music)
            return
        queue, track = music.queue, music.current or {}
        cover = pygame.Rect(30, 90, 210, 210)
        pygame.draw.rect(self.screen, PANEL, cover, border_radius=6)
        surface, loading = artwork.get(track, cover.size, True) if artwork else (None, False)
        if surface:
            self.screen.blit(surface, (cover.centerx - surface.get_width() // 2,
                                       cover.centery - surface.get_height() // 2))
        elif loading:
            self.spinner(*cover.center)
        else:
            self.text("♪", cover.x, cover.centery - 30, cover.width, self.title, MUTED, center=True)
        x, width = 266, 356
        self.text("Now playing", x, 94, width, self.small, MUTED)
        if (track.get("UserData") or {}).get("IsFavorite"):
            self.text("♥ Favourite", x, 94, width - 4, self.small, ACCENT, right=True)
        self.wrap(track.get("Name") or "Untitled", x, 118, width, 2, self.heading, TEXT)
        self.text(", ".join(track.get("Artists") or []) or track.get("AlbumArtist"), x, 180, width, self.font, TEXT)
        self.text(track.get("Album"), x, 206, width, self.small, MUTED)
        self.text("Track %d of %d" % (queue.position + 1, len(queue.order)), x, 236, width, self.small, MUTED)
        chip_x = x
        chips = [("Shuffle", queue.shuffle), ("Repeat " + queue.repeat, queue.repeat != "off")]
        if music.sleep:
            chips.append(("Sleep " + sleep_label(music), True))
        for label, on in chips:
            chip = pygame.Rect(chip_x, 266, self.small.size(label)[0] + 22, 26)
            pygame.draw.rect(self.screen, ACCENT if on else PANEL, chip, border_radius=13)
            self.text(label, chip.x, chip.y + 4, chip.width, self.small, TEXT if on else MUTED, center=True)
            chip_x = chip.right + 8
        elapsed, duration = music.position()
        bar = pygame.Rect(30, 328, 580, 6)
        pygame.draw.rect(self.screen, PANEL, bar, border_radius=3)
        if duration:
            pygame.draw.rect(self.screen, ACCENT, (bar.x, bar.y, round(bar.width * min(1, elapsed / duration)), bar.height), border_radius=3)
        self.text(clock(elapsed), bar.x, 342, 100, self.small, MUTED)
        total = clock(duration) if duration else "--:--"
        self.text(total, bar.right - self.small.size(total)[0], 342, 100, self.small, MUTED)
        state = "Paused" if music.paused else ("Playing" if music.started else "Loading...")
        self.text(state, 18, 368, 604, self.heading, TEXT if music.paused else ACCENT, center=True)
        self.text(status or music.status, 18, 406, 604, self.small, MUTED, center=True)
        self.footer(("A  Play" if music.paused else "A  Pause") + "    ◀ ▶  Seek    ▲ ▼  Track    X  Queue    Y  Shuffle    R1  More    B  Back")
        options = page.get("options")
        if options:
            favourite = (track.get("UserData") or {}).get("IsFavorite")
            rows = [("Favourite", "♥ Yes" if favourite else "No"), ("Sleep timer", sleep_label(music)),
                    ("Saved queues", "Switch to another queue"), ("Screen", "Turn off now")]
            pygame.draw.rect(self.screen, (30, 32, 41), (120, 150, 400, 196), border_radius=8)
            self.text("Music options", 140, 164, 360, self.heading)
            for index, (label, value) in enumerate(rows):
                y = 202 + index * 34
                if index == options["row"]:
                    pygame.draw.rect(self.screen, ACCENT, (134, y - 4, 372, 28), border_radius=4)
                self.text(label, 148, y, 130, self.font, TEXT)
                self.text(value, 290, y + 2, 206, self.small, TEXT)
            self.footer("▲ ▼  Choose     A  Change     ◀ ▶  Sleep time     B  Close")

    def sessions(self, page, sessions, playing_key, status=""):
        self.header(False)
        self.text("Saved queues", 18, 79, 200, self.heading)
        message = "Press X again to remove it" if page.get("confirm") is not None else status
        self.text(message or "%d of %d kept" % (len(sessions), SAVED_QUEUES), 222, 84, 400, self.small,
                  ACCENT if message else MUTED, right=True)
        if not sessions:
            self.text("Nothing saved yet", 18, 230, 604, self.heading, MUTED, center=True)
        today = time.localtime()
        for index, session in enumerate(sessions[:SAVED_QUEUES]):
            queue = session_queue(session)
            track = queue.current or {}
            y, selected = 114 + index * 31, index == page["selection"]
            if selected:
                pygame.draw.rect(self.screen, ACCENT, (18, y, 604, 28), border_radius=4)
            playing = queue_key(session) == playing_key
            title = session.get("title") or track.get("Album") or track.get("Name") or "Queue"
            self.text(("▶ " if playing else "") + title, 28, y + 4, 214, self.font,
                      TEXT if selected or not playing else ACCENT)
            # The chapter you are on matters most for audiobooks.
            where = "%d/%d  %s · %s" % (queue.position + 1, len(queue.order), track.get("Name") or "",
                                        clock(session.get("elapsed") or 0))
            self.text(where, 250, y + 6, 238, self.small, TEXT if selected else MUTED)
            played = time.localtime(session.get("played_at") or 0)
            when = ("Today" if played[:3] == today[:3] else time.strftime("%d %b", played))
            self.text(when, 496, y + 6, 116, self.small, TEXT if selected else MUTED, right=True)
        self.footer("▲ ▼  Choose     A  Resume     X  Remove     B  Back")

    def queue_view(self, page, music):
        queue = music.queue
        tracks = queue.upcoming()
        self.text("Queue", 18, 79, 280, self.heading)
        mode = ("Shuffle on" if queue.shuffle else "In order") + " · Repeat " + queue.repeat
        self.text(mode, 622 - self.small.size(mode)[0], 84, 330, self.small, MUTED)
        rows = 10
        first = max(0, min(page["selection"] - rows // 2, len(tracks) - rows))
        for offset, track in enumerate(tracks[first:first + rows]):
            index, y = first + offset, 114 + offset * 31
            selected, playing = index == page["selection"], index == queue.position
            if selected:
                pygame.draw.rect(self.screen, ACCENT, (18, y, 604, 28), border_radius=4)
            self.text("▶" if playing else str(index + 1), 26, y + 5, 34, self.small,
                      TEXT if selected or playing else MUTED, center=True)
            self.text(track.get("Name") or "Untitled", 66, y + 4, 380, self.font,
                      TEXT if selected else (ACCENT if playing else TEXT))
            self.text(", ".join(track.get("Artists") or []), 456, y + 6, 104, self.small, TEXT if selected else MUTED)
            if track.get("RunTimeTicks"):
                self.text(clock(track["RunTimeTicks"] / 10000000), 566, y + 6, 50, self.small, TEXT if selected else MUTED)
        self.footer("▲ ▼  Choose     A  Play     Y  Shuffle     X  Repeat     B  Close")


class LibraryUI:
    def __init__(self, config, error, build, authenticate, play_item, prepare_playback, headers, verify, save_config, normalize_server_url=None):
        pygame.init()
        self.joysticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        for joystick in self.joysticks:
            joystick.init()
        self.screen = pygame.display.set_mode((640, 480))
        self.renderer = Renderer(self.screen, build)
        self.config, self.authenticate, self.play_item = config, authenticate, play_item
        self.prepare_playback = prepare_playback
        self.headers, self.verify, self.save_config, self.normalize_server_url = headers, verify, save_config, normalize_server_url or (lambda value: value)
        self.jobs = Jobs()
        self.api = self.artwork = None
        self.rows = Rows([])
        self.pages = []
        self.pending = None
        self.loading = ""
        self.status = error or ""
        self.reporter = None
        self.music = None
        self.downloads = None
        self.session_path = os.path.join(DATA_DIR, "music-queue.json")
        # Saved queues for every account on this device, most recently played first.
        self.sessions = []
        self.session_text = None
        self.session_save_at = 0
        self.backlight = Backlight()
        # Never leave the panel dark, even after an uncaught exception.
        atexit.register(self.backlight.turn_on)
        self.dark = False
        self.last_input = time.monotonic()
        self.media_keys = MediaKeys()
        # Started without a server: only downloads, saved queues and settings work.
        self.offline = False
        self.running = True
        self.home_generation = 0
        self.return_home_refresh = False
        self.repeat_at = 0
        self.direction = (0, 0)
        self.setup_page = None if config else {"kind": "setup", "selection": 0, "values": {"serverUrl": "", "username": "", "password": "", "quality": "480p"}}
        if config and config.get("password"):
            self.start("Connecting to Jellyfin...", lambda: authenticate(config), self.connected, self.connect_failed)
        elif config:
            self.setup_page = {"kind": "setup", "selection": 2, "values": dict(config)}

    def begin_input(self, key, label, secret=False):
        values = self.setup_page["values"] if self.setup_page else self.pages[-1]["values"]
        if not self.setup_page:
            self.setup_page = {"values": values, "return_settings": True}
        self.pages.append(dict(kind="input", target=key, title=label, placeholder="Enter " + label.lower(), secret=secret,
                               query=values.get(key, ""), layer=0, key_x=0, key_y=0, focus="keys",
                               items=[], searched=False, layers=[[list("1234567890-="), list("qwertyuiop[]"), list("asdfghjkl;'\\"), list("zxcvbnm,./") + [" ", "←"]], [list("!@#$%^&*()_+"), list("QWERTYUIOP{}"), list('ASDFGHJKL:\"|'), list("ZXCVBNM<>?") + [" ", "←"]]]))

    def finish_input(self, page):
        if page["target"] == "_new_folder":
            # A name for the folder browser, not a setting: nothing is saved to the config.
            self.pages.pop()
            self.setup_page = None
            self.make_folder(page["query"].strip())
            return
        self.setup_page["values"][page["target"]] = page["query"].strip()
        self.pages.pop()
        if self.setup_page.get("return_settings"):
            self.save_config(self.setup_page["values"])
            self.config = self.setup_page["values"]
            self.setup_page = None
            if page["target"] == "downloadPath" and self.pages and self.pages[-1]["kind"] == "storage":
                # A typed folder is checked like a listed one, then any pending download starts.
                self.choose_storage(self.config["downloadPath"])

    def begin_settings(self):
        self.pages.append(dict(kind="settings", selection=0, values=dict(self.config)))

    def settings_activate(self):
        page = self.pages[-1]
        if page.get("confirm_logout"):
            self.logout()
            return
        if page["selection"] == 0:
            page["values"]["quality"] = {"480p": "720p", "720p": "1080p", "1080p": "360p", "360p": "480p"}.get(page["values"].get("quality"), "480p")
        elif page["selection"] == 1:
            current = page["values"].get("screenOff", 30)
            choice = SCREEN_OFF_CHOICES.index(current) if current in SCREEN_OFF_CHOICES else -1
            page["values"]["screenOff"] = SCREEN_OFF_CHOICES[(choice + 1) % len(SCREEN_OFF_CHOICES)]
            self.config["screenOff"] = page["values"]["screenOff"]
        elif page["selection"] == 2:
            self.open_downloads()
        elif page["selection"] == 3:
            self.begin_input("serverUrl", "Server URL")
        elif page["selection"] == 4:
            self.begin_input("username", "Username")
        elif page["selection"] == 5:
            self.begin_input("password", "Password", True)
        elif page["selection"] == 6:
            page["confirm_logout"] = True
        else:
            self.pages.pop()
        if self.pages and self.pages[-1].get("kind") == "settings":
            self.save_config(page["values"])

    def setup_activate(self):
        page = self.setup_page
        keys = [("serverUrl", "Server URL", False), ("username", "Username", False), ("password", "Password", True)]
        if page["selection"] < 3:
            self.begin_input(*keys[page["selection"]]); return
        if page["selection"] == 3:
            values = page["values"]
            values["quality"] = {"480p": "720p", "720p": "360p", "360p": "480p"}.get(values.get("quality"), "480p"); return
        values = page["values"]
        if not all(values.get(key) for key in ("serverUrl", "username", "password")):
            self.status = "Complete server URL, username, and password first."; return
        values["serverUrl"] = self.normalize_server_url(values["serverUrl"])
        self.save_config(values); self.config = values; self.setup_page = None
        self.start("Connecting to Jellyfin...", lambda: self.authenticate(values), self.connected)

    def start(self, label, function, done, failed=None):
        if self.pending:
            self.jobs.cancel(self.pending)
        self.loading = label
        def completed(value, error):
            self.pending = None
            self.loading = ""
            if error:
                hint = {"HTTPError": "login/server rejected (check user, password, URL)",
                        "SSLError": "HTTPS certificate problem (try http:// and the server port)",
                        "ConnectionError": "server unreachable (check Wi-Fi, IP and port)",
                        "ConnectTimeout": "server unreachable (check Wi-Fi, IP and port)",
                        "ReadTimeout": "server timed out"}.get(error, error)
                self.status = "Could not load: " + str(hint) + ". Press X to retry."
                if failed:
                    failed(error)
            else:
                self.status = ""
                done(value)
        self.pending = self.jobs.submit(function, completed)

    def connected(self, credentials):
        token, user = credentials
        self.offline = self.renderer.offline = False
        self.api = LibraryAPI(self.config, token, user, self.headers(token), self.verify)
        if self.config.get("lastUserId") != user:
            # Remember the account so its downloads can be opened without a server.
            self.config["lastUserId"] = user
            self.save_config(self.config)
        self.open_account(user, token)
        self.refresh()

    def downloads_index(self, user):
        """One downloads index per account, kept with the app data (not on the card)."""
        account = hashlib.sha256((self.api.server + "\n" + user).encode()).hexdigest()[:12]
        return os.path.join(DATA_DIR, "downloads-" + account + ".json")

    def open_account(self, user, token):
        """Downloads, music and artwork for this account. Reconnecting the same account
        (e.g. coming back online) keeps them, so playing music is not interrupted."""
        index = self.downloads_index(user)
        if self.downloads and self.downloads.index_path == index:
            if self.offline:
                self.downloads.set_offline(True)
            else:
                self.downloads.connect(self.api.server, self.api.headers)
        else:
            if self.music:
                self.music.stop()
                self.music = None
            if self.downloads:
                self.downloads.close()
            self.downloads = Downloads(index, self.api.server, self.api.headers, self.verify,
                                       offline=self.offline)
        if not self.music:
            self.music = MusicPlayer(self.track_url, self.report_music, token)
            self.sessions = load_sessions(self.session_path)
            self.session_text = json.dumps(self.sessions, sort_keys=True)
        self.artwork = Artwork(self.api, os.path.join(DATA_DIR, "cache", "artwork"))

    def report_music(self, event, track, session_id, ticks, paused):
        # Looked up on each report: after reconnecting, reports go to the new login.
        return self.api.report_playing(event, track, session_id, ticks, paused)

    def connect_failed(self, error):
        """Can't reach the server: open what's on the device instead, if anything."""
        if error in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout"):
            self.go_offline()

    def go_offline(self):
        user = (self.config or {}).get("lastUserId")
        if not user or not self.config.get("serverUrl"):
            return False  # never logged in on this device: nothing to show
        self.offline = self.renderer.offline = True
        self.api = OfflineLibrary(self.config, user, self.verify)
        self.open_account(user, None)
        self.pages = [p for p in self.pages if p["kind"] in ("player", "sessions", "downloads")]
        downloaded = [dict(entry["parent"], _download=entry["id"]) for entry in self.downloads.listing()]
        self.rows = Rows([{"title": "Downloads", "items": downloaded, "loaded": True,
                           "empty": "Nothing downloaded yet: X to try the server again"}])
        self.status = "Offline: showing your downloads"
        LOG.info("Offline mode: %d downloads", len(downloaded))
        return True

    def refresh(self):
        if not self.api or self.offline:
            # Not connected yet, or X on the offline Home: try the server (again).
            if self.config:
                self.start("Connecting to Jellyfin...", lambda: self.authenticate(self.config),
                           self.connected, self.connect_failed)
            return
        self.home_generation += 1
        def ready(views):
            values = [{"title": "My Media", "items": views, "loaded": True},
                      {"title": "Next Up", "fetch": self.api.next_up, "empty": "You are all caught up"},
                      {"title": "Continue Watching", "fetch": self.api.resume, "empty": "No unfinished videos"}]
            downloaded = [dict(entry["parent"], _download=entry["id"]) for entry in self.downloads.listing()]
            if downloaded:
                values.insert(1, {"title": "Downloads", "items": downloaded, "loaded": True})
            if any(view.get("CollectionType") == "music" for view in views):
                values.append({"title": "Favourite Songs", "fetch": self.api.favourite_songs,
                               "empty": "Add favourites from Now Playing with R1"})
            for view in views:
                values.append({"title": "Recently Added in " + view.get("Name", "Library"),
                               "fetch": lambda parent=view["Id"]: self.api.latest(parent)})
            self.rows = Rows(values)
        self.start("Loading My Media...", self.api.views, ready)

    def logout(self):
        self.offline = self.renderer.offline = False
        if self.music:
            self.music.stop()
            self.save_music_session()
            self.music = None
        if self.downloads:
            self.downloads.close()
            self.downloads = None
        self.api = self.artwork = None
        self.pages = []
        self.rows = Rows([])
        values = dict(self.config or {})
        values["password"] = ""
        self.save_config(values)
        self.config = None
        self.setup_page = {"kind": "setup", "selection": 2, "values": values}
        self.status = "Logged out. Enter your details to connect again."

    def load_row(self, index):
        row = self.rows.values[index]
        if row.get("loaded") or row.get("loading"):
            return
        row["loading"] = True
        generation = self.home_generation
        def done(value, error):
            if generation != self.home_generation:
                return
            row["loading"] = False
            row["loaded"] = True
            row["items"] = value or []
            if row["items"] and self.artwork:
                self.artwork.prefetch(row["items"])
            if error:
                row["error"] = "Could not load this row. X to refresh."
        self.jobs.submit(row["fetch"], done)

    def open_item(self, item, library=False):
        if not item:
            return
        if item.get("_download"):
            self.play_download(item["_download"])
            return
        kind = item.get("Type", "")
        if library or item.get("IsFolder") or kind in ("Series", "Season", "Folder", "CollectionFolder", "UserView", "BoxSet"):
            def ready(result):
                items, total, more = result
                portrait = not items or items[0].get("Type") not in ("Episode", "Video", "MusicVideo", "Audio")
                self.pages.append(dict(kind="list", title=item.get("Name", "Library"), parent=item,
                                       items=items, total=total, more=more, next_start=len(items),
                                       selection=0, portrait=portrait, paging=False))
            self.start("Opening " + item.get("Name", "folder") + "...", lambda: self.api.children(item), ready)
        elif kind == "Audio":
            source = self.pages[-1] if self.pages and self.pages[-1]["kind"] == "list" else None
            if source:
                self.play_music(item, parent=source["parent"])
            elif not self.pages:
                # A home shelf such as Favourite Songs plays as its own queue.
                row = self.rows.values[self.rows.row]
                self.play_music(item, tracks=row.get("items"), title=row.get("title"))
            else:
                self.play_music(item)
        else:
            self.start("Loading details...", lambda: self.api.detail(item),
                       lambda value: self.pages.append(dict(kind="detail", item=value, options=None, track_options=None)))

    def toggle_played(self, item):
        played = not bool((item.get("UserData") or {}).get("Played"))
        label = "Marking watched..." if played else "Marking unwatched..."
        def done(value):
            self.status = "Marked watched" if played else "Marked unwatched"
            if self.pages and self.pages[-1].get("kind") == "detail":
                self.pages[-1]["item"] = value
        self.start(label, lambda: self.api.set_played(item, played), done)

    def play_chain(self, item, prepared=None):
        """Play item, then the next episode while each one plays to the end."""
        while True:
            self.screen, self.status, self.reporter = self.play_item(
                self.config, self.api.token, self.api.user_id, item, prepared=prepared)
            self.renderer.screen = self.screen
            if not (item.pop("_PlaybackCompleted", False) and item.get("Type") == "Episode"):
                break
            next_item = self.api.next_episode(item)
            if not next_item:
                break
            self.status = "Playing next episode..."
            self.renderer.next_episode_splash(next_item)
            pygame.display.flip()
            prepared = self.prepare_playback(
                self.config, self.api.token, self.api.user_id, next_item)
            item = next_item
        if self.pages and self.pages[-1].get("kind") == "detail":
            self.pages[-1]["item"] = item
        return item

    def play_music(self, item, parent=None, tracks=None, title=None):
        """Queue the album/playlist parent (or given tracks) starting at item; just item otherwise."""
        def work():
            queue = self.api.album_tracks(parent) if parent else [t for t in tracks or [] if t.get("Type") == "Audio"]
            ids = [track.get("Id") for track in queue]
            return (queue, ids.index(item["Id"])) if item.get("Id") in ids else ([item], 0)
        def ready(result):
            queue, start = result
            key = tuple(track.get("Id") for track in queue)
            saved = next((s for s in self.my_sessions() if queue_key(s) == key), None)
            if saved and (session_queue(saved).current or {}).get("Id") == item.get("Id"):
                # Picking the chapter you were on resumes mid-chapter (audiobooks);
                # any other track starts there.
                self.resume_session(saved)
                return
            self.save_music_session()
            self.music.play(queue, start, title or (parent or {}).get("Name") or item.get("Name"))
            self.open_player()
        self.start("Preparing music...", work, ready)

    def track_url(self, track):
        """Downloaded tracks always play from the card, even when online."""
        return (self.downloads and self.downloads.local_path(track["Id"])) or self.api.stream_url(track)

    def play_download(self, collection_id):
        entry = self.downloads.collections.get(collection_id)
        if not entry or not entry["tracks"]:
            return
        key = tuple(track["Id"] for track in entry["tracks"])
        saved = next((s for s in self.my_sessions() if queue_key(s) == key), None)
        if saved:
            self.resume_session(saved)  # audiobooks pick up where you left them
        else:
            self.save_music_session()
            self.music.play(entry["tracks"], 0, entry["title"])
            self.open_player()

    def download_page(self, page):
        """R1 on a music page: download the album/playlist it shows."""
        parent = page["parent"]
        if not self.config.get("downloadPath"):
            self.open_storage(then=lambda: self.download_page(page))
            return
        def notice(text):
            page["notice"] = (text, time.monotonic() + 4)
        state = self.downloads.status(parent["Id"])
        if state and state["state"] == "done":
            notice("Already downloaded · manage in Settings → Downloads")
            return
        if state and state["state"] in ("downloading", "queued"):
            notice("Already downloading")
            return
        folder = self.config["downloadPath"]
        def ready(tracks):
            if not tracks:
                notice("No tracks to download here")
                return
            self.downloads.add(parent["Id"], parent.get("Name") or page["title"], parent, tracks, folder)
            notice("Downloading %d tracks to %s" % (len(tracks), folder))
            # Cache the covers now (album card on Home, each track in Now Playing),
            # using the same item data the offline screens draw from.
            entry = self.downloads.collections[parent["Id"]]
            for item in [entry["parent"]] + entry["tracks"] if self.artwork else []:
                self.artwork.get(item, (188, 113), True)
        self.start("Preparing download...", lambda: self.api.album_tracks(parent), ready)

    def open_downloads(self):
        self.pages.append(dict(kind="downloads", selection=0, confirm=None))

    def open_storage(self, then=None):
        # The keyboard edits "values" in place, so the old location is kept apart for undo.
        self.pages.append(dict(kind="storage", selection=0, choices=storage_choices(),
                               values=dict(self.config), previous=self.config.get("downloadPath"), then=then))

    def choose_storage(self, path):
        """Use path for new downloads (existing ones stay playable where they are)."""
        picker = next((p for p in reversed(self.pages) if p["kind"] == "storage"), {})
        error = None
        if path and not on_real_storage(path):
            # e.g. /media/MEDIA while the card is not mounted: only RAM, lost at reboot.
            error = "%s is in memory, not on a card. Is the card mounted?" % path
        else:
            try:
                os.makedirs(path, exist_ok=True)
                probe = os.path.join(path, ".jellyfin-write-test")
                with open(probe, "w", encoding="ascii") as target:
                    target.write("ok")
                os.remove(probe)
            except OSError as exc:
                error = "Can't write to %s (%s)" % (path or "that folder", type(exc).__name__)
        if error:
            self.status = error
            if picker and self.config.get("downloadPath") == path:
                # A typed folder was already saved by the keyboard; put the old one back.
                self.config["downloadPath"] = picker["previous"]
                picker["values"]["downloadPath"] = picker["previous"]
                self.save_config(self.config)
            return
        self.config["downloadPath"] = path
        self.save_config(self.config)
        # Close the picker and any folder browser opened from it.
        while self.pages and self.pages[-1]["kind"] in ("storage", "folders"):
            self.pages.pop()
        page = picker
        for other in self.pages:
            if other["kind"] == "settings":
                other["values"]["downloadPath"] = path
        self.status = "Downloads go to " + path
        if page.get("then"):
            page["then"]()

    def open_folders(self, path=None):
        """Browse from path, or from the extra cards (/media), or /userdata."""
        path = path or next((p for p in ("/media", "/userdata") if os.path.isdir(p)), "/")
        dirs, error = list_folders(path)
        self.pages.append(dict(kind="folders", path=path, dirs=dirs, error=error, selection=0, values={},
                               memory=not on_real_storage(path)))

    def enter_folder(self, page, path):
        dirs, error = list_folders(path)
        page.update(path=path, dirs=dirs, error=error, selection=0, memory=not on_real_storage(path))

    def folders_activate(self, page):
        kind, name = folder_rows(page)[page["selection"]]
        if kind == "use":
            self.choose_storage(page["path"])
        elif kind == "new":
            self.begin_input("_new_folder", "New folder name")
        elif kind == "up":
            self.enter_folder(page, os.path.dirname(page["path"].rstrip("/")) or "/")
        else:
            self.enter_folder(page, os.path.join(page["path"], name))

    def make_folder(self, name):
        page = self.pages[-1]
        safe = safe_name(name, "")
        if not safe:
            page["error"] = "A folder needs a name"
            return
        path = os.path.join(page["path"], safe)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            page["error"] = "Can't create %s (%s)" % (safe, type(exc).__name__)
            return
        self.enter_folder(page, path)  # open it, ready for "Use this folder"

    def download_label(self, page):
        """For a page of tracks: where its offline copy stands (None: not a music page)."""
        if not self.downloads or not any(item.get("Type") == "Audio" for item in page["items"]):
            return None
        notice, until = page.get("notice") or ("", 0)
        if time.monotonic() < until:
            return notice
        state = self.downloads.status(page["parent"].get("Id"))
        if not state:
            return ""
        return {"done": "✓ Downloaded · %d tracks" % state["total"],
                "downloading": "Downloading %d/%d" % (state["done"] + 1, state["total"]),
                "queued": "Download queued",
                "paused": state["error"],
                "failed": "Download failed · retry in Settings → Downloads",
                "incomplete": "Partly downloaded · %d/%d" % (state["done"], state["total"])}[state["state"]]

    def downloads_activate(self, page):
        entries = self.downloads.listing()
        if page["selection"] == 0:
            self.open_storage()
        elif page["selection"] - 1 < len(entries):
            self.play_download(entries[page["selection"] - 1]["id"])

    def downloads_button(self, page, button):
        """X removes (pressed twice), Y retries or resumes."""
        entries = self.downloads.listing()
        if not 0 < page["selection"] <= len(entries):
            return
        entry = entries[page["selection"] - 1]
        if button == 6:
            if page["confirm"] != page["selection"]:
                page["confirm"] = page["selection"]
                return
            self.downloads.remove(entry["id"])
            page["confirm"] = None
            page["selection"] = min(page["selection"], len(entries) - 1)
            self.status = "Removed " + entry["title"]
        else:
            # Also for "done" entries: retry re-fetches files missing from the card.
            self.downloads.retry(entry["id"])
            self.status = "Checking " + entry["title"]

    def open_player(self):
        self.status = ""
        if not (self.pages and self.pages[-1]["kind"] == "player"):
            self.pages.append(dict(kind="player", view="now", selection=0, options=None))

    def account(self):
        return {"server": self.api.server, "user": self.api.user_id}

    def my_sessions(self):
        """Saved queues of the signed-in account only, most recently played first."""
        if not self.api:
            return []
        account = self.account()
        return [s for s in self.sessions if all(s.get(k) == v for k, v in account.items())]

    def save_music_session(self):
        """Fold the playing queue's progress into the saved queues and write the
        file only when something changed; position is kept to the nearest 10 s."""
        if not self.music or not self.api:
            return
        if self.music.finished_session:
            # Played to the end (an audiobook finished): nothing left to resume.
            self.sessions = forget_session(self.sessions, queue_key(self.music.finished_session), self.account())
            self.music.finished_session = None
        session = self.music.session()
        if not self.music.active:
            self.music.last_session = None  # consumed; a removed entry must not come back
        if session:
            session = dict(session, elapsed=int(session.get("elapsed") or 0) // 10 * 10)
            self.sessions = remember_session(self.sessions, session, self.account())
        text = json.dumps(self.sessions, sort_keys=True)
        if text != self.session_text:
            save_sessions(self.session_path, self.sessions)
            self.session_text = text

    def resume_session(self, session):
        # Save the queue being left first, so swapping books never loses its place.
        self.save_music_session()
        self.music.resume(session)
        if self.pages and self.pages[-1]["kind"] == "sessions":
            self.pages.pop()
        self.open_player()
        if self.pages[-1].get("view"):
            self.pages[-1]["view"] = "now"

    def open_sessions(self):
        self.pages.append(dict(kind="sessions", selection=0, confirm=None))

    def toggle_sessions(self):
        """R2: Saved queues from anywhere (not while typing); R2 again closes them."""
        if not self.api or not self.music or self.pending:
            return
        if self.pages and self.pages[-1]["kind"] in ("search", "input"):
            return
        if self.pages and self.pages[-1]["kind"] == "sessions":
            self.pages.pop()
            return
        self.save_music_session()  # so the playing queue shows its current position
        self.open_sessions()

    def remove_session(self, page):
        sessions = self.my_sessions()
        if not sessions:
            return
        session = sessions[page["selection"]]
        if self.music.active and self.music.queue and queue_key(session) == queue_key(self.music.session()):
            self.status = "This queue is playing; it can't be removed"
            return
        if page["confirm"] != page["selection"]:
            page["confirm"] = page["selection"]
            return
        self.sessions = forget_session(self.sessions, queue_key(session), self.account())
        page["confirm"] = None
        page["selection"] = max(0, min(page["selection"], len(self.my_sessions()) - 1))
        self.save_music_session()

    def toggle_favourite(self, track):
        favourite = not (track.get("UserData") or {}).get("IsFavorite")
        def done(value):
            self.status = "Added to favourites" if favourite else "Removed from favourites"
        self.start("Saving favourite...", lambda: self.api.set_favourite(track, favourite), done)

    def media_action(self, action):
        """Headset buttons drive the music from any screen, even with it turned off."""
        if not self.music or self.pending:
            return
        if not self.music.active:
            # Like a phone: play on the headset picks up the most recent queue.
            if action in ("play", "playpause") and self.my_sessions():
                self.resume_session(self.my_sessions()[0])
            return
        if (action == "playpause" or (action == "play" and self.music.paused)
                or (action == "pause" and not self.music.paused)):
            self.music.toggle_pause()
        elif action == "next":
            self.music.next()
        elif action == "previous":
            self.music.previous()

    def go_dark(self):
        self.dark = True
        self.direction = (0, 0)
        self.backlight.turn_off()
        # Without a sysfs backlight (desktop) a black frame still shows the state.
        self.screen.fill((0, 0, 0))
        pygame.display.flip()

    def wake(self):
        self.dark = False
        self.backlight.turn_on()
        self.last_input = time.monotonic()

    def player_button(self, button):
        """X, Y and R1 on the music screens; A/B go through activate() and back()."""
        page = self.pages[-1]
        if button == 8:
            page["options"] = None if page.get("options") else {"row": 0}
            page["view"] = "now"
            return True
        if page.get("options"):
            return button in (5, 6, 7)
        if button == 6:
            if page["view"] == "now":
                page["view"], page["selection"] = "queue", self.music.queue.position
            else:
                self.music.cycle_repeat()
            return True
        if button in (5, 7):
            self.music.toggle_shuffle()
            page["selection"] = self.music.queue.position
            return True
        return False

    def options_for(self, item):
        streams = ((item.get("MediaSources") or [{}])[0].get("MediaStreams")
                   or item.get("MediaStreams") or [])
        def label(stream):
            return stream.get("DisplayTitle") or stream.get("Language") or stream.get("Codec") or "Unknown"
        audio = [{"index": s.get("Index"), "label": label(s)} for s in streams if s.get("Type") == "Audio"]
        subtitles = [{"index": None, "label": "Off"}] + [{"index": s.get("Index"), "label": label(s)} for s in streams if s.get("Type") == "Subtitle"]
        return {"row": 0, "audio": audio or [{"index": None, "label": "Default"}], "audio_at": 0,
                "subtitles": subtitles, "subtitle_at": 0}

    def more(self, page):
        if not page["more"] or page["paging"]:
            return
        page["paging"] = True
        def done(result, error):
            page["paging"] = False
            if error:
                self.status = "Could not load more titles. Press X to retry."
                return
            items, total, more = result
            known = {item.get("Id") for item in page["items"]}
            page["items"].extend(item for item in items if item.get("Id") not in known)
            page["next_start"] += len(items)
            page["total"], page["more"] = total, bool(more and items)
        self.jobs.submit(lambda: self.api.children(page["parent"], page["next_start"]), done)

    def begin_search(self):
        parent = self.pages[-1].get("parent") if self.pages and self.pages[-1]["kind"] == "list" else None
        self.pages.append(dict(kind="search", parent=parent, query="", layer=0, key_x=0, key_y=0,
                               focus="keys", selection=0, items=[], searched=False,
                               layers=[
                                   [list("1234567890-="), list("qwertyuiop[]"), list("asdfghjkl;'\\"),
                                    list("zxcvbnm,./") + [" ", "←"]],
                                   [list("!@#$%^&*()_+"), list("QWERTYUIOP{}"), list('ASDFGHJKL:\"|'),
                                    list("ZXCVBNM<>?") + [" ", "←"]],
                               ]))

    def search_now(self, page):
        term = page["query"].strip()
        if not term:
            self.status = "Type something to search"
            return
        page["searched"] = True
        def done(result):
            page["items"], _ = result
            page["selection"] = 0
            page["focus"] = "results" if page["items"] else "keys"
        self.start("Searching Jellyfin...", lambda: self.api.search(term, page.get("parent")), done)

    def keyboard_button(self, button):
        page = self.pages[-1]
        if button == 8:
            page["layer"] = (page["layer"] + 1) % len(page["layers"])
            return True
        if button == 9 and page["kind"] == "input":
            self.finish_input(page)
            return True
        if button in (5, 7):  # Y: KNULLI exposes this as either index by input profile.
            if page["focus"] == "keys":
                page["query"] = page["query"][:-1]
            return True
        if button == 6:  # X: space, matching OD Commander.
            if page["focus"] == "keys":
                page["query"] += " "
            return True
        if button in (10, 11):  # Start / Select: confirm the search.
            self.finish_input(page) if page["kind"] == "input" else self.search_now(page)
            return True
        return False

    def move(self, x, y, repeat=False):
        if self.pending:
            return
        if not self.api and self.setup_page and not self.pages:
            if y: self.setup_page["selection"] = max(0, min(4, self.setup_page["selection"] - y))
            return
        if not self.pages:
            self.rows.move(x, y)
            return
        page = self.pages[-1]
        if page["kind"] == "sessions":
            if y:
                page["selection"] = max(0, min(len(self.my_sessions()) - 1, page["selection"] - y))
                page["confirm"] = None
            return
        if page["kind"] == "downloads":
            if y:
                # Row 0 is the location; collections follow.
                page["selection"] = max(0, min(len(self.downloads.listing()), page["selection"] - y))
                page["confirm"] = None
            return
        if page["kind"] == "storage":
            if y:  # the drives, then "Browse folders..." and "Type a folder..."
                page["selection"] = max(0, min(len(page["choices"]) + 1, page["selection"] - y))
            return
        if page["kind"] == "folders":
            if y:
                page["selection"] = max(0, min(len(folder_rows(page)) - 1, page["selection"] - y))
            return
        if page["kind"] == "player":
            options = page.get("options")
            if options:
                if y:
                    options["row"] = max(0, min(3, options["row"] - y))
                elif x and not repeat and options["row"] == 1:
                    self.music.cycle_sleep(x)
            elif page["view"] == "queue":
                if y:
                    page["selection"] = max(0, min(len(self.music.queue.order) - 1, page["selection"] - y))
            elif y and not repeat:
                # Holding up/down must not skip through the whole queue.
                # Up/down follow the queue list: up is the previous track.
                self.music.previous() if y > 0 else self.music.next()
            elif x:
                self.music.seek(10 * x)
            return
        if page["kind"] in ("search", "input"):
            if page["focus"] == "results":
                if x:
                    page["selection"] = max(0, min(len(page["items"]) - 1, page["selection"] + x))
                if y:
                    page["selection"] = max(0, min(len(page["items"]) - 1, page["selection"] - y))
                return
            rows = page["layers"][page["layer"]]
            if x:
                page["key_x"] = (page["key_x"] + x) % len(rows[page["key_y"]])
            if y:
                page["key_y"] = (page["key_y"] - y) % len(rows)
                page["key_x"] = min(page["key_x"], len(rows[page["key_y"]]) - 1)
            return
        if page["kind"] == "settings":
            if y: page["selection"] = max(0, min(7, page["selection"] - y))
            return
        if page["kind"] == "detail" and page.get("options"):
            options = page["options"]
            if y: options["row"] = max(0, min(2, options["row"] - y))
            if x and options["row"] == 1:
                options["audio_at"] = (options["audio_at"] + x) % len(options["audio"])
            if x and options["row"] == 2:
                options["subtitle_at"] = (options["subtitle_at"] + x) % len(options["subtitles"])
            return
        if page["kind"] != "list":
            return
        columns = 4 if page["portrait"] else 3
        page["selection"] = max(0, min(len(page["items"]) - 1, page["selection"] + x - y * columns))
        if len(page["items"]) - page["selection"] <= 8:
            self.more(page)

    def activate(self):
        if self.pending:
            return
        if not self.api and self.setup_page and not self.pages:
            self.setup_activate(); return
        if self.pages and self.pages[-1]["kind"] == "settings":
            self.settings_activate(); return
        if self.pages and self.pages[-1]["kind"] == "sessions":
            sessions = self.my_sessions()
            if sessions:
                self.resume_session(sessions[self.pages[-1]["selection"]])
            return
        if self.pages and self.pages[-1]["kind"] == "downloads":
            self.downloads_activate(self.pages[-1])
            return
        if self.pages and self.pages[-1]["kind"] == "storage":
            page = self.pages[-1]
            if page["selection"] < len(page["choices"]):
                self.choose_storage(page["choices"][page["selection"]]["path"])
            elif page["selection"] == len(page["choices"]):
                self.open_folders()
            else:
                self.begin_input("downloadPath", "Download folder")
            return
        if self.pages and self.pages[-1]["kind"] == "folders":
            self.folders_activate(self.pages[-1])
            return
        if self.pages and self.pages[-1]["kind"] == "player":
            page = self.pages[-1]
            options = page.get("options")
            if options:
                if options["row"] == 0:
                    self.toggle_favourite(self.music.current)
                elif options["row"] == 1:
                    self.music.cycle_sleep(1)
                elif options["row"] == 2:
                    page["options"] = None
                    self.save_music_session()  # so the list shows the current position
                    self.open_sessions()
                else:
                    page["options"] = None
                    self.go_dark()
            elif page["view"] == "queue":
                self.music.jump(page["selection"])
                page["view"] = "now"
            else:
                self.music.toggle_pause()
            return
        if not self.pages:
            self.open_item(self.rows.selected(), self.rows.row == 0)
        elif self.pages[-1]["kind"] in ("search", "input"):
            page = self.pages[-1]
            if page["focus"] == "results":
                if page["items"]:
                    self.open_item(page["items"][page["selection"]])
                return
            key = page["layers"][page["layer"]][page["key_y"]][page["key_x"]]
            page["query"] = page["query"][:-1] if key == "←" else page["query"] + key
        elif self.pages[-1]["kind"] == "list":
            page = self.pages[-1]
            if page["items"]:
                self.open_item(page["items"][page["selection"]])
        else:
            page = self.pages[-1]
            item = page["item"]
            if item.get("Type") not in ("Movie", "Episode", "Video", "MusicVideo", "Audio"):
                return
            options = page.get("options") or page.get("track_options")
            if options:
                page["track_options"] = options
            if page.get("options") and options["row"] == 1:
                    options["audio_at"] = (options["audio_at"] + 1) % len(options["audio"]); return
            if page.get("options") and options["row"] == 2:
                    options["subtitle_at"] = (options["subtitle_at"] + 1) % len(options["subtitles"]); return
            if options:
                audio_index = options["audio"][options["audio_at"]]["index"]
                subtitle_index = options["subtitles"][options["subtitle_at"]]["index"]
            else:
                audio_index = subtitle_index = None
            if self.reporter and not self.reporter.finished.is_set():
                self.status = "Saving previous playback..."
                return
            if item.get("Type") == "Audio":
                self.play_music(item); return
            if self.music.active:
                # Video takes the display and audio device; music cannot continue under it.
                self.music.stop()
            def ready(prepared):
                self.play_chain(item, prepared)
                self.renderer.screen = self.screen
                self.return_home_refresh = True
                self.direction = (0, 0)
                pygame.event.clear()
            self.start("Preparing video...", lambda: self.prepare_playback(
                self.config, self.api.token, self.api.user_id, item, audio_index, subtitle_index), ready)

    def back(self):
        if self.pending:
            self.jobs.cancel(self.pending)
            self.pending = None
            self.loading = ""
        elif self.pages and self.pages[-1].get("confirm_logout"):
            self.pages[-1]["confirm_logout"] = False
        elif self.pages and self.pages[-1].get("options"):
            # Keep the selected streams when returning to the details page.
            self.pages[-1]["track_options"] = self.pages[-1]["options"]
            self.pages[-1]["options"] = None
        elif self.pages and self.pages[-1]["kind"] == "search" and self.pages[-1]["focus"] == "results":
            self.pages[-1]["focus"] = "keys"
        elif self.pages and self.pages[-1]["kind"] == "player" and self.pages[-1].get("options"):
            self.pages[-1]["options"] = None
        elif self.pages and self.pages[-1]["kind"] == "player" and self.pages[-1]["view"] == "queue":
            self.pages[-1]["view"] = "now"
        elif not self.pages and self.music and self.music.active:
            self.open_player()
        elif not self.pages and self.my_sessions():
            self.open_sessions()
        elif self.pages:
            if self.pages[-1]["kind"] == "input" and self.setup_page and self.setup_page.get("return_settings"):
                self.setup_page = None
            self.pages.pop()
            # Offline, Home stays as is: reconnecting is X's job, not every Back press.
            if not self.pages and self.return_home_refresh and not self.reporter and not self.offline:
                self.return_home_refresh = False
                self.refresh()

    def draw(self):
        playing = self.music.current if self.music and self.music.active else None
        self.renderer.now_playing = (playing or {}).get("Name", "")
        # R2 always opens Saved queues, so B only needs a hint while music plays.
        self.renderer.music_hint = "Music" if playing else ""
        if not self.api:
            if self.pages and self.pages[-1]["kind"] == "input":
                self.renderer.keyboard(self.pages[-1], bool(self.pending))
            elif self.setup_page:
                self.renderer.setup(self.setup_page)
            else:
                self.renderer.header(); self.renderer.wrap(self.status or "Connect to your Jellyfin library", 82, 205, 476, 4, self.renderer.font); self.renderer.footer("X  Retry     Start  Exit")
        elif not self.pages:
            self.renderer.home(self.rows, self.artwork, self.load_row)
        elif self.pages[-1]["kind"] in ("search", "input"):
            page = self.pages[-1]
            self.renderer.keyboard(page, bool(self.pending))
        elif self.pages[-1]["kind"] == "settings":
            self.renderer.settings(self.pages[-1])
        elif self.pages[-1]["kind"] == "player":
            self.renderer.player(self.pages[-1], self.music, self.artwork, self.status)
        elif self.pages[-1]["kind"] == "sessions":
            playing = queue_key(self.music.session()) if self.music.active and self.music.queue else None
            self.renderer.sessions(self.pages[-1], self.my_sessions(), playing, self.status)
        elif self.pages[-1]["kind"] == "downloads":
            location = self.config.get("downloadPath")
            self.renderer.downloads(self.pages[-1], self.downloads.listing(), location,
                                    free_bytes(location) if location else 0, self.downloads.summary(), self.status)
        elif self.pages[-1]["kind"] == "storage":
            self.renderer.storage(self.pages[-1], self.pages[-1]["choices"], self.config.get("downloadPath"))
        elif self.pages[-1]["kind"] == "folders":
            self.renderer.folders(self.pages[-1], free_bytes(self.pages[-1]["path"]), self.status)
        elif self.pages[-1]["kind"] == "list":
            page = self.pages[-1]
            page["download"] = self.download_label(page)
            self.renderer.listing(page, self.artwork, bool(self.pending))
            if page["paging"]:
                self.renderer.spinner(606, 418, 7)
        else:
            page = self.pages[-1]
            self.renderer.detail(page["item"], self.artwork, self.status, page.get("options"))
        if self.pending:
            self.renderer.loading(self.loading)
            self.renderer.footer("Loading...     B  Cancel     Start  Exit")
        elif self.status.startswith("Could not") and self.api:
            self.renderer.footer(self.status)

    def run(self):
        clock = pygame.time.Clock()
        while self.running:
            self.jobs.poll()
            if self.artwork:
                self.artwork.jobs.poll()
            if self.reporter and self.reporter.finished.is_set():
                self.status = "Progress saved" if self.reporter.stop_ok else "Could not save progress to Jellyfin"
                self.reporter = None
            if self.music:
                was_active = self.music.active
                self.music.update()
                player_page = next((page for page in self.pages if page["kind"] == "player"), None)
                if player_page and not self.music.active:
                    # The queue ended or failed: leave the music screen with its message.
                    self.pages.remove(player_page)
                    self.status = self.music.status or "Music stopped"
                if was_active != self.music.active or time.monotonic() >= self.session_save_at:
                    self.save_music_session()
                    self.session_save_at = time.monotonic() + 5
            for action in self.media_keys.poll():
                self.media_action(action)
            for event in pygame.event.get():
                if event.type == pygame.KEYDOWN and event.key in MEDIA_KEYS:
                    # SDL may see headset buttons as keys too; MediaKeys already handled
                    # them, and they must not wake the screen in a pocket.
                    continue
                if (event.type in (pygame.JOYBUTTONDOWN, pygame.KEYDOWN)
                        or (event.type == pygame.JOYHATMOTION and event.value != (0, 0))):
                    self.last_input = time.monotonic()
                    if self.dark:
                        # The waking press only turns the screen back on.
                        self.wake()
                        continue
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.JOYHATMOTION:
                    self.direction = event.value
                    self.move(*self.direction)
                    self.repeat_at = time.monotonic() + 0.35
                elif event.type == pygame.JOYBUTTONDOWN:
                    if self.pages and self.pages[-1]["kind"] in ("search", "input") and self.keyboard_button(event.button):
                        continue
                    if self.pages and self.pages[-1]["kind"] == "player" and self.player_button(event.button):
                        continue
                    if self.pages and self.pages[-1]["kind"] == "downloads" and event.button in (5, 6, 7):
                        self.downloads_button(self.pages[-1], 6 if event.button == 6 else 5)
                        continue
                    if event.button == R2_BUTTON:
                        self.toggle_sessions()
                        continue
                    if event.button == 8 and not self.pages and self.api:
                        self.begin_settings()
                    elif event.button == 8 and self.pages and self.pages[-1].get("download") is not None:
                        self.download_page(self.pages[-1])
                    elif event.button in (9, 10, 11):
                        self.running = False
                    elif event.button == 4:
                        self.back()
                    elif event.button == 3:
                        self.activate()
                        break
                    elif event.button == 6:
                        if self.pages and self.pages[-1]["kind"] == "sessions":
                            self.remove_session(self.pages[-1])
                        elif self.pages and self.pages[-1]["kind"] == "detail":
                            page = self.pages[-1]
                            page["options"] = page.get("track_options") or self.options_for(page["item"])
                            page["track_options"] = page["options"]
                        elif self.pages and self.pages[-1]["kind"] == "list":
                            page = self.pages[-1]
                            if self.status.startswith("Could not load more"):
                                self.status = ""
                                self.more(page)
                            elif page["items"]:
                                self.toggle_played(page["items"][page["selection"]])
                        else:
                            self.refresh()
                    elif event.button in (5, 7):
                        if self.pages and self.pages[-1]["kind"] == "detail":
                            self.toggle_played(self.pages[-1]["item"])
                        else:
                            self.begin_search()
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self.running = False
                    elif event.key == pygame.K_RETURN:
                        self.activate()
                        break
                    elif event.key == pygame.K_BACKSPACE:
                        self.back()
                    elif event.key in (pygame.K_LSHIFT, pygame.K_RSHIFT):
                        if self.pages and self.pages[-1]["kind"] == "search":
                            page = self.pages[-1]
                            page["layer"] = (page["layer"] + 1) % len(page["layers"])
                    elif event.key in (pygame.K_x, pygame.K_y, pygame.K_o) and self.pages and self.pages[-1]["kind"] == "player":
                        self.player_button({pygame.K_x: 6, pygame.K_y: 5, pygame.K_o: 8}[event.key])
                    elif event.key == pygame.K_x and self.pages and self.pages[-1]["kind"] == "sessions":
                        self.remove_session(self.pages[-1])
                    elif event.key in (pygame.K_x, pygame.K_y) and self.pages and self.pages[-1]["kind"] == "downloads":
                        self.downloads_button(self.pages[-1], 6 if event.key == pygame.K_x else 5)
                    elif event.key == pygame.K_o and self.pages and self.pages[-1].get("download") is not None:
                        self.download_page(self.pages[-1])
                    elif event.key == pygame.K_q:
                        self.toggle_sessions()
                    elif event.key == pygame.K_y and not self.pages:
                        self.begin_search()
                    elif event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
                        self.move(*{pygame.K_LEFT: (-1, 0), pygame.K_RIGHT: (1, 0), pygame.K_UP: (0, 1), pygame.K_DOWN: (0, -1)}[event.key])
            if self.direction != (0, 0) and time.monotonic() >= self.repeat_at:
                self.move(*self.direction, repeat=True)
                self.repeat_at = time.monotonic() + 0.15
            timeout = (self.config or {}).get("screenOff", 30)
            if (not self.dark and timeout and self.music and self.music.active and not self.music.paused
                    and not self.pending and time.monotonic() - self.last_input > timeout):
                self.go_dark()
            if self.dark:
                # Nothing to draw; a slow loop still feeds mpv IPC and the sleep timer.
                clock.tick(5)
                continue
            self.draw()
            pygame.display.flip()
            clock.tick(30)
        self.backlight.turn_on()
        self.media_keys.close()
        reporters = [self.reporter, self.music.stop() if self.music else None]
        self.save_music_session()
        deadline = time.monotonic() + 25
        while (any(r and not r.finished.is_set() for r in reporters)
               and time.monotonic() < deadline):
            pygame.event.pump()
            self.renderer.header()
            self.renderer.loading("Saving playback progress...", False)
            pygame.display.flip()
            clock.tick(30)
        pygame.quit()
