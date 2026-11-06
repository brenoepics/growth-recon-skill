#!/usr/bin/env python3
"""Read a GTM container's archived history and diff two points in it.

One container read describes today. The Wayback Machine archives `gtm.js` keyed
by container ID, so the same file is readable at dozens of earlier dates -- and
what changed between two dates is a stronger signal than either date on its own,
because a dated change is evidence of a decision. "They added 27 GA4 events and
dropped Universal Analytics sometime between 2019 and 2021" is a strategy shift
with a timestamp on it; "they have 27 GA4 events" is just an inventory.

Usage:
    python3 history.py GTM-XXXXXXX                    # capture timeline, summarised by year
    python3 history.py GTM-XXXXXXX --diff             # earliest readable vs latest, both parsed
    python3 history.py GTM-XXXXXXX --diff --from 2021 --to 2024
    python3 history.py GTM-XXXXXXX --diff --live      # oldest archived vs the container served today
    python3 history.py GTM-XXXXXXX --json             # machine-readable

Honest limits, which belong in any report built on this:
  - The archive is sparse and uneven. A gap in captures is a gap in the ARCHIVE,
    not a gap in their tracking. A change found "between 2019 and 2023" happened
    on some unknown day in that window, and the window is often years wide.
  - Containers archived before roughly mid-2018 are in GTM's older compiled
    format, which ships no readable configuration object. They count in the
    timeline and are skipped for diffing -- the file exists, the facts don't.
  - CDX `length` is the size of the compressed archive record, not the file's
    byte size. Use it for the growth trend only; the real byte count is reported
    for the snapshots this script actually fetches.
  - Everything gtm_parse.py cannot see today (tag names, paused tags, whether
    anything fired) it could not see in 2019 either. A diff of two configs is
    still a diff of two configs.
"""

import json
import os
import re
import sys
import gzip
import time
import zlib
import ssl
import shutil
import hashlib
import tempfile
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gtm_parse import (extract_data_object, summarise, fetch as fetch_text,  # noqa: E402
                       _ssl_context, TAG_TYPES, UA)

CDX = "http://web.archive.org/cdx/search/cdx"
TIMEOUT = 90

# Wayback answers a burst of requests with 429 and no body. Backing off and
# retrying is the difference between a working script and one that silently
# reports "no history" for a container that has ten years of it.
RETRY_DELAYS = (5, 15, 40, 90)

# A capture whose archived record is this small is a redirect stub, an error page
# or a revisit pointer -- never a container. The smallest real container records
# observed across live targets are ~22KB compressed; an empty-but-valid container
# still runs to a few KB. Screening on the CDX metadata avoids fetching junk.
MIN_RECORD_BYTES = 1200

# Same test applied after fetching, on the decoded text, because CDX length is
# compressed and occasionally missing.
MIN_SNAPSHOT_BYTES = 2048


class RateLimited(RuntimeError):
    """Wayback refused us for long enough that the answer has to be 'unknown'."""


# ---------------------------------------------------------------------------
# Cache. Snapshots are immutable once archived -- a timestamp always replays the
# same bytes -- so they are cached forever. The CDX listing is not immutable
# (new captures land), so it expires, but it still has to be cached: a --diff
# run otherwise re-queries the index that is doing the rate-limiting.
# ---------------------------------------------------------------------------
CDX_CACHE_SECONDS = 6 * 3600


def cache_dir():
    d = os.path.join(tempfile.gettempdir(), "growth-recon-wayback")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(name):
    return os.path.join(cache_dir(), name)


# ---------------------------------------------------------------------------
# HTTP with backoff
# ---------------------------------------------------------------------------
def _http(url, timeout=TIMEOUT, accept_encoding="gzip, deflate"):
    """Fetch raw bytes, retrying through 429/503 with increasing delay.

    Returns (body_bytes, content_encoding). Raises RateLimited when the backoff
    schedule runs out, so the caller can say so instead of reporting an empty
    history as an empty history.
    """
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": accept_encoding}
    last = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            ctx = _ssl_context() if url.startswith("https") else None
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read(), (resp.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 503, 504):
                raise
            # Wayback sometimes states how long to wait; believe it over our schedule.
            wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            hinted = e.headers.get("Retry-After") if e.headers else None
            if hinted and str(hinted).isdigit():
                wait = max(wait, min(int(hinted), 120))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if "CERTIFICATE" in str(e).upper() or isinstance(getattr(e, "reason", None), ssl.SSLError):
                raise RuntimeError(
                    f"TLS verification failed for {url}: {e}. This is a local CA-store "
                    f"problem, not the archive -- `pip install certifi`, or run the "
                    f"request through curl.") from None
            # Rate limiting also shows up as a dropped connection with no status.
            last = e
            wait = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        if attempt >= len(RETRY_DELAYS):
            break
        sys.stderr.write(f"[wayback backed us off; waiting {wait}s then retrying]\n")
        sys.stderr.flush()
        time.sleep(wait)
    raise RateLimited(
        f"web.archive.org kept refusing after {len(RETRY_DELAYS)} retries ({last}). "
        f"This is a rate limit, not an absence of history -- wait a few minutes and "
        f"re-run; anything already fetched is cached in {cache_dir()}")


