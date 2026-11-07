#!/usr/bin/env python3
"""Render a report as an MP4 video.

Usage:
    python3 -I scripts/report_video.py report.md
    python3 -I scripts/report_video.py report.md -o acme.mp4
    python3 -I scripts/report_video.py report.md --vertical     # 1080x1920
    python3 -I scripts/report_video.py report.md --seconds 5    # fixed per slide

How it works: the deck is built first, then a headless browser photographs each
slide at full resolution and ffmpeg assembles them with crossfades. Each slide
is held in proportion to how much there is to read, so a dense slide stays up
longer than a title card.

Why not Remotion, which is the usual suggestion. Two reasons, and only the
second is about taste. Its licence is free for individuals, non-profits and
for-profit companies of up to three employees, and a Company License is
required above that, so a tool under MIT cannot depend on it without passing
that condition to whoever installs the tool. And it would mean a React project,
an npm install of a few hundred megabytes, and a second rendering of content
that this repository already lays out in CSS. Motion Canvas and Revideo are MIT
and would avoid the licence problem, but not the duplication.

What this costs instead: ffmpeg, which is one install and is already present on
most machines that do anything with media. If it is missing, the frames are
still written out as PNGs and the ffmpeg command needed to finish the job is
printed, so nothing is lost.

What this will not do: per-element motion inside a slide, like text flying in
word by word. Each slide is one still frame, and the movement is between them.
If motion graphics of that kind are the point, a dedicated animation tool is
the right answer and this is not pretending otherwise.
"""

import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_deck import build_html, find_browser  # noqa: E402

FPS = 30
FADE = 0.7           # seconds of crossfade between slides
MIN_HOLD = 3.0
MAX_HOLD = 11.0


def shoot(browser, html_path, out_png, index, width, height):
    """Photograph one slide by asking the deck to open at that slide."""
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--hide-scrollbars", "--force-device-scale-factor=1",
           f"--window-size={width},{height}",
           # Generous, because web fonts, the entrance transition and the
           # counting number tiles all have to finish before the shutter.
           "--virtual-time-budget=9000",
           "--run-all-compositor-stages-before-draw",
           f"--screenshot={out_png}",
           f"file://{os.path.abspath(html_path)}#{index + 1}"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return os.path.isfile(out_png) and os.path.getsize(out_png) > 2000


def hold_for(slide_html, fixed=None):
    """How long to leave a slide up.

    Reading time, not a fixed beat: a title card does not need the same seconds
    as a slide with a table on it.
    """
    if fixed:
        return float(fixed)
    text = re.sub(r"<[^>]+>", " ", slide_html)
    words = len(text.split())
    return max(MIN_HOLD, min(MAX_HOLD, 2.2 + words * 0.055))


def build_video(pngs, holds, out, width, height):
    """Crossfade the stills together with ffmpeg.

    xfade works on a pair at a time, so the chain is built up one slide at a
    time with the running offset tracked by hand.
    """
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for png, hold in zip(pngs, holds):
        args += ["-loop", "1", "-t", f"{hold + FADE:.3f}", "-i", png]

    parts, prev, offset = [], None, 0.0
    for i, hold in enumerate(holds):
        parts.append(f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,"
                     f"setsar=1,fps={FPS}[v{i}]")
    chain = ";".join(parts)

    if len(holds) == 1:
        chain += ";[v0]null[out]"
    else:
        prev = "v0"
        offset = holds[0]
        for i in range(1, len(holds)):
            label = "out" if i == len(holds) - 1 else f"x{i}"
            chain += (f";[{prev}][v{i}]xfade=transition=fade:duration={FADE}:"
                      f"offset={offset:.3f}[{label}]")
            prev = label
            offset += holds[i]

    args += ["-filter_complex", chain, "-map", "[out]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=900)
    return proc.returncode == 0, (proc.stderr or "").strip()[:500]


def main():
    # An explicit --help is a request that succeeded, so it exits 0. Running with
    # no arguments at all is a usage error and still exits 1.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 1

    src = args[0]
    if not os.path.isfile(src):
        print(f"not found: {src}", file=sys.stderr)
        return 2

    def opt(flag):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("-"):
                return sys.argv[i + 1]
        return None

    vertical = "--vertical" in sys.argv
    width, height = (1080, 1920) if vertical else (1920, 1080)
    out = opt("-o") or opt("--out") or os.path.splitext(src)[0] + ".mp4"
    fixed = opt("--seconds")

    browser = find_browser()
    if not browser:
        print("No headless browser found, so the slides cannot be photographed.",
              file=sys.stderr)
        print("Set CHROME_BIN to a Chrome, Chromium, Edge or Brave binary.",
              file=sys.stderr)
        return 3

    with open(src, encoding="utf-8", errors="replace") as fh:
        md = fh.read()
    base_dir = os.path.dirname(os.path.abspath(src))
    page = build_html(md, base_dir, title=opt("--title"), subtitle=opt("--subtitle"),
                      lang=opt("--lang"))

    html_path = os.path.splitext(out)[0] + ".deck.html"
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    slides = re.findall(r'<section class="slide.*?</section>', page, re.S)
    frame_dir = os.path.splitext(out)[0] + ".frames"
    os.makedirs(frame_dir, exist_ok=True)

    print(f"shooting {len(slides)} slides at {width}x{height}")
    pngs, holds = [], []
    for i, slide in enumerate(slides):
        png = os.path.join(frame_dir, f"{i:03d}.png")
        if not shoot(browser, html_path, png, i, width, height):
            print(f"  slide {i + 1}: could not be photographed, skipped", file=sys.stderr)
            continue
        pngs.append(png)
        holds.append(hold_for(slide, fixed))
        print(f"  slide {i + 1}/{len(slides)}  {holds[-1]:.1f}s")

    if not pngs:
        print("No slides were photographed, so there is nothing to encode.", file=sys.stderr)
        return 4

    total = sum(holds)
    if not shutil.which("ffmpeg"):
        print(f"\nFrames are in {frame_dir}, but ffmpeg is not installed so they "
              f"were not encoded.")
        print("Install it (brew install ffmpeg, or apt install ffmpeg) and re-run,")
        print("or assemble them yourself from that folder.")
        return 0

    print(f"encoding {total:.0f}s of video")
    ok, err = build_video(pngs, holds, os.path.abspath(out), width, height)
    if not ok:
        print(f"\nffmpeg failed: {err}", file=sys.stderr)
        print(f"The frames are still in {frame_dir}.", file=sys.stderr)
        return 5

    size = os.path.getsize(out)
    print(f"\nvideo {out}")
    print(f"      {len(pngs)} slides, {total:.0f}s, {size / 1_000_000:.1f} MB, "
          f"{width}x{height}")
    print(f"      frames kept in {frame_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
