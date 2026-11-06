#!/usr/bin/env python3
"""Turn a finished markdown report into a designed, self-contained PDF.

A markdown file is a working artefact. The people a recon report is actually
written for -- a founder, a client, someone in a meeting -- want a document,
and the plain-English section at the top of the template only does its job if
it survives into something they will open.

Usage:
    python3 -I scripts/report_pdf.py report.md
    python3 -I scripts/report_pdf.py report.md -o acme-recon.pdf
    python3 -I scripts/report_pdf.py report.md --html-only     # skip the PDF step
    python3 -I scripts/report_pdf.py report.md --subtitle "Prepared for ..."

How it works: the markdown is rendered to one HTML file with the CSS inlined and
every local image embedded as a data URI, then a headless browser prints that to
PDF. Nothing is installed and no Python packages are needed -- the browser is
already on almost every machine, and its print engine handles page breaks,
widows and web fonts better than anything that could reasonably be vendored
here.

If no browser is found the HTML is still written and the path reported, because
an HTML file someone can open and print is a usable deliverable, whereas a
traceback is not.
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
CSS = """
:root {
  --ink: #15171c; --muted: #5b6472; --rule: #e3e7ed;
  --accent: #2f5bd0; --accent-soft: #eef3ff;
  --obs: #1c7a4a; --obs-bg: #e7f5ed;
  --inf: #8a5a00; --inf-bg: #fdf1dc;
  --spec: #5b6472; --spec-bg: #eef0f4;
}
@page { size: A4; margin: 17mm 15mm 19mm 15mm; }
@page { @bottom-right { content: counter(page); } }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font: 10.5pt/1.55 "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: var(--ink); margin: 0;
}

/* Cover: one full page, content set a third of the way down, footnote pinned. */
.cover { height: 247mm; display: flex; flex-direction: column; page-break-after: always; }
.cover .top { flex: 0 0 auto; padding-top: 52mm; }
.cover .eyebrow { font-size: 8.5pt; letter-spacing: .18em; text-transform: uppercase;
                  color: var(--accent); font-weight: 700; margin-bottom: 5mm; }
.cover .rule { height: 3px; width: 42mm; background: var(--accent); margin-bottom: 8mm; }
.cover h1 { font-size: 34pt; line-height: 1.08; margin: 0 0 3mm; letter-spacing: -.022em;
            max-width: 150mm; }
.cover h1.long { font-size: 25pt; }
.cover .domain { font-size: 13pt; color: var(--accent); margin: 0 0 11mm; font-weight: 500; }
.cover .sub { font-size: 10.5pt; color: var(--muted); margin: 0; max-width: 128mm;
              line-height: 1.5; }
.cover .meta { margin-top: 7mm; font-size: 9.5pt; color: var(--muted); }
.cover .meta b { color: var(--ink); font-weight: 600; }
.cover .note { margin-top: auto; font-size: 8pt; line-height: 1.45; color: var(--muted);
               border-top: 1px solid var(--rule); padding-top: 4mm; max-width: 150mm; }
.chip-note { font-size: 7.6pt; color: var(--muted); margin-left: 1.2mm; font-style: italic; }

/* Headings */
h1, h2, h3, h4 { page-break-after: avoid; break-after: avoid; letter-spacing: -.01em; }
h1 { font-size: 19pt; margin: 0 0 5mm; }
h2 { font-size: 14pt; margin: 9mm 0 3.5mm; padding-bottom: 2mm;
     border-bottom: 2px solid var(--rule); }
