"""E-books and comics for the reader: EPUB text extraction, comic and PDF page
sources, the local cache of opened books, and reading progress.

Nothing here draws: reader.py lays the text out and shows the pages. The device
ships only Python, so EPUB is parsed with the standard library; CBR and PDF use
whichever command-line tool KNULLI happens to have (see EXTRACTORS / PDF_TOOLS).
"""
import html.parser
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import time
import zipfile
from xml.etree import ElementTree

import requests

from downloads import file_extension

LOG = logging.getLogger("jellyfin.books")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
BOOK_FIELDS = ("Id", "Name", "Type", "ImageTags", "Overview", "ProductionYear", "UserData", "Container")


class BookError(Exception):
    """A book that can't be shown, with a message meant for the screen."""


def detect(path):
    """epub, cbz, cbr, cb7 or pdf, from the file's first bytes (extensions lie:
    plenty of .cbr files are really zips)."""
    with open(path, "rb") as source:
        head = source.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"Rar!"):
        return "cbr"
    if head.startswith(b"7z\xbc\xaf"):
        return "cb7"
    if head.startswith(b"PK"):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "META-INF/container.xml" in names:
                return "epub"
        return "cbz"
    raise BookError("Unknown book format")


# ---- EPUB --------------------------------------------------------------------

BLOCK_TAGS = {"p", "div", "li", "blockquote", "section", "article", "pre", "dd", "dt", "figcaption", "td",
              "tr", "table", "ul", "ol", "body", "header", "footer", "aside", "figure", "nav", "main",
              "h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "head", "title", "svg"}


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


class _Blocks(html.parser.HTMLParser):
    """XHTML -> blocks: {"kind": "p"|"h"|"img"|"hr", "level", "runs": [(text, bold, italic)]}."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.blocks, self.runs = base, [], []
        self.bold = self.italic = self.skip = self.pre = 0
        self.kind, self.level = "p", 0

    def flush(self):
        runs = []
        for text, bold, italic in self.runs:
            if not self.pre:
                text = re.sub(r"[ \t\r\n\f\v]+", " ", text) if text != "\n" else text
            if runs and runs[-1][0].endswith((" ", "\n")) and text.startswith(" "):
                text = text.lstrip(" ")
            if text:
                runs.append((text, bold, italic))
        while runs and not runs[0][0].strip():
            runs.pop(0)
        if runs:
            runs[0] = (runs[0][0].lstrip(), runs[0][1], runs[0][2])
            runs[-1] = (runs[-1][0].rstrip(), runs[-1][1], runs[-1][2])
        if any(text.strip() for text, _, _ in runs):
            self.blocks.append({"kind": self.kind, "level": self.level, "runs": runs})
        self.runs, self.kind, self.level = [], "p", 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in BLOCK_TAGS:
            self.flush()
            if len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
                self.kind, self.level = "h", int(tag[1])
        if tag in ("b", "strong"):
            self.bold += 1
        elif tag in ("i", "em", "cite", "var"):
            self.italic += 1
        elif tag == "pre":
            self.pre += 1
        elif tag == "br":
            self.runs.append(("\n", self.bold > 0, self.italic > 0))
        elif tag == "hr":
            self.flush()
            self.blocks.append({"kind": "hr", "level": 0, "runs": []})
        elif tag in ("img", "image"):
            values = dict(attrs)
            source = values.get("src") or values.get("xlink:href") or values.get("href")
            if source and not source.startswith("data:"):
                self.flush()
                path = posixpath.normpath(posixpath.join(posixpath.dirname(self.base), source.split("#")[0]))
                self.blocks.append({"kind": "img", "level": 0, "runs": [], "src": path})

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        elif tag in ("i", "em", "cite", "var"):
            self.italic = max(0, self.italic - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
        if tag in BLOCK_TAGS:
            self.flush()

    def handle_data(self, data):
        if not self.skip:
            self.runs.append((data, self.bold > 0, self.italic > 0))

    def close(self):
        super().close()
        self.flush()


class _Links(html.parser.HTMLParser):
    """(href, text) of the links in an EPUB 3 navigation document's first <nav>."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.href, self.text, self.depth, self.done = [], None, [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "nav" and not self.done:
            self.depth += 1
        elif tag == "a" and self.depth:
            self.href, self.text = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag == "nav" and self.depth:
            self.depth -= 1
            self.done = not self.depth
        elif tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join("".join(self.text).split())))
            self.href = None

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)


