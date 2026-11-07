#!/usr/bin/env python3
"""Turn a finished markdown report into an animated, self-contained HTML deck.

The markdown stays the detailed record. This is the thing you send someone, or
put on a screen in a meeting: one page per idea, each with a line saying why it
matters, animated as you move through it.

Usage:
    python3 -I scripts/report_deck.py report.md
    python3 -I scripts/report_deck.py report.md -o acme.html
    python3 -I scripts/report_deck.py report.md --pdf      # also print it
    python3 -I scripts/report_deck.py report.md --subtitle "Prepared for ..."

Output is ONE html file with the CSS, the JavaScript and every image inlined.
No build step, no npm install, no CDN, nothing to serve. It opens from a double
click and still works attached to an email or copied onto a stick.

Why not a presentation framework: reveal.js is MIT and would do this well, but
vendoring it means committing a few hundred KB of third-party JavaScript into a
repository that gets copied wholesale into every user's skills directory. The
deck needs slides, transitions, staggered reveals and an overview grid, which is
a few hundred lines of plain CSS and JavaScript. Remotion is sometimes suggested
for this and is the wrong tool twice over: it renders videos rather than
interactive pages, and its licence is free only for individuals, non-profits and
for-profit companies with up to three employees, so it cannot sit under an MIT
tool without passing that condition on to whoever uses it.

Markdown conventions the deck understands:

    ## Heading          starts a new slide
    ---                 also starts a new slide
    > line after a      becomes the "why this matters" band under the heading
      heading
    ::: stats           a block of "value | label" lines, rendered as tiles
    210 | Japan         with the numbers counting up on arrival
    :::
"""

import base64
import html as html_mod
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from datetime import date

# ---------------------------------------------------------------------------
# Browser discovery
#
# Ordered by how likely the binary is to be the one a person already has. The
# env var wins so a caller can point at something unusual without editing this.
# ---------------------------------------------------------------------------
BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
BROWSER_NAMES = ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "msedge",
                 "brave-browser", "chrome"]


# ---------------------------------------------------------------------------
# Plain-language check
#
# A recon report is read by people who do not work in analytics, and the habits
# that creep into this kind of writing -- em dashes stacking clauses onto one
# sentence, and words like "optimisation surface" -- are exactly what loses
# them. The renderer cannot rewrite the prose, but it can say where the reading
# gets hard, which is enough to send someone back to fix it.
# ---------------------------------------------------------------------------
HARD_WORDS = {
    "utilise": "use", "utilize": "use", "leverage": "use", "leveraging": "using",
    "facilitate": "help", "methodology": "method", "subsequently": "later",
    "aforementioned": "that", "commence": "start", "terminate": "end",
    "endeavour": "try", "endeavor": "try", "ascertain": "find out",
    "demonstrates": "shows", "indicates": "shows", "additionally": "also",
    "furthermore": "also", "moreover": "also", "consequently": "so",
    "nevertheless": "but", "notwithstanding": "despite", "approximately": "about",
    "numerous": "many", "sufficient": "enough", "requisite": "needed",
    "operationalise": "put to work", "granular": "detailed", "holistic": "whole",
    "synergy": "fit", "paradigm": "model", "robust": "strong",
}


def style_check(md):
    """Report where the writing is harder than it needs to be."""
    prose = re.sub(r"```.*?```", "", md, flags=re.S)       # skip code blocks
    prose = re.sub(r"`[^`]*`", "", prose)                  # and inline code
    prose = re.sub(r"\|.*\|", "", prose)                   # and table rows
    prose = re.sub(r"^\s*#.*$", "", prose, flags=re.M)      # and headings

    notes = []

    dashes = len(re.findall(r"\s[—–]\s", prose))
    if dashes:
        notes.append(f"{dashes} em dash{'es' if dashes != 1 else ''}. "
                     "Most become a full stop and a new sentence.")

    long_sents = []
    for sent in re.split(r"(?<=[.!?])\s+", prose):
        words = len(sent.split())
        if words > 34:
            long_sents.append((words, " ".join(sent.split()[:9])))
    if long_sents:
        notes.append(f"{len(long_sents)} sentence{'s' if len(long_sents) != 1 else ''} "
                     f"over 34 words. Longest {max(w for w, _ in long_sents)}: "
                     f'"{max(long_sents)[1]}..."')

    found = {}
    low = prose.lower()
    for word, simpler in HARD_WORDS.items():
        n = len(re.findall(r"\b" + word + r"\b", low))
        if n:
            found[word] = (n, simpler)
    if found:
        top = sorted(found.items(), key=lambda kv: -kv[1][0])[:5]
        notes.append("long words: " + ", ".join(f"{w} -> {s}" for w, (n, s) in top))

    return notes


def find_browser():
    env = os.environ.get("CHROME_BIN") or os.environ.get("BROWSER_BIN")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for path in BROWSER_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# ---------------------------------------------------------------------------
