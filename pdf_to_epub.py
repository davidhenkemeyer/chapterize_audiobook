#!/usr/bin/env python3
"""
pdf_to_epub.py - rebuild a plain epub from a PDF "printout" of an ebook, keeping its
scene breaks, for chapterize.py.

Some ebooks only turn up as PDFs (e.g. printed from an epub by calibre). Their scene
breaks survive as blank lines, which chapterize.py can use if they come back as
"* * *" paragraphs. Uses pdftohtml from poppler (bundled with calibre).

  python pdf_to_epub.py book.pdf book.epub --title "Deadhouse Gates"

Paragraphs are rebuilt from indented lines, headings from lines in a larger font, and
a blank line (or a line-sized gap) between two paragraphs becomes "* * *".
"""
import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

PDFTOHTML_DIRS = [r"C:\Program Files\Calibre2\app\bin", r"C:\Program Files (x86)\Calibre2\app\bin"]


def find_pdftohtml():
    exe = shutil.which("pdftohtml")
    if exe:
        return exe
    for d in PDFTOHTML_DIRS:
        p = Path(d) / "pdftohtml.exe"
        if p.exists():
            return str(p)
    sys.exit("pdftohtml not found: install calibre (winget install calibre.calibre) or poppler.")


def pdf_lines(pdf):
    """[(page, top, left, font size, text)] for every text run in the PDF."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([find_pdftohtml(), "-xml", "-i", "-q", "-enc", "UTF-8", str(pdf), str(Path(tmp) / "out")],
                       check=True)
        x = (Path(tmp) / "out.xml").read_text(encoding="utf8")
    fonts = {i: int(s) for i, s in re.findall(r'<fontspec id="(\d+)" size="(\d+)"', x)}
    lines = []
    for no, body in re.findall(r'<page number="(\d+)"[^>]*>(.*?)</page>', x, re.S):
        for top, left, font, s in re.findall(
                r'<text top="(\d+)" left="(\d+)"[^>]*font="(\d+)"[^>]*>(.*?)</text>', body):
            lines.append((int(no), int(top), int(left), fonts[font], html.unescape(re.sub("<[^>]+>", "", s))))
    return lines


def paragraphs(lines):
    """[(kind, text)] with kind "h" (heading), "p" (paragraph) or "sep" (scene break)."""
    body = Counter(z[3] for z in lines if z[4].strip()).most_common(1)[0][0]
    lefts = Counter(z[2] for z in lines if z[3] == body and z[4].strip())
    # Continuation lines sit at the margin; a paragraph's first line is indented.
    margin = min(left for left, n in lefts.items() if n >= 0.05 * sum(lefts.values()))
    out, cur, prev = [], None, None

    def flush():
        nonlocal cur
        if cur and cur[1].strip():
            out.append(cur)
        cur = None

    for page, top, left, size, s in lines:
        if not s.strip():
            flush()
            out.append(("sep", ""))
            prev = (page, top)
            continue
        gap = top - prev[1] if prev and prev[0] == page else None
        prev = (page, top)
        if gap is not None and abs(gap) <= 3 and cur:       # same line, another font run
            cur = (cur[0], cur[1] + s)
        elif size > body:
            if cur and cur[0] == "h" and gap is not None and gap <= size + 6:
                cur = ("h", cur[1] + " " + s)
            else:
                flush()
                cur = ("h", s)
        elif cur is None or cur[0] == "h" or left > margin + 3 or (gap is not None and gap > size + 10):
            if gap is not None and gap > size + 10 and cur and cur[0] == "p":
                flush()
                out.append(("sep", ""))
            flush()
            cur = ("p", s)
        else:
            joined = cur[1][:-1] if cur[1].endswith("-") and not cur[1].endswith(" -") else cur[1] + " "
            cur = ("p", joined + s)
    flush()
    out = [x for i, x in enumerate(out) if not (x[0] == "sep" and i and out[i - 1][0] == "sep")]
    # Only a gap between two paragraphs is a scene break (not one around a heading).
    return [("sep", "* * *") if kind == "sep" else (kind, s)
            for i, (kind, s) in enumerate(out)
            if kind != "sep" or (0 < i < len(out) - 1 and out[i - 1][0] == "p" and out[i + 1][0] == "p")]


def write_epub(paras, out, title):
    body = "".join(f"<h2>{html.escape(s.strip())}</h2>\n" if kind == "h" else f"<p>{html.escape(s.strip())}</p>\n"
                   for kind, s in paras)
    t = html.escape(title)
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{t}</dc:title></metadata>'
                   '<manifest><item id="text" href="text.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="text"/></spine></package>')
        z.writestr("text.xhtml",
                   '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                   f"<head><title>{t}</title></head><body>\n{body}</body></html>")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("epub")
    ap.add_argument("--title", help="book title (default: the PDF's file name)")
    args = ap.parse_args()
    paras = paragraphs(pdf_lines(args.pdf))
    write_epub(paras, args.epub, args.title or Path(args.pdf).stem)
    n = Counter(kind for kind, _ in paras)
    print(f"{args.epub}: {n['p']} paragraphs, {n['h']} headings, {n['sep']} scene breaks")


if __name__ == "__main__":
    main()
