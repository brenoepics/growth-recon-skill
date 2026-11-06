#!/usr/bin/env python3
"""Parse a published Google Tag Manager container (gtm.js) into structured facts.

gtm.js is the compiled, *published* version of a container. Every visitor's browser
downloads it, so it is public by construction. Inside it is a single `var data = {...}`
object literal that happens to be strict JSON. This module extracts that object and
turns it into a report-ready summary.

Usage:
    python3 gtm_parse.py GTM-XXXXXXX            # fetch and summarise
    python3 gtm_parse.py path/to/gtm.js         # parse a local copy
    python3 gtm_parse.py GTM-XXXXXXX --json     # machine-readable output

Honest limits (see references/gtm-container.md for the full list):
  - Tag/trigger/variable NAMES are not published. Only types and fields are.
  - Conversion VALUES are almost always runtime dataLayer references, not literals.
  - Paused tags, drafts, folders, notes and server-container config never ship.
"""

import json
import re
import sys
import gzip
import zlib
import urllib.request
import urllib.error
from collections import Counter, defaultdict

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ---------------------------------------------------------------------------
# Tag type map. GTM compiles every tag to a `function` key like "__awct".
# Anything not in this map is reported as unknown rather than guessed at --
# a wrong vendor attribution is worse than an honest "unidentified template".
# ---------------------------------------------------------------------------
TAG_TYPES = {
    "__googtag": ("Google Tag (gtag base)", "google"),
    "__gaawe": ("GA4 Event", "google"),
    "__gaawc": ("GA4 Configuration", "google"),
    "__awct": ("Google Ads Conversion Tracking", "google-ads"),
    "__sp": ("Google Ads Remarketing / user list", "google-ads"),
    "__gclidw": ("Conversion Linker", "google-ads"),
    # Universal Analytics. Sunset July 2023, so its presence dates a container
    # and its disappearance is usually the clearest single signal in a
    # historical diff -- one real container shed 273 of these between 2022 and
    # now. Left unnamed it printed as "Unidentified template", which buried the
    # most legible finding a diff can produce.
    "__ua": ("Universal Analytics (legacy, sunset July 2023)", "google"),
    # The one place a published container admits a tag exists but is switched
    # off. Its settings are not published, so it is a name and nothing more.
    "__paused": ("Paused tag (settings not published)", "gtm"),
    "__flc": ("Floodlight Counter", "google-dv360"),
    "__fls": ("Floodlight Sales", "google-dv360"),
    "__ogt_google_signals": ("Google Signals", "google"),
    "__baut": ("Microsoft Advertising UET", "microsoft"),
    "__twitter_website_tag": ("X / Twitter Website Tag", "x"),
    "__bzi": ("LinkedIn Insight Tag", "linkedin"),
    "__pntr": ("Pinterest Tag", "pinterest"),
    "__qpx": ("Quora Pixel", "quora"),
    "__crto": ("Criteo OneTag", "criteo"),
    "__html": ("Custom HTML", "custom"),
    "__img": ("Custom Image pixel", "custom"),
    "__zone": ("Zone (loads another container)", "gtm"),
    # __tg is a Trigger Group, not a vendor tag. Worth naming explicitly because
    # a high __tg count looks like a mystery ad platform if you don't know this.
    "__tg": ("Trigger Group", "gtm"),
    # Built-in listeners: these describe *what the site measures*, which is
    # often more revealing than the destination tags.
    "__cl": ("Click listener (all elements)", "listener"),
    "__lcl": ("Link click listener", "listener"),
    "__fsl": ("Form submit listener", "listener"),
    "__sdl": ("Scroll depth listener", "listener"),
    "__tl": ("Timer listener", "listener"),
    "__ytl": ("YouTube video listener", "listener"),
    "__evl": ("Element visibility listener", "listener"),
    "__hl": ("History change listener", "listener"),
    "__jel": ("JavaScript error listener", "listener"),
}

MACRO_TYPES = {
    "__v": "Data Layer / built-in variable",
    "__c": "Constant",
    "__jsm": "Custom JavaScript",
    "__smm": "Lookup Table",
    "__remm": "RegEx Lookup Table",
    "__u": "URL variable",
    "__e": "Event name",
    "__d": "DOM element",
    "__k": "1st-party cookie",
    "__aev": "Auto-event variable",
    "__r": "Random number",
    "__gas": "GA settings",
    "__cid": "Container ID",
    "__ctv": "Container version",
    "__dbg": "Debug mode",
    "__f": "Referrer",
    "__j": "JS global variable",
}

ID_PATTERNS = {
    "google_ads": r"AW-\d{6,12}",
    "ga4": r"G-[A-Z0-9]{8,12}",
    "universal_analytics": r"UA-\d{4,10}-\d+",
    "floodlight": r"DC-\d{6,12}",
    "google_tag": r"GT-[A-Z0-9]{6,12}",
    "conversion_linker": r"MC-[A-Z0-9]{6,12}",
}


