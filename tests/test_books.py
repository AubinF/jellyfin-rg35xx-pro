from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import books
from books import BookError, ReadingProgress, detect, extract_comic, parse_epub, ZipPages

CONTAINER = """<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""


def make_epub(path, nav=True):
    """A small EPUB: two chapters, an image, a table of contents (EPUB 3 nav or EPUB 2 NCX)."""
    opf = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title></metadata>
<manifest>
 <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
 <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
 <item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/>
 <item id="c2" href="text/two.xhtml" media-type="application/xhtml+xml"/>
 <item id="img" href="images/pic.png" media-type="image/png"/>
</manifest><spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/></spine></package>"""
    if not nav:
        opf = opf.replace(' properties="nav"', "")
    one = """<html><head><title>ignored</title><style>p{}</style></head><body>
<h1>The   Beginning</h1>
<p>It was a <b>dark</b> and <i>stormy</i>
   night.<br/>A new line.</p>
<script>var hidden = 1;</script>
<p>Second &amp; last paragraph.</p><img src="../images/pic.png"/></body></html>"""
    two = "<html><body><h2>Ending</h2><p>" + " ".join("word%d" % i for i in range(400)) + "</p></body></html>"
    nav_doc = """<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
<li><a href="text/one.xhtml">Chapter One</a></li><li><a href="text/two.xhtml#top">Chapter Two</a></li></ol></nav>
<nav epub:type="landmarks"><a href="text/two.xhtml">Not the toc</a></nav></body></html>"""
    ncx = """<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="a"><navLabel><text>First (NCX)</text></navLabel><content src="text/one.xhtml"/></navPoint>
<navPoint id="b"><navLabel><text>Second (NCX)</text></navLabel><content src="text/two.xhtml"/></navPoint>
</navMap></ncx>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/nav.xhtml", nav_doc)
        archive.writestr("OEBPS/toc.ncx", ncx)
        archive.writestr("OEBPS/text/one.xhtml", one)
        archive.writestr("OEBPS/text/two.xhtml", two)
        archive.writestr("OEBPS/images/pic.png", PNG)


def _png():
    """A valid 1x1 green PNG, built with correct checksums."""
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\xff\x00")) + chunk(b"IEND", b""))


PNG = _png()


def make_pdf(path, pages=2):
    """A minimal valid PDF with `pages` blank pages (xref offsets computed)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % (3 + i) for i in range(pages)), pages)]
    objects += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] >>"] * pages
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body.encode())
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    Path(path).write_bytes(out)


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def test_chapters_text_styles_and_table_of_contents(self):
        path = os.path.join(self.root.name, "book.epub")
        make_epub(path)
        self.assertEqual(detect(path), "epub")
        book = parse_epub(path)
        self.assertEqual(book["title"], "Test Book")
        self.assertEqual([c["title"] for c in book["chapters"]], ["Chapter One", "Chapter Two"])
        self.assertEqual(book["toc"], [("Chapter One", 0), ("Chapter Two", 1)])  # the landmarks nav is ignored
        blocks = book["chapters"][0]["blocks"]
        self.assertEqual((blocks[0]["kind"], blocks[0]["level"], blocks[0]["runs"]), ("h", 1, [("The Beginning", False, False)]))
        # Whitespace collapsed, styles kept, <br> kept as a line break, <script> dropped.
        self.assertEqual(blocks[1]["runs"], [("It was a ", False, False), ("dark", True, False), (" and ", False, False),
                                             ("stormy", False, True), (" night.", False, False), ("\n", False, False),
                                             ("A new line.", False, False)])
        self.assertEqual(blocks[2]["runs"], [("Second & last paragraph.", False, False)])
        self.assertEqual((blocks[3]["kind"], blocks[3]["src"]), ("img", "OEBPS/images/pic.png"))
        self.assertTrue(book["chars"][1] > book["chars"][0])

    def test_epub2_table_of_contents_from_ncx(self):
        path = os.path.join(self.root.name, "old.epub")
        make_epub(path, nav=False)
        self.assertEqual([t for t, _ in parse_epub(path)["toc"]], ["First (NCX)", "Second (NCX)"])

    def test_broken_files_give_a_readable_message(self):
        path = os.path.join(self.root.name, "bad.epub")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/container.xml", "not xml")
        with self.assertRaisesRegex(BookError, "can't be read"):
            parse_epub(path)
        junk = os.path.join(self.root.name, "junk.bin")
        Path(junk).write_bytes(b"hello world")
        with self.assertRaisesRegex(BookError, "Unknown book format"):
            detect(junk)


class ComicTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def comic_files(self, folder):
        os.makedirs(folder)
        for name in ("page10.png", "page2.png", "page1.png", ".hidden.png", "notes.txt"):
            Path(folder, name).write_bytes(PNG if name.endswith(".png") else b"x")

    def test_cbz_pages_in_natural_order(self):
        path = os.path.join(self.root.name, "comic.cbz")
        with zipfile.ZipFile(path, "w") as archive:
            for name in ("p10.jpg", "p2.jpg", "p1.jpg", "info.xml"):
                archive.writestr(name, b"x")
        self.assertEqual(detect(path), "cbz")
        self.assertEqual(ZipPages(path).names, ["p1.jpg", "p2.jpg", "p10.jpg"])

    @unittest.skipUnless(shutil.which("7z") or shutil.which("bsdtar"), "needs 7z or bsdtar")
    def test_archived_comic_is_unpacked_once(self):
        source = os.path.join(self.root.name, "src")
        self.comic_files(source)
        archive = os.path.join(self.root.name, "comic.cb7")
        if shutil.which("7z"):
            subprocess.run(["7z", "a", archive, source + "/*"], stdout=subprocess.DEVNULL, check=True)
        else:
            subprocess.run(["bsdtar", "-cf", archive, "-C", source, "."], check=True)
        pages = extract_comic(archive, os.path.join(self.root.name, "cache"))
        self.assertEqual([os.path.basename(p) for p in pages.paths], ["page1.png", "page2.png", "page10.png"])
        with patch.object(subprocess, "run", side_effect=AssertionError("extracted twice")):
            self.assertEqual(len(extract_comic(archive, os.path.join(self.root.name, "cache"))), 3)

    def test_missing_tools_give_a_readable_message(self):
        archive = os.path.join(self.root.name, "comic.cbr")
        Path(archive).write_bytes(b"Rar!\x1a\x07\x00rest")
        self.assertEqual(detect(archive), "cbr")
        with patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(BookError, "need unrar, 7z, bsdtar or unar"):
                extract_comic(archive, os.path.join(self.root.name, "cache"))
            pdf = os.path.join(self.root.name, "doc.pdf")
            make_pdf(pdf)
            with patch.dict(sys.modules, {"fitz": None}):
                with self.assertRaisesRegex(BookError, "mutool .* or pdftoppm"):
                    books.PdfPages(pdf, os.path.join(self.root.name, "pdf"))

    @unittest.skipUnless(shutil.which("pdftoppm") or shutil.which("mutool"), "needs a PDF renderer")
    def test_pdf_pages_render_to_png(self):
        pdf = os.path.join(self.root.name, "doc.pdf")
        make_pdf(pdf, pages=2)
        self.assertEqual(detect(pdf), "pdf")
        with patch.dict(sys.modules, {"fitz": None}):
            pages = books.PdfPages(pdf, os.path.join(self.root.name, "pdf"))
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages.page(1).startswith(b"\x89PNG"))


class ProgressTests(unittest.TestCase):
    def test_continue_reading_order_and_finished_books(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "reading.json")
            progress = ReadingProgress(path)
            progress.update({"Id": "a", "Name": "A", "Type": "Book"}, [0, 3, 10], 0.2, force=True)
            time.sleep(1.1)
            progress.update({"Id": "b", "Name": "B", "Type": "Book"}, [2], 0.5, force=True)
            progress.update({"Id": "c", "Name": "C", "Type": "Book"}, [9], 1.0, finished=True, force=True)
            again = ReadingProgress(path)
            self.assertEqual([book["Id"] for book in again.recent()], ["b", "a"])
            self.assertEqual(again.get("a")["position"], [0, 3, 10])


if __name__ == "__main__":
    unittest.main()