# Markdown -> HTML
#
# A general markdown library is not in the standard library and vendoring one
# would undo the zero-dependency promise, so this covers the subset the report
# template actually produces: headings, paragraphs, GFM tables, lists, fenced
# code, blockquotes, rules, links, images and inline emphasis. Anything it does
# not recognise is emitted as escaped text rather than silently dropped, so a
# report never loses content just because it used a construct not handled here.
# ---------------------------------------------------------------------------

# Reports write these markers inside backticks as often as bare, and sometimes
# with a qualifier ("[inferred, moderate confidence]"). Matching the backticks
# here matters: if the code-span pass runs first it stashes the marker and the
# chip rule never sees it, which is exactly how the first build shipped them as
# grey monospace instead of coloured badges.
CONFIDENCE_RE = re.compile(
    r"`?\[(observed|inferred|speculative)([^\]]{0,48})\]`?", re.I)
CONF_CLASS = {"observed": "obs", "inferred": "inf", "speculative": "spec"}


def embed_image(src, base_dir):
    """Inline a local image so the PDF is one self-contained file.

    A PDF that silently loses its screenshots when the folder moves is worse
    than one with no images, because the gap is invisible to whoever opens it.
    """
    if re.match(r"^(https?:)?//", src) or src.startswith("data:"):
        return src
    path = src if os.path.isabs(src) else os.path.join(base_dir, src)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def inline(text, base_dir):
    """Inline spans. Code is extracted first so its contents are never parsed."""
    stash = []

    def keep(markup):
        stash.append(markup)
        return f"\x00{len(stash) - 1}\x00"

    # Chips first -- see the note on CONFIDENCE_RE.
    def conf_sub(m):
        word = m.group(1).lower()
        qualifier = (m.group(2) or "").strip(" ,;:")
        chip = f'<span class="chip {CONF_CLASS[word]}">{word}</span>'
        if qualifier:
            chip += f'<span class="chip-note">{html_mod.escape(qualifier)}</span>'
        return keep(chip)

    text = CONFIDENCE_RE.sub(conf_sub, text)

    def code_sub(m):
        return keep("<code>" + html_mod.escape(m.group(1)) + "</code>")

    text = re.sub(r"`([^`]+)`", code_sub, text)

    def img_sub(m):
        alt, src = m.group(1), m.group(2).strip()
        data = embed_image(src, base_dir)
        if data is None:
            return keep('<span class="missing-img">[image not found: '
                        + html_mod.escape(src) + "]</span>")
        fig = f'<figure><img src="{html_mod.escape(data, quote=True)}" alt="{html_mod.escape(alt, quote=True)}">'
        if alt.strip():
            fig += f"<figcaption>{html_mod.escape(alt)}</figcaption>"
        return keep(fig + "</figure>")

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", img_sub, text)

    def link_sub(m):
        label, href = m.group(1), m.group(2).strip()
        return keep(f'<a href="{html_mod.escape(href, quote=True)}">'
                    + html_mod.escape(label) + "</a>")

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_sub, text)

    text = html_mod.escape(text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w~])~~([^~\n]+)~~(?![\w~])", r"<del>\1</del>", text)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def split_row(line):
    cells, buf, esc = [], "", False
    for ch in line.strip().strip("|"):
        if esc:
            buf += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "|":
            cells.append(buf)
            buf = ""
        else:
            buf += ch
    cells.append(buf)
    return [c.strip() for c in cells]


def is_divider(line):
    s = line.strip().strip("|")
    return bool(s) and all(re.fullmatch(r"\s*:?-{1,}:?\s*", c) for c in s.split("|"))


def alignments(line):
    out = []
    for c in line.strip().strip("|").split("|"):
        c = c.strip()
        if c.startswith(":") and c.endswith(":"):
            out.append("center")
        elif c.endswith(":"):
            out.append("right")
        else:
            out.append("left")
    return out