def _ssl_context():
    """Build a verifying SSL context, using certifi when the system store is
    unavailable. Many macOS Pythons ship without a usable CA bundle, which would
    otherwise make every fetch fail with CERTIFICATE_VERIFY_FAILED."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch(url, timeout=30, headers=None):
    """Fetch a URL as text. Returns (text, status).

    Tries urllib first, then falls back to curl. The fallback matters because
    Python's CA store is broken on a lot of machines while curl works fine, and a
    recon script that dies on certificate setup is useless.
    """
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw.decode("utf-8", errors="replace"), resp.status
    except urllib.error.HTTPError:
        raise
    except Exception:
        return _fetch_curl(url, timeout, hdrs)


def _fetch_curl(url, timeout, hdrs):
    import subprocess
    cmd = ["curl", "-sSL", "--compressed", "--max-time", str(timeout),
           "-w", "\n__HTTP_STATUS__%{http_code}"]
    for k, v in hdrs.items():
        if k.lower() != "accept-encoding":
            cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    if p.returncode != 0:
        raise RuntimeError(f"fetch failed for {url}: {p.stderr.strip()[:200]}")
    body, _, status = p.stdout.rpartition("\n__HTTP_STATUS__")
    return body, int(status or 0)


def extract_data_object(src):
    """Pull the `var data = {...}` object out of gtm.js and parse it as JSON.

    Brace counting is string-aware so that braces inside Custom HTML bodies and
    regex literals don't end the object early.
    """
    m = re.search(r"var data\s*=\s*\{", src)
    if not m:
        raise ValueError("no `var data = {` anchor found -- not a gtm.js container "
                         "(a gtag/js file or an error page looks like this)")
    start = src.index("{", m.start())
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(src)):
        ch = src[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(src[start:i + 1])
    raise ValueError("unbalanced braces while scanning the data object")


def _is_macro_ref(v):
    """GTM encodes a variable reference as ["macro", <index>]."""
    return isinstance(v, list) and len(v) >= 2 and v[0] == "macro"


def describe_value(v, macros):
    """Render a compiled GTM field value as something a human can read.

    Distinguishing a literal from a runtime reference is the whole ballgame for
    conversion values, so this keeps that distinction explicit.
    """
    if _is_macro_ref(v):
        idx = v[1]
        mac = macros[idx] if isinstance(idx, int) and idx < len(macros) else {}
        fn = mac.get("function", "?")
        kind = MACRO_TYPES.get(fn, fn)
        name = mac.get("vtp_name") or mac.get("vtp_dataLayerVersion") and mac.get("vtp_name")
        detail = f" '{name}'" if name else ""
        return {"runtime": True, "repr": f"<{kind}{detail}>", "macro_index": idx}
    if isinstance(v, list):
        return {"runtime": True, "repr": json.dumps(v)[:160]}
    return {"runtime": False, "repr": v}


def flatten_params(v, macros, out=None):
    """Flatten GTM's ["list", ["map","parameter",k,"parameterValue",val], ...]."""
    out = out if out is not None else []
    if not isinstance(v, list) or not v or v[0] != "list":
        return out
    for item in v[1:]:
        if not (isinstance(item, list) and item and item[0] == "map"):
            continue
        d = {}
        pairs = item[1:]
        for i in range(0, len(pairs) - 1, 2):
            d[pairs[i]] = pairs[i + 1]
        key = d.get("parameter") or d.get("name") or d.get("key")
        raw = d.get("parameterValue", d.get("value"))
        if key is not None:
            out.append({"param": key, "value": describe_value(raw, macros)})
    return out


# ---------------------------------------------------------------------------
# Trigger resolution.
#
# A tag's type tells you where data goes; its trigger tells you what the company
# counts as the event worth sending. That second thing is usually the finding.
# GTM stores it as three parallel lists cross-referenced by index, so recovering
# it means walking rules -> predicates -> macros. Without this you can see that
# 7 conversion tags exist but not that all 7 fire only for companies with 20+
# employees, which is the kind of thing a report is actually for.
# ---------------------------------------------------------------------------
PREDICATE_OPS = {
    "_eq": "equals", "_sw": "starts with", "_ew": "ends with",
    "_cn": "contains", "_re": "matches regex", "_lt": "<", "_gt": ">",
    "_le": "<=", "_ge": ">=", "_css": "matches CSS selector",
    "_lc": "is in list", "_mt": "matches", "_um": "URL matches",
}

BUILTIN_MACRO_LABELS = {
    "__e": "Event", "__u": "Page URL", "__f": "Referrer", "__r": "Random Number",
    "__cid": "Container ID", "__ctv": "Container Version", "__dbg": "Debug Mode",
    "__aev": "Auto-Event Variable", "__d": "DOM Element",
}


def macro_label(idx, macros):
    """Best available human name for a variable reference.

    Configured names aren't published, so this prefers the dataLayer key or
    cookie name the variable reads (which *is* published and is usually more
    informative than a GTM label would have been) and falls back to the type.
    """
    if not isinstance(idx, int) or idx < 0 or idx >= len(macros):
        return f"{{{{macro {idx}}}}}"
    m = macros[idx]
    fn = m.get("function", "?")
    name = m.get("vtp_name")
    if isinstance(name, str) and name.strip():
        return "{{%s}}" % name.strip()
    if fn in BUILTIN_MACRO_LABELS:
        return "{{%s}}" % BUILTIN_MACRO_LABELS[fn]
    if fn == "__c":
        v = m.get("vtp_value")
        if isinstance(v, (str, int, float)) and len(str(v)) <= 40:
            return "const(%s)" % v
        return "{{Constant}}"
    return "{{%s}}" % MACRO_TYPES.get(fn, fn)


def describe_predicate(p, macros):
    """Render one condition as a readable line."""
    if not isinstance(p, dict):
        return str(p)[:120]
    fn = p.get("function", "?")
    op = PREDICATE_OPS.get(fn, fn)
    a0, a1 = p.get("arg0"), p.get("arg1")
    left = macro_label(a0[1], macros) if _is_macro_ref(a0) else (
        a0 if isinstance(a0, str) else json.dumps(a0, default=str))
    if _is_macro_ref(a1):
        right = macro_label(a1[1], macros)
    elif isinstance(a1, str):
        right = f"'{a1}'" if len(a1) <= 90 else f"'{a1[:90]}...'"
    else:
        right = json.dumps(a1, default=str)
    return f"{left} {op} {right}"