def _decompress(raw, encoding):
    """Undo whatever the origin server's Content-Encoding was.

    `id_` replay hands back the original bytes *with* the original encoding, so
    recent snapshots arrive brotli-compressed. Brotli is not in the standard
    library, so this tries an optional module, then the `brotli` CLI, and reports
    failure so the caller can fall back to a replay Wayback decodes for us.
    """
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if "br" in encoding or not _looks_like_text(raw):
        for mod in ("brotli", "brotlicffi"):
            try:
                return __import__(mod).decompress(raw)
            except ImportError:
                continue
            except Exception:
                break
        exe = shutil.which("brotli")
        if exe:
            p = subprocess.run([exe, "-d", "-c"], input=raw, capture_output=True)
            if p.returncode == 0 and p.stdout:
                return p.stdout
        if not _looks_like_text(raw):
            raise ValueError("body is compressed with brotli and no brotli decoder is available")
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _looks_like_text(raw):
    head = raw[:256]
    return head.startswith((b"//", b"(function", b"var ", b"/*", b"\n", b"window.")) or (
        b"\x00" not in head and sum(c < 9 or (13 < c < 32) for c in head) < 8)


# Wayback's rewriting replay prefixes every URL inside the file with its own
# archive path, escaped slashes included. Stripping that prefix restores the
# original hosts, which matters because host names are how the parser identifies
# vendors -- left in place, every third party in the container reads as
# web.archive.org.
WOMBAT_PREFIX = re.compile(
    r"https?:(?:\\/|/){2}web\.archive\.org(?:\\/|/)web(?:\\/|/)\d{14}(?:[a-z]{2}_)?(?:\\/|/)")


# ---------------------------------------------------------------------------
# CDX index
# ---------------------------------------------------------------------------
def cdx_captures(container_id, timeout=TIMEOUT, use_cache=True):
    """Every archived capture of this container's gtm.js, oldest first.

    Queried with matchType=prefix on purpose. The GTM snippet appends `&l=` when
    the site renames its dataLayer, and Wayback keys URLs including the query
    string, so an exact-match query silently returns only part of the history:
    for one live container the exact query returned 107 captures ending in 2020
    while the prefix query returned 164 ending in 2022. Prefix matching can also
    catch a *longer* container ID that starts with this one, so every row's own
    `id` parameter is checked before it is kept.
    """
    cid = container_id.strip().upper()
    cache = _cache_path(f"cdx-{cid}.json")
    if use_cache and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CDX_CACHE_SECONDS:
        with open(cache, encoding="utf-8") as fh:
            return json.load(fh)

    q = urllib.parse.urlencode({
        "url": f"googletagmanager.com/gtm.js?id={cid}",
        "matchType": "prefix",
        "output": "json",
        "fl": "timestamp,original,length,statuscode,mimetype,digest",
    })
    raw, enc = _http(f"{CDX}?{q}", timeout)
    body = _decompress(raw, enc).decode("utf-8", errors="replace").strip()
    if not body:
        rows = []
    else:
        try:
            rows = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"CDX returned something that is not JSON: {body[:160]!r}")

    caps, seen = [], set()
    for r in rows[1:] if rows else []:
        ts, original, length, status, mime, digest = (list(r) + [""] * 6)[:6]
        if _id_param(original) != cid:
            continue
        if (ts, original) in seen:      # CDX duplicates the same capture sometimes
            continue
        seen.add((ts, original))
        caps.append({
            "timestamp": ts,
            "date": _date(ts),
            "url": original,
            "record_bytes": int(length) if str(length).isdigit() else None,
            "status": status,
            "mimetype": mime,
            "digest": digest,
        })
    caps.sort(key=lambda c: c["timestamp"])
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(caps, fh)
    return caps


def _id_param(url):
    m = re.search(r"[?&]id=([^&#]+)", url or "")
    return urllib.parse.unquote(m.group(1)).strip().upper() if m else ""


def _date(ts):
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"


def classify(cap):
    """Why a capture is or is not worth fetching.

    Wayback records a repeat crawl of unchanged content as a `warc/revisit`
    pointer with status `-` and a few hundred bytes. That is not an error and not
    an empty container -- it means "identical to an earlier capture", and the
    capture it points at is already in the list. Reporting either of these as a
    thin container would invent a period where the company stopped measuring.
    """
    if cap["mimetype"] == "warc/revisit" or cap["status"] == "-":
        return "duplicate of an earlier capture (revisit record)"
    if cap["status"] and not cap["status"].startswith("2"):
        return f"archived HTTP {cap['status']}"
    if cap["record_bytes"] is not None and cap["record_bytes"] < MIN_RECORD_BYTES:
        return f"{cap['record_bytes']} byte record -- an error or redirect page, not a container"
    return None


