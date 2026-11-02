#!/usr/bin/env python3
"""
split_omnibus.py - split the retail "Complete Malazan Book of the Fallen" omnibus epub
into one epub per book, for chapterize.py.

The omnibus is the publisher's ebook of all ten novels, so unlike the OCR'd single-book
epubs it still marks every scene break. Each book keeps its own files (most are named
after the book's ISBN), in reading order; this cuts the spine at each change of prefix.

  python split_omnibus.py "<omnibus>.epub" d:\\tmp

writes "Malazan 1 - Gardens of the Moon - Steven Erikson (omnibus).epub" and so on.
Every split keeps the stylesheets (chapterize.py reads the scene-break styling) but
drops the images.
"""
import argparse
import html
import re
import sys
import zipfile
from pathlib import Path

TITLES = ["Gardens of the Moon", "Deadhouse Gates", "Memories of Ice", "House of Chains", "Midnight Tides",
          "The Bonehunters", "Reaper's Gale", "Toll the Hounds", "Dust of Dreams", "The Crippled God"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("omnibus")
    ap.add_argument("out_dir")
    args = ap.parse_args()

    z = zipfile.ZipFile(args.omnibus)
    opf = next(n for n in z.namelist() if n.endswith(".opf"))
    txt = z.read(opf).decode("utf8")
    manifest = {}
    for item in re.findall(r"<item\b[^>]*>", txt):
        manifest[re.search(r'id="([^"]+)"', item).group(1)] = re.search(r'href="([^"]+)"', item).group(1)
    # Runs of spine entries sharing a file-name prefix: "9780765310026_cha01.html", or
    # none for the three books whose files aren't named by ISBN.
    books = []
    for idref in re.findall(r'<itemref[^>]*?idref="([^"]+)"', txt):
        m = re.match(r"(\d{13})_", manifest.get(idref, "").rsplit("/", 1)[-1])
        key = m.group(1) if m else None
        if not books or books[-1][0] != key:
            books.append((key, []))
        books[-1][1].append(idref)
    if len(books) != len(TITLES):
        sys.exit(f"Expected {len(TITLES)} books in the spine, found {len(books)}.")

    base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
    pages = {base + h for h in manifest.values() if re.search(r"\.x?html?$", h)}
    spine = re.search(r"<spine[^>]*>.*?</spine>", txt, re.S).group(0)
    spine_tag = re.match(r"<spine[^>]*>", spine).group(0)
    for n, ((_, ids), title) in enumerate(zip(books, TITLES), 1):
        keep = {base + manifest[i] for i in ids}
        book_opf = txt.replace(spine, spine_tag + "".join(f'<itemref idref="{i}"/>' for i in ids) + "</spine>")
        book_opf = re.sub(r"<dc:title[^>]*>.*?</dc:title>", f"<dc:title>{html.escape(title)}</dc:title>",
                          book_opf, count=1, flags=re.S)
        out = Path(args.out_dir) / f"Malazan {n} - {title} - Steven Erikson (omnibus).epub"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            w.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
            for name in z.namelist():
                if name == "mimetype" or (name in pages and name not in keep) \
                        or re.search(r"\.(jpe?g|png|gif)$", name, re.I):
                    continue
                w.writestr(name, book_opf if name == opf else z.read(name))
        print(f"{out.name}: {len(ids)} files")


if __name__ == "__main__":
    main()
