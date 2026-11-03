"""The reading screen: EPUB text laid out into pages, and comics/PDFs page by page.

Positions are stable across text sizes: a text page is identified by where it
starts in the book (chapter, block, character), not by its page number, so
changing the size or reopening the book lands on the same sentence.
"""
import io
import re
import threading
import time
import zipfile

import pygame

SIZES = (17, 20, 24)
SIZE_NAMES = ("Small", "Medium", "Large")
THEMES = {"dark": {"bg": (18, 18, 22), "text": (226, 224, 220), "muted": (130, 132, 140)},
          "light": {"bg": (244, 239, 227), "text": (40, 36, 30), "muted": (120, 112, 100)}}
LEFT, WIDTH, TOP, BOTTOM = 24, 592, 34, 448  # the text area on the 640x480 screen
WORDS = re.compile(r"\n|[^\S\n]+|\S+")


def status_bar(screen, renderer, colours, left, right):
    """Top line: where you are on the left, battery and clock on the right."""
    renderer.text(left, 16, 8, 440, renderer.small, colours["muted"])
    level, charging = renderer.battery()
    clock = time.strftime("%H:%M") + ("   %s%d%%" % ("⚡" if charging else "", level) if level is not None else "")
    renderer.text(clock, 456, 8, 168, renderer.small, colours["muted"], right=True)
    renderer.text(right, 16, 456, 608, renderer.small, colours["muted"])