def resolve_triggers(res):
    """Map each tag index to the conditions that fire and block it.

    Rule clauses are ["if"|"unless"|"add"|"block", <index>, ...]. A rule fires
    its "add" tags when every "if" predicate holds and no "unless" predicate
    does; "block" entries suppress a tag under the same condition set.
    """
    tags = res.get("tags", [])
    rules = res.get("rules", [])
    preds = res.get("predicates", [])
    macros = res.get("macros", [])
    out = {i: {"fires_when": [], "blocked_when": []} for i in range(len(tags))}

    for rule in rules:
        if not isinstance(rule, list):
            continue
        ifs, unlesses, adds, blocks = [], [], [], []
        for clause in rule:
            if not (isinstance(clause, list) and clause):
                continue
            kind = clause[0]
            bucket = {"if": ifs, "unless": unlesses, "add": adds, "block": blocks}.get(kind)
            if bucket is None:
                continue
            bucket.extend(v for v in clause[1:] if isinstance(v, int))
        cond = {
            "all_of": [describe_predicate(preds[i], macros) for i in ifs if i < len(preds)],
            "none_of": [describe_predicate(preds[i], macros) for i in unlesses if i < len(preds)],
        }
        for t in adds:
            if t in out:
                out[t]["fires_when"].append(cond)
        for t in blocks:
            if t in out:
                out[t]["blocked_when"].append(cond)
    return out


def condition_text(cond):
    parts = list(cond.get("all_of", []))
    parts += [f"NOT ({c})" for c in cond.get("none_of", [])]
    return " AND ".join(parts) if parts else "(always / container load)"


def gating_groups(triggers, tags, min_tags=3):
    """Find conditions that gate several tags at once.

    A condition shared by many destination tags is a policy decision -- the
    company's working definition of a countable event -- so it deserves to be
    surfaced rather than left implicit across N separate tag entries.
    """
    counts = defaultdict(lambda: {"tags": [], "tag_types": Counter()})
    for idx, tr in triggers.items():
        if idx >= len(tags):
            continue
        fn = tags[idx].get("function", "?")
        for cond in tr["fires_when"]:
            for clause in cond.get("all_of", []) + [f"NOT ({c})" for c in cond.get("none_of", [])]:
                # Container-load and event-name clauses are structural noise here;
                # it's the extra qualifying conditions that carry intent.
                if re.match(r"^\{\{Event\}\} equals", clause) or "gtm.js" in clause or "gtm.load" in clause:
                    continue
                g = counts[clause]
                if idx not in g["tags"]:
                    g["tags"].append(idx)
                    g["tag_types"][TAG_TYPES.get(fn, (fn, None))[0] or fn] += 1
    return [
        {"condition": c, "tag_count": len(g["tags"]),
         "tag_types": dict(g["tag_types"]), "tag_indexes": g["tags"][:40]}
        for c, g in sorted(counts.items(), key=lambda kv: -len(kv[1]["tags"]))
        if len(g["tags"]) >= min_tags
    ]


# Vendor account IDs live inside Custom HTML bodies and template fields, which
# the structured summary throws away. They matter because several vendors expose
# a public config endpoint keyed on exactly this ID -- VWO's settings file, for
# instance, returns the live test names.
VENDOR_ACCOUNT_PATTERNS = {
    # The SmartCode minifies its own variable name, so match the first numeric
    # assignment after the _vwo_code marker rather than a fixed identifier:
    # live containers carry `window._vwo_code||function(){var h=765415,...`.
    "vwo": [r"visualwebsiteoptimizer\.com/j\.php\?a=(\d{4,10})",
            r"_vwo_code\b[\s\S]{0,300}?\bvar\s+[A-Za-z_$]\w*\s*=\s*(\d{4,10})",
            r"_vwo_[\\\"']*(\d{4,10})[\\\"']*_settings",
            r"\bvwo[_A-Za-z]*account[_A-Za-z]*\W{1,4}(\d{4,10})"],
    "hotjar": [r"hjid\s*[:=]\s*['\"]?(\d{5,9})"],
    "clarity": [r"clarity\.ms/tag/([a-z0-9]{6,15})"],
    "marketo_munchkin": [r"(\d{3}-[A-Z0-9]{3}-\d{3})"],
    "hubspot_portal": [r"js\.hs-scripts\.com/(\d{5,10})"],
    "meta_pixel": [r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{13,17})"],
    "linkedin_partner": [r"_linkedin_partner_id\s*=\s*['\"]?(\d{4,10})"],
    "tiktok_pixel": [r"ttq\.load\(\s*['\"]([A-Z0-9]{10,25})"],
    "optimizely_project": [r"cdn\.optimizely\.com/(?:js|public|datafiles)/([A-Za-z0-9_-]{6,30})"],
    "google_optimize_dead": [r"(OPT-[A-Z0-9]{6,10})"],
}


def extract_vendor_account_ids(raw_src):
    found = {}
    for vendor, pats in VENDOR_ACCOUNT_PATTERNS.items():
        hits = []
        for pat in pats:
            try:
                hits += re.findall(pat, raw_src, re.I if vendor != "marketo_munchkin" else 0)
            except re.error:
                continue
        if hits:
            found[vendor] = sorted({h for h in hits if h})[:6]
    return found


ADS_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{17,24}$")


def looks_like_ads_label(s):
    """Google Ads conversion labels look like '2rsECMaohNcYEPKW1Y4q' -- ~20 chars
    of mixed-case base64url. Used to catch labels passed through the dataLayer or
    lookup tables rather than sitting in an __awct tag, which is how most modern
    setups do it."""
    return (isinstance(s, str) and ADS_LABEL_RE.match(s)
            and re.search(r"[A-Z]", s) and re.search(r"[a-z]", s)
            and re.search(r"\d", s) and s.count("_") < 2
            and not s.startswith("vtp_"))


