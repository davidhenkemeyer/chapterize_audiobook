#!/usr/bin/env python3
"""
chapterize.py - turn an audiobook into one .m4b with a chapter marker per scene.

How it works
  1. Transcribes the audio with faster-whisper (cached, so reruns are fast).
  2. Reads the epub, finds the Prologue / Chapter N / Epilogue headings and
     matches the first words of each chapter against the transcript to find
     where each chapter starts in the audio. Works across multiple audio files
     (a chapter that spans two parts is stitched together automatically).
  3. Finds scene breaks inside each chapter: the epub's own ("* * *") when it
     marks them all, otherwise the narrator's long pauses (plus any breaks the
     epub does mark). Many OCR'd epubs have lost their scene markers.
  4. Joins all the audio files into one .m4b with ffmpeg stream-copy (no
     re-encoding, no quality loss), with a chapter marker for every scene.

Requirements
  - Python 3.10+
  - ffmpeg/ffprobe, numpy and faster-whisper are installed automatically on first
    run if missing (ffmpeg via `winget install Gyan.FFmpeg`, packages via pip).

Usage (defaults match the Midnight Tides paths):
  python chapterize.py --dry-run     # transcribe + write plan.csv, don't build
  python chapterize.py               # do everything

Tune pause-based scene detection with --scene-gap (seconds of silence that count
as a scene break; by default picked per book, since some narrators pause 2 s at
every other paragraph) and --min-scene (shortest allowed scene, default 90 s).
Re-running is cheap: transcripts and silences are cached in <out>/_work.
"""
import argparse
import bisect
import csv
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path

DEFAULT_AUDIO = r""
DEFAULT_EPUB = r""
DEFAULT_OUT = r"C:\AudioBooks\Chapterized"
AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav"}


# ------------------------------------------------------------------- dependencies
def _find_ffmpeg_dir():
    """Look in the usual winget install locations (PATH isn't refreshed mid-run)."""
    import os
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Microsoft" / "WinGet" / "Links")
        roots += list((Path(local) / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg*"))
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramData")):
        if pf:
            roots.append(Path(pf) / "WinGet" / "Links")
    for r in roots:
        if not r.exists():
            continue
        for exe in r.rglob("ffmpeg.exe"):
            if (exe.parent / "ffprobe.exe").exists():
                return exe.parent
    return None


def ensure_ffmpeg():
    import os
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    print("ffmpeg not found.")
    d = _find_ffmpeg_dir()
    if not d and sys.platform == "win32" and shutil.which("winget"):
        print("Installing Gyan.FFmpeg with winget (this may take a minute)...")
        subprocess.run(["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                        "--accept-source-agreements", "--accept-package-agreements"])
        d = _find_ffmpeg_dir()
    if d:
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        print(f"Using ffmpeg from {d}")
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        sys.exit("Could not install ffmpeg automatically. Install it manually "
                 "(winget install Gyan.FFmpeg), open a new terminal, and run again.")


def ensure_packages(packages=(("numpy", "numpy"), ("faster_whisper", "faster-whisper"))):
    import importlib
    missing = []
    for mod, pipname in packages:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pipname)
    if not missing:
        return
    print("Installing missing Python packages:", ", ".join(missing))
    r = subprocess.run([sys.executable, "-m", "pip", "install", *missing])
    if r.returncode != 0:
        sys.exit("pip install failed. Try manually: pip install " + " ".join(missing))
    importlib.invalidate_caches()


# --------------------------------------------------------------------------- utils
def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))]


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]', "", s).strip().rstrip(".")


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)])
    return float(out.decode().strip())


def format_tag(path, key):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", f"format_tags={key}",
         "-of", "default=nw=1:nk=1", str(path)])
    return out.decode("utf8", "replace").strip() or None