h2:first-of-type { margin-top: 0; }
h3 { font-size: 11.5pt; margin: 6mm 0 2mm; color: #262b33; }
h4 { font-size: 10.5pt; margin: 4mm 0 1.5mm; color: var(--muted); }
p { margin: 0 0 3mm; orphans: 3; widows: 3; }
a { color: var(--accent); text-decoration: none; word-break: break-word; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 6mm 0; }
strong { font-weight: 650; }

/* Tables */
.table-wrap { page-break-inside: auto; margin: 0 0 4mm; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; break-inside: avoid; }
th { background: #f5f7fa; font-weight: 650; text-align: left;
     border-bottom: 2px solid var(--rule); padding: 2.2mm 2.6mm; }
td { border-bottom: 1px solid var(--rule); padding: 2.2mm 2.6mm; vertical-align: top; }
tbody tr:nth-child(even) td { background: #fafbfc; }

/* Lists */
ul, ol { margin: 0 0 3mm; padding-left: 5.5mm; }
li { margin-bottom: 1.4mm; }
li > ul, li > ol { margin-top: 1.4mm; }

/* Code */
code { font: 9pt/1.4 "SF Mono", Menlo, Consolas, monospace;
       background: #f3f4f7; padding: .6mm 1.2mm; border-radius: 2px; }
pre.code { background: #f7f8fa; border: 1px solid var(--rule); border-left: 3px solid var(--accent);
           border-radius: 3px; padding: 3mm 3.5mm; overflow-x: auto;
           page-break-inside: avoid; margin: 0 0 4mm; }
pre.code code { background: none; padding: 0; font-size: 8.6pt; white-space: pre-wrap;
                word-break: break-word; }

/* Confidence chips -- the report's own convention, made legible at a glance */
.chip { display: inline-block; font-size: 7.6pt; font-weight: 700; letter-spacing: .05em;
        text-transform: uppercase; padding: .5mm 1.8mm; border-radius: 2.6mm;
        vertical-align: .4mm; white-space: nowrap; }
.chip.obs  { color: var(--obs);  background: var(--obs-bg); }
.chip.inf  { color: var(--inf);  background: var(--inf-bg); }
.chip.spec { color: var(--spec); background: var(--spec-bg); }

/* Callouts and quotes */
blockquote { margin: 0 0 4mm; padding: 2mm 0 2mm 4mm; border-left: 3px solid var(--rule);
             color: var(--muted); }
blockquote p:last-child { margin-bottom: 0; }
.callout { border: 1px solid var(--rule); border-left: 3px solid var(--accent);
           background: var(--accent-soft); border-radius: 3px; padding: 3mm 3.5mm;
           margin: 0 0 4mm; page-break-inside: avoid; }
.callout-label { font-size: 7.6pt; font-weight: 700; letter-spacing: .1em;
                 text-transform: uppercase; color: var(--accent); margin-bottom: 1.5mm; }
.callout p:last-child { margin-bottom: 0; }
.callout.warning { border-left-color: #c77700; background: #fdf4e6; }
.callout.warning .callout-label { color: #a65f00; }
.callout.important { border-left-color: #7a3fb8; background: #f5eefc; }
.callout.important .callout-label { color: #6a33a3; }

/* Figures */
figure { margin: 0 0 4mm; page-break-inside: avoid; }
figure img { max-width: 100%; height: auto; border: 1px solid var(--rule); border-radius: 3px; }
figcaption { font-size: 8.5pt; color: var(--muted); margin-top: 1.5mm; }
.missing-img { font-size: 8.5pt; color: #b00020; }

/* The plain-English opener is the part most readers will actually read. */
h2.plain + p, h2.plain + p + p { font-size: 11.5pt; line-height: 1.6; }
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head><body>
<section class="cover">
  <div class="top">
    <div class="eyebrow">{eyebrow}</div>
    <div class="rule"></div>
    <h1 class="{title_class}">{title}</h1>
    {domain}
    <p class="sub">{subtitle}</p>
    <div class="meta">{meta}</div>
  </div>
  <div class="note">{note}</div>
</section>
{body}
</body></html>
"""

COVER_NOTE = ("Compiled from public, unauthenticated sources: the pages and files this site "
              "serves to any visitor. Claims are marked observed, inferred or speculative. "
              "Configuration describes what would happen; it is not proof that it did.")


def build_html(md, base_dir, title=None, subtitle=None, eyebrow="Growth recon"):
    """Lift the title and subtitle off the report so the cover matches the body."""
    lines = md.replace("\r\n", "\n").split("\n")

    doc_title, sub, start = None, None, 0
    for idx, line in enumerate(lines):
        if line.strip().startswith("# "):
            doc_title = line.strip()[2:].strip()
            start = idx + 1
            break
    # A short unmarked line straight after the H1 is the template's metadata
    # line ("Collected <date> · N containers · ..."), which belongs on the cover.
    for idx in range(start, min(start + 3, len(lines))):
        s = lines[idx].strip()
        if s and not s.startswith(("#", "-", "*", ">", "|", "`")):
            sub = s
            lines[idx] = ""
            break

    if doc_title:
        del lines[start - 1]

    # Report titles follow the template's "Growth recon: Acme (https://acme.com)".
    # A URL set in 34pt is just a long ugly string, so the prefix becomes the
    # eyebrow and the URL becomes its own accent-coloured line.
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

    title = title or doc_title or "Growth recon report"
    subtitle = subtitle or sub or ""

    body_md = "\n".join(lines).strip()
    body = render_markdown(body_md, base_dir)
    # Mark the plain-English opener so it can be set slightly larger.
    body = re.sub(r"<h2>(\s*In plain English\s*)</h2>",
                  r'<h2 class="plain">\1</h2>', body, flags=re.I)

    meta = f"Prepared <b>{html_mod.escape(date.today().isoformat())}</b>"
    return PAGE.format(
        title=html_mod.escape(title),
        title_class="long" if len(title) > 26 else "",
        domain=domain_html,
        css=CSS,
        eyebrow=html_mod.escape(eyebrow),
        subtitle=inline(subtitle, base_dir) if subtitle else "",
        meta=meta,
        note=html_mod.escape(COVER_NOTE),
        body=body,
    )


def to_pdf(html_path, pdf_path, browser):
    """Print the HTML with a headless browser.

    The virtual time budget gives web fonts and layout a moment to settle;
    without it a complex table occasionally prints mid-reflow.
    """
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", "--virtual-time-budget=6000",
           "--run-all-compositor-stages-before-draw",
           f"--print-to-pdf={pdf_path}", "file://" + os.path.abspath(html_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 1000:
        return True, ""
    # Older builds reject --headless=new; retry the legacy flag before giving up.
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

    out = opt("-o") or opt("--out") or os.path.splitext(src)[0] + ".pdf"
    with open(src, encoding="utf-8", errors="replace") as fh:
        md = fh.read()

    base_dir = os.path.dirname(os.path.abspath(src))
    page = build_html(md, base_dir, title=opt("--title"), subtitle=opt("--subtitle"),
                      eyebrow=opt("--eyebrow") or "Growth recon")

    html_path = os.path.splitext(out)[0] + ".html"
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    imgs = page.count("<figure>")
    print(f"html  {html_path}  ({len(page):,} bytes, {imgs} image{'s' if imgs != 1 else ''} embedded)")

    if "--html-only" in sys.argv:
        return 0

    browser = find_browser()
    if not browser:
        print("\nNo headless browser found, so the PDF step was skipped.")
        print("The HTML above is complete and self-contained -- open it and print to PDF,")
        print("or set CHROME_BIN to a Chrome, Chromium, Edge or Brave binary and re-run.")
        return 0

    ok, err = to_pdf(html_path, os.path.abspath(out), browser)
    if ok:
        print(f"pdf   {out}  ({os.path.getsize(out):,} bytes)")
        print(f"      rendered by {os.path.basename(browser)}")
        return 0

    print(f"\nThe browser could not print the PDF: {err}", file=sys.stderr)
    print(f"The HTML is still at {html_path} -- open it and print to PDF.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