# ---------------------------------------------------------------------------
# Snapshot fetch
# ---------------------------------------------------------------------------
def fetch_snapshot(cap, timeout=TIMEOUT, use_cache=True):
    """Fetch one archived capture as the original file's text.

    `id_` is the important part of the URL: it replays the archived bytes
    unmodified. Without it Wayback injects its own rewriting shim, which both
    inflates the file and rewrites every vendor host inside it.
    """
    key = hashlib.sha1(f"{cap['timestamp']}|{cap['url']}".encode()).hexdigest()[:16]
    body_path = _cache_path(f"snap-{key}.js")
    meta_path = _cache_path(f"snap-{key}.json")
    if use_cache and os.path.exists(body_path) and os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(body_path, encoding="utf-8") as fh:
            return fh.read(), meta

    ts, url = cap["timestamp"], cap["url"]
    meta = {"replay": "id_", "rewritten": False}
    try:
        raw, enc = _http(f"http://web.archive.org/web/{ts}id_/{url}", timeout)
        text = _decompress(raw, enc).decode("utf-8", errors="replace")
    except ValueError:
        # No brotli decoder here, so take the replay Wayback decompresses for us
        # and undo its URL rewriting. Best-effort, and flagged as such.
        raw, enc = _http(f"http://web.archive.org/web/{ts}js_/{url}", timeout)
        text = _decompress(raw, enc).decode("utf-8", errors="replace")
        meta = {"replay": "js_", "rewritten": True}
    if meta["rewritten"] or "web.archive.org" in text[:4000]:
        text = WOMBAT_PREFIX.sub("", text)
        meta["rewritten"] = True
    meta["bytes"] = len(text)
    with open(body_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return text, meta


def fetch_live(container_id, timeout=TIMEOUT):
    """Today's published container, for diffing against an archived one.

    Worth having as an endpoint because the archive lags badly and unevenly: one
    live target's newest capture is four years old, so "latest archived" would
    have reported a 2022 container as current.
    """
    cid = container_id.strip().upper()
    src, _status = fetch_text(f"https://www.googletagmanager.com/gtm.js?id={cid}", timeout)
    return src


def parse_snapshot(cap, timeout=TIMEOUT, use_cache=True):
    """Parse one capture with the normal container parser.

    Returns (summary, meta) or raises. The failure that matters is the missing
    `var data` anchor: that is GTM's pre-2018 compiled format, which contains the
    container's behaviour as obfuscated JavaScript and no configuration object.
    """
    text, meta = fetch_snapshot(cap, timeout, use_cache)
    if len(text) < MIN_SNAPSHOT_BYTES:
        raise ValueError(f"only {len(text)} bytes of body -- an error page, not a container")
    summary = summarise(extract_data_object(text), text)
    summary["_bytes"] = len(text)
    summary["_source"] = f"web.archive.org {cap['date']}"
    return summary, meta


def is_legacy_format(exc):
    return "var data" in str(exc)


def _why(exc):
    """Say what an unparseable snapshot actually is. The parser's "no anchor"
    error is accurate but reads like a broken fetch; on an old capture it means
    the file is GTM's pre-2018 compiled format, which has no config object."""
    if is_legacy_format(exc):
        return "GTM's pre-2018 compiled format -- no readable configuration object in it"
    return str(exc)[:140]


# ---------------------------------------------------------------------------
# Endpoint selection
# ---------------------------------------------------------------------------
def earliest_parseable(caps, timeout=TIMEOUT, use_cache=True, log=None):
    """Find the oldest capture that carries a readable configuration object.

    Bisected rather than walked. GTM's switch from the old compiled format to the
    `var data = {...}` object was a change Google made once, so parseability only
    ever turns on as time moves forward -- which makes a bisect valid and turns
    1,000 candidate captures into about ten fetches instead of hundreds of
    hopeless ones against a rate-limited archive.
    """
    lo, hi = 0, len(caps) - 1
    best = None
    attempts = []
    while lo <= hi:
        mid = (lo + hi) // 2
        cap = caps[mid]
        try:
            summary, meta = parse_snapshot(cap, timeout, use_cache)
            best = (cap, summary, meta)
            hi = mid - 1
        except RateLimited:
            raise
        except Exception as e:
            attempts.append((cap["date"], _why(e)))
            if log:
                log(f"  {cap['date']}: {_why(e)}")
            if is_legacy_format(e):
                lo = mid + 1          # everything before this is older still
            else:
                # A one-off bad capture, not a format boundary. Try its neighbour
                # before concluding anything about the era it sits in.
                moved = False
                for nxt in range(mid + 1, min(mid + 3, hi + 1)):
                    try:
                        summary, meta = parse_snapshot(caps[nxt], timeout, use_cache)
                        best = (caps[nxt], summary, meta)
                        hi = nxt - 1
                        moved = True
                        break
                    except RateLimited:
                        raise
                    except Exception as e2:
                        attempts.append((caps[nxt]["date"], _why(e2)))
                if not moved:
                    lo = mid + 1
    return best, attempts


def latest_parseable(caps, limit=5, timeout=TIMEOUT, use_cache=True, log=None):
    """Newest readable capture, walked backwards. Recent captures almost always
    parse, so a short walk is cheaper than a bisect here."""
    attempts = []
    for cap in reversed(caps[-limit:]) if len(caps) > limit else reversed(caps):
        try:
            summary, meta = parse_snapshot(cap, timeout, use_cache)
            return (cap, summary, meta), attempts
        except RateLimited:
            raise
        except Exception as e:
            attempts.append((cap["date"], _why(e)))
            if log:
                log(f"  {cap['date']}: {_why(e)}")
    return None, attempts


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------
def timeline(caps):
    """Per-year shape of the archive. Built from CDX metadata alone, so it costs
    one request no matter how long the history is."""
    years = {}
    for c in caps:
        y = c["timestamp"][:4]
        slot = years.setdefault(y, {"year": y, "captures": 0, "usable": 0,
                                    "first": None, "last": None,
                                    "min_record": None, "max_record": None,
                                    "unusable_reasons": Counter()})
        slot["captures"] += 1
        slot["first"] = slot["first"] or c["date"]
        slot["last"] = c["date"]
        why = classify(c)
        if why:
            slot["unusable_reasons"][why.split(" -- ")[0]] += 1
            continue
        slot["usable"] += 1
        rb = c["record_bytes"]
        if rb is not None:
            slot["min_record"] = rb if slot["min_record"] is None else min(slot["min_record"], rb)
            slot["max_record"] = rb if slot["max_record"] is None else max(slot["max_record"], rb)
    out = []
    for y in sorted(years):
        s = years[y]
        s["unusable_reasons"] = dict(s["unusable_reasons"])
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
def _ga4_names(summary):
    """Literal GA4 event names only, plus a count of the runtime-named ones.

    An event whose name is built at runtime has no name to compare -- the parser
    renders it as the expression that produces it, and those expressions differ
    between two versions for reasons that have nothing to do with what the
    company measures. Comparing them would manufacture added/removed events.
    """
    literal = {e["event_name"] for e in summary.get("ga4_events", [])
               if not e.get("dynamic_name") and isinstance(e.get("event_name"), str)}
    dynamic = sum(1 for e in summary.get("ga4_events", []) if e.get("dynamic_name"))
    return literal, dynamic


def _vendor_tag_types(summary):
    """Tag types that name a destination someone's data goes to.

    A Trigger Group, a Custom HTML shell and a click listener are not vendors, so
    their arrival or departure must not read as a change of ad platform -- that
    is the `__tg` trap from references/gtm-container.md, and in a diff it is
    worse, because it dates a platform decision that never happened. Unknown
    functions are excluded too: `__cvt_` templates are reported as templates, and
    anything else the parser could not name should not be given a vendor's
    standing just because it moved.
    """
    return {t["function"]: t for t in summary.get("tag_types", [])
            if t["function"] in TAG_TYPES
            and TAG_TYPES[t["function"]][1] not in ("listener", "gtm", "custom")}


# A template's fingerprint "hosts" come from a loose URL regex over its
# permission grants, so JS member expressions (`fbq.queue`, `rdt.call`) land in
# it looking exactly like domains. Requiring a real TLD is what keeps a
# diff from announcing that a company "added fbq.push" as a vendor.
COMMON_TLDS = {
    "com", "net", "org", "io", "co", "ai", "app", "dev", "cloud", "me", "tv",
    "us", "uk", "de", "fr", "es", "it", "nl", "se", "no", "dk", "fi", "pl",
    "ru", "jp", "cn", "in", "br", "ca", "au", "nz", "ch", "at", "be", "ie",
    "info", "biz", "tech", "digital", "agency", "media", "live", "link", "sh",
    "ly", "gg", "xyz", "online", "site", "store", "cc", "to", "fm", "am", "is",
}


def _third_party_hosts(summary):
    hosts = set()
    # Custom HTML hosts were matched behind an http(s):// prefix, so they are
    # real; template fingerprints were not, so they get screened.
    for h in summary.get("custom_html_hosts", []):
        hosts.add(h["host"].lower().strip("."))
    for t in summary.get("custom_templates", []):
        for h in t.get("fingerprint", {}).get("hosts", []):
            h = h.lower().strip(".")
            if h.rsplit(".", 1)[-1] in COMMON_TLDS:
                hosts.add(h)
    return {h for h in hosts if "." in h and "googletagmanager" not in h}


def _clean_url(u):
    """gtm.js escapes forward slashes inside its JSON strings, so a transport URL
    arrives as `https:\\/\\/t-antenna.example.com`. Unescape before comparing or
    printing it."""
    return (u or "").replace("\\/", "/")


def _lookup_classes(summary):
    return Counter(t["classification"] for t in summary.get("lookup_tables", []))


def diff_summaries(old, new):
    """What changed between two parsed containers.

    Deliberately asymmetric in emphasis: the point is the delta, so counts that
    held steady are reported as a single number rather than as two inventories.
    """
    d = {"headline": [], "tag_types": {}, "ga4_events": {}, "google_ads": {},
         "vendors": {}, "consent": {}, "lookup_tables": {}, "server_side": {},
         "data_layer_keys": {}}

    ot, nt = old["counts"]["tags"], new["counts"]["tags"]
    ov, nv = old.get("container_version"), new.get("container_version")
    d["headline"].append(("tags", ot, nt))
    d["headline"].append(("variables", old["counts"]["variables"], new["counts"]["variables"]))

    # --- tag types -------------------------------------------------------
    o = {t["function"]: t for t in old["tag_types"]}
    n = {t["function"]: t for t in new["tag_types"]}
    d["tag_types"] = {
        "added": sorted(({"function": f, "type": n[f]["type"], "count": n[f]["count"]}
                         for f in n if f not in o), key=lambda x: -x["count"]),
        "removed": sorted(({"function": f, "type": o[f]["type"], "count": o[f]["count"]}
                           for f in o if f not in n), key=lambda x: -x["count"]),
        "count_changed": sorted(
            ({"function": f, "type": n[f]["type"], "then": o[f]["count"], "now": n[f]["count"]}
             for f in n if f in o and o[f]["count"] != n[f]["count"]),
            key=lambda x: -abs(x["now"] - x["then"])),
    }

    # --- GA4 events ------------------------------------------------------
    og, od = _ga4_names(old)
    ng, nd = _ga4_names(new)
    d["ga4_events"] = {"added": sorted(ng - og), "removed": sorted(og - ng),
                       "kept": len(og & ng), "then": len(og), "now": len(ng),
                       "runtime_named_then": od, "runtime_named_now": nd}
    d["headline"].append(("GA4 event names", len(og), len(ng)))

    # --- Google Ads ------------------------------------------------------
    oa, na = old.get("google_ads_summary", {}), new.get("google_ads_summary", {})
    ol, nl = set(oa.get("labels") or []), set(na.get("labels") or [])
    oacc, nacc = set(oa.get("ads_accounts") or []), set(na.get("ads_accounts") or [])
    d["google_ads"] = {
        "labels_added": sorted(nl - ol), "labels_removed": sorted(ol - nl),
        "labels_kept": len(ol & nl), "labels_then": len(ol), "labels_now": len(nl),
        "accounts_added": sorted(nacc - oacc), "accounts_removed": sorted(oacc - nacc),
        "accounts_then": sorted(oacc), "accounts_now": sorted(nacc),
    }
    d["headline"].append(("Ads conversion labels", len(ol), len(nl)))

    # --- vendors ---------------------------------------------------------
    ov_t, nv_t = _vendor_tag_types(old), _vendor_tag_types(new)
    oh, nh = _third_party_hosts(old), _third_party_hosts(new)
    o_tpl = {t["template_id"] for t in old.get("custom_templates", [])}
    n_tpl = {t["template_id"] for t in new.get("custom_templates", [])}
    o_acct, n_acct = old.get("vendor_account_ids", {}), new.get("vendor_account_ids", {})
    d["vendors"] = {
        "destinations_added": sorted(nv_t[f]["type"] for f in nv_t if f not in ov_t),
        "destinations_removed": sorted(ov_t[f]["type"] for f in ov_t if f not in nv_t),
        "hosts_added": sorted(nh - oh), "hosts_removed": sorted(oh - nh),
        "templates_added": sorted(n_tpl - o_tpl), "templates_removed": sorted(o_tpl - n_tpl),
        "accounts_added": sorted(set(n_acct) - set(o_acct)),
        "accounts_removed": sorted(set(o_acct) - set(n_acct)),
    }

    # --- consent ---------------------------------------------------------
    oc, nc = old.get("consent", {}), new.get("consent", {})
    d["consent"] = {
        "then": {"consent_aware_tags": oc.get("consent_aware_tags"),
                 "total_tags": oc.get("total_tags"),
                 "types_required": oc.get("consent_types_required") or {},
                 "cmp": sorted(oc.get("cmp_hints") or {}),
                 "regions": len(oc.get("default_consent_regions") or [])},
        "now": {"consent_aware_tags": nc.get("consent_aware_tags"),
                "total_tags": nc.get("total_tags"),
                "types_required": nc.get("consent_types_required") or {},
                "cmp": sorted(nc.get("cmp_hints") or {}),
                "regions": len(nc.get("default_consent_regions") or [])},
    }

    # --- lookup tables ---------------------------------------------------
    olt, nlt = _lookup_classes(old), _lookup_classes(new)
    d["lookup_tables"] = {
        "then_total": sum(olt.values()), "now_total": sum(nlt.values()),
        "by_class": sorted(
            ({"classification": k, "then": olt.get(k, 0), "now": nlt.get(k, 0)}
             for k in set(olt) | set(nlt)),
            key=lambda x: -(x["now"] + x["then"])),
    }

    # --- server-side -----------------------------------------------------
    d["server_side"] = {
        "then": sorted(_clean_url(u) for u in old.get("server_side", {}).get("transport_urls") or []),
        "now": sorted(_clean_url(u) for u in new.get("server_side", {}).get("transport_urls") or []),
    }

    # --- dataLayer schema ------------------------------------------------
    odl = set(old.get("notable_variables", {}).get("data_layer_keys") or [])
    ndl = set(new.get("notable_variables", {}).get("data_layer_keys") or [])
    d["data_layer_keys"] = {"added": sorted(ndl - odl), "removed": sorted(odl - ndl),
                            "then": len(odl), "now": len(ndl)}

    if ov and nv and str(ov).isdigit() and str(nv).isdigit():
        d["container_versions"] = {"then": int(ov), "now": int(nv),
                                   "publishes_between": int(nv) - int(ov)}
    return d


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _delta(a, b):
    if a == b == 0:
        return "0  (none in either version)"
    if a == b:
        return f"{a}"
    return f"{a} -> {b}  ({b - a:+d})"


def render_timeline(h):
    L = []
    a = L.append
    a(f"# GTM container history  {h['container_id']}")
    caps = h["captures"]
    if not caps:
        a("\nNo captures of this container's gtm.js in the Wayback Machine.")
        a("That means nobody archived this URL -- it is not evidence that the container")
        a("is new, nor that the ID is wrong. Check the ID against the page source.")
        return "\n".join(L)

    usable = [c for c in caps if not classify(c)]
    a(f"{len(caps)} captures, {caps[0]['date']} .. {caps[-1]['date']}   "
      f"({len(usable)} readable-looking, {len(caps) - len(usable)} skipped)")
    if len(h["url_variants"]) > 1:
        a(f"archived under {len(h['url_variants'])} URL variants: "
          f"{', '.join(sorted(h['url_variants']))}")
    a(f"[cache: {cache_dir()}]")
    if h.get("range_note"):
        a(h["range_note"])

    a("\n## Captures by year")
    a("  year  captures  readable  first        last         archived record size (gzipped)")
    for y in h["timeline"]:
        lo = f"{y['min_record']:,}" if y["min_record"] is not None else "-"
        hi = f"{y['max_record']:,}" if y["max_record"] is not None else "-"
        size = f"{lo} .. {hi}" if lo != hi else lo
        a(f"  {y['year']}  {y['captures']:>8}  {y['usable']:>8}  "
          f"{y['first'] or '-':<11}  {y['last'] or '-':<11}  {size}")
    a("")
    a("  Record size is the compressed size of the archived WARC record, not the")
    a("  container's byte count -- it tracks growth, it is not the file's weight.")
    a("  A thin year is a thin year in the ARCHIVE. It is not evidence that the")
    a("  company tracked less then.")

    skipped = Counter()
    for c in caps:
        why = classify(c)
        if why:
            skipped[why.split(" -- ")[0]] += 1
    if skipped:
        a("\n## Captures skipped")
        for why, n in skipped.most_common():
            a(f"  {n:>4}  {why}")

    gaps = h.get("gaps") or []
    if gaps:
        a("\n## Gaps longer than six months between readable captures")
        for g in gaps:
            a(f"  {g['from']} .. {g['to']}   {g['days']} days unarchived")
        a("  A change found across one of these windows is dated only to the window.")

    if h.get("stale_days") is not None and h["stale_days"] > 180:
        a(f"\n!! Newest capture is {h['stale_days']} days old. For today's container run:")
        a(f"     python3 -I scripts/gtm_parse.py {h['container_id']}")
        a(f"   or diff the archive against it with:")
        a(f"     python3 -I scripts/history.py {h['container_id']} --diff --live")

    a(f"\nNext: python3 -I scripts/history.py {h['container_id']} --diff")
    return "\n".join(L)


def render_diff(r):
    L = []
    a = L.append
    old, new, d = r["old"], r["new"], r["diff"]
    a(f"# GTM container diff  {r['container_id']}")
    a(f"  then  {old['label']:<22} container version {old['container_version']:<6} {old['bytes']:>9,} bytes")
    a(f"  now   {new['label']:<22} container version {new['container_version']:<6} {new['bytes']:>9,} bytes")
    a(f"  {r['span_text']} apart")
    for w in r.get("warnings", []):
        a(f"!! {w}")

    a("\n## What changed")
    rotations = {
        "GA4 event names": (len(d["ga4_events"]["added"]), len(d["ga4_events"]["removed"])),
        "Ads conversion labels": (len(d["google_ads"]["labels_added"]),
                                  len(d["google_ads"]["labels_removed"])),
    }
    for name, then, now in d["headline"]:
        line = f"  {name:<24} {_delta(then, now)}"
        add, rem = rotations.get(name, (0, 0))
        if then == now and (add or rem):
            line += f"  -- same count, {add} added and {rem} dropped"
        a(line)
    cv = d.get("container_versions")
    if cv:
        a(f"  {'container publishes':<24} {cv['publishes_between']} between these two dates"
          f"  (~{r['publishes_per_year']}/year)")
    if d["server_side"]["then"] != d["server_side"]["now"]:
        a(f"  {'server-side transport':<24} "
          f"{', '.join(d['server_side']['then']) or 'none'} -> "
          f"{', '.join(d['server_side']['now']) or 'none'}")

    tt = d["tag_types"]
    if tt["added"] or tt["removed"]:
        a("\n## Tag types that arrived or left")
        for t in tt["added"]:
            a(f"  + {t['count']:>4}  {t['type']}  [{t['function']}]")
        for t in tt["removed"]:
            a(f"  - {t['count']:>4}  {t['type']}  [{t['function']}]  (gone)")
    if tt["count_changed"]:
        a("\n## Tag types that grew or shrank")
        for t in tt["count_changed"][:15]:
            a(f"  {t['then']:>4} -> {t['now']:<4}  {t['type']}  [{t['function']}]")

    g = d["ga4_events"]
    if g["added"] or g["removed"]:
        a(f"\n## GA4 events  ({g['then']} -> {g['now']} named, {g['kept']} unchanged)")
        if g["runtime_named_then"] or g["runtime_named_now"]:
            a(f"  plus {g['runtime_named_then']} -> {g['runtime_named_now']} events whose name is "
              f"built at runtime and cannot be compared")
        if g["added"]:
            a(f"  started measuring ({len(g['added'])}):")
            a(_wrap(g["added"]))
        if g["removed"]:
            a(f"  stopped measuring ({len(g['removed'])}):")
            a(_wrap(g["removed"]))
        a("  An event removed from the container is an event they stopped sending from")
        a("  this container -- it may have moved to another container or server-side.")

    ads = d["google_ads"]
    if (ads["labels_added"] or ads["labels_removed"]
            or ads["accounts_added"] or ads["accounts_removed"]):
        a(f"\n## Google Ads  (conversion labels {ads['labels_then']} -> {ads['labels_now']}, "
          f"{ads['labels_kept']} unchanged)")
        # These lists come from the accounts carrying CONVERSION TAGS, which is
        # not the same as every Ads ID in the file. One real container dropped
        # an account from its conversion tags while the same AW- ID still
        # appeared elsewhere in the file, so a bare "removed" would have read as
        # "they left that account entirely" and been wrong.
        if ads["accounts_added"]:
            a(f"  now carrying conversion tags:     {', '.join(ads['accounts_added'])}")
        if ads["accounts_removed"]:
            a(f"  no longer carrying conversion tags: {', '.join(ads['accounts_removed'])}")
            a("    (the ID may still appear elsewhere in the container -- this is about")
            a("     which accounts have conversion tags, not which IDs are present)")
        if ads["labels_added"]:
            a(f"  conversion labels added ({len(ads['labels_added'])}):")
            a(_wrap(ads["labels_added"]))
        if ads["labels_removed"]:
            a(f"  conversion labels removed ({len(ads['labels_removed'])}):")
            a(_wrap(ads["labels_removed"]))
        a("  A label count is the size of the optimisation surface, not of the spend.")

    v = d["vendors"]
    if any(v.values()):
        a("\n## Vendors and templates")
        for key, sign, note in (("destinations_added", "+", ""),
                                ("destinations_removed", "-", " (gone)"),
                                ("templates_added", "+ template", ""),
                                ("templates_removed", "- template", " (gone)"),
                                ("accounts_added", "+ vendor account", ""),
                                ("accounts_removed", "- vendor account", " (gone)"),
                                ("hosts_added", "+ host", ""),
                                ("hosts_removed", "- host", " (gone)")):
            for item in v[key][:25]:
                a(f"  {sign} {item}{note}")
            if len(v[key]) > 25:
                a(f"    ... {len(v[key]) - 25} more")
        if r.get("hosts_unreliable"):
            a("  (host lines are best-effort: one snapshot was read through Wayback's")
            a("   rewriting replay, which rewrites URLs inside the file)")

    c = d["consent"]
    a("\n## Consent declarations, then vs now")
    for when in ("then", "now"):
        s = c[when]
        types = ", ".join(f"{k} ({n})" for k, n in (s["types_required"] or {}).items()) or "none declared"
        a(f"  {when:<5} {s['consent_aware_tags']} of {s['total_tags']} tags declare consent "
          f"requirements; {types}")
        a(f"        CMP: {', '.join(s['cmp']) or 'none detected'};  "
          f"{s['regions']} regions with explicit defaults")

    lt = d["lookup_tables"]
    if lt["then_total"] or lt["now_total"]:
        a(f"\n## Lookup tables  ({lt['then_total']} -> {lt['now_total']})")
        for row in lt["by_class"]:
            if row["then"] != row["now"]:
                a(f"  {row['then']:>3} -> {row['now']:<3}  {row['classification']}")
        a("  Rows are not diffed here. Read them with:")
        a(f"     python3 -I scripts/gtm_parse.py {r['container_id']}")

    dl = d["data_layer_keys"]
    if dl["added"] or dl["removed"]:
        a(f"\n## dataLayer schema  ({dl['then']} -> {dl['now']} keys)")
        if dl["added"]:
            a(f"  added ({len(dl['added'])}):")
            a(_wrap(dl["added"][:60]))
        if dl["removed"]:
            a(f"  removed ({len(dl['removed'])}):")
            a(_wrap(dl["removed"][:60]))

    a("\n## Reading this honestly")
    a(f"  Two dates {r['span_text']} apart, with {r['between_readable']} other readable")
    a("  captures between them. Every change above happened somewhere inside that")
    a("  window, not on either date -- narrow it with --from and --to if the timing")
    a("  carries weight in the report.")
    a("  The diff compares configuration, so it shows what they set up and took")
    a("  down -- not what fired, not what the account spent, and not anything")
    a("  that was paused rather than deleted.")
    if r.get("unreadable"):
        a(f"  {len(r['unreadable'])} capture(s) were fetched while locating the endpoints")
        a("  and carried no readable configuration:")
        for date, why in r["unreadable"][:6]:
            a(f"    {date}: {why}")
    return "\n".join(L)


def _wrap(items, width=76, indent="    "):
    lines, cur = [], indent
    for it in items:
        piece = f"{it}, "
        if len(cur) + len(piece) > width and cur.strip():
            lines.append(cur.rstrip())
            cur = indent
        cur += piece
    if cur.strip():
        lines.append(cur.rstrip().rstrip(","))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------
def _in_range(cap, y_from, y_to):
    y = int(cap["timestamp"][:4])
    return (y_from is None or y >= y_from) and (y_to is None or y <= y_to)


def _gaps(caps, min_days=180):
    out = []
    for a, b in zip(caps, caps[1:]):
        days = (_epoch(b["timestamp"]) - _epoch(a["timestamp"])) // 86400
        if days >= min_days:
            out.append({"from": a["date"], "to": b["date"], "days": int(days)})
    return out


def _epoch(ts):
    import calendar
    return calendar.timegm((int(ts[0:4]), int(ts[4:6]), int(ts[6:8]),
                            int(ts[8:10]), int(ts[10:12]), int(ts[12:14]), 0, 0, 0))


def history(container_id, y_from=None, y_to=None, use_cache=True):
    caps = cdx_captures(container_id, use_cache=use_cache)
    kept = [c for c in caps if _in_range(c, y_from, y_to)]
    usable = [c for c in kept if not classify(c)]
    h = {
        "container_id": container_id.strip().upper(),
        "captures": kept,
        "url_variants": sorted({c["url"] for c in kept}),
        "timeline": timeline(kept),
        "gaps": _gaps(usable),
        "stale_days": (int((time.time() - _epoch(caps[-1]["timestamp"])) // 86400)
                       if caps else None),
    }
    if (y_from or y_to) and len(kept) != len(caps):
        h["range_note"] = (f"bounded to {y_from or 'start'}..{y_to or 'now'}: "
                           f"{len(kept)} of {len(caps)} captures shown")
    return h


def diff(container_id, y_from=None, y_to=None, use_live=False, use_cache=True, quiet=False):
    cid = container_id.strip().upper()
    log = (lambda m: None) if quiet else (lambda m: sys.stderr.write(m + "\n"))
    caps = cdx_captures(cid, use_cache=use_cache)
    if not caps:
        raise RuntimeError(
            "no Wayback captures of this container's gtm.js. Nobody archived the URL; "
            "that is not evidence about the container itself.")
    cands = [c for c in caps if _in_range(c, y_from, y_to) and not classify(c)]
    if not cands:
        raise RuntimeError(
            f"{len(caps)} captures exist but none in {y_from or 'start'}..{y_to or 'now'} "
            f"are readable records (all are revisit pointers, errors or stubs).")

    log(f"[{len(cands)} readable captures; finding the oldest one with a parseable config]")
    old_pick, old_fails = earliest_parseable(cands, use_cache=use_cache, log=log)
    if not old_pick:
        raise RuntimeError(
            f"none of the {len(cands)} captures in range carry a readable configuration "
            f"object. Captures before roughly mid-2018 are in GTM's older compiled format, "
            f"which has none -- try --from 2019.")

    warnings, unreadable = [], list(old_fails)
    old_cap, old_sum, old_meta = old_pick

    if use_live:
        log("[fetching today's published container]")
        src = fetch_live(cid)
        new_sum = summarise(extract_data_object(src), src)
        new_sum["_bytes"] = len(src)
        new_cap = {"date": time.strftime("%Y-%m-%d"), "timestamp": time.strftime("%Y%m%d%H%M%S")}
        new_meta = {"replay": "live", "rewritten": False}
        new_label = f"{new_cap['date']} (live)"
    else:
        later = [c for c in cands if c["timestamp"] > old_cap["timestamp"]]
        if not later:
            raise RuntimeError(
                f"only one parseable capture in range ({old_cap['date']}). A diff needs two; "
                f"widen the range, or compare it against today with --live.")
        log("[finding the newest parseable capture]")
        new_pick, new_fails = latest_parseable(later, use_cache=use_cache, log=log)
        unreadable += new_fails
        if not new_pick:
            raise RuntimeError(
                f"the {min(5, len(later))} newest captures in range could not be parsed. "
                f"Narrow with --to, or compare against today with --live.")
        new_cap, new_sum, new_meta = new_pick
        new_label = new_cap["date"]
        if h_stale(new_cap) > 180:
            warnings.append(
                f"the newest capture in range is {h_stale(new_cap)} days old, so 'now' here "
                f"means {new_cap['date']}, not today. Add --live to diff against the "
                f"container being served right now.")

    if old_meta.get("rewritten") or new_meta.get("rewritten"):
        warnings.append(
            "a snapshot was read through Wayback's rewriting replay because no brotli "
            "decoder is available here; URLs inside it were un-rewritten best-effort, so "
            "treat the host lines as indicative.")

    days = (_epoch(new_cap["timestamp"]) - _epoch(old_cap["timestamp"])) // 86400
    between = sum(1 for c in cands
                  if old_cap["timestamp"] < c["timestamp"] < new_cap["timestamp"])
    d = diff_summaries(old_sum, new_sum)
    years = max(days / 365.25, 0.08)
    cv = d.get("container_versions")
    return {
        "container_id": cid,
        "old": {"label": old_cap["date"], "timestamp": old_cap["timestamp"],
                "container_version": old_sum.get("container_version"),
                "bytes": old_sum.get("_bytes"), "replay": old_meta.get("replay")},
        "new": {"label": new_label, "timestamp": new_cap["timestamp"],
                "container_version": new_sum.get("container_version"),
                "bytes": new_sum.get("_bytes"), "replay": new_meta.get("replay")},
        "span_days": int(days),
        "span_text": _span_text(int(days)),
        "between_readable": between,
        "publishes_per_year": round(cv["publishes_between"] / years) if cv else None,
        "hosts_unreliable": bool(old_meta.get("rewritten") or new_meta.get("rewritten")),
        "warnings": warnings,
        "unreadable": unreadable,
        "diff": d,
    }


def h_stale(cap):
    return int((time.time() - _epoch(cap["timestamp"])) // 86400)


def _span_text(days):
    y, rem = divmod(days, 365)
    m = rem // 30
    parts = []
    if y:
        parts.append(f"{y} year{'s' if y != 1 else ''}")
    if m:
        parts.append(f"{m} month{'s' if m != 1 else ''}")
    return " ".join(parts) or f"{days} days"


def main():
    # An explicit --help is a request that succeeded, so it exits 0. Running with
    # no arguments at all is a usage error and still exits 1.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    target = args[0]
    if not re.fullmatch(r"GTM-[A-Z0-9]{4,10}", target.strip(), re.I):
        print(f"expected a container ID like GTM-XXXXXXX, got {target!r}", file=sys.stderr)
        return 2

    def opt(name):
        if name in argv:
            i = argv.index(name)
            if len(argv) > i + 1 and not argv[i + 1].startswith("--"):
                return argv[i + 1]
        return None

    y_from = int(opt("--from")) if opt("--from") else None
    y_to = int(opt("--to")) if opt("--to") else None
    as_json = "--json" in argv
    no_cache = "--no-cache" in argv

    try:
        if "--diff" in argv:
            r = diff(target, y_from, y_to, use_live="--live" in argv,
                     use_cache=not no_cache, quiet=as_json)
            print(json.dumps(r, indent=2, default=str) if as_json else render_diff(r))
        else:
            h = history(target, y_from, y_to, use_cache=not no_cache)
            print(json.dumps(h, indent=2, default=str) if as_json else render_timeline(h))
    except RateLimited as e:
        print(f"RATE LIMITED: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"could not read history: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