class TextReader:
    """An EPUB, paginated one chapter at a time as it is reached."""
    kind = "text"

    def __init__(self, book, font_path, position=None, size=1, theme="dark"):
        self.book, self.font_path = book, font_path
        self.size, self.theme = max(0, min(len(SIZES) - 1, size)), theme if theme in THEMES else "dark"
        self.archive = zipfile.ZipFile(book["zip"])
        self.images, self.rendered = {}, {}
        self._fonts()
        chapter, block, char = (list(position) + [0, 0, 0])[:3] if position else (0, 0, 0)
        self._go(max(0, min(len(book["chapters"]) - 1, chapter)), block, char)

    # ---- layout ---------------------------------------------------------------
    def _fonts(self):
        def font(points, bold=False, italic=False):
            made = pygame.font.Font(self.font_path, points)
            made.set_bold(bold)
            made.set_italic(italic)
            return made
        points = SIZES[self.size]
        self.fonts = {(b, i): font(points, b, i) for b in (False, True) for i in (False, True)}
        self.heading = {(b, i): font(round(points * 1.3), True, i) for b in (False, True) for i in (False, True)}
        self.pages = {}  # chapter -> (pages, starts)
        self.rendered = {}

    def _image(self, source):
        if source not in self.images:
            try:
                surface = pygame.image.load(io.BytesIO(self.archive.read(source)))
                width, height = surface.get_size()
                scale = min(1, WIDTH / width, (BOTTOM - TOP) * 0.9 / height)
                self.images[source] = pygame.transform.smoothscale(
                    surface, (max(1, int(width * scale)), max(1, int(height * scale))))
            except (KeyError, pygame.error, ValueError, ZeroDivisionError):
                self.images[source] = None
        return self.images[source]

    def _lines(self, block):
        """(height, drawing ops, character offset where the line starts) for a block."""
        if block["kind"] == "img":
            surface = self._image(block["src"])
            return [(surface.get_height() + 6, [("img", (WIDTH - surface.get_width()) // 2, surface)], 0)] if surface else []
        if block["kind"] == "hr":
            return [(self.fonts[(False, False)].get_linesize(), [("hr",)], 0)]
        fonts = self.heading if block["kind"] == "h" else self.fonts
        height = fonts[(False, False)].get_linesize()
        lines, ops, x, start, pos, space = [], [], 0, 0, 0, False
        for text, bold, italic in block["runs"]:
            font = fonts[(bold, italic)]
            for token in WORDS.findall(text):
                if token == "\n":
                    lines.append((height, ops, start))
                    ops, x, space, start = [], 0, False, pos + 1
                    pos += 1
                    continue
                if token.isspace():
                    space, pos = x > 0, pos + len(token)
                    continue
                gap = font.size(" ")[0] if space else 0
                width = font.size(token)[0]
                if x and x + gap + width > WIDTH:
                    lines.append((height, ops, start))
                    ops, x, gap, start = [], 0, 0, pos
                while width > WIDTH:  # a word longer than the line: break it
                    cut = max(1, next((n for n in range(len(token), 0, -1) if font.size(token[:n])[0] <= WIDTH), 1))
                    ops.append(("text", 0, token[:cut], font))
                    lines.append((height, ops, start))
                    token, pos, ops, start = token[cut:], pos + cut, [], pos + cut
                    width = font.size(token)[0]
                ops.append(("text", x + gap, token, font))
                x, pos, space = x + gap + width, pos + len(token), False
        if ops:
            lines.append((height, ops, start))
        return lines

    def _paginate(self, chapter):
        if chapter not in self.pages:
            pages, starts, y = [[]], [(0, 0)], 0
            gap = self.fonts[(False, False)].get_linesize() // 2
            for index, block in enumerate(self.book["chapters"][chapter]["blocks"]):
                lines = self._lines(block)
                if lines and pages[-1]:
                    y += gap * (2 if block["kind"] == "h" else 1)
                for height, ops, char in lines:
                    if y + height > BOTTOM - TOP and pages[-1]:
                        pages.append([])
                        starts.append((index, char))
                        y = 0
                    pages[-1].append((y, ops))
                    y += height
            self.pages[chapter] = (pages, starts)
        return self.pages[chapter]

    def _go(self, chapter, block=0, char=0):
        """Show the page of chapter that contains (block, char)."""
        self.chapter = chapter
        _, starts = self._paginate(chapter)
        self.page = max(i for i, start in enumerate(starts) if i == 0 or start <= (block, char))

    # ---- navigation -------------------------------------------------------------
    def next(self):
        pages, _ = self._paginate(self.chapter)
        if self.page + 1 < len(pages):
            self.page += 1
        elif self.chapter + 1 < len(self.book["chapters"]):
            self._go(self.chapter + 1)

    def previous(self):
        if self.page > 0:
            self.page -= 1
        elif self.chapter > 0:
            pages, _ = self._paginate(self.chapter - 1)
            self.chapter, self.page = self.chapter - 1, len(pages) - 1

    def move(self, x, y):
        # Left/right and up/down all turn pages: whatever the thumb finds first.
        if x > 0 or y < 0:
            self.next()
        elif x < 0 or y > 0:
            self.previous()

    def jump(self, chapter):
        self._go(max(0, min(len(self.book["chapters"]) - 1, chapter)))

    def at_end(self):
        pages, _ = self._paginate(self.chapter)
        return self.chapter == len(self.book["chapters"]) - 1 and self.page == len(pages) - 1

    def position(self):
        _, starts = self._paginate(self.chapter)
        return [self.chapter] + list(starts[self.page])

    def percent(self):
        chars = self.book["chars"]
        chapter, block, char = self.position()
        blocks = self.book["chapters"][chapter]["blocks"]
        done = sum(chars[:chapter]) + sum(len("".join(t for t, _, _ in b["runs"])) + 1 for b in blocks[:block]) + char
        return min(1.0, done / max(1, sum(chars)))

    # ---- settings ------------------------------------------------------------------
    def cycle_size(self):
        chapter, block, char = self.position()
        self.size = (self.size + 1) % len(SIZES)
        self._fonts()
        self._go(chapter, block, char)

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.rendered = {}

    # ---- drawing -----------------------------------------------------------------
    def _surface(self):
        key = (self.chapter, self.page, self.theme, self.size)
        if key not in self.rendered:
            colours = THEMES[self.theme]
            surface = pygame.Surface((WIDTH, BOTTOM - TOP))
            surface.fill(colours["bg"])
            pages, _ = self._paginate(self.chapter)
            for y, ops in pages[self.page]:
                for op in ops:
                    if op[0] == "text":
                        surface.blit(op[3].render(op[2], True, colours["text"]), (op[1], y))
                    elif op[0] == "img":
                        surface.blit(op[2], (op[1], y))
                    else:
                        pygame.draw.line(surface, colours["muted"], (WIDTH // 3, y + 9), (WIDTH * 2 // 3, y + 9))
            if len(self.rendered) > 6:
                self.rendered.clear()
            self.rendered[key] = surface  # drawn once, then blitted every frame
        return self.rendered[key]

    def draw(self, screen, renderer):
        colours = THEMES[self.theme]
        screen.fill(colours["bg"])
        screen.blit(self._surface(), (LEFT, TOP))
        pages, _ = self._paginate(self.chapter)
        status_bar(screen, renderer, colours, self.book["chapters"][self.chapter]["title"],
                   "Page %d/%d of this part · %d%% of the book      A  Next   Y  Back   X  Size   R1  Menu   B  Close"
                   % (self.page + 1, len(pages), self.percent() * 100))


# Comic/PDF zoom levels: the whole page, the page width, then 25% steps past the width.
ZOOMS = ("page", "width", 1.25, 1.5, 1.75, 2.0, 2.25, 2.5)
ZOOM_NAMES = ("Whole page", "Fit width", "125%", "150%", "175%", "200%", "225%", "250%")
ROTATIONS = (0, 90, 270)
ROTATION_NAMES = {0: "None", 90: "Turned right", 270: "Turned left"}
MAX_PIXELS = 4000000  # a zoomed page this big is ~16 MB: enough on a 1 GB handheld
PAN = 160


class ImageReader:
    """Comics and PDFs: one page image at a time, from the whole page up to 2.5x the
    screen width, panned with the D-pad. Pages decode (and PDFs render) in a
    background thread; the next one is fetched ahead so turning feels instant."""
    kind = "image"

    def __init__(self, pages, position=None, zoom=0, rotation=0):
        self.pages = pages
        self.zoom = max(0, min(len(ZOOMS) - 1, zoom if isinstance(zoom, int) else 0))
        # 0, or 90/270: the page turned for reading with the handheld held upright.
        self.rotation = rotation if rotation in ROTATIONS else 0
        self.index = max(0, min(len(pages) - 1, (position or [0])[0]))
        self.raw, self.errors, self.scaled = {}, {}, {}
        self.x = self.y = 0  # top-left of the screen, in page pixels
        self.loading = set()
        self.lock = threading.Lock()
        self.hint_until = time.monotonic() + 4  # the buttons, briefly, when a comic opens
        self.close_until = 0                    # B at the whole page: press again to close
        self._want(self.index)

    def back(self):
        """B: zoom out; at the whole page, a second press closes. Returns True to close."""
        if self.zoom:
            self.set_zoom(self.zoom - 1)
            return False
        if time.monotonic() < self.close_until:
            return True
        self.close_until = time.monotonic() + 2
        return False

    @property
    def fit(self):
        """For the settings: "page" for the whole page, "width" for anything closer."""
        return "page" if self.zoom == 0 else "width"

    def _load(self, index):
        try:
            surface = pygame.image.load(io.BytesIO(self.pages.page(index)))
            with self.lock:
                self.raw[index] = surface
        except Exception as exc:  # BookError from a renderer, or a broken image
            with self.lock:
                self.errors[index] = str(exc) if str(exc) else "This page can't be shown"
        finally:
            with self.lock:
                self.loading.discard(index)

    def _want(self, index):
        for wanted in (index, index + 1):
            if 0 <= wanted < len(self.pages):
                with self.lock:
                    if wanted in self.raw or wanted in self.errors or wanted in self.loading:
                        continue
                    self.loading.add(wanted)
                threading.Thread(target=self._load, args=(wanted,), daemon=True).start()
        with self.lock:  # keep memory bounded: a few decoded pages at most
            for old in [i for i in self.raw if abs(i - index) > 2]:
                del self.raw[old]
        self.scaled = {k: v for k, v in self.scaled.items() if k[0] == index}

    @property
    def view(self):
        """The screen as the reader sees it: 640x480, or 480x640 when rotated."""
        return (640, 480) if self.rotation == 0 else (480, 640)

    def _scale(self, width, height):
        level, (view_w, view_h) = ZOOMS[self.zoom], self.view
        whole, across = min(view_w / width, view_h / height), view_w / width
        scale = whole if level == "page" else across * (1 if level == "width" else level)
        return max(whole, min(scale, (MAX_PIXELS / (width * height)) ** 0.5))

    def _page(self, index):
        with self.lock:
            raw = self.raw.get(index)
        if raw is None:
            return None
        key = (index, self.zoom, self.rotation)
        if key not in self.scaled:
            width, height = raw.get_size()
            scale = self._scale(width, height)
            self.scaled = {k: v for k, v in self.scaled.items()
                           if k[0] == index and k[2] == self.rotation and abs(k[1] - self.zoom) <= 1}
            page = pygame.transform.smoothscale(raw.convert(), (max(1, int(width * scale)), max(1, int(height * scale))))
            # Rotated once here, so each frame is a plain blit. Sizes stay in view terms.
            turned = pygame.transform.rotate(page, -90 if self.rotation == 90 else 90) if self.rotation else page
            self.scaled[key] = (turned, page.get_size())
        return self.scaled[key]

    def _size(self, index):
        """The page's size as the reader sees it (before turning it onto the screen)."""
        page = self._page(index)
        return page[1] if page else None

    def _overflow(self):
        size = self._size(self.index)
        if size is None:
            return 0, 0
        return max(0, size[0] - self.view[0]), max(0, size[1] - self.view[1])

    def _turn(self, step):
        target = self.index + step
        if 0 <= target < len(self.pages):
            self.index = target
            # Forward starts at the top-left, back lands on the bottom-right (reading order);
            # the far edge is clamped once the page's size is known (see draw).
            self.x = self.y = 0 if step > 0 else 10 ** 6
            self._want(target)
            return True
        return False

    def next(self):
        self._turn(1)

    def previous(self):
        self._turn(-1)

    def move(self, x, y):
        """D-pad: pan around a page bigger than the screen. Only along a direction with
        nothing to pan (e.g. left/right at fit width) does it turn pages instead: A and
        Y turn pages, so a zoomed-in pan never flips the page by accident."""
        # Held upright, the D-pad turns with the handheld: map it to the page's directions.
        if self.rotation == 90:
            x, y = -y, x
        elif self.rotation == 270:
            x, y = y, -x
        wide, tall = self._overflow()
        self.x, self.y = min(self.x, wide), min(self.y, tall)
        if x:
            if wide:
                self.x = max(0, min(wide, self.x + x * PAN))
            else:
                self._turn(1 if x > 0 else -1)
        elif y:
            if tall:
                self.y = max(0, min(tall, self.y - y * PAN))
            else:
                self._turn(1 if y < 0 else -1)

    def set_zoom(self, level):
        """Zoom in or out around the middle of the screen."""
        level = max(0, min(len(ZOOMS) - 1, level))
        view_w, view_h = self.view
        old = self._size(self.index)
        if old is not None:
            centre = ((self.x + min(view_w, old[0]) / 2) / old[0], (self.y + min(view_h, old[1]) / 2) / old[1])
        self.zoom = level
        new = self._size(self.index)
        if old is not None and new is not None:
            self.x = int(centre[0] * new[0] - min(view_w, new[0]) / 2)
            self.y = int(centre[1] * new[1] - min(view_h, new[1]) / 2)
            wide, tall = self._overflow()
            self.x, self.y = max(0, min(wide, self.x)), max(0, min(tall, self.y))

    def zoom_in(self):
        self.set_zoom(self.zoom + 1)

    def zoom_out(self):
        self.set_zoom(self.zoom - 1)

    def toggle_fit(self):
        self.set_zoom(0 if self.zoom else 1)

    def rotate(self, rotation):
        """0 (as is), 90 (turned right) or 270 (turned left); starts at the page's top."""
        self.rotation = rotation if rotation in ROTATIONS else 0
        self.x = self.y = 0

    def jump(self, index):
        self._turn(max(0, min(len(self.pages) - 1, index)) - self.index)
        self.x = self.y = 0

    def at_end(self):
        return self.index == len(self.pages) - 1

    def position(self):
        return [self.index]

    def percent(self):
        return (self.index + 1) / len(self.pages)

    def draw(self, screen, renderer):
        screen.fill((0, 0, 0))
        page = self._page(self.index)
        view_w, view_h = self.view
        if page is not None:
            turned, (width, height) = page
            wide, tall = self._overflow()
            self.x, self.y = max(0, min(wide, self.x)), max(0, min(tall, self.y))
            # Where the page's top-left sits in the reader's view, then on the screen.
            left = (view_w - width) // 2 if not wide else -self.x
            top = (view_h - height) // 2 if not tall else -self.y
            screen.blit(turned, self._to_screen(left, top, width, height))
        elif self.index in self.errors:
            renderer.wrap(self.errors[self.index], 120, 220, 400, 3, renderer.font, (200, 200, 200))
        else:
            renderer.spinner(320, 240, 14)
        # A small counter that never covers much of the page; the zoom once zoomed in.
        label = "%d / %d" % (self.index + 1, len(self.pages)) + ("  ·  " + ZOOM_NAMES[self.zoom] if self.zoom > 1 else "")
        text = renderer.small.render(label, True, (200, 200, 200))
        badge = pygame.Surface((text.get_width() + 16, 22))
        badge.blit(text, (8, (22 - text.get_height()) // 2))
        if self.rotation:  # readable the way the handheld is held
            badge = pygame.transform.rotate(badge, -90 if self.rotation == 90 else 90)
            width, height = badge.get_height(), badge.get_width()
        else:
            width, height = badge.get_size()
        screen.blit(badge, self._to_screen(view_w - width - 16, view_h - height - 6, width, height))
        if time.monotonic() < self.close_until:
            self._note(screen, renderer, "Press B again to close")
        elif time.monotonic() < self.hint_until:
            self._note(screen, renderer, "A next  ·  Y back  ·  X / B zoom  ·  D-pad move  ·  R1 menu")

    def _note(self, screen, renderer, message):
        """A line of text across the top of the page, readable the way it's held."""
        text = renderer.small.render(message, True, (230, 230, 230))
        box = pygame.Surface((min(text.get_width() + 20, self.view[0] - 20), 26))
        box.blit(text, (10, 5))
        if self.rotation:
            box = pygame.transform.rotate(box, -90 if self.rotation == 90 else 90)
            width, height = box.get_height(), box.get_width()
        else:
            width, height = box.get_size()
        screen.blit(box, self._to_screen((self.view[0] - width) // 2, 8, width, height))

    def _to_screen(self, left, top, width, height):
        """Screen position of a width x height box placed at (left, top) in the view."""
        view_w, view_h = self.view
        if self.rotation == 90:   # view turned clockwise onto the screen
            return view_h - top - height, left
        if self.rotation == 270:  # counter-clockwise
            return top, view_w - left - width
        return left, top