def extract_lookup_tables(macros, max_rows=60):
    """Pull every Lookup Table and RegEx Table with its rows intact.

    These are the richest thing in a container and the part almost everyone
    misses. A marketer building a table has to type both sides of a mapping by
    hand, so the rows carry names and numbers that exist nowhere else in public:
    per-market conversion values, plan/SKU identifiers, firmographic segment
    bands, A/B test names, gated-asset titles. Classification below is a hint to
    steer attention, not a verdict -- read the rows.
    """
    tables = []
    for idx, m in enumerate(macros):
        fn = m.get("function")
        if fn not in ("__smm", "__remm"):
            continue
        mp = m.get("vtp_map")
        if not isinstance(mp, list):
            continue
        rows = []
        for item in mp[1:]:
            if not (isinstance(item, list) and item and item[0] == "map"):
                continue
            pairs = item[1:]
            d = {pairs[j]: pairs[j + 1] for j in range(0, len(pairs) - 1, 2)}
            k, v = d.get("key"), d.get("value")
            if isinstance(k, (str, int, float)) or isinstance(v, (str, int, float)):
                rows.append([k if isinstance(k, (str, int, float)) else "<dynamic>",
                             v if isinstance(v, (str, int, float)) else "<dynamic>"])
        if not rows:
            continue

        vals = [str(r[1]) for r in rows]
        keys = [str(r[0]) for r in rows]
        numeric = [v for v in vals if re.fullmatch(r"-?\d+(\.\d+)?", v)]
        uuids = [v for v in vals if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(-\w+)?", v, re.I)]

        def share(pattern, cells):
            """How many cells match, on token boundaries.

            Earlier this ran a bare substring search over every row joined into
            one string, so a single OneTrust UUID ending '-test' relabelled a
            13-row consent table as experiment names. Requiring a real share of
            rows to match, on word boundaries, is what stops one incidental
            suffix speaking for the whole table.
            """
            hits = sum(1 for c in cells if re.search(pattern, c, re.I))
            return hits / max(1, len(cells))

        half = 0.5
        kind = "other"
        # Decimal numbers across markets/segments are the classic conversion-value
        # or lead-value table -- the thing the viral "grep for value" trick looks
        # for and misses, because it never lands in a field called value.
        if len(numeric) >= max(2, len(vals) // 2) and any("." in v for v in numeric):
            kind = "numeric-values (possible conversion/lead values or bid modifiers)"
        elif len(uuids) >= max(2, int(len(vals) * half)):
            # Per-market vendor script IDs (OneTrust and friends) look like this.
            kind = "uuid-map (per-market vendor/script IDs)"
        elif sum(1 for v in vals if looks_like_ads_label(v)) >= max(2, int(len(vals) * half)):
            kind = "google ads conversion labels"
        elif len(numeric) >= max(2, len(vals) // 2):
            kind = "numeric-codes"
        elif (share(r"(\btest\b|[_\-@]test\b|\bexperiment\b|\bvariant\b|\bexp_|@|\bholdback\b|\bcontrol\b)",
                    keys + vals) >= 0.3):
            kind = "experiment / variant names"
        elif share(r"(\btier\b|\bpremium\b|\bplan\b|\bsku\b|pricing|\btrial\b|enterprise|starter|\bpro\b)",
                   keys + vals) >= 0.3:
            kind = "plan / SKU / pricing"
        elif share(r"(\bvsb\b|\bsmb\b|mid-?market|\bcorp\b|\bent\b|segment|employee)", keys + vals) >= 0.3:
            kind = "segmentation bands"
        elif share(r"(^\s*[a-z]{2}(-[a-zA-Z]{2})?\s*$|locale|\blang\b)", vals) >= 0.5:
            kind = "locale / geo routing"

        tables.append({
            "macro_index": idx,
            "type": "Lookup Table" if fn == "__smm" else "RegEx Lookup Table",
            "classification": kind,
            "row_count": len(rows),
            "input": describe_value(m.get("vtp_input"), macros)["repr"] if m.get("vtp_input") is not None else None,
            "default": m.get("vtp_defaultValue") if not _is_macro_ref(m.get("vtp_defaultValue")) else "<dynamic>",
            "rows": rows[:max_rows],
            "truncated": len(rows) > max_rows,
        })
    # Surface the interesting classifications first.
    order = {"numeric-values (possible conversion/lead values or bid modifiers)": 0,
             "google ads conversion labels": 1, "experiment / variant names": 2,
             "plan / SKU / pricing": 3, "segmentation bands": 4,
             "numeric-codes": 5, "uuid-map (per-market vendor/script IDs)": 6,
             "locale / geo routing": 7, "other": 8}
    tables.sort(key=lambda t: (order.get(t["classification"], 9), -t["row_count"]))
    return tables


def identify_template(template_id, permissions):
    """Fingerprint a __cvt_ community template from the permissions it requests.

    Template names aren't published, but a template that asks to touch the global
    `fbq` or send a pixel to `analytics.tiktok.com` has told you who it is.
    """
    perm = permissions.get(template_id) or {}
    hints = {"globals": [], "hosts": [], "cookies": [], "permissions": sorted(perm.keys())}
    blob = json.dumps(perm)
    for g in re.findall(r'"key"\s*,\s*"([A-Za-z_$][\w$.]{1,40})"', blob):
        hints["globals"].append(g)
    for h in re.findall(r'(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})/?', blob):
        if "." in h and not h.endswith((".js", ".com.js")):
            hints["hosts"].append(h)
    hints["globals"] = sorted(set(hints["globals"]))[:12]
    hints["hosts"] = sorted(set(hints["hosts"]))[:12]
    return hints


def summarise(data, raw_src=""):
    """Turn a parsed container into report-ready facts."""
    res = data.get("resource", {})
    macros = res.get("macros", [])
    tags = res.get("tags", [])
    predicates = res.get("predicates", [])
    rules = res.get("rules", [])
    permissions = data.get("permissions", {})

    out = {
        "container_version": res.get("version"),
        "counts": {
            "tags": len(tags), "variables": len(macros),
            "triggers_conditions": len(predicates), "trigger_rules": len(rules),
        },
        "tag_types": [], "ga4_events": [], "google_ads_conversions": [],
        "floodlight": [], "custom_templates": [], "custom_html_hosts": [],
        "ids_seen": {}, "server_side": {}, "consent": {},
        "zones": [], "notable_variables": [], "warnings": [],
    }

    # --- tag type census -------------------------------------------------
    census = Counter()
    for t in tags:
        fn = t.get("function", "?")
        label = TAG_TYPES.get(fn, (None, None))[0] or f"Unidentified template ({fn})"
        census[(fn, label)] += 1
    out["tag_types"] = [
        {"function": fn, "type": label, "count": n}
        for (fn, label), n in census.most_common()
    ]

    # --- GA4 events ------------------------------------------------------
    ga4 = []
    for t in tags:
        if t.get("function") != "__gaawe":
            continue
        name = t.get("vtp_eventName")
        entry = {
            "event_name": name if isinstance(name, str) else describe_value(name, macros)["repr"],
            "dynamic_name": not isinstance(name, str),
            "measurement_id": describe_value(t.get("vtp_measurementIdOverride"), macros)["repr"]
                              if t.get("vtp_measurementIdOverride") is not None else None,
            "parameters": [p["param"] for p in flatten_params(t.get("vtp_eventSettingsTable"), macros)],
            "sends_ecommerce": bool(t.get("vtp_sendEcommerceData")),
            "tag_id": t.get("tag_id"),
        }
        ga4.append(entry)
    out["ga4_events"] = sorted(ga4, key=lambda e: str(e["event_name"]))

    # --- Google Ads conversions -----------------------------------------
    # Distinct conversionId = Ads account. Distinct conversionLabel = conversion
    # ACTION. Counting "AW-" occurrences conflates the two and overstates nothing
    # useful -- the label count is the number of things being optimised toward.
    convs = []
    for t in tags:
        if t.get("function") != "__awct":
            continue
        val = t.get("vtp_conversionValue")
        convs.append({
            "conversion_id": t.get("vtp_conversionId"),
            "conversion_label": t.get("vtp_conversionLabel"),
            "value": describe_value(val, macros) if val is not None else None,
            "currency": describe_value(t.get("vtp_currencyCode"), macros)["repr"]
                        if t.get("vtp_currencyCode") is not None else None,
            "has_order_id": t.get("vtp_orderId") is not None,
            "enhanced_conversions": bool(t.get("vtp_enableEnhancedConversions")
                                         or t.get("vtp_enableEnhancedConversionsLoad")),
            "tag_id": t.get("tag_id"),
        })
    out["google_ads_conversions"] = convs
    accounts = sorted({str(c["conversion_id"]) for c in convs if c["conversion_id"]})
    labels = sorted({str(c["conversion_label"]) for c in convs if c["conversion_label"]})
    # Labels also travel through the dataLayer or lookup tables in server-side and
    # GA4-routed setups, where no __awct tag exists at all. Scanning the whole file
    # for the label shape catches those; without it you can badly undercount a
    # competitor's optimisation surface.
    hay_all = raw_src or json.dumps(data)
    label_strings = sorted({s for s in re.findall(r'"([A-Za-z0-9_-]{17,24})"', hay_all)
                            if looks_like_ads_label(s)})
    # A label-shaped string is not proof of an Ads conversion action: on one live
    # container the extra "label" was an OpenAI ads pixel ID. Only count the ones
    # attached to an __awct tag or to an Ads-specific parameter, and report the
    # rest as unattributed so nobody folds a third-party pixel into the
    # optimisation-surface count.
    ads_context = set(labels)

    # 1. Labels sitting directly on an Ads tag or an Ads-named parameter.
    for t in tags:
        blob = json.dumps(t)
        if t.get("function") == "__awct" or re.search(
                r"(google_ads_conversion_label|conversionLabel|awct)", blob, re.I):
            for s_ in re.findall(r'"([A-Za-z0-9_-]{17,24})"', blob):
                if looks_like_ads_label(s_):
                    ads_context.add(s_)

    # 2. Follow the macro an Ads tag reads its label FROM. A conversion label is
    # very often supplied by a lookup table keyed on product or page, and that
    # table's own JSON contains no Ads-related word -- so matching on vocabulary
    # alone marked 27 real labels on one live container as unattributed, which is
    # the exact undercount this attribution was added to prevent.
    label_macro_idxs = set()
    for t in tags:
        for field in ("vtp_conversionLabel", "vtp_conversionId"):
            v = t.get(field)
            if _is_macro_ref(v) and isinstance(v[1], int):
                label_macro_idxs.add(v[1])
        if re.search(r"(google_ads_conversion_label|conversionLabel)", json.dumps(t), re.I):
            for v in re.findall(r'\["macro",\s*(\d+)\]', json.dumps(t)):
                label_macro_idxs.add(int(v))
    for idx in label_macro_idxs:
        if 0 <= idx < len(macros):
            for s_ in re.findall(r'"([A-Za-z0-9_-]{17,24})"', json.dumps(macros[idx])):
                if looks_like_ads_label(s_):
                    ads_context.add(s_)

    # 3. Account signature. Google Ads labels carry an account-derived trailing
    # segment, so a label-shaped string sharing the tail of a confirmed label is
    # from the same account. Only applied when there is a confirmed label to
    # learn the tail from, and reported separately so it stays auditable.
    by_signature = set()
    tails = {l[-8:] for l in ads_context if len(l) >= 12}
    if tails:
        for s_ in label_strings:
            if s_ not in ads_context and any(s_.endswith(t) for t in tails):
                by_signature.add(s_)
    ads_context |= by_signature
    attached = sorted(ads_context)
    unattributed = sorted(set(label_strings) - ads_context)
    out["google_ads_summary"] = {
        "ads_accounts": accounts,
        "conversion_tags": len(convs),
        "conversion_actions_in_awct_tags": len(labels),
        "labels_attached_to_ads_context": len(attached),
        "matched_by_account_signature": sorted(by_signature)[:40],
        "label_shaped_strings_in_file": len(label_strings),
        "labels": attached[:80],
        "unattributed_label_shaped_strings": unattributed[:30],
        "unattributed_note": ("label-shaped but not tied to any Ads tag or Ads parameter -- "
                              "could be another vendor's pixel ID; verify before counting"),
        "literal_values_present": sorted({
            str(c["value"]["repr"]) for c in convs
            if c["value"] and not c["value"]["runtime"]
        }),
    }

    # --- Floodlight -------------------------------------------------------
    for t in tags:
        if t.get("function") in ("__flc", "__fls"):
            out["floodlight"].append({
                "kind": "counter" if t.get("function") == "__flc" else "sales",
                "advertiser_id": t.get("vtp_advertiserId"),
                "group_tag": t.get("vtp_groupTag"),
                "activity_tag": t.get("vtp_activityTag"),
            })

    # --- community templates ---------------------------------------------
    cvt_counts = Counter(t.get("function") for t in tags
                         if str(t.get("function", "")).startswith("__cvt_"))
    for tid, n in cvt_counts.most_common():
        out["custom_templates"].append({
            "template_id": tid, "tag_count": n, "fingerprint": identify_template(tid, permissions),
        })

    # --- Custom HTML: which third-party hosts it pulls in -----------------
    html_hosts = Counter()
    for t in tags:
        if t.get("function") != "__html":
            continue
        body = t.get("vtp_html")
        if isinstance(body, list):
            body = json.dumps(body)
        if not isinstance(body, str):
            continue
        for h in re.findall(r"https?://([a-z0-9.-]+\.[a-z]{2,})", body, re.I):
            html_hosts[h.lower()] += 1
    out["custom_html_hosts"] = [{"host": h, "references": n} for h, n in html_hosts.most_common(40)]

    # --- vendor account IDs (keys to other vendors' public endpoints) -----
    out["vendor_account_ids"] = extract_vendor_account_ids(raw_src or json.dumps(data))

    # --- raw IDs anywhere in the file ------------------------------------
    hay = raw_src or json.dumps(data)
    for name, pat in ID_PATTERNS.items():
        found = sorted(set(re.findall(pat, hay)))
        if found:
            out["ids_seen"][name] = found

    # --- server-side GTM --------------------------------------------------
    transport = sorted(set(re.findall(r'"(?:transport_url|vtp_serverContainerUrl)"\s*:\s*"([^"]+)"', hay)))
    if transport:
        out["server_side"]["transport_urls"] = transport
    fp = sorted(set(re.findall(r'"vtp_firstPartyCollectionDomain"\s*:\s*"([^"]+)"', hay)))
    if fp:
        out["server_side"]["first_party_domains"] = fp

    # --- consent mode -----------------------------------------------------
    # A tag declares its consent requirements in a bare top-level `consent` key
    # (e.g. {"consent": ["list", "ad_storage"]}), NOT under metadata and usually
    # not as a vtp_ field. Looking in the wrong place reported 0 consent-aware
    # tags on a container where 74 tags are gated, which inverts the finding:
    # "no consent enforcement" instead of "consent enforced on most of the stack".
    consent_required = Counter()
    consent_tags = 0
    for t in tags:
        decl = t.get("consent")
        vtp_decl = [k for k in t if k.startswith("vtp_consent")]
        if decl or vtp_decl:
            consent_tags += 1
        if isinstance(decl, list):
            for item in decl[1:] if decl and decl[0] == "list" else decl:
                if isinstance(item, str):
                    consent_required[item] += 1

    # A bare vendor name can arrive from somewhere unrelated -- Google's own
    # auto-redaction regex mentions CMP names -- so require host-like evidence.
    cmp_names = ("onetrust", "cookiebot", "cookiehub", "trustarc", "usercentrics",
                 "osano", "ketch", "didomi", "iubenda", "termly", "sourcepoint",
                 "quantcast", "consentmanager", "klaro", "axeptio", "secureprivacy")
    cmp_hits = {}
    for name in cmp_names:
        host = re.search(name + r"[a-z0-9-]*\.[a-z]{2,6}\b", hay, re.I)
        bare = re.search(r"\b" + name + r"\b", hay, re.I)
        if host:
            cmp_hits[name] = "host reference (strong)"
        elif bare:
            cmp_hits[name] = "name only (weak -- may be an unrelated mention)"
    out["consent"] = {
        "consent_aware_tags": consent_tags,
        "total_tags": len(tags),
        "consent_types_required": dict(consent_required.most_common()),
        "ad_storage_referenced": "ad_storage" in hay,
        "analytics_storage_referenced": "analytics_storage" in hay,
        "cmp_hints": cmp_hits,
        "default_consent_regions": sorted(set(re.findall(
            r'"vtp_regions?"\s*:\s*"([A-Z]{2}(?:-[A-Z0-9]{1,3})?)"', hay)))[:40],
    }

    # --- zones (other containers loaded conditionally) --------------------
    for t in tags:
        if t.get("function") == "__zone":
            out["zones"].append({"tag_ids": t.get("vtp_tagIds"), "child_containers": t.get("vtp_childContainers")})

    # --- lookup tables: usually the highest-value content in the container --
    out["lookup_tables"] = extract_lookup_tables(macros)

    # --- trigger resolution: what each tag actually waits for ---------------
    triggers = resolve_triggers(res)
    out["gating_groups"] = gating_groups(triggers, tags)
    for e in out["ga4_events"]:
        idx = next((i for i, t in enumerate(tags)
                    if t.get("tag_id") == e.get("tag_id")), None)
        if idx is not None and idx in triggers:
            e["fires_when"] = [condition_text(c) for c in triggers[idx]["fires_when"]][:4]
    for c in out["google_ads_conversions"]:
        idx = next((i for i, t in enumerate(tags)
                    if t.get("tag_id") == c.get("tag_id")), None)
        if idx is not None and idx in triggers:
            c["fires_when"] = [condition_text(x) for x in triggers[idx]["fires_when"]][:4]

    # --- notable variables: dataLayer keys reveal their data model --------
    dl_names = sorted({m.get("vtp_name") for m in macros
                       if m.get("function") == "__v" and isinstance(m.get("vtp_name"), str)})
    out["notable_variables"] = {
        "data_layer_keys": dl_names,
        "lookup_tables": sum(1 for m in macros if m.get("function") in ("__smm", "__remm")),
        "custom_js_variables": sum(1 for m in macros if m.get("function") == "__jsm"),
        "cookie_variables": sorted({m.get("vtp_name") for m in macros
                                    if m.get("function") == "__k" and isinstance(m.get("vtp_name"), str)}),
    }

    # --- trigger conditions: what user behaviour fires things -------------
    events_in_predicates = Counter()
    for p in predicates:
        blob = json.dumps(p)
        for ev in re.findall(r'"([a-z][a-z0-9_]{2,40})"', blob):
            if ev not in ("arg0", "arg1", "function", "_eq", "_cn", "_re", "_sw", "_ew", "_lt", "_gt", "macro"):
                events_in_predicates[ev] += 1
    out["trigger_event_hints"] = [e for e, _ in events_in_predicates.most_common(40)]

    # --- hygiene warnings -------------------------------------------------
    fired = set()
    for r in rules:
        for clause in r:
            if isinstance(clause, list) and clause and clause[0] == "add":
                fired.update(x for x in clause[1:] if isinstance(x, int))
    orphan = len(tags) - len(fired)
    if orphan > 0:
        out["warnings"].append(
            f"{orphan} of {len(tags)} tags are not referenced by any firing rule "
            f"(fired by another tag's setup/teardown, or dead weight)")
    if len(labels) > 12:
        out["warnings"].append(
            f"{len(labels)} distinct Google Ads conversion actions -- a wide optimisation "
            f"surface; worth asking which one their bidding actually targets")
    return out


def analyse(target, timeout=30):
    """Accept a container ID, a gtm.js URL, or a local file path."""
    if re.fullmatch(r"GTM-[A-Z0-9]{4,10}", target.strip(), re.I):
        url = f"https://www.googletagmanager.com/gtm.js?id={target.strip().upper()}"
        src, status = fetch(url, timeout)
        source = url
    elif target.startswith("http"):
        src, status = fetch(target, timeout)
        source = target
    else:
        with open(target, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        status, source = 200, target
    data = extract_data_object(src)
    summary = summarise(data, src)
    summary["_source"] = source
    summary["_bytes"] = len(src)
    summary["_http_status"] = status

    # Google answers an unknown container ID with HTTP 200 and an *empty*
    # container rather than a 404. Without this check a typo reads as a real
    # company with no tracking, which is a badly wrong finding to report.
    if summary["counts"]["tags"] == 0 and summary["counts"]["variables"] == 0:
        summary["warnings"].insert(0, (
            "EMPTY CONTAINER: 0 tags and 0 variables. Google returns HTTP 200 with an "
            "empty container for IDs that do not exist or were never published, so treat "
            "this as 'container not found' unless the ID is confirmed from the page source. "
            "Do not report it as a company that does no tracking."))
    if "/gtag/js" in source:
        summary["warnings"].insert(0, (
            "This is a gtag/js file, not a GTM container. It parses because gtag ships a "
            "similar structure, but it describes one Google tag's own configuration -- not a "
            "tag manager container. Counts here are not comparable to a GTM-XXXXXXX read."))
    return summary


def render(s):
    L = []
    a = L.append
    a(f"# GTM container  ({s['_source']})")
    a(f"version {s['container_version']}   {s['_bytes']:,} bytes")
    c = s["counts"]
    a(f"{c['tags']} tags · {c['variables']} variables · {c['trigger_rules']} trigger rules\n")
    # Surface disqualifying warnings before the data, so an empty or wrong-file
    # read can't be mistaken for a real finding.
    for w in s["warnings"]:
        if w.startswith(("EMPTY CONTAINER", "This is a gtag/js")):
            a(f"!! {w}\n")

    a("## Tag types")
    for t in s["tag_types"]:
        a(f"  {t['count']:>4}  {t['type']}  [{t['function']}]")

    g = s["google_ads_summary"]
    a("\n## Google Ads")
    a(f"  accounts: {', '.join(g['ads_accounts']) or 'none'}")
    a(f"  conversion actions in __awct tags: {g['conversion_actions_in_awct_tags']}  (from {g['conversion_tags']} tags)")
    a(f"  labels tied to an Ads tag/parameter: {g['labels_attached_to_ads_context']}"
      f"   <- the real optimisation surface if they route conversions via GA4/server-side")
    if g["labels"]:
        a(f"  labels: {', '.join(g['labels'][:20])}")
    if g.get("unattributed_label_shaped_strings"):
        a(f"  UNATTRIBUTED label-shaped strings ({len(g['unattributed_label_shaped_strings'])}): "
          f"{', '.join(g['unattributed_label_shaped_strings'][:10])}")
        a(f"    {g['unattributed_note']}")
    a(f"  hardcoded conversion values on tags: {', '.join(g['literal_values_present']) or 'none (runtime dataLayer; check lookup tables below)'}")

    if s.get("lookup_tables"):
        a(f"\n## Lookup tables ({len(s['lookup_tables'])}) - hand-typed by their marketers, highest-signal section")
        for t in s["lookup_tables"]:
            a(f"\n  [{t['classification']}]  macro[{t['macro_index']}] {t['type']}, {t['row_count']} rows")
            if t["input"]:
                a(f"    keyed on: {t['input']}")
            for k, v in t["rows"][:25]:
                a(f"      {str(k)[:70]:<70} -> {str(v)[:50]}")
            if t["truncated"] or t["row_count"] > 25:
                a(f"      ... {t['row_count'] - 25} more rows (use --json for all)")

    if s.get("gating_groups"):
        a("\n## Conditions gating several tags at once")
        a("   (a condition shared by many destination tags is a policy decision:")
        a("    their working definition of an event worth counting)")
        for grp in s["gating_groups"][:12]:
            a(f"\n  {grp['tag_count']} tags gated on:  {grp['condition']}")
            a(f"     {', '.join(f'{k} x{v}' for k, v in grp['tag_types'].items())}")

    if s["ga4_events"]:
        a(f"\n## GA4 events ({len(s['ga4_events'])})")
        for e in s["ga4_events"]:
            params = f"  params: {', '.join(e['parameters'][:6])}" if e["parameters"] else ""
            ecom = "  [ecommerce]" if e["sends_ecommerce"] else ""
            a(f"  {e['event_name']}{ecom}{params}")
            for w in e.get("fires_when", [])[:2]:
                a(f"      fires when: {w[:220]}")

    if s["google_ads_conversions"]:
        a("\n## Google Ads conversion tags and their triggers")
        for c in s["google_ads_conversions"][:30]:
            a(f"  id={c['conversion_id']} label={c['conversion_label']}")
            for w in c.get("fires_when", [])[:2]:
                a(f"      fires when: {w[:220]}")

    if s["custom_templates"]:
        a("\n## Community templates (names not published - fingerprinted from permissions)")
        for t in s["custom_templates"]:
            f = t["fingerprint"]
            a(f"  {t['template_id']}  x{t['tag_count']}")
            if f["globals"]:
                a(f"     globals: {', '.join(f['globals'])}")
            if f["hosts"]:
                a(f"     hosts:   {', '.join(f['hosts'])}")

    if s["custom_html_hosts"]:
        a("\n## Hosts referenced from Custom HTML")
        for h in s["custom_html_hosts"][:25]:
            a(f"  {h['references']:>3}  {h['host']}")

    if s.get("vendor_account_ids"):
        a("\n## Vendor account IDs (several unlock that vendor's public config endpoint)")
        for k, v in s["vendor_account_ids"].items():
            a(f"  {k}: {', '.join(v)}")

    if s["ids_seen"]:
        a("\n## IDs present in the file")
        for k, v in s["ids_seen"].items():
            a(f"  {k}: {', '.join(v)}")

    if s["server_side"]:
        a("\n## Server-side tagging")
        for k, v in s["server_side"].items():
            a(f"  {k}: {', '.join(v)}")

    a("\n## Consent")
    cs = s["consent"]
    a(f"  tags declaring consent requirements: {cs['consent_aware_tags']} of {cs['total_tags']}")
    if cs.get("consent_types_required"):
        a(f"  types required: {', '.join(f'{k} ({v} tags)' for k, v in cs['consent_types_required'].items())}")
    if cs.get("cmp_hints"):
        a("  CMP:")
        for name, how in cs["cmp_hints"].items():
            a(f"    {name}: {how}")
    else:
        a("  CMP: none detected in this container")
    if cs.get("default_consent_regions"):
        a(f"  regions with explicit consent defaults: {', '.join(cs['default_consent_regions'])}")

    nv = s["notable_variables"]
    if nv["data_layer_keys"]:
        a(f"\n## dataLayer keys ({len(nv['data_layer_keys'])}) - their data model")
        a("  " + ", ".join(nv["data_layer_keys"][:60]))
    if nv["cookie_variables"]:
        a(f"  cookies read: {', '.join(nv['cookie_variables'])}")

    if s["trigger_event_hints"]:
        a("\n## Strings appearing in trigger conditions")
        a("  " + ", ".join(s["trigger_event_hints"][:40]))

    if s["warnings"]:
        a("\n## Notes")
        for w in s["warnings"]:
            a(f"  - {w}")
    return "\n".join(L)


def main():
    # An explicit --help is a request that succeeded, so it exits 0. Running with
    # no arguments at all is a usage error and still exits 1.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    try:
        s = analyse(args[0])
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} fetching container -- the ID may be wrong, unpublished, "
              f"or Google may be rate-limiting. Retry, or paste a saved gtm.js path.", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"could not parse: {e}", file=sys.stderr)
        return 2
    print(json.dumps(s, indent=2, default=str) if "--json" in sys.argv else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
