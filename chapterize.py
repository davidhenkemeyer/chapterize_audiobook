#!/usr/bin/env python3
"""
chapterize.py - split an audiobook into chapters, and each chapter into scenes.

How it works
  1. Transcribes the audio with faster-whisper (cached, so reruns are fast).
  2. Reads the epub, finds the Prologue / Chapter N / Epilogue headings and
     matches the first words of each chapter against the transcript to find
     where each chapter starts in the audio. Works across multiple audio files
     (a chapter that spans two parts is stitched together automatically).
  3. Finds scene breaks inside each chapter from long silences (the epub has
     no scene markers, so this is pause-based).
  4. Cuts with ffmpeg stream-copy (no re-encoding, no quality loss).

Requirements
  - Python 3.9+
  - ffmpeg/ffprobe, numpy and faster-whisper are installed automatically on first
    run if missing (ffmpeg via `winget install Gyan.FFmpeg`, packages via pip).

Usage (defaults match the Midnight Tides paths):
  python chapterize.py --dry-run     # transcribe + write plan.csv, don't cut
  python chapterize.py               # do everything

Tune scene detection with --scene-gap (seconds of silence that count as a scene
break, default 2.0) and --min-scene (shortest allowed scene, default 90 s).
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

DEFAULT_AUDIO = r"C:\AudioBooks\Steven Erikson - Malazan Book of the Fallen 05 - Midnight Tides"
DEFAULT_EPUB = r"C:\Users\david\Malazan 5 - Midnight Tides - Erikson_ Steven.epub"
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
        self.paras, self.buf, self.depth = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6") and self.depth:
            self.depth -= 1
            t = " ".join("".join(self.buf).split())
            if t:
                self.paras.append(t)
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
    paras = []
    for n in order:
        if n not in names:
            continue
        p = _P()
        p.feed(z.read(n).decode("utf8", "replace"))
        paras.extend(p.paras)
    return paras


HEAD_RE = re.compile(r"^(prolog|proloq|epilog|epiloq|chap)", re.I)


def find_chapters(paras, min_words=400):
    """Returns [{'kind','num','title','words'(body words)}]; OCR typos tolerated."""
    heads = [i for i, t in enumerate(paras) if len(t) <= 40 and HEAD_RE.match(t.strip())]
    chapters = []
    for k, i in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(paras)
        body = paras[i + 1:end]
        words = " ".join(body).split()
        if len(words) < min_words:      # table of contents, stray headings, etc.
            continue
        h = paras[i].strip().lower()
        kind = "prologue" if h.startswith(("prolog", "proloq")) else \
               "epilogue" if h.startswith(("epilog", "epiloq")) else "chapter"
        chapters.append({"kind": kind, "words": words})
    n = 0
    for c in chapters:
        if c["kind"] == "chapter":
            n += 1
            c["num"] = n
            c["title"] = f"Chapter {n:02d}"
        else:
            c["num"] = 0
            c["title"] = c["kind"].capitalize()
    return chapters


# ------------------------------------------------------------------------ alignment
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


def align_chapters(chapters, tw, times):
    """Sets c['start'] (seconds on the global timeline) for every chapter."""
    nw = [norm(w) for w in tw]
    index = defaultdict(list)
    for i in range(len(nw) - 2):
        index[(nw[i], nw[i + 1], nw[i + 2])].append(i)
    total_epub = sum(len(c["words"]) for c in chapters)
    cum, prev = 0, 0
    for c in chapters:
        probe = [norm(w) for w in c["words"][:250] if norm(w)]
        frac_idx = int(cum / total_epub * len(nw))
        lo = prev + 100 if prev else 0
        pos, v = locate(nw, index, probe, lo, len(nw))
        c["votes"] = v
        if pos is None:
            pos = max(lo, frac_idx)
            c["guessed"] = True
            print(f"  WARNING: could not match the start of {c['title']} "
                  f"({v} votes); using a position estimate - check it.")
        # Look back for the spoken "Chapter N" / "Prologue" / "Book One" announcement.
        key = {"prologue": "prologue", "epilogue": "epilogue", "chapter": "chapter"}[c["kind"]]
        s = pos
        for k in range(pos - 1, max(0, pos - 45) - 1, -1):
            if nw[k] == key:
                s = k
                for k2 in range(k - 1, max(0, k - 25) - 1, -1):
                    if nw[k2] == "book":
                        s = k2
                        break
                break
        c["start"] = max(0.0, times[s] - 0.4)
        c["body_start_word"] = pos
        prev = pos
        cum += len(c["words"])


# -------------------------------------------------------------------------- scenes
def plan_scenes(a, b, silences, gap, min_scene, guard=8.0):
    """Return scene boundaries (list of times) inside [a, b)."""
    starts = [s for s, _ in silences]
    cands = []
    i = bisect.bisect_left(starts, a + guard)
    while i < len(silences) and silences[i][0] < b - guard:
        s, e = silences[i]
        if e - s >= gap:
            cands.append((e - s, max(s, e - 0.5)))
        i += 1
    accepted = []
    for d, t in sorted(cands, reverse=True):
        pts = sorted(accepted + [a, b])
        j = bisect.bisect_left(pts, t)
        if t - pts[j - 1] >= min_scene and pts[j] - t >= min_scene:
            accepted.append(t)
    return sorted(accepted)


# ---------------------------------------------------------------------------- cut
def cut(parts, a, b, out, meta, tmp):
    """Cut global-time range [a, b) to `out`, stitching across audio files if needed,
    inheriting original metadata, and fixing container durations for audiobook players."""
    pieces = []
    for idx, (path, off, dur) in enumerate(parts):
        s, e = max(a, off), min(b, off + dur)
        if e - s > 0.05:
            pieces.append((path, s - off, e - off, idx))
    out.parent.mkdir(parents=True, exist_ok=True)
    
    # Grab the primary source file to inherit full original metadata (author, cover art, etc.)
    primary_path = pieces[0][0] if pieces else parts[0][0]

    mdargs = []
    for k, v in meta.items():
        mdargs += ["-metadata", f"{k}={v}"]

    tmp.mkdir(parents=True, exist_ok=True)
    tmpfiles = []
    for n, (path, s, e, idx) in enumerate(pieces):
        t = tmp / f"piece{n}.m4a"
        # Using -avoid_negative_ts make_zero ensures timestamps start cleanly at zero for each slice
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", str(path),
             "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero", str(t)])
        tmpfiles.append(t)

    if len(tmpfiles) == 1:
        run(["ffmpeg", "-v", "error", "-y", "-i", str(tmpfiles[0]), 
             "-i", str(primary_path), "-map_metadata", "1", "-map", "0:a", "-c", "copy",
             *mdargs, "-movflags", "+faststart", str(out)])
    else:
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{t.as_posix()}'\n" for t in tmpfiles), encoding="utf8")
        run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-i", str(primary_path), "-map_metadata", "1", "-map", "0:a", "-c", "copy",
             *mdargs, "-movflags", "+faststart", str(out)])
    for t in tmpfiles:
        t.unlink(missing_ok=True)


# ---------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio-dir", default=DEFAULT_AUDIO)
    ap.add_argument("--epub", default=DEFAULT_EPUB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--album", default="Midnight Tides")
    ap.add_argument("--model", default="base.en", help="faster-whisper model (tiny.en, base.en, small.en)")
    ap.add_argument("--scene-gap", type=float, default=2.0, help="min silence (s) that counts as a scene break")
    ap.add_argument("--min-scene", type=float, default=90.0, help="shortest allowed scene (s)")
    ap.add_argument("--noise", default="-35dB", help="silence threshold; raise (e.g. -30dB) if too few pauses found")
    ap.add_argument("--no-scenes", action="store_true", help="split into chapters only")
    ap.add_argument("--dry-run", action="store_true", help="write plan.csv, don't cut audio")
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
    parts, tw, times, silences = [], [], [], []
    off = 0.0
    for f in files:
        dur = duration(f)
        print(f"  {f.name}  ({hms(dur)})")
        parts.append((f, off, dur))
        key = safe_name(f.stem).replace(" ", "_")
        print("  transcribing (first run is slow; cached afterwards)...")
        words = transcribe(f, args.model, work / f"transcript_{key}.json")
        sil = detect_silences(f, work / f"silence_{key}_{args.noise}.json", args.noise)
        for w, s, _ in words:
            tw.append(w)
            times.append(off + s)
        silences += [(off + s, off + e) for s, e in sil]
        off += dur
    total = off
    silences.sort()

    print("Reading epub...")
    chapters = find_chapters(epub_paragraphs(args.epub))
    print(f"  found {len(chapters)} chapters/sections: "
          + ", ".join(c["title"] for c in chapters[:3]) + " ... " + chapters[-1]["title"])

    print("Aligning chapters to audio...")
    align_chapters(chapters, tw, times)
    for k, c in enumerate(chapters):
        c["end"] = chapters[k + 1]["start"] if k + 1 < len(chapters) else total

    # Build the plan: front matter, then each chapter split into scenes.
    plan = []   # (label, folder, filename, start, end)
    if chapters[0]["start"] > 5:
        plan.append(("Front matter", "00 Front matter", "00 Front matter.m4a", 0.0, chapters[0]["start"]))
    for k, c in enumerate(chapters, start=1):
        folder = f"{k:02d} {c['title']}"
        cuts = [] if args.no_scenes else plan_scenes(c["start"], c["end"], silences, args.scene_gap, args.min_scene)
        bounds = [c["start"]] + cuts + [c["end"]]
        n = len(bounds) - 1
        for s in range(n):
            if n == 1:
                label, fname = c["title"], f"{k:02d} {c['title']}.m4a"
            else:
                label = f"{c['title']} - Scene {s + 1:02d}"
                fname = f"{k:02d}.{s + 1:02d} {label}.m4a"
            plan.append((label, folder, fname, bounds[s], bounds[s + 1]))

    with open(out / "plan.csv", "w", newline="", encoding="utf8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "start", "end", "length_min"])
        for label, folder, fname, s, e in plan:
            w.writerow([f"{folder}/{fname}", hms(s), hms(e), f"{(e - s) / 60:.1f}"])
    print(f"Plan: {len(plan)} files -> {out / 'plan.csv'}")
    shaky = [c["title"] for c in chapters if c.get("guessed")]
    if shaky:
        print("  Check these chapter starts by ear:", ", ".join(shaky))
    if args.dry_run:
        print("Dry run: nothing cut. Review plan.csv, then run again without --dry-run.")
        return

    for i, (label, folder, fname, s, e) in enumerate(plan, start=1):
        dest = out / folder / fname
        if dest.exists():
            continue
        print(f"[{i}/{len(plan)}] {folder}/{fname}  ({hms(s)}-{hms(e)})", flush=True)
        cut(parts, s, e, dest,
            {"title": label, "album": args.album, "track": f"{i}/{len(plan)}"}, work / "tmp")
    print("Done.")


if __name__ == "__main__":
    main()