def render_markdown(md, base_dir):
    lines = md.replace("\r\n", "\n").split("\n")
    out, i = [], 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # fenced code
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker = fence.group(1)[0] * 3
            body, i = [], i + 1
            while i < n and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1
            out.append('<pre class="code"><code>'
                       + html_mod.escape("\n".join(body)) + "</code></pre>")
            continue

        # heading
        h = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if h:
            level = len(h.group(1))
            out.append(f"<h{level}>{inline(h.group(2).strip(), base_dir)}</h{level}>")
            i += 1
            continue

        # horizontal rule
        if re.fullmatch(r"(\*\s*){3,}|(-\s*){3,}|(_\s*){3,}", stripped):
            out.append("<hr>")
            i += 1
            continue

        # table: a header row followed by a divider
        if "|" in stripped and i + 1 < n and is_divider(lines[i + 1]):
            head = split_row(stripped)
            aligns = alignments(lines[i + 1])
            i += 2
            body = []
            while i < n and "|" in lines[i] and lines[i].strip():
                body.append(split_row(lines[i]))
                i += 1
            th = "".join(
                f'<th style="text-align:{aligns[c] if c < len(aligns) else "left"}">'
                f"{inline(cell, base_dir)}</th>"
                for c, cell in enumerate(head))
            rows = []
            for r in body:
                tds = "".join(
                    f'<td style="text-align:{aligns[c] if c < len(aligns) else "left"}">'
                    f"{inline(cell, base_dir)}</td>"
                    for c, cell in enumerate(r))
                rows.append(f"<tr>{tds}</tr>")
            out.append('<div class="table-wrap"><table><thead><tr>' + th
                       + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
            continue

        # blockquote, including GitHub-style alerts
        if stripped.startswith(">"):
            body = []
            while i < n and lines[i].strip().startswith(">"):
                body.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            text = "\n".join(body).strip()
            alert = re.match(r"^\[!(\w+)\]\s*\n?(.*)$", text, re.S)
            if alert:
                kind = alert.group(1).lower()
                inner = render_markdown(alert.group(2).strip(), base_dir)
                out.append(f'<div class="callout {kind}">'
                           f'<div class="callout-label">{html_mod.escape(kind)}</div>{inner}</div>')
            else:
                out.append("<blockquote>" + render_markdown(text, base_dir) + "</blockquote>")
            continue

        # lists, one level of nesting
        li = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if li:
            ordered = not li.group(2) in ("-", "*", "+")
            tag = "ol" if ordered else "ul"
            items, base_indent = [], len(li.group(1))
            while i < n:
                m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if not m:
                    # a plain indented line continues the previous bullet
                    if items and lines[i].strip() and lines[i].startswith(" " * (base_indent + 2)):
                        items[-1][1] += " " + lines[i].strip()
                        i += 1
                        continue
                    break
                items.append([len(m.group(1)), m.group(3)])
                i += 1
            html_items, depth_open = [], False
            for indent, content in items:
                if indent > base_indent:
                    if not depth_open:
                        html_items.append(f"<{tag}>")
                        depth_open = True
                    html_items.append("<li>" + inline(content, base_dir) + "</li>")
                else:
                    if depth_open:
                        html_items.append(f"</{tag}>")
                        depth_open = False
                    html_items.append("<li>" + inline(content, base_dir) + "</li>")
            if depth_open:
                html_items.append(f"</{tag}>")
            out.append(f"<{tag}>" + "".join(html_items) + f"</{tag}>")
            continue

        # paragraph
        para = []
        while i < n and lines[i].strip() and not re.match(
                r"^\s*(#{1,6}\s|>|```|~~~|[-*+]\s|\d+[.)]\s)", lines[i]) \
                and not (("|" in lines[i]) and i + 1 < n and is_divider(lines[i + 1])):
            para.append(lines[i].strip())
            i += 1
        if para:
            out.append("<p>" + inline(" ".join(para), base_dir) + "</p>")
        else:
            out.append("<p>" + inline(stripped, base_dir) + "</p>")
            i += 1

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Page design
#
# Print CSS rather than screen CSS: real page geometry, page numbers, and
# break rules so a heading never strands itself at the foot of a page and a
# table never splits its header away from its first row.
# ---------------------------------------------------------------------------
COVER_NOTE = ("Built only from public files, the ones this website sends to anyone who "
              "visits it. Nothing here needed a login.")

# ---------------------------------------------------------------------------
# The deck
#
# Plain CSS and plain JavaScript, inlined into the one output file. Everything
# here exists to make a slide readable at a glance and to make movement between
# slides feel deliberate rather than abrupt.
# ---------------------------------------------------------------------------
DECK_CSS = """
:root{
  --ink:#0e1116; --body:#2b3038; --muted:#6b7482; --rule:#e5e9f0;
  --accent:#2b5ce6; --accent-2:#7b3fe4; --wash:#f2f6ff; --paper:#fff;
  --obs:#11704a; --obs-bg:#e3f3ea; --inf:#8a5300; --inf-bg:#fdf0da;
  --spec:#5b6472; --spec-bg:#eceff4;
  --ease:cubic-bezier(.22,.8,.3,1);
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--ink)}
body{font:16px/1.6 "Helvetica Neue",Helvetica,Arial,sans-serif;color:var(--body);
     -webkit-font-smoothing:antialiased}

/* The base styles below are the NO-SCRIPT document: every slide stacked as a
   plain scrolling page. Everything that turns it into a deck is gated on
   .js-deck, which the script adds to <html> on load.
   This ordering is deliberate. An earlier version hid every slide by default
   and relied on the script to reveal one, which meant the file rendered as a
   blank dark page with no JavaScript. A report that can show nothing at all is
   a worse failure than one that is simply not animated. */
.slide{position:relative;padding:6vh 8vw;background:var(--paper);
       border-bottom:1px solid var(--rule)}
.slide-inner{width:100%;max-width:1000px;margin:0 auto}
.bar,.count,.hint,.nav,.grid{display:none}

/* --- with the script: a deck -------------------------------------------- */
html.js-deck,html.js-deck body{height:100%;overflow:hidden}
.js-deck .deck{position:relative;height:100vh;width:100vw}
.js-deck .bar,.js-deck .count,.js-deck .hint,.js-deck .nav{display:block}
.js-deck .nav{display:flex}
/* Centring with justify-content clips anything taller than the viewport AND
   makes the clipped part unreachable, because flexbox does not let you scroll
   past the start of a centred item. Auto margins on the child centre it while
   it fits and let it scroll normally once it does not. */
.js-deck .slide{position:absolute;inset:0;display:flex;flex-direction:column;
       justify-content:flex-start;border-bottom:0;
       opacity:0;visibility:hidden;transform:translateY(14px) scale(.995);
       transition:opacity .42s var(--ease),transform .42s var(--ease),visibility 0s .42s;
       overflow-y:auto;overscroll-behavior:contain}
.js-deck .slide-inner{margin:auto}
.js-deck .slide.active{opacity:1;visibility:visible;transform:none;
       transition-delay:0s,0s,0s}
.js-deck .slide.leaving{opacity:0;transform:translateY(-10px) scale(.995)}

/* --- title slide -------------------------------------------------------- */
.js-deck .slide.title{min-height:100vh}
.slide.title{background:
   radial-gradient(1100px 620px at 12% -10%,rgba(123,63,228,.30),transparent 60%),
   radial-gradient(900px 560px at 88% 112%,rgba(43,92,230,.34),transparent 60%),
   var(--ink);
   color:#fff}
.slide.title .eyebrow{font-size:.78rem;letter-spacing:.22em;text-transform:uppercase;
   color:#8fb0ff;font-weight:700;margin-bottom:1.1rem}
.slide.title h1{font-size:clamp(2.6rem,6.6vw,5rem);line-height:1.02;margin:0 0 .7rem;
   letter-spacing:-.035em;color:#fff;font-weight:700}
.slide.title .domain{font-size:clamp(1rem,2vw,1.5rem);color:#8fb0ff;margin:0 0 2rem}
.slide.title .sub{font-size:1.05rem;color:#aab4c4;max-width:60ch;margin:0}
.slide.title .note{margin-top:3rem;font-size:.8rem;color:#78839a;max-width:62ch;
   border-top:1px solid rgba(255,255,255,.12);padding-top:1rem}

/* --- headings and copy --------------------------------------------------- */
.slide h2{font-size:clamp(1.7rem,3.4vw,2.6rem);line-height:1.12;margin:0 0 .5rem;
   color:var(--ink);letter-spacing:-.025em;font-weight:700}
.slide h3{font-size:1.15rem;margin:1.6rem 0 .5rem;color:var(--ink);font-weight:650}
.slide p{margin:0 0 .85rem;font-size:1.02rem;max-width:74ch}
.slide a{color:var(--accent);text-decoration:none}
strong{color:var(--ink);font-weight:650}

/* the one-line explainer under a heading */
.why{display:flex;gap:.7rem;align-items:flex-start;background:var(--wash);
     border-left:3px solid var(--accent);border-radius:0 10px 10px 0;
     padding:.85rem 1.1rem;margin:0 0 1.6rem;max-width:80ch}
.why b{color:var(--accent);font-size:.72rem;letter-spacing:.14em;
       text-transform:uppercase;white-space:nowrap;padding-top:.18rem}
.why span{font-size:1.02rem;color:var(--ink)}

/* --- stat tiles ---------------------------------------------------------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
       gap:1rem;margin:.4rem 0 1.5rem}
.stat{background:var(--wash);border-radius:14px;padding:1.3rem 1.2rem;
      border:1px solid var(--rule)}
.stat .v{font-size:clamp(1.8rem,3.6vw,2.7rem);font-weight:700;color:var(--accent);
         letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums}
.stat .l{font-size:.86rem;color:var(--muted);margin-top:.45rem;line-height:1.35}

/* --- tables, lists, code -------------------------------------------------- */
table{border-collapse:collapse;width:100%;font-size:.94rem;margin:0 0 1.2rem}
th{background:var(--ink);color:#fff;text-align:left;padding:.7rem .85rem;
   font-size:.74rem;letter-spacing:.06em;text-transform:uppercase;font-weight:600}
th:first-child{border-top-left-radius:9px}
th:last-child{border-top-right-radius:9px}
td{border-bottom:1px solid var(--rule);padding:.7rem .85rem;vertical-align:top}
tbody tr:nth-child(even) td{background:#fafbfd}
ul,ol{margin:0 0 1rem;padding-left:1.3rem;max-width:76ch}
li{margin-bottom:.5rem}
li::marker{color:var(--accent)}
code{font:.88em/1.4 "SF Mono",Menlo,Consolas,monospace;background:#eef1f6;
     color:#30363f;padding:.12em .36em;border-radius:5px}
pre.code{background:var(--ink);border-radius:12px;padding:1.1rem 1.3rem;overflow-x:auto;
         margin:0 0 1.2rem}
pre.code code{background:none;color:#e8ecf3;padding:0;font-size:.85rem}
blockquote{margin:0 0 1rem;padding:.2rem 0 .2rem 1rem;border-left:3px solid var(--rule);
           color:var(--muted)}
figure{margin:0 0 1.2rem}
figure img{max-width:100%;height:auto;border-radius:12px;border:1px solid var(--rule)}
figcaption{font-size:.84rem;color:var(--muted);margin-top:.5rem}
.callout{background:var(--wash);border-left:3px solid var(--accent);border-radius:0 10px 10px 0;
         padding:1rem 1.2rem;margin:0 0 1.2rem}
.callout-label{font-size:.72rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
               color:var(--accent);margin-bottom:.4rem}
.callout.warning{border-left-color:#c77700;background:#fdf5e8}
.callout.warning .callout-label{color:#a65f00}
.chip{display:inline-block;font-size:.66rem;font-weight:700;letter-spacing:.06em;
      text-transform:uppercase;padding:.16em .5em;border-radius:7px;vertical-align:.12em}
.chip.obs{color:var(--obs);background:var(--obs-bg)}
.chip.inf{color:var(--inf);background:var(--inf-bg)}
.chip.spec{color:var(--spec);background:var(--spec-bg)}
.chip-note{font-size:.72rem;color:var(--muted);margin-left:.3rem;font-style:italic}

/* --- the staggered entrance ---------------------------------------------- */
/* Children rise in sequence so the eye lands on one thing at a time. Each
   element carries its index as --i, which is steadier than animating from
   JavaScript.
   Two deliberate choices here. The hidden state is gated behind .js-deck, which
   the script adds on load, so a reader with JavaScript off sees every slide in
   full rather than a blank page. And it is a transition rather than a keyframe
   animation, because the visible state is then the resting state of an active
   slide: if the transition never runs, the content is simply already there. An
   animation that fails leaves opacity at 0 and the slide empty. */
.js-deck .slide .anim{opacity:0;transform:translateY(16px);
  transition:opacity .5s var(--ease),transform .5s var(--ease);
  transition-delay:calc(var(--i,0)*70ms + 90ms)}
.js-deck .slide.active .anim{opacity:1;transform:none}
@media (prefers-reduced-motion:reduce){
  .slide{transition:none!important}
  .js-deck .slide .anim{transition:none!important;opacity:1;transform:none}
}

/* --- chrome -------------------------------------------------------------- */
.bar{position:fixed;top:0;left:0;height:3px;width:100%;z-index:40;
     background:linear-gradient(90deg,var(--accent),var(--accent-2));
     transform-origin:0 50%;transform:scaleX(0);transition:transform .45s var(--ease)}
.count{position:fixed;right:1.6rem;bottom:1.2rem;z-index:40;font-size:.8rem;
       color:var(--muted);font-variant-numeric:tabular-nums;
       background:rgba(255,255,255,.85);padding:.3rem .6rem;border-radius:20px;
       border:1px solid var(--rule)}
.slide.title ~ .count{color:#8fb0ff}
.hint{position:fixed;left:50%;bottom:1.2rem;transform:translateX(-50%);z-index:40;
      font-size:.78rem;color:#8fb0ff;opacity:1;transition:opacity .4s}
.hint.gone{opacity:0;pointer-events:none}
.js-deck .slide.more.active ~ .hint{opacity:0}
.hint kbd{background:rgba(255,255,255,.14);border-radius:5px;padding:.1em .45em;
          font-family:inherit;font-size:.9em}
.slide.more::after{content:"more below \\2193";position:absolute;left:50%;bottom:3.2rem;
  transform:translateX(-50%);font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);background:rgba(255,255,255,.92);border:1px solid var(--rule);
  border-radius:20px;padding:.25rem .7rem;pointer-events:none}
.nav{position:fixed;inset:0;z-index:20;display:flex;pointer-events:none}
.nav div{flex:1;pointer-events:auto;cursor:w-resize}
.nav div:last-child{flex:2;cursor:e-resize}

/* --- overview grid ------------------------------------------------------- */
.grid{position:fixed;inset:0;z-index:60;background:rgba(14,17,22,.97);
      display:none;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
      gap:1rem;padding:2.5rem;overflow-y:auto;align-content:start}
.js-deck .grid.open{display:grid}
.grid button{background:var(--paper);border:2px solid transparent;border-radius:12px;
             padding:1rem;text-align:left;cursor:pointer;font:inherit;color:var(--ink);
             transition:transform .18s var(--ease),border-color .18s}
.grid button:hover{transform:translateY(-3px);border-color:var(--accent)}
.grid button .n{font-size:.7rem;color:var(--accent);font-weight:700}
.grid button .t{font-size:.95rem;font-weight:650;margin-top:.3rem;line-height:1.3}

/* --- print: one slide per page ------------------------------------------- */
@media print{
  @page{size:A4 landscape;margin:12mm}
  html,body{height:auto;overflow:visible;background:#fff}
  .bar,.count,.hint,.nav,.grid{display:none!important}
  .deck{height:auto}
  .slide{position:static;opacity:1!important;visibility:visible!important;
         transform:none!important;page-break-after:always;min-height:auto;
         padding:0 0 10mm;overflow:visible}
  .slide.active .anim{animation:none!important}
  .slide.title{background:var(--ink)!important;-webkit-print-color-adjust:exact;
               print-color-adjust:exact;padding:20mm}
}
"""

DECK_JS = r"""
(function(){
  // Mark the document as script-driven. Everything that hides content is gated
  // on this, so the deck degrades to a plain readable page without JavaScript.
  document.documentElement.classList.add('js-deck');
  var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var bar = document.querySelector('.bar');
  var count = document.querySelector('.count');
  var hint = document.querySelector('.hint');
  var grid = document.querySelector('.grid');
  var i = 0;

  // Index every child of a slide so the CSS can stagger it. Done once up front
  // rather than per transition, so moving between slides stays cheap.
  slides.forEach(function(s){
    var kids = s.querySelectorAll('.slide-inner > *');
    Array.prototype.forEach.call(kids, function(k, n){
      k.classList.add('anim'); k.style.setProperty('--i', n);
    });
  });

  // Count a number up on arrival.
  //
  // The guarantee matters more than the animation: a report that leaves a wrong
  // number on screen is worse than one with no movement at all. Observed in
  // testing that rAF timestamps do not always advance as expected, which left
  // tiles frozen partway and showing 51 where the real figure was 210. So the
  // final value is written by a timer that runs regardless, the animation only
  // ever moves toward it, and anyone who prefers reduced motion skips it.
  var reduce = window.matchMedia &&
               window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function countUp(el){
    var raw = el.getAttribute('data-v') || el.textContent || '';
    var m = raw.match(/^([^\d\-]*)(-?[\d.,]+)(.*)$/);
    if (!m || reduce) { el.textContent = raw; return; }
    var pre = m[1], post = m[3];
    var clean = m[2].replace(/,/g, '');
    var target = parseFloat(clean);
    if (isNaN(target)) { el.textContent = raw; return; }

    var dec = (clean.split('.')[1] || '').length;
    var dur = 760;
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    var done = false;

    function finish(){
      if (done) return;
      done = true;
      el.textContent = raw;                 // always the real value, verbatim
    }
    // The animation is decoration. This is the thing that guarantees the number.
    setTimeout(finish, dur + 250);

    function step(){
      if (done) return;
      var now = (window.performance && performance.now) ? performance.now() : Date.now();
      var p = Math.min((now - t0) / dur, 1);
      if (p >= 1) { finish(); return; }
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = pre + (target * eased).toFixed(dec) + post;
      requestAnimationFrame(step);
    }
    el.textContent = pre + (0).toFixed(dec) + post;
    requestAnimationFrame(step);
  }

  function show(n, dir){
    n = Math.max(0, Math.min(slides.length - 1, n));
    if (n === i && slides[i].classList.contains('active')) return;
    var prev = slides[i];
    if (prev) { prev.classList.add('leaving'); prev.classList.remove('active');
                setTimeout(function(){ prev.classList.remove('leaving'); }, 420); }
    i = n;
    var s = slides[i];
    s.classList.add('active');
    s.scrollTop = 0;
    bar.style.transform = 'scaleX(' + ((i + 1) / slides.length) + ')';
    count.textContent = (i + 1) + ' / ' + slides.length;
    Array.prototype.forEach.call(s.querySelectorAll('.stat .v'), countUp);
    // Tell the reader when a slide continues past the fold.
    setTimeout(function(){
      s.classList.toggle('more', s.scrollHeight - s.clientHeight > 8);
    }, 60);
    if (history.replaceState) history.replaceState(null, '', '#' + (i + 1));
    if (i > 0 && hint) hint.classList.add('gone');
  }

  // Returns true if the current slide still had room to scroll in that
  // direction, meaning the key should scroll rather than change slide.
  function scrollBy(dir){
    var s = slides[i];
    if (!s) return false;
    var room = s.scrollHeight - s.clientHeight;
    if (room < 8) return false;
    var atEnd = dir > 0 ? (s.scrollTop >= room - 4) : (s.scrollTop <= 4);
    if (atEnd) return false;
    s.scrollBy({ top: dir * Math.round(s.clientHeight * 0.82), behavior: 'smooth' });
    return true;
  }

  function next(){ show(i + 1); }
  function prev(){ show(i - 1); }

  document.addEventListener('keydown', function(e){
    if (grid.classList.contains('open')) {
      if (e.key === 'Escape' || e.key === 'o' || e.key === 'O') toggleGrid();
      return;
    }
    switch (e.key) {
      case 'ArrowDown': case ' ': case 'PageDown':
        // On a slide taller than the window, down scrolls it first and only
        // advances once the bottom is reached. Otherwise the end of a long
        // slide is unreachable with the keyboard.
        if (scrollBy(1)) { e.preventDefault(); break; }
        e.preventDefault(); next(); break;
      case 'ArrowUp': case 'PageUp':
        if (scrollBy(-1)) { e.preventDefault(); break; }
        e.preventDefault(); prev(); break;
      case 'ArrowRight': e.preventDefault(); next(); break;
      case 'ArrowLeft': e.preventDefault(); prev(); break;
      case 'Home': show(0); break;
      case 'End': show(slides.length - 1); break;
      case 'o': case 'O': case 'Escape': toggleGrid(); break;
    }
  });

  var zones = document.querySelectorAll('.nav div');
  if (zones.length === 2) {
    zones[0].addEventListener('click', prev);
    zones[1].addEventListener('click', next);
  }

  var x0 = null;
  document.addEventListener('touchstart', function(e){ x0 = e.touches[0].clientX; }, {passive:true});
  document.addEventListener('touchend', function(e){
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 50) { dx < 0 ? next() : prev(); }
    x0 = null;
  }, {passive:true});

  function toggleGrid(){
    grid.classList.toggle('open');
    if (grid.classList.contains('open') && !grid.dataset.built) {
      slides.forEach(function(s, n){
        var h = s.querySelector('h1, h2');
        var b = document.createElement('button');
        b.innerHTML = '<div class="n">' + (n + 1) + '</div><div class="t">' +
                      (h ? h.textContent : 'Slide ' + (n + 1)) + '</div>';
        b.addEventListener('click', function(){ toggleGrid(); show(n); });
        grid.appendChild(b);
      });
      grid.dataset.built = '1';
    }
  }

  var start = parseInt((location.hash || '').replace('#', ''), 10);
  show(isNaN(start) ? 0 : start - 1);
})();
"""

DECK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head><body>
<div class="bar"></div>
<div class="deck">{slides}</div>
<div class="nav"><div></div><div></div></div>
<div class="count"></div>
<div class="hint">Use <kbd>&larr;</kbd> <kbd>&rarr;</kbd> to move, <kbd>O</kbd> for all slides</div>
<div class="grid"></div>
<script>{js}</script>
</body></html>
"""


def build_stats(block):
    """Render a `::: stats` block as tiles.

    Each line is "value | label". The value is carried in data-v so the counter
    can animate it without having to re-parse the rendered text.
    """
    tiles = []
    for line in block.strip().splitlines():
        if "|" not in line:
            continue
        value, _, label = line.partition("|")
        tiles.append(
            '<div class="stat"><div class="v" data-v="{v}">{v}</div>'
            '<div class="l">{l}</div></div>'.format(
                v=html_mod.escape(value.strip()), l=html_mod.escape(label.strip())))
    return '<div class="stats">' + "".join(tiles) + "</div>" if tiles else ""


def split_slides(md):
    """Cut the report into slides on H2 headings and horizontal rules.

    Anything before the first H2 belongs to the title slide, which is built
    separately, so it is dropped here.
    """
    parts, current = [], []
    for line in md.replace("\r\n", "\n").split("\n"):
        if re.match(r"^##\s+\S", line) or re.match(r"^---\s*$", line):
            if any(l.strip() for l in current):
                parts.append("\n".join(current))
            current = [] if line.startswith("---") else [line]
        else:
            current.append(line)
    if any(l.strip() for l in current):
        parts.append("\n".join(current))
    return parts


def render_slide(md, base_dir):
    """One slide: heading, optional explainer band, optional stat tiles, body."""
    lines = md.split("\n")
    head = ""
    if lines and lines[0].startswith("##"):
        head = f"<h2>{inline(lines[0].lstrip('# ').strip(), base_dir)}</h2>"
        lines = lines[1:]

    # A blockquote straight after the heading is the "why this matters" line.
    why = ""
    j = 0
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and lines[j].strip().startswith(">"):
        quote = []
        while j < len(lines) and lines[j].strip().startswith(">"):
            quote.append(re.sub(r"^\s*>\s?", "", lines[j]))
            j += 1
        text = " ".join(q.strip() for q in quote).strip()
        if text and not text.startswith("[!"):
            why = ('<div class="why"><b>Why this matters</b><span>'
                   + inline(text, base_dir) + "</span></div>")
            lines = lines[j:]

    body_md = "\n".join(lines)
    stats_html = []

    def take_stats(m):
        stats_html.append(build_stats(m.group(1)))
        return f"\x00STATS{len(stats_html) - 1}\x00"

    body_md = re.sub(r"^:::\s*stats\s*\n(.*?)^:::\s*$", take_stats, body_md,
                     flags=re.S | re.M)
    body = render_markdown(body_md, base_dir)
    body = re.sub(r"<p>\x00STATS(\d+)\x00</p>|\x00STATS(\d+)\x00",
                  lambda m: stats_html[int(m.group(1) or m.group(2))], body)
    return f'<section class="slide"><div class="slide-inner">{head}{why}{body}</div></section>'


def build_html(md, base_dir, title=None, subtitle=None, eyebrow="Growth recon"):
    lines = md.replace("\r\n", "\n").split("\n")

    doc_title, sub, start = None, None, 0
    for idx, line in enumerate(lines):
        if line.strip().startswith("# "):
            doc_title = line.strip()[2:].strip()
            start = idx + 1
            break
    for idx in range(start, min(start + 3, len(lines))):
        t = lines[idx].strip()
        if t and not t.startswith(("#", "-", "*", ">", "|", "`", ":")):
            sub = t
            lines[idx] = ""
            break
    if doc_title:
        del lines[start - 1]

    # "Growth recon: Acme (https://acme.com)" -> eyebrow / title / domain
    domain_html = ""
    if doc_title and not title:
        m = re.match(r"^\s*([^:]{3,40}):\s*(.+)$", doc_title)
        if m and re.search(r"recon|teardown|analysis|report", m.group(1), re.I):
            eyebrow, doc_title = m.group(1).strip(), m.group(2).strip()
        m = re.match(r"^(.*?)\s*\((https?://[^)]+|[\w.-]+\.[a-z]{2,}[^)]*)\)\s*$",
                     doc_title, re.I)
        if m and m.group(1).strip():
            doc_title = m.group(1).strip()
            dom = re.sub(r"^https?://", "", m.group(2).strip()).rstrip("/")
            domain_html = f'<p class="domain">{html_mod.escape(dom)}</p>'

    title = title or doc_title or "Growth recon"
    subtitle = subtitle or sub or ""

    cover = (
        '<section class="slide title"><div class="slide-inner">'
        f'<div class="eyebrow">{html_mod.escape(eyebrow)}</div>'
        f"<h1>{html_mod.escape(title)}</h1>{domain_html}"
        f'<p class="sub">{inline(subtitle, base_dir) if subtitle else ""}</p>'
        f'<div class="note">{html_mod.escape(COVER_NOTE)}</div>'
        "</div></section>")

    slides = [cover] + [render_slide(part, base_dir)
                        for part in split_slides("\n".join(lines))]
    return DECK_PAGE.format(title=html_mod.escape(title), css=DECK_CSS,
                            js=DECK_JS, slides="".join(slides))


def to_pdf(html_path, pdf_path, browser):
    """Print the deck, one slide per page, via the browser's own engine."""
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", "--virtual-time-budget=6000",
           "--run-all-compositor-stages-before-draw",
           f"--print-to-pdf={pdf_path}", "file://" + os.path.abspath(html_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
        return True, ""
    cmd[1] = "--headless"
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
        return True, ""
    return False, (proc.stderr or proc.stdout or "no output").strip()[:400]


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

    out = opt("-o") or opt("--out") or os.path.splitext(src)[0] + ".html"
    with open(src, encoding="utf-8", errors="replace") as fh:
        md = fh.read()

    base_dir = os.path.dirname(os.path.abspath(src))
    page = build_html(md, base_dir, title=opt("--title"), subtitle=opt("--subtitle"),
                      eyebrow=opt("--eyebrow") or "Growth recon")

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)

    slides = page.count('<section class="slide')
    imgs = page.count("<figure>")
    print(f"deck  {out}")
    print(f"      {slides} slides, {len(page):,} bytes, "
          f"{imgs} image{'s' if imgs != 1 else ''} inlined, nothing external")
    print(f"      open it in any browser. arrows to move, O for all slides.")
    print(f"      the detailed write-up stays in {os.path.basename(src)}")

    notes = style_check(md)
    if notes and "--no-style-check" not in sys.argv:
        print("\nplain language: this report asks more of a reader than it needs to")
        for note in notes:
            print(f"  - {note}")

    if "--pdf" not in sys.argv:
        return 0

    browser = find_browser()
    if not browser:
        print("\nNo browser found for --pdf, so the deck was not printed.")
        print("The HTML above is complete; open it and print from there.")
        return 0

    pdf_path = os.path.splitext(out)[0] + ".pdf"
    ok, err = to_pdf(out, os.path.abspath(pdf_path), browser)
    if ok:
        print(f"pdf   {pdf_path}  ({os.path.getsize(pdf_path):,} bytes)")
        return 0
    print(f"\nThe browser could not print the deck: {err}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