def parse_epub(path):
    """{"title", "chapters": [{"title", "blocks"}], "toc": [(title, chapter)], "chars": [...]}
    chapters follow the spine (reading order); toc lists the book's own contents."""
    try:
        archive = zipfile.ZipFile(path)
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        opf_path = next(e.get("full-path") for e in container.iter() if local_name(e.tag) == "rootfile")
        opf = ElementTree.fromstring(archive.read(opf_path))
    except (OSError, KeyError, StopIteration, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise BookError("This EPUB can't be read (%s)" % type(exc).__name__)
    base = posixpath.dirname(opf_path)
    title = next((e.text for e in opf.iter() if local_name(e.tag) == "title" and e.text), None)
    manifest, spine, ncx_id = {}, [], None
    for element in opf.iter():
        name = local_name(element.tag)
        if name == "item":
            manifest[element.get("id")] = (posixpath.normpath(posixpath.join(base, element.get("href", ""))),
                                           element.get("media-type", ""), element.get("properties", ""))
        elif name == "spine":
            ncx_id = element.get("toc")
        elif name == "itemref":
            spine.append(element.get("idref"))  # resolved against the manifest below
    files = [manifest[i][0] for i in spine if i in manifest]
    chapters, chars = [], []
    for file_path in files:
        try:
            text = archive.read(file_path).decode("utf-8", errors="replace")
        except KeyError:
            continue
        parser = _Blocks(file_path)
        parser.feed(text)
        parser.close()
        chapters.append({"file": file_path, "title": "", "blocks": parser.blocks})
        chars.append(sum(len("".join(t for t, _, _ in b["runs"])) + 1 for b in parser.blocks))
    toc = _toc(archive, manifest, ncx_id, [c["file"] for c in chapters])
    for entry_title, chapter in toc:
        if not chapters[chapter]["title"]:
            chapters[chapter]["title"] = entry_title
    last = ""
    for number, chapter in enumerate(chapters):
        last = chapter["title"] = chapter["title"] or last or "Part %d" % (number + 1)
    if not chapters or not sum(chars):
        raise BookError("This EPUB has no readable text")
    return {"title": title, "chapters": chapters, "toc": toc or [(c["title"], i) for i, c in enumerate(chapters)],
            "chars": chars, "zip": path}


def _toc(archive, manifest, ncx_id, files):
    """The book's contents as (title, chapter index), from EPUB 3 nav or EPUB 2 NCX."""
    links = []
    nav = next((item for item in manifest.values() if "nav" in item[2].split()), None)
    try:
        if nav:
            parser = _Links()
            parser.feed(archive.read(nav[0]).decode("utf-8", errors="replace"))
            links = [(posixpath.normpath(posixpath.join(posixpath.dirname(nav[0]), href)), text)
                     for href, text in parser.links if href]
        elif ncx_id in manifest:
            ncx_path = manifest[ncx_id][0]
            ncx = ElementTree.fromstring(archive.read(ncx_path))
            for point in (e for e in ncx.iter() if local_name(e.tag) == "navPoint"):
                label = next((t.text for t in point.iter() if local_name(t.tag) == "text" and t.text), "")
                content = next((c.get("src") for c in point if local_name(c.tag) == "content"), None)
                if content:
                    links.append((posixpath.normpath(posixpath.join(posixpath.dirname(ncx_path), content)),
                                  " ".join(label.split())))
    except (KeyError, ElementTree.ParseError):
        return []
    toc = []
    for href, text in links:
        target = href.split("#")[0]
        if target in files and text:
            toc.append((text, files.index(target)))
    return toc


# ---- comics and PDF -----------------------------------------------------------

def natural_key(name):
    """Page 2 before page 10."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


class ZipPages:
    """A CBZ: one image per page, read straight from the archive."""
    def __init__(self, path):
        self.archive = zipfile.ZipFile(path)
        self.names = sorted((n for n in self.archive.namelist()
                             if n.lower().endswith(IMAGE_EXTENSIONS) and not posixpath.basename(n).startswith(".")),
                            key=natural_key)
        if not self.names:
            raise BookError("No pages found in this comic")

    def __len__(self):
        return len(self.names)

    def page(self, index):
        """The page image as bytes (decoded by the reader)."""
        return self.archive.read(self.names[index])


class FolderPages:
    """Images extracted from a CBR/CB7 into the cache, in page order."""
    def __init__(self, folder):
        self.paths = []
        for root, _, files in os.walk(folder):
            self.paths += [os.path.join(root, f) for f in files
                           if f.lower().endswith(IMAGE_EXTENSIONS) and not f.startswith(".")]
        self.paths.sort(key=lambda p: natural_key(os.path.relpath(p, folder)))
        if not self.paths:
            raise BookError("No pages found in this comic")

    def __len__(self):
        return len(self.paths)

    def page(self, index):
        with open(self.paths[index], "rb") as source:
            return source.read()


# Tried in order; each is (program, arguments for: archive, destination).
EXTRACTORS = (("unrar", lambda a, d: ["x", "-o+", "-y", a, d + "/"]),
              ("7z", lambda a, d: ["x", "-y", "-o" + d, a]),
              ("7za", lambda a, d: ["x", "-y", "-o" + d, a]),
              ("bsdtar", lambda a, d: ["-xf", a, "-C", d]),
              ("unar", lambda a, d: ["-f", "-o", d, a]))


def extract_comic(path, folder):
    """Unpack a CBR/CB7 once into folder (reused next time)."""
    done = os.path.join(folder, ".complete")
    if os.path.exists(done):
        return FolderPages(folder)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    for program, arguments in EXTRACTORS:
        if not shutil.which(program):
            continue
        result = subprocess.run([program] + arguments(path, folder), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=300)
        if result.returncode == 0:
            open(done, "w").close()
            LOG.info("Comic extracted with %s", program)
            return FolderPages(folder)
        LOG.warning("%s could not extract the comic (code %d)", program, result.returncode)
    shutil.rmtree(folder, ignore_errors=True)
    raise BookError("CBR comics need unrar, 7z, bsdtar or unar on the device")


class PdfPages:
    """PDF pages rendered to PNG (in the cache) by whichever renderer the device has:
    PyMuPDF, mutool (MuPDF) or pdftoppm (Poppler). Rendering takes a moment, so the
    reader asks for pages from a background thread."""
    def __init__(self, path, folder, width=1280):  # twice the screen: zooming in stays sharp
        self.path, self.folder, self.width = path, folder, width
        os.makedirs(folder, exist_ok=True)
        self.tool, self.count = self._open()

    def _open(self):
        try:
            import fitz  # PyMuPDF, if someone installed it
            return "fitz", fitz.open(self.path).page_count
        except Exception:
            pass
        if shutil.which("mutool"):
            info = subprocess.run(["mutool", "info", self.path], capture_output=True, text=True, timeout=60)
            match = re.search(r"Pages:\s*(\d+)", info.stdout)
            if match:
                return "mutool", int(match.group(1))
        if shutil.which("pdftoppm") and shutil.which("pdfinfo"):
            info = subprocess.run(["pdfinfo", self.path], capture_output=True, text=True, timeout=60)
            match = re.search(r"Pages:\s*(\d+)", info.stdout)
            if match:
                return "pdftoppm", int(match.group(1))
        raise BookError("PDFs need mutool (MuPDF) or pdftoppm (Poppler) on the device")

    def __len__(self):
        return self.count

    def page(self, index):
        target = os.path.join(self.folder, "page-%04d-%d.png" % (index + 1, self.width))
        if not os.path.exists(target):
            if self.tool == "fitz":
                import fitz
                document = fitz.open(self.path)
                page = document[index]
                zoom = self.width / page.rect.width
                page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(target)
            elif self.tool == "mutool":
                subprocess.run(["mutool", "draw", "-q", "-w", str(self.width), "-o", target, self.path,
                                str(index + 1)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            else:
                prefix = target[:-4]
                subprocess.run(["pdftoppm", "-png", "-singlefile", "-f", str(index + 1), "-l", str(index + 1),
                                "-scale-to-x", str(self.width), "-scale-to-y", "-1", self.path, prefix],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        try:
            with open(target, "rb") as source:
                return source.read()
        except OSError:
            raise BookError("Page %d could not be rendered" % (index + 1))


def open_pages(path, kind, cache):
    """The page source for a comic or PDF; cache is a per-book folder."""
    if kind == "cbz":
        return ZipPages(path)
    if kind in ("cbr", "cb7"):
        return extract_comic(path, os.path.join(cache, "pages"))
    if kind == "pdf":
        return PdfPages(path, os.path.join(cache, "pdf"))
    raise BookError("Not a comic or PDF")


# ---- fetching and progress ----------------------------------------------------

def cached_book(folder, item_id):
    """A previously opened book's file in the cache, if any."""
    try:
        for name in os.listdir(folder):
            if name.startswith(item_id + ".") and not name.endswith(".part"):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def fetch_book(server, headers, verify, item, folder):
    """Download a book's original file into the cache (books are small)."""
    found = cached_book(folder, item["Id"])
    if found:
        return found
    os.makedirs(folder, exist_ok=True)
    part = os.path.join(folder, item["Id"] + ".part")
    with requests.get(server + "/Items/" + item["Id"] + "/Download", headers=headers, stream=True,
                      timeout=(6, 60), verify=verify) as response:
        response.raise_for_status()
        with open(part, "wb") as target:
            for chunk in response.iter_content(256 * 1024):
                target.write(chunk)
        extension = file_extension(response, item)
    path = os.path.join(folder, item["Id"] + (extension if extension != ".audio" else ".book"))
    os.replace(part, path)
    return path


class ReadingProgress:
    """Where each book was left (and when), kept on the device: Jellyfin has no
    portable place for a reading position, so this is the reliable copy."""
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as source:
                self.entries = dict(json.load(source))
        except (OSError, ValueError, TypeError):
            self.entries = {}
        self.saved_at = 0

    def save(self, force=False):
        # Page turns are frequent: write at most every few seconds unless asked.
        if not force and time.monotonic() - self.saved_at < 5:
            return
        self.saved_at = time.monotonic()
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as target:
                json.dump(self.entries, target)
            os.replace(self.path + ".tmp", self.path)
        except OSError as exc:
            LOG.warning("Could not save reading progress (%s)", type(exc).__name__)

    def get(self, item_id):
        return self.entries.get(item_id)

    def update(self, item, position, percent, finished=False, force=False):
        entry = self.entries.get(item["Id"], {})
        entry.update(item={k: item[k] for k in BOOK_FIELDS if k in item}, position=position,
                     percent=round(percent, 4), at=time.time(), finished=finished or entry.get("finished", False))
        self.entries[item["Id"]] = entry
        self.save(force)

    def recent(self, count=12):
        """Books in progress, most recently read first (the Continue reading row)."""
        books = [e["item"] for e in sorted(self.entries.values(), key=lambda e: -e.get("at", 0))
                 if not e.get("finished") and e.get("item")]
        return books[:count]