def extract_cover(path, tmp):
    """Save the embedded cover image (an attached_pic video stream) to a file in `tmp`.
    Returns the file, or None if there's no cover."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=index,codec_name:stream_disposition=attached_pic",
         "-of", "json", str(path)])
    pics = [s for s in json.loads(out)["streams"] if s.get("disposition", {}).get("attached_pic")]
    if not pics:
        return None
    img = tmp / ("cover.png" if pics[0]["codec_name"] == "png" else "cover.jpg")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-map", f"0:{pics[0]['index']}",
         "-frames:v", "1", "-c", "copy", "-f", "image2", str(img)])     # some covers are 2-frame tracks
    return img


def hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ----------------------------------------------------------------------- transcribe
def transcribe(path, model_name, cache, chunk=1200.0):
    """Returns list of [word, start, end] with times relative to this file."""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf8"))
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    total = duration(path)
    words = []
    start = 0.0
    while start < total:
        raw = subprocess.check_output(
            ["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(chunk), "-i", str(path),
             "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"])
        audio = np.frombuffer(raw, dtype=np.float32)
        segs, _ = model.transcribe(audio, language="en", beam_size=1,
                                   condition_on_previous_text=False, word_timestamps=True)
        for s in segs:
            for w in s.words or []:
                words.append([w.word.strip(), round(start + w.start, 2), round(start + w.end, 2)])
        start += chunk
        print(f"    transcribed {hms(min(start, total))} / {hms(total)}", flush=True)
    cache.write_text(json.dumps(words), encoding="utf8")
    return words


def transcribe_head(path, model_name, cache, secs=120.0, gap=8.0):
    """Transcribe just the opening of a file, carefully. Whisper can skip a credit read
    over music near the start ("This is Audible." [skips "...presents the unabridged
    recording of..."] "Prologue."): any long gap between words is transcribed again on
    its own, which catches it."""
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf8"))
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def words_in(t0, t1):
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", f"{t0:.2f}", "-t", f"{t1 - t0:.2f}",
                                       "-i", str(path), "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"])
        segs, _ = model.transcribe(np.frombuffer(raw, dtype=np.float32), language="en", beam_size=1,
                                   word_timestamps=True)
        return [[w.word.strip(), round(t0 + w.start, 2), round(t0 + w.end, 2)] for s in segs for w in s.words or []]

    words = words_in(0.0, secs)
    filled = []
    for k, w in enumerate(words):
        prev_end = words[k - 1][2] if k else 0.0
        if w[1] - prev_end > gap:
            filled += [x for x in words_in(prev_end, w[1]) if x[2] <= w[1]]
        filled.append(w)
    cache.write_text(json.dumps(filled), encoding="utf8")
    return filled


def detect_silences(path, cache, noise="-35dB", min_dur=0.5):
    """Returns list of (start, end) silences, times relative to this file."""
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text())]
    p = subprocess.run(
        ["ffmpeg", "-nostats", "-i", str(path), "-vn", "-af",
         f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-"],
        stderr=subprocess.PIPE, text=True, errors="replace")
    sil, cur = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1))))
            cur = None
    cache.write_text(json.dumps(sil))
    return sil


# ----------------------------------------------------------------------------- epub
class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras, self.classes, self.buf, self.depth, self.cls = [], [], [], 0, ""

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.depth += 1
            if self.depth == 1:
                self.cls = f"{tag}." + (dict(attrs).get("class") or "")

    def handle_endtag(self, tag):
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6") and self.depth:
            self.depth -= 1
            t = " ".join("".join(self.buf).split())
            if t or tag == "p":        # empty ones may be spacers (see _styled_breaks)
                self.paras.append(t)
                self.classes.append(self.cls)
            self.buf = []

    def handle_data(self, d):
        if self.depth:
            self.buf.append(d)


def epub_paragraphs(path):
    z = zipfile.ZipFile(path)
    names = z.namelist()
    order = []
    opf = next((n for n in names if n.endswith(".opf")), None)
    if opf:
        txt = z.read(opf).decode("utf8", "replace")
        manifest = dict(re.findall(r'<item[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', txt))
        manifest.update({k: v for v, k in re.findall(r'<item[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', txt)})
        base = opf.rsplit("/", 1)[0] + "/" if "/" in opf else ""
        for idref in re.findall(r'<itemref[^>]*?idref="([^"]+)"', txt):
            if idref in manifest:
                order.append(base + manifest[idref])
    if not order:
        order = sorted(n for n in names if re.search(r"\.(x?html?)$", n))
    paras, classes = [], []
    for n in order:
        if n not in names:
            continue
        p = _P()
        p.feed(z.read(n).decode("utf8", "replace"))
        paras.extend(p.paras)
        classes.extend(p.classes)
    css = "".join(z.read(n).decode("utf8", "replace") for n in names if n.endswith(".css"))
    return _styled_breaks(paras, classes, css)


def _css_em(v):
    m = re.match(r"(-?[\d.]+)(em|rem|px|pt)?", v.strip())
    if not m:
        return 0.0
    return float(m.group(1)) / {"px": 16, "pt": 12}.get(m.group(2), 1)


def _styled_breaks(paras, classes, css):
    """Retail epubs mark a scene break with spacing, not "* * *", in one of two ways:
      - an empty spacer paragraph (<p class="spaceBreak"> </p>), in a class used for
        little else - OCR'd epubs leave empty paragraphs at page breaks too, but in
        the same class as the text;
      - the scene's first paragraph is unindented, with a blank line's worth of margin
        above. Only once the story has begun under the current heading: a chapter's
        epigraph and dateline use the same style.
    Turns both into "* * *" paragraphs, and drops other empty paragraphs."""
    style = {}
    for names, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        decl = dict((k.strip().lower(), v.strip()) for k, _, v in
                    (d.partition(":") for d in body.split(";")) if v)
        top = decl.get("margin-top") or (decl.get("margin") or "0").split()[0]
        for name in re.findall(r"\.([\w-]+)", names):
            style[name] = (_css_em(decl.get("text-indent", "0")), _css_em(top))
    empty = Counter(c for t, c in zip(paras, classes) if not t)
    used = Counter(classes)
    spacers = {c for c, n in empty.items() if n >= 10 and n >= 0.9 * used[c]}
    normal = Counter(c for t, c in zip(paras, classes) if t and c.startswith("p.")).most_common(1)
    normal = normal[0][0] if normal else None
    starts = set()
    if normal and max((style.get(n, (0, 0))[0] for n in normal[2:].split()), default=0) > 0:
        starts = {c for c in set(classes) if c.startswith("p.") and c != normal
                  and any(style.get(n, (1, 0))[0] == 0 and style[n][1] >= 1.0 for n in c[2:].split())}
    out, story = [], False
    for t, c in zip(paras, classes):
        if not t:
            if c in spacers:
                out.append("* * *")
            continue
        if not c.startswith("p."):
            story = False
        elif c in starts and story:
            out.append("* * *")
        story = story or c == normal
        out.append(t)
    return out


def epub_title(path):
    """The book title from the epub's OPF metadata (dc:title), or None."""
    import html
    z = zipfile.ZipFile(path)
    opf = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf:
        return None
    m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", z.read(opf).decode("utf8", "replace"), re.S)
    return " ".join(html.unescape(m.group(1)).split()) if m else None


HEAD_RE = re.compile(r"^(prolog|proloq|epilog|epiloq|chap)", re.I)
DASHES = "-–—"
SENTENCE_END = ".!?'\"’”…�"     # �: mis-decoded quotes in OCR'd epubs


def _caps_start(t):
    """Story text opens in small caps ("THE DOG HAD SAVAGED a woman...")."""
    t = t.strip()
    w = re.findall(r"[A-Za-z]+", t)[:3]
    return (len(w) >= 2 and all(x.isupper() for x in w) and t[:1] not in DASHES
            and bool(re.search(r"[a-z]", t) or t[-1:] in SENTENCE_END))


def _attribution(t):
    """An epigraph's source line ("Nathii folk saying", "- ANOMANDARIS, BOOK IV")."""
    t = t.strip()
    return len(t.split()) <= 25 and (t[:1] in DASHES or t[-1:] not in SENTENCE_END)


def _story_para(t):
    """Could open the story: prose, or dialogue. Poem lines are short and unquoted, and
    poems wrapped across paragraphs continue in lowercase."""
    t = t.strip()
    quoted = t[:1] in "'\"‘“�"
    return (not _attribution(t) and (t[:1].isupper() or t[:1].isdigit() or quoted)
            and (len(t.split()) >= 15 or quoted))


def _source_line(t):
    """Looks like a real source ("Blind Tobor of the Reach"), not just a short poem line."""
    w = re.findall(r"[A-Za-z]+", t)
    return t.strip()[:1] in DASHES or len(w) <= 4 or sum(x[0].isupper() for x in w) >= 0.4 * len(w)


def join_drop_caps(paras):
    """OCR'd epubs split a drop cap into its own paragraph: "T" / "he ascent began..."."""
    out = []
    for t in paras:
        if out and len(out[-1].strip()) == 1 and out[-1].strip().isalpha() and t[:1].islower():
            out[-1] = out[-1].strip() + t
        else:
            out.append(t)
    return out


def _after_attribution(body, k, between=3):
    """body[k] comes after an epigraph's attribution, perhaps with a few short title or
    dateline lines between ("I" / "Maeth'ki Im..." / "298,665 years before Burn's Sleep.").
    The attribution must look like a source, or real prose must follow within a few
    paragraphs - otherwise it's likely dialogue inside a poem."""
    for j in range(k - 1, max(0, k - 1 - between) - 1, -1):
        if _attribution(body[j]) and (_source_line(body[j]) or any(len(t.split()) >= 30 for t in body[k:k + 5])):
            return True
        if len(body[j].split()) > 12:
            return False
    return False


def epigraph_len(body, max_words=450, scan=45):
    """Number of leading paragraphs of a chapter body that are its epigraph (0 if none).
    The story starts at the first small-caps paragraph, or failing that at the first
    prose or dialogue after an attribution line. An epigraph is at least a quote plus a
    source."""
    n = min(len(body), scan)
    k = next((k for k in range(n) if _caps_start(body[k])), None)
    if k is None:
        k = next((k for k in range(1, n) if _story_para(body[k]) and _after_attribution(body, k)), None)
    if k is None or k < 2 or sum(len(t.split()) for t in body[:k]) > max_words:
        return 0
    return k


PART_RE = re.compile(r"\s*(?i:book)\s*(?:(?i:one|two|three|four|five|six|seven|eight|nine|ten)|\d+|[IVX]+)(?![a-z])")
SCENE_SEP = re.compile(r"^[\s*■•◆~#]*[*■•◆~#][\s*■•◆~#]*$|^_{3,}$")     # "* * *", "■", "_____"


def scene_breaks(body, story_start=0, min_story=40):
    """Scene breaks the epub marks ("* * *"): [(word offset of the next paragraph within the
    body, separators not counted; its opening words)]. Separators before the story has
    run min_story words (around an epigraph, its attribution, a dateline) don't count.
    Most OCR'd epubs lost these; some kept all of them."""
    sep = [bool(SCENE_SEP.match(t)) for t in body] + [False]

    def verse(j):       # a line of a poem with a gap after every line
        return 0 <= j < len(body) and not sep[j] and len(body[j].split()) <= 8

    out, n = [], 0
    for k, t in enumerate(body):
        if not sep[k]:
            n += len(t.split())
        elif (n >= story_start + min_story and k < len(body) - 1 and not sep[k + 1]
              and not (verse(k + 1) and sep[k + 2]) and not (verse(k - 1) and k >= 2 and sep[k - 2])):
            nxt = [p for p in body[k + 1:k + 8] if not SCENE_SEP.match(p)]
            out.append((n, " ".join(nxt).split()[:250]))
    return out


def find_chapters(paras, min_words=400):
    """Returns [{'kind','num','title','words'(body words)}]; OCR typos tolerated."""
    # Spaces are dropped before matching so letter-spaced headings ("C H A P T E R") count.
    heads = [i for i, t in enumerate(paras) if len(t) <= 40 and HEAD_RE.match(re.sub(r"\s", "", t))]
    chapters = []
    for k, i in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(paras)
        # A heading within two paragraphs of another is a table-of-contents entry. The
        # word-count check below misses the last TOC entry, whose "body" is whatever
        # follows the TOC (appendix, author bio...).
        if (k > 0 and i - heads[k - 1] <= 2) or end - i <= 2:
            continue
        marked = paras[i + 1:end]
        # A part page ("Book One / Pale" and its extract) ends the chapter before it: no
        # scene breaks in it, and it isn't part of the chapter's text.
        part = next((j for j, t in enumerate(marked) if len(t) <= 60 and PART_RE.match(t)), None)
        if part is not None and part > 0:
            marked = marked[:part]
        body = [t for t in marked if not SCENE_SEP.match(t)]
        words = " ".join(body).split()
        if len(words) < min_words:      # stray headings, etc.
            continue
        h = paras[i].strip().lower()
        hc = re.sub(r"\s", "", h)
        kind = "prologue" if hc.startswith(("prolog", "proloq")) else \
               "epilogue" if hc.startswith(("epilog", "epiloq")) else "chapter"
        # Keep a numeral on prologues/epilogues ("Epilogue II") so the names stay distinct.
        suffix = h.split()[1:2]
        suffix = " " + suffix[0].upper() if suffix and re.fullmatch(r"[ivx]+|\d+", suffix[0]) else ""
        c = {"kind": kind, "words": words, "suffix": suffix, "head": i}
        body = join_drop_caps(body)
        epi = epigraph_len(body)
        if epi:
            c["epigraph_words"] = len(" ".join(body[:epi]).split())
            c["story_words"] = " ".join(body[epi:]).split()
        c["breaks"] = scene_breaks(marked, c.get("epigraph_words", 0))
        chapters.append(c)
    # Some scanned epubs have a prologue with no heading: story text straight after the
    # front matter. Offer the text before the first heading as a possible prologue,
    # starting at the first real paragraph; align_chapters drops it if the audio disagrees.
    if chapters and chapters[0]["kind"] != "prologue":
        pre = paras[:chapters[0]["head"]]
        first = next((j for j, t in enumerate(pre) if len(t.split()) >= 40), None)
        if first is not None:
            words = " ".join(pre[first:]).split()
            if len(words) >= min_words:
                chapters.insert(0, {"kind": "prologue", "words": words, "suffix": "", "implied": True})
    n = 0
    for c in chapters:
        if c["kind"] == "chapter":
            n += 1
            c["num"] = n
            c["title"] = f"Chapter {n:02d}"
        else:
            c["num"] = 0
            c["title"] = c["kind"].capitalize() + c["suffix"]
    return chapters


# ------------------------------------------------------------------------ alignment
_UNITS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                     "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40}
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50}


def spoken_number(tokens):
    """The chapter number in a narrator's "Chapter 17" / "Chapter IX" / "Chapter Twenty-One"."""
    if not tokens:
        return None
    w = norm(tokens[0])
    if w.isdigit():
        return int(w)
    if tokens[0].strip(".,:;").isupper() and re.fullmatch(r"[ivxl]+", w):
        vals = [_ROMAN[ch] for ch in w]
        return sum(-v if i + 1 < len(vals) and v < vals[i + 1] else v for i, v in enumerate(vals))
    if w in _UNITS:
        return _UNITS[w]
    for t, n in _TENS.items():
        if w == t:      # "Twenty One", unless the sentence ended: "Twenty. One day..."
            nxt = norm(tokens[1]) if len(tokens) > 1 and tokens[0][-1:] not in ".!?" else ""
            return n + _UNITS.get(nxt, 0) if _UNITS.get(nxt, 0) < 10 else n
        if w.startswith(t) and w[len(t):] in _UNITS and _UNITS[w[len(t):]] < 10:    # "twentyone"
            return n + _UNITS[w[len(t):]]
    return None


def _numbered(chapters):
    """Number chapters in order: the narrator's number where it continues the sequence,
    otherwise the next one."""
    prev = 0
    for c in chapters:
        if c["kind"] == "chapter":
            sp = c.get("spoken")
            c["num"] = sp if sp and prev < sp <= prev + 3 else prev + 1
            c["title"] = f"Chapter {c['num']:02d}"
            prev = c["num"]


def fix_chapter_numbers(chapters, tw, times):
    """Number chapters from the narrator's announcements rather than by counting epub
    headings: OCR'd epubs can lose a heading, which merges two chapters and shifts every
    number after it. A chapter the narrator announces well inside another chapter is split
    back out there, if its number fits between its neighbours'."""
    nw = [norm(w) for w in tw]
    announced = {}
    for i in range(len(nw) - 2):
        if nw[i] == "chapter" and (i == 0 or tw[i - 1].rstrip()[-1:] in ".!?\"'"):
            n = spoken_number(tw[i + 1:i + 3])
            if n:
                announced.setdefault(n, i)
    _numbered(chapters)
    fixed = []
    for n, at in sorted(announced.items()):
        if any(c.get("spoken") == n for c in chapters):
            continue
        k = next((k for k, c in enumerate(chapters) if c.get("start_word", 0) > at), len(chapters))
        before, after = chapters[k - 1] if k else None, chapters[k] if k < len(chapters) else None
        if after and after["kind"] == "chapter" and after.get("spoken") is None and after["start_word"] - at <= 500:
            # The announcement of the next chapter, which its start search missed: start there.
            after.update(spoken=n, start_word=at, start=max(0.0, times[at] - 0.4))
            _numbered(chapters)
            continue
        if (before is None or before["kind"] != "chapter" or at - before.get("start_word", 0) < 200
                or before["num"] >= n or (after and after["kind"] == "chapter" and after.get("spoken")
                                          and after["spoken"] <= n)):
            continue
        chapters.insert(k, {"kind": "chapter", "words": [], "suffix": "", "start_word": at, "spoken": n,
                            "start": max(0.0, times[at] - 0.4), "inserted": True})
        fixed.append(n)
        _numbered(chapters)
    if fixed:
        print("  restored chapters missing from the epub (found in the narration):",
              ", ".join(f"Chapter {m:02d}" for m in fixed))


# ----------------------------------------------------------------- publisher credits
PRESENTS = "presents the unabridged recording of".split()


def _find(nw, seq, lo=0, hi=None):
    hi = len(nw) - len(seq) + 1 if hi is None else min(hi, len(nw) - len(seq) + 1)
    return [i for i in range(max(0, lo), hi) if nw[i:i + len(seq)] == seq]


def _presents_end(nw, tw, i):
    """End (exclusive) of "<Publisher> presents the unabridged recording of <book> by
    <author>, performed by <narrator>." starting from the "presents" at i."""
    p = next((k for k in range(i, min(len(nw), i + 40)) if nw[k] == "performed"), None)
    b = next((k for k in range(p, min(len(nw), p + 3)) if nw[k] == "by"), None) if p is not None else None
    if b is None:
        return None
    return next((k + 1 for k in range(b + 1, min(len(nw), b + 5)) if tw[k][-1:] in ".,"), b + 3)


def _opening_spans(tw):
    """Word ranges [a, b) of the opening credits: "This is Audible." and "<Publisher>
    presents the unabridged recording of <book> by <author>, performed by <narrator>."."""
    nw = [norm(w) for w in tw]
    spans = [(i, i + 3) for i in _find(nw, ["this", "is", "audible"], 0, 300)]
    for i in _find(nw, PRESENTS, 0, 300):
        a = i
        while a > 0 and i - a < 3 and tw[a - 1][-1:] not in ".!?":     # publisher name
            a -= 1
        b = _presents_end(nw, tw, i)
        if b:
            spans.append((a, b))
    return spans


def find_credits(tw, times, ends, total, silences, head=None):
    """Publisher boilerplate to cut, as (start, end) times, plus where back matter read
    after the story (glossary, Dramatis Personae...) begins, or None. Cut: "This is
    Audible."; "<Publisher> presents the unabridged recording of..."; Audible's "this
    audiobook has been broken into multiple parts..." notice; and the closing "We hope you
    have enjoyed our presentation of... Copyright..." to the end. `head` is a separate
    transcript of the opening (see transcribe_head), searched for the opening credits."""
    nw = [norm(w) for w in tw]
    spans = []      # word index ranges [a, b)
    timed = []      # opening credits found in `head`, as times
    if head:
        hw, hs, he = [w for w, _, _ in head], [s for _, s, _ in head], [e for _, _, e in head]
        hspans = sorted(_opening_spans(hw))
        if hspans and hspans[0][0] <= 3:
            # The book opens with credits: cut from the start, music between them included,
            # up to the first word that isn't part of them (the dedication, "Prologue"...).
            k = next((j for j in range(len(hw)) if not any(a <= j < b for a, b in hspans)), len(hw))
            timed.append((0.0, _cut_time(he[k - 1], hs[k], silences) if k < len(hw) else he[-1]))
        else:
            for a, b in hspans:
                timed.append((_cut_time(he[a - 1], hs[a], silences) if a else 0.0,
                              _cut_time(he[b - 1], hs[b], silences) if b < len(hw) else he[-1]))
        # The opening credits are cut by time; drop their (possibly partial) copy from tw.
        last = max((b for _, b in timed), default=0.0)
        spans += [(a, b) for a, b in _opening_spans(tw) if times[a] < last]
    else:
        spans += _opening_spans(tw)
    for i in _find(nw, "audiobook has been broken into multiple parts".split()):
        a = i - 1 if i > 0 and nw[i - 1] == "this" else i
        b = next((k + 5 for k in range(i, min(len(nw), i + 120))
                  if nw[k:k + 5] == "you have enjoyed this program".split()), None)
        if b is None:
            continue
        if nw[b:b + 3] == ["this", "is", "audible"]:
            b += 3
        p = next((k for k in range(b, min(len(nw), b + 8)) if nw[k:k + len(PRESENTS)] == PRESENTS), None)
        if p is not None:
            b = _presents_end(nw, tw, p) or b
        spans.append((a, b))
    tail = len(nw) - 8000
    credits = _find(nw, "we hope you have enjoyed our presentation".split(), tail)
    if credits:
        spans.append((credits[-1], len(nw)))
    end_of_book = credits[-1] if credits else len(nw)

    # "This ends the seventh tale of the Malazan Book of the Fallen." closes the story;
    # anything read after it, before the credits, is back matter.
    back = None
    for i in reversed(_find(nw, ["this", "ends", "the"], tail, end_of_book)):
        k = next((k for k in range(i + 3, min(end_of_book, i + 16)) if tw[k][-1:] in ".!?"), None)
        if k is not None and k + 1 < end_of_book:
            t0 = _cut_time(ends[k], times[k + 1], silences)
            t1 = times[end_of_book] if end_of_book < len(nw) else total
            if t1 - t0 > 20:
                back = t0
        break

    for a, b in spans:
        t0 = 0.0 if a == 0 else _cut_time(ends[a - 1], times[a], silences)
        t1 = total if b >= len(nw) else _cut_time(ends[b - 1], times[b], silences)
        timed.append((t0, t1))
    cuts = []
    for t0, t1 in sorted(timed):
        if t1 > t0:
            if cuts and t0 <= cuts[-1][1] + 1.0:     # merge credits read back to back
                cuts[-1] = (cuts[-1][0], max(cuts[-1][1], t1))
            else:
                cuts.append((t0, t1))
    return cuts, back


def _cut_time(prev_end, next_start, silences):
    """A cut point between two words: the middle of the pause there, if one was detected."""
    lo, hi = min(prev_end, next_start), max(prev_end, next_start)
    i = bisect.bisect_left(silences, (lo - 2.0,))
    for s, e in silences[i:i + 6]:
        if s <= hi + 0.3 and e >= lo - 0.3:
            return (max(s, lo - 0.3) + min(e, hi + 0.3)) / 2
    return (lo + hi) / 2


def remap(t, cuts):
    """Time t on the original timeline -> time in the output, with `cuts` removed."""
    return t - sum(max(0.0, min(t, b) - a) for a, b in cuts)


def locate(tw, index, probe, lo, hi, min_votes=6):
    """Find where `probe` (list of normalized words) starts in transcript words tw[lo:hi]."""
    votes = Counter()
    first = {}
    for j in range(len(probe) - 2):
        for pos in index.get(tuple(probe[j:j + 3]), ()):
            if lo <= pos < hi:
                est = pos - j
                votes[est // 10] += 1
                first.setdefault(est // 10, []).append(est)
    best, bv = None, 0
    for b in list(votes):
        v = votes[b] + votes.get(b - 1, 0) + votes.get(b + 1, 0)
        if v > bv:
            best, bv = b, v
    if best is None or bv < min_votes:
        return None, bv
    ests = sorted(first[best])
    return max(0, ests[len(ests) // 2]), bv


def _snap(nw, tw, probe, pos, lo):
    """locate() can be several words off: move pos to an exact match of the probe's
    opening words nearby, if there is one (there isn't for e.g. "Fifteen" vs "1500")."""
    # Three words, starting up to 5 words in (the first few may be spelt differently:
    # "Grey" vs "Gray"), then two, then one distinctive word.
    for j, n, reach in [(j, 3, 30) for j in range(6)] + [(0, 2, 15), (0, 1, 5)]:
        if n == 1 and len(probe[0]) < 4:
            break
        exact = [i for i in range(max(lo + j, pos + j - reach), min(len(nw) - n, pos + j + reach + 1))
                 if nw[i:i + n] == probe[j:j + n]]
        if exact:
            i = min(exact, key=lambda i: abs(i - j - pos))
            # Step back over the j unmatched words, but not across a sentence end: the
            # transcript may have fewer of them ("days of..." for "The First Days of...").
            for _ in range(j):
                if i - 1 < lo or tw[i - 1][-1:] in ".!?":
                    break
                i -= 1
            return i
    return pos


def align_chapters(chapters, tw, times):
    """Sets c['start'] (seconds on the global timeline) for every chapter."""
    nw = [norm(w) for w in tw]
    index = defaultdict(list)
    for i in range(len(nw) - 2):
        index[(nw[i], nw[i + 1], nw[i + 2])].append(i)
    total_epub = sum(len(c["words"]) for c in chapters)
    cum, prev = 0, 0
    for c in list(chapters):
        probe = [norm(w) for w in c["words"][:250] if norm(w)]
        frac_idx = int(cum / total_epub * len(nw))
        lo = min(prev + 100, len(nw) - 1) if prev else 0
        if c.get("implied"):
            # An implied prologue must match near the start of the audio, or it isn't one.
            pos, v = locate(nw, index, probe, 0, len(nw) // 10)
            if pos is None:
                chapters.remove(c)
                continue
            print(f"  found an unheaded prologue in the epub ({v} votes)")
        else:
            pos, v = locate(nw, index, probe, lo, len(nw))
        c["votes"] = v
        if pos is None:
            pos = min(max(lo, frac_idx), len(nw) - 1)
            c["guessed"] = True
            print(f"  WARNING: could not match the start of {c['title']} "
                  f"({v} votes); using a position estimate - check it.")
        else:
            pos = _snap(nw, tw, probe, pos, lo)
        # Look back for the spoken "Chapter N" / "Prologue" / "Book One" announcement.
        key = {"prologue": "prologue", "epilogue": "epilogue", "chapter": "chapter"}[c["kind"]]
        s = pos
        for k in range(pos - 1, max(0, pos - 45) - 1, -1):
            if nw[k] == key or (key != "chapter" and nw[k] == key[:-2]):     # also "Prolog"
                s = k
                # A "Book Two" heading just before: capitalised and numbered, so not e.g.
                # the "Malazan Book of the Fallen" in an epigraph's attribution.
                s = next((k2 for k2 in range(k - 1, max(0, k - 25) - 1, -1)
                          if tw[k2].startswith("Book") and spoken_number(tw[k2 + 1:k2 + 2])), s)
                break
        else:
            if c["kind"] == "chapter":
                # Sometimes only the number is read out ("...would wait. 8. Among the..."),
                # possibly after the book's title ("Book 4. Toll the Hounds").
                k = next((k for k in (pos - 1, pos - 2) if k >= 0 and re.fullmatch(r"\d+|[IVXL]+", tw[k].strip(".,:;"))), None)
                if k is not None:
                    s = k
                    c["spoken"] = spoken_number([tw[k]])
                s = next((k2 for k2 in range(s - 1, max(0, s - 12) - 1, -1)
                          if tw[k2].startswith("Book") and spoken_number(tw[k2 + 1:k2 + 2])), s)
        c["start"] = max(0.0, times[s] - 0.4)
        c["start_word"] = s
        c["body_start_word"] = pos
        ck = next((i for i in range(s, pos) if nw[i] == "chapter"), None)   # after any "Book Two..."
        if ck is not None:
            c["spoken"] = spoken_number(tw[ck + 1:ck + 3])
        # Where the story starts after the epigraph: match its first words just after pos.
        if c.get("story_words") and not c.get("guessed"):
            sprobe = [norm(w) for w in c["story_words"][:250] if norm(w)]
            spos, _ = locate(nw, index, sprobe, pos, min(len(nw), pos + 2 * c["epigraph_words"] + 150))
            if spos is not None:
                spos = _snap(nw, tw, sprobe, spos, pos + 1)
                if spos > pos:
                    c["story_time"], c["story_prev_time"] = times[spos], times[spos - 1]
        # The epub's own scene breaks: match the words after each one near where its word
        # offset puts it (the transcript's word count drifts a little from the epub's).
        c["marked"] = []
        for off, words in ([] if c.get("guessed") else c.get("breaks", ())):
            bprobe = [norm(w) for w in words if norm(w)]
            reach = 300 + off // 10
            bpos, _ = locate(nw, index, bprobe, max(pos + 1, pos + off - reach), min(len(nw), pos + off + reach))
            if bpos is not None:
                c["marked"].append(_snap(nw, tw, bprobe, bpos, pos + 1))
        prev = pos
        cum += len(c["words"])


# -------------------------------------------------------------------------- scenes
def story_boundary(c, silences, min_len=3.0):
    """Time the story starts after a chapter's epigraph, moved back into the pause the
    narrator leaves before it; None if the chapter has no (usable) epigraph."""
    t = c.get("story_time")
    if t is None:
        return None
    t = break_before(t, c["story_prev_time"], silences)
    return t if c["start"] + min_len <= t < c["end"] else None


def break_before(t, prev_t, silences):
    """Move a section start (its first word at t) back into the pause before it. Only a
    pause after the previous word (starting at prev_t) counts: the narrator also pauses
    mid-sentence, e.g. between an epigraph's quote and its attribution. Whisper often puts
    the word's start well inside the pause, so the pause may end after t."""
    i = bisect.bisect_left(silences, prev_t, key=lambda p: p[0])
    pauses = [(s, e) for s, e in silences[i:i + 50] if s <= t + 0.3]
    if pauses:
        s, e = max(pauses, key=lambda p: p[1])
        t = max(s, e - 0.5)
    return t


def marked_break(k, times, silences, reach=3, min_pause=1.0):
    """Time of an epub-marked scene break whose next paragraph starts at transcript word k.
    The narrator pauses there; if the match is a word or two off, use the longest pause
    before a nearby word instead."""
    best, bd = k, 0.0
    for j in range(max(1, k - reach), min(len(times), k + reach + 1)):
        i = bisect.bisect_left(silences, times[j - 1], key=lambda p: p[0])
        d = max((e - s for s, e in silences[i:i + 5] if s <= times[j] + 0.3), default=0.0)
        if d > bd + 0.2 or (abs(d - bd) <= 0.2 and abs(j - k) < abs(best - k)):
            best, bd = j, d
    if bd < min_pause:
        best = k
    return break_before(times[best], times[best - 1], silences)


def auto_scene_gap(silences, total, per_hour=12.0, lo=2.0, hi=3.5):
    """Narrators differ: some pause 2 s and more only between scenes, others at every
    other paragraph. Take the shortest gap that at most `per_hour` pauses an hour reach
    (scenes run 7-10 an hour in the books whose epubs mark them all)."""
    lens = sorted((e - s for s, e in silences), reverse=True)
    k = int(per_hour * total / 3600)
    return min(hi, max(lo, round(lens[k] + 0.05, 1))) if k < len(lens) else lo


def plan_scenes(a, b, silences, gap, min_scene, guard=8.0, fixed=()):
    """Return scene boundaries (list of times) inside [a, b): the `fixed` ones (marked in
    the epub) plus the longest pauses that leave every scene at least min_scene long."""
    starts = [s for s, _ in silences]
    cands = []
    i = bisect.bisect_left(starts, a + guard)
    while i < len(silences) and silences[i][0] < b - guard:
        s, e = silences[i]
        if e - s >= gap:
            cands.append((e - s, max(s, e - 0.5)))
        i += 1
    accepted = [t for t in fixed if a + guard <= t < b - guard]
    for d, t in sorted(cands, reverse=True):
        pts = sorted(accepted + [a, b])
        j = bisect.bisect_left(pts, t)
        if t - pts[j - 1] >= min_scene and pts[j] - t >= min_scene:
            accepted.append(t)
    return sorted(accepted)


# -------------------------------------------------------------------------- build
def _ffmeta(s):
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def build_m4b(parts, plan, out, meta, tmp, cuts=()):
    """Join all audio files into one .m4b (stream copy, no re-encoding) with a chapter
    marker per plan entry, the source's tags and a single embedded cover. `cuts` are
    (start, end) ranges on the joined timeline to leave out; plan times are already on
    the output timeline."""
    tmp.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    primary = parts[0][0]

    # Each part becomes one or more concat entries; inpoint/outpoint skip the cuts.
    entries = []
    for p, off, dur in parts:
        keep, t = [], 0.0
        for a, b in cuts:
            a, b = max(a - off, 0.0), min(b - off, dur)
            if b > a:
                if a > t:
                    keep.append((t, a))
                t = max(t, b)
        if t < dur:
            keep.append((t, dur))
        name = p.as_posix().replace("'", r"'\''")
        for a, b in keep:
            entries.append(f"file '{name}'\n" + (f"inpoint {a:.3f}\n" if a > 0 else "")
                           + (f"outpoint {b:.3f}\n" if b < dur else ""))
    lst = tmp / "parts.txt"
    lst.write_text("".join(entries), encoding="utf8")
    chap = tmp / "chapters.txt"
    chap.write_text(";FFMETADATA1\n" + "".join(
        f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={round(s * 1000)}\nEND={round(e * 1000)}\ntitle={_ffmeta(label)}\n"
        for label, s, e in plan), encoding="utf8")

    mdargs = []
    for k, v in meta.items():
        mdargs += ["-metadata", f"{k}={v}"]
    # Inputs: 0 = the joined audio, 1 = chapter markers, 2 = first source file (tags),
    # 3 = cover. Source chapters are dropped: each part has one chapter spanning the whole
    # part. The cover goes in as a standalone image: copied straight from the source it
    # keeps that file's timing, which garbles the first chapter markers.
    cover = extract_cover(primary, tmp)
    coverargs = [] if cover is None else ["-i", str(cover), "-map", "3", "-disposition:v:0", "attached_pic"]
    partial = out.with_suffix(".partial.m4b")
    run(["ffmpeg", "-v", "error", "-stats", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-i", str(chap), "-i", str(primary), *coverargs[:2],
         "-map", "0:a:0", *coverargs[2:], "-map_metadata", "2", "-map_chapters", "1",
         "-c", "copy", *mdargs, "-movflags", "+faststart", "-f", "ipod", str(partial)])
    partial.replace(out)
    for f in (lst, chap, cover):
        if f:
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio-dir", default=DEFAULT_AUDIO)
    ap.add_argument("--epub", default=DEFAULT_EPUB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--album", help="album/title tag for the output (default: the epub's title)")
    ap.add_argument("--model", default="base.en", help="faster-whisper model (tiny.en, base.en, small.en)")
    ap.add_argument("--scene-gap", type=float, help="min silence (s) that counts as a scene break "
                    "(default: picked per book from how often the narrator pauses)")
    ap.add_argument("--min-scene", type=float, default=90.0, help="shortest allowed scene (s)")
    ap.add_argument("--noise", default="-35dB", help="silence threshold; raise (e.g. -30dB) if too few pauses found")
    ap.add_argument("--no-scenes", action="store_true", help="one marker per chapter, not per scene")
    ap.add_argument("--dry-run", action="store_true", help="write plan.csv, don't build the .m4b")
    args = ap.parse_args()

    ensure_ffmpeg()
    ensure_packages()

    audio_dir, out = Path(args.audio_dir), Path(args.out)
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    files = sorted((p for p in audio_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS), key=natural_key)
    if not files:
        sys.exit(f"No audio files found in {audio_dir}")

    print("Audio files:")
    parts, tw, times, ends, silences = [], [], [], [], []
    off = 0.0
    for f in files:
        dur = duration(f)
        print(f"  {f.name}  ({hms(dur)})")
        parts.append((f, off, dur))
        key = safe_name(f.stem).replace(" ", "_")
        print("  transcribing (first run is slow; cached afterwards)...")
        words = transcribe(f, args.model, work / f"transcript_{key}.json")
        sil = detect_silences(f, work / f"silence_{key}_{args.noise}.json", args.noise)
        for w, s, e in words:
            tw.append(w)
            times.append(off + s)
            ends.append(off + e)
        silences += [(off + s, off + e) for s, e in sil]
        off += dur
    total = off
    silences.sort()

    print("Reading epub...")
    album = args.album or epub_title(args.epub) or Path(args.epub).stem
    artist = format_tag(files[0], "album_artist") or format_tag(files[0], "artist")
    print(f"  album: {album}" + (f"  (artist: {artist})" if artist else ""))
    chapters = find_chapters(epub_paragraphs(args.epub))

    print("Aligning chapters to audio...")
    align_chapters(chapters, tw, times)
    fix_chapter_numbers(chapters, tw, times)
    print(f"  {len(chapters)} chapters/sections: "
          + ", ".join(c["title"] for c in chapters[:3]) + " ... " + chapters[-1]["title"])

    head = transcribe_head(files[0], args.model, work / f"head_{safe_name(files[0].stem).replace(' ', '_')}.json")
    removed, back = find_credits(tw, times, ends, total, silences, head)
    if removed and removed[0][0] == 0 and removed[0][1] < chapters[0]["start"]:
        # Nothing but music between the opening credits and the first chapter: cut that too.
        if bisect.bisect_left(times, removed[0][1]) == bisect.bisect_left(times, chapters[0]["start"]):
            removed[0] = (0.0, chapters[0]["start"])
    for a, b in removed:
        i = bisect.bisect_left(times, a)
        print(f"  removing publisher credits at {hms(a)} ({b - a:.0f} s): {' '.join(tw[i:i + 8])}...")
    for k, c in enumerate(chapters):
        c["end"] = chapters[k + 1]["start"] if k + 1 < len(chapters) else (back or total)

    # Scene breaks: the epub's own where it marks them all; otherwise long pauses, plus
    # whichever breaks the epub still marks.
    for c in chapters:
        # A "scene" under 20 s is a stylistic gap: spaced-out fragments, a poem with a gap
        # between every line, a closing line. Of a run of such gaps keep only the first.
        kept, last = [], c["start"]
        for t in sorted(marked_break(k, times, silences) for k in c.get("marked", ())):
            r = remap(t, removed)     # lengths as heard, publisher credits cut
            if r - remap(last, removed) >= 20 and remap(c["end"], removed) - r >= 20:
                kept.append(t)
            last = t
        c["marked"] = kept
    n_marked = sum(len(c["marked"]) for c in chapters)
    marks_only = n_marked >= 4 * total / 3600
    gap = args.scene_gap or auto_scene_gap(silences, total)
    if args.no_scenes:
        pass
    elif marks_only:
        print(f"  scenes: the {n_marked} scene breaks marked in the epub")
    else:
        print(f"  scenes: pauses of {gap:.1f} s or more"
              + (f", plus {n_marked} scene breaks marked in the epub" if n_marked else ""))

    # Build the plan: front matter, each chapter split into scenes, back matter.
    plan = []   # (label, start, end)
    if chapters[0]["start"] > 5:
        plan.append(("Front matter", 0.0, chapters[0]["start"]))
    for c in chapters:
        begin = c["start"]
        story = None if args.no_scenes else story_boundary(c, silences)
        if story:
            plan.append((f"{c['title']} - Epigraph", begin, story))
            begin = story
        cuts = [] if args.no_scenes else \
            [t for t in c["marked"] if begin + 10 <= t < c["end"] - 10] if marks_only else \
            plan_scenes(begin, c["end"], silences, gap, args.min_scene, fixed=c["marked"])
        bounds = [begin] + cuts + [c["end"]]
        n = len(bounds) - 1
        for s in range(n):
            label = c["title"] if n == 1 and not story else f"{c['title']} - Scene {s + 1:02d}"
            plan.append((label, bounds[s], bounds[s + 1]))
    if back:
        plan.append(("Back matter", back, total))

    # Move the markers onto the output timeline (credits removed). A marker left with
    # (almost) nothing in it is folded into its neighbour.
    final = []
    for label, s, e in plan:
        s, e = remap(s, removed), remap(e, removed)
        if e - s < 2.0:
            if final:
                final[-1] = (final[-1][0], final[-1][1], e)
            continue
        final.append((label, final[-1][2] if final else 0.0, e))
    plan = final

    with open(out / "plan.csv", "w", newline="", encoding="utf8") as fh:
        w = csv.writer(fh)
        w.writerow(["chapter marker", "start", "end", "length_min"])
        for label, s, e in plan:
            w.writerow([label, hms(s), hms(e), f"{(e - s) / 60:.1f}"])
    print(f"Plan: {len(plan)} chapter markers -> {out / 'plan.csv'}")
    shaky = [c["title"] for c in chapters if c.get("guessed")]
    if shaky:
        print("  Check these chapter starts by ear:", ", ".join(shaky))
    if args.dry_run:
        print("Dry run: nothing built. Review plan.csv, then run again without --dry-run.")
        return

    dest = out / f"{safe_name(album)}.m4b"
    if dest.exists():
        print(f"{dest} already exists; delete it to rebuild.")
        return
    print(f"Building {dest.name} ({hms(remap(total, removed))}, {len(plan)} chapter markers)...", flush=True)
    # The source parts carry "Part N" titles and track numbers; this is the whole book.
    meta = {"title": album, "album": album, "track": "", "disc": ""}
    if artist:
        meta["album_artist"] = artist
    build_m4b(parts, plan, dest, meta, work / "tmp", removed)
    print("Done.")


if __name__ == "__main__":
    main()