#!/usr/bin/env python3
"""Compare several sites' public growth setups, so each number has a baseline.

A single-site report says "364 tags" and "27 conversion actions". No reader can
tell whether either of those is a lot, because there is nothing to read them
against. This script collects several targets with the same collector and puts
the same measurements side by side, which is what makes any one of them mean
something.

Usage:
    python3 compare.py asana.com monday.com clickup.com
    python3 compare.py a.com b.com c.com --json cmp.json
    python3 compare.py a.com b.com c.com --fast        # skip container parsing
    python3 compare.py --regional monday.com           # one company's variants
    python3 compare.py a.com b.com --pages /pricing,/demo

Two modes:

  default      several companies, measured the same way and ranked by what
               separates them.

  --regional   one company's regional (ccTLD) and functional (subdomain)
               variants, reporting which of them serve a *different* tag
               container. Different markets and the signup app routinely run
               their own containers with their own conversion actions, so a
               homepage-only read understates the setup.

Output leads with what differentiates the targets. Per-target inventories come
last, because an inventory of one target is what recon.py already prints.
"""

import json
import os
import re
import socket
import statistics
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recon import collect, fetch_full, fingerprint, norm_url  # noqa: E402
from gtm_parse import analyse as analyse_container  # noqa: E402

# recon.collect() parses every container it finds, so one target is tens of
# requests and several seconds of CPU. Targets run concurrently to make a
# multi-target run bearable, but the pool stays small: each worker is a separate
# host seeing an ordinary visitor's request pattern, and a wide pool would turn
# a comparison into something that looks like a scan.
DEFAULT_WORKERS = 3
MAX_WORKERS = 4

# A median over three numbers is just the middle one. Below this many targets,
# say that in the output instead of dressing it up as a benchmark.
MIN_FOR_STATS = 4

# The measurements compared across targets. Order is the fallback reading order;
# the lead section re-sorts by how much each one actually separates the set.
# (key, long label, unit, short label for one-line summaries)
METRICS = [
    ("containers", "GTM containers served", "count", "containers"),
    ("tags", "tags across those containers", "count", "tags"),
    ("ga4_events", "distinct GA4 event names", "count", "GA4 events"),
    ("ads_accounts", "Google Ads accounts", "count", "Ads accounts"),
    ("conversion_actions", "distinct Ads conversion actions", "count", "conversion actions"),
    ("consent_share_pct", "tags declaring consent requirements", "pct", "consent share"),
    ("vendors", "vendors with load evidence", "count", "vendors"),
    ("lookup_tables", "lookup tables in containers", "count", "lookup tables"),
    ("sitemap_urls", "URLs in sitemap", "count", "sitemap URLs"),
]

# Measurements that only exist once a container has been parsed, so --fast can
# explain their absence in one line instead of flagging every target.
CONTAINER_METRICS = {"tags", "ga4_events", "ads_accounts", "conversion_actions",
                     "consent_share_pct", "lookup_tables"}

# Regional forms a brand actually registers, including the two-part public
# suffixes. Every candidate is DNS-gated before any HTTP request, so this list
# costs resolver lookups rather than requests to the company's web servers.
REGIONAL_SUFFIXES = [
    "co.uk", "uk", "de", "fr", "es", "it", "nl", "be", "ch", "at", "se", "no",
    "dk", "fi", "pl", "cz", "pt", "ie", "gr", "tr", "com.tr", "ru", "com.br",
    "mx", "com.mx", "com.ar", "cl", "co", "ca", "com.au", "co.nz", "co.jp",
    "jp", "co.kr", "kr", "cn", "com.cn", "tw", "com.tw", "hk", "sg", "com.sg",
    "in", "co.in", "id", "co.id", "co.th", "vn", "ph", "my", "com.my", "ae",
    "sa", "il", "co.il", "co.za", "eu",
]

# Functional subdomains worth testing when the collected pages do not already
# name them. The signup/auth host is the one that matters most: it is where the
# conversion actually happens, and it is frequently a different container.
LIKELY_SUBDOMAINS = [
    "app", "auth", "login", "signup", "start", "try", "go", "get", "my",
    "account", "shop", "store", "help", "support", "docs", "blog", "learn",
    "community", "partners", "developers", "status", "careers", "jobs",
]


# ---------------------------------------------------------------------------
# domain shapes
# ---------------------------------------------------------------------------

TWO_PART = {"co", "com", "org", "net", "gov", "edu", "ac"}


def split_host(host):
    """Return (apex, brand, suffix) for a hostname.

    Needed because a regional sweep has to swap the suffix while keeping the
    brand, and the naive "second label from the right" is wrong for the two-part
    public suffixes a sweep is specifically looking for: example.co.uk's brand
    is example, not co.
    """
    labels = [l for l in host.lower().strip(".").replace("www.", "").split(".") if l]
    if len(labels) >= 3 and labels[-2] in TWO_PART and len(labels[-1]) == 2:
        return ".".join(labels[-3:]), labels[-3], ".".join(labels[-2:])
    if len(labels) >= 2:
        return ".".join(labels[-2:]), labels[-2], labels[-1]
    return host, host, ""


def host_of(url_or_host):
    u = norm_url(url_or_host)
    return urllib.parse.urlparse(u).netloc.lower()


def resolves(host):
    """Does this hostname exist at all?

    The gate that keeps a regional sweep polite. Most brand.ccTLD guesses do not
    exist, and resolving them asks a DNS resolver rather than knocking on fifty
    web servers that were never there.
    """
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# profiles: one comparable row per target
# ---------------------------------------------------------------------------

def _container_rollup(containers):
    """Fold several parsed containers into one set of per-target numbers.

    Union rather than sum for the identity-like things: two containers on one
    site routinely declare the same GA4 events and the same Ads account, and
    summing those would reward duplication as if it were coverage.
    """
    tags = consent_tags = consent_total = lookups = 0
    ga4, ga4_dynamic, labels, accounts = set(), set(), set(), set()
    per_container, parsed = {}, 0
    for gid, c in containers.items():
        if not isinstance(c, dict) or c.get("error"):
            per_container[gid] = None
            continue
        parsed += 1
        n = (c.get("counts") or {}).get("tags", 0)
        per_container[gid] = n
        tags += n
        # An event whose name is built at runtime has no name to compare. Its
        # placeholder ("<Event name>", or a raw macro expression) would otherwise
        # show up as vocabulary the targets share, which is the opposite of true.
        for e in c.get("ga4_events", []):
            (ga4_dynamic if e.get("dynamic_name") else ga4).add(str(e.get("event_name")))
        g = c.get("google_ads_summary") or {}
        labels |= set(g.get("labels") or [])
        accounts |= set(g.get("ads_accounts") or [])
        cs = c.get("consent") or {}
        consent_tags += cs.get("consent_aware_tags", 0)
        consent_total += cs.get("total_tags", 0)
        lookups += len(c.get("lookup_tables") or [])
    return {
        "parsed": parsed,
        "per_container_tags": per_container,
        "tags": tags,
        "ga4_events": sorted(ga4),
        "ga4_dynamic_events": sorted(ga4_dynamic),
        "conversion_actions": sorted(labels),
        "ads_accounts": sorted(accounts),
        "consent_share_pct": (100.0 * consent_tags / consent_total) if consent_total else None,
        "lookup_tables": lookups,
    }


def profile_from_collection(label, r, do_containers=True):
    """Build the comparable row from a full recon.collect() result."""
    all_containers = r.get("gtm_containers") or {}
    containers = {g: c for g, c in all_containers.items() if isinstance(c, dict)}
    roll = _container_rollup(containers)
    for gid in all_containers:
        roll["per_container_tags"].setdefault(gid, None)
    strong = {k for k, v in (r.get("fingerprints") or {}).items()
              if v.get("evidence") != "weak"}
    sitemap = (r.get("sitemap") or {}).get("total_urls")

    prof = {
        "label": label,
        "ok": True,
        "depth": "full collection",
        "final_url": r.get("final_url"),
        "http_status": r.get("http_status"),
        "containers": sorted((r.get("gtm_containers") or {}).keys()),
        "per_container_tags": roll["per_container_tags"],
        "vendors": sorted(strong),
        "ga4_event_names": roll["ga4_events"],
        "ga4_runtime_named_events": len(roll["ga4_dynamic_events"]),
        "conversion_action_labels": roll["conversion_actions"],
        "ads_account_ids": roll["ads_accounts"],
        "sibling_hosts": (r.get("container_discovery") or {}).get("sibling_hosts", []),
        "notes": list(r.get("collection_notes") or []),
        "metrics": {
            "containers": len(r.get("gtm_containers") or {}),
            "tags": roll["tags"] if do_containers and roll["parsed"] else None,
            "ga4_events": (len(roll["ga4_events"]) + len(roll["ga4_dynamic_events"])
                           if do_containers and roll["parsed"] else None),
            "ads_accounts": len(roll["ads_accounts"]) if do_containers and roll["parsed"] else None,
            "conversion_actions": (len(roll["conversion_actions"])
                                   if do_containers and roll["parsed"] else None),
            "consent_share_pct": roll["consent_share_pct"],
            "vendors": len(strong),
            "lookup_tables": roll["lookup_tables"] if do_containers and roll["parsed"] else None,
            "sitemap_urls": sitemap,
        },
    }
    google_direct = [k for k in ("Google Ads", "GA4 measurement ID", "Google Tag ID")
                     if k in (r.get("fingerprints") or {})]
    prof["tags_without_a_container"] = bool(google_direct) and not all_containers
    prof["google_tags_in_html"] = google_direct

    ident = r.get("identity") or {}
    if ident.get("warning"):
        prof["identity_warning"] = ident["warning"]
    if not r.get("html_bytes"):
        prof["ok"] = False
        prof["gap"] = f"no HTML served (HTTP {r.get('http_status')})"
    return prof


def gap_profile(label, reason):
    """A target that failed is a gap in the comparison, not a zero.

    Reporting a timeout as 0 tags would quietly drag every statistic down and
    read as a company with no tracking, which is the wrong finding entirely.
    """
    return {"label": label, "ok": False, "gap": reason, "depth": "failed",
            "containers": [], "vendors": [], "ga4_event_names": [],
            "conversion_action_labels": [], "ads_account_ids": [], "notes": [],
            "metrics": {k: None for k, _, _, _ in METRICS}}


def collect_one(target, pages, do_containers):
    try:
        r = collect(target, pages, do_containers=do_containers)
    except Exception as e:
        return gap_profile(target, f"collection failed: {type(e).__name__}: {str(e)[:160]}")
    return profile_from_collection(target, r, do_containers)


def collect_many(targets, pages=(), do_containers=True, workers=DEFAULT_WORKERS):
    workers = max(1, min(workers, MAX_WORKERS, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda t: collect_one(t, pages, do_containers), targets))


# ---------------------------------------------------------------------------
# cross-target statistics
# ---------------------------------------------------------------------------

def fmt(v, kind):
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v:.0f}%"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}"


def stat_block(profiles):
    """min / median / max per metric, with who sits at each end.

    The whole point of the feature: a number is only large or small relative to
    the set, so every metric carries the names of the targets holding the
    extremes rather than bare figures.
    """
    out = {}
    for key, label, kind, short in METRICS:
        pairs = [(p["label"], p["metrics"].get(key)) for p in profiles
                 if p.get("ok") and p["metrics"].get(key) is not None]
        if len(pairs) < 2:
            continue
        vals = [v for _, v in pairs]
        lo, hi = min(vals), max(vals)
        mid = statistics.median(vals)
        # Relative spread, so a metric measured in thousands (sitemap URLs)
        # doesn't automatically outrank one measured in tens (conversion
        # actions) when ranking what actually differentiates the set.
        denom = mid if mid else (hi if hi else 1)
        out[key] = {
            "label": label, "short": short, "kind": kind, "n": len(pairs),
            "min": lo, "median": mid, "max": hi,
            "spread": (hi - lo) / denom,
            "holders_max": sorted(n for n, v in pairs if v == hi),
            "holders_min": sorted(n for n, v in pairs if v == lo),
            "values": dict(pairs),
        }
    return out


def outlier_summary(profiles, stats):
    """Per target, the metrics where it alone holds an extreme.

    This is the read a single-site report cannot produce: not "27 conversion
    actions" but "more conversion actions than anyone else here".
    """
    summary = {}
    for p in profiles:
        if not p.get("ok"):
            continue
        rows = []
        for key, s in stats.items():
            if s["max"] == s["min"]:
                continue
            if s["holders_max"] == [p["label"]]:
                rows.append(("highest", key, s))
            elif s["holders_min"] == [p["label"]]:
                rows.append(("lowest", key, s))
        summary[p["label"]] = rows
    return summary


def vendor_sets(profiles):
    ok = [p for p in profiles if p.get("ok")]
    if not ok:
        return {"common": [], "unique": {}, "spread": []}
    sets = {p["label"]: set(p["vendors"]) for p in ok}
    common = set.intersection(*sets.values()) if sets else set()
    unique = {}
    for name, s in sets.items():
        others = set.union(*[v for k, v in sets.items() if k != name]) if len(sets) > 1 else set()
        unique[name] = sorted(s - others)
    # Vendors held by some but not all: the choices that are contested rather
    # than either table stakes or one company's quirk.
    allv = set.union(*sets.values())
    contested = sorted(v for v in allv - common
                       if not any(v in u for u in unique.values()))
    return {"common": sorted(common), "unique": unique, "contested": contested}


# ---------------------------------------------------------------------------
# regional / functional variant sweep
# ---------------------------------------------------------------------------

def variant_candidates(apex, brand, suffix, discovered):
    """ccTLD forms of the brand plus functional subdomains of the apex."""
    cands = []
    for s in REGIONAL_SUFFIXES:
        if s == suffix:
            continue
        cands.append((f"{brand}.{s}", "regional ccTLD"))
    for h in discovered:
        cands.append((h.lower(), "subdomain seen in the collected pages"))
    for p in LIKELY_SUBDOMAINS:
        cands.append((f"{p}.{apex}", "likely functional subdomain"))
    seen, out = set(), []
    for h, why in cands:
        h = h.lower().strip(".")
        if h in seen or h == apex or h == "www." + apex:
            continue
        seen.add(h)
        out.append({"host": h, "why": why})
    return out


def probe_variant(v, base_apex, brand, base_ids=()):
    """One request per variant host: enough to read its container IDs.

    Deliberately not a full collect(). A sweep touches many hosts, so each gets
    a single homepage request like any visitor, and only the containers that
    turn out to be new are parsed afterwards.
    """
    host = v["host"]
    url = "https://" + host
    r = fetch_full(url, timeout=18)
    html = r.get("body") or ""
    final_host = urllib.parse.urlparse(r.get("url") or url).netloc.lower()
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    out = dict(v)
    out.update({
        "status": r.get("status"),
        "final_host": final_host,
        "bytes": len(html),
        "title": (title.group(1).strip()[:120] if title else None),
        "containers": sorted(set(re.findall(r"GTM-[A-Z0-9]{4,10}", html))),
        "ga4_ids": sorted(set(re.findall(r"G-[A-Z0-9]{8,12}", html))),
        "vendors": sorted(k for k, d in fingerprint(html).items()
                          if d.get("evidence") != "weak"),
    })
    out["redirects_into_base"] = bool(final_host) and final_host.replace("www.", "").endswith(base_apex)

    # A brand's ccTLD very often belongs to somebody else: on a live sweep,
    # monday.com.br served a Brazilian ecommerce tool and monday.pt a design
    # studio, each with its own container. Attributing those to the company
    # would be the worst failure available here, and the brand name appearing on
    # the page proves nothing -- it is in the hostname. So require positive
    # evidence tying the variant to the base company, and park everything else
    # as unconfirmed instead of counting it.
    ev = []
    if host == base_apex or host.endswith("." + base_apex):
        ev.append("on the base domain")
    if out["redirects_into_base"]:
        ev.append("redirects into the base host")
    if re.search(r"//(?:[a-z0-9-]+\.)*" + re.escape(base_apex) + r"\b", html, re.I):
        ev.append("its page references the base domain")
    shared = sorted((set(out["containers"]) | set(out["ga4_ids"])) & set(base_ids))
    if shared:
        ev.append("shares " + ", ".join(shared) + " with the base site")
    # No page means no evidence either way, so an unreachable host is a gap --
    # not a verdict that somebody else owns it.
    out["served_a_page"] = r.get("status") == 200 and bool(html.strip())
    out["same_company_evidence"] = ev
    out["same_company"] = bool(ev)
    hay = ((out["title"] or "") + " " + html[:4000]).lower()
    out["brand_on_page"] = brand in hay
    return out


def sweep_regional(target, pages=(), do_containers=True, workers=DEFAULT_WORKERS):
    base_host = host_of(target)
    apex, brand, suffix = split_host(base_host)

    base_raw = collect(target, pages, do_containers=do_containers)
    base = profile_from_collection(apex, base_raw, do_containers)
    base_containers = set(base["containers"])

    discovered = [h for h in base.get("sibling_hosts", [])
                  if h.lower().endswith(apex)]
    cands = variant_candidates(apex, brand, suffix, discovered)

    # DNS first, HTTP second. Resolving the whole candidate list is cheap and
    # asks nothing of the company's web servers; without this gate a sweep would
    # fire dozens of requests at hosts that mostly do not exist.
    with ThreadPoolExecutor(max_workers=8) as ex:
        live = list(ex.map(lambda c: (c, resolves(c["host"])), cands))
    reachable = [c for c, ok in live if ok]
    skipped = [c for c, ok in live if not ok]

    # The base site's own IDs: a variant reusing one of these is the same company.
    base_ids = sorted(base_containers | set(base_raw.get("gtag") or {}))
    workers = max(1, min(workers, MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        probes = list(ex.map(lambda c: probe_variant(c, apex, brand, base_ids), reachable))

    confirmed = [p for p in probes if p["same_company"]]
    unconfirmed = [p for p in probes if not p["same_company"] and p["served_a_page"]]
    unreachable = [p for p in probes if not p["same_company"] and not p["served_a_page"]]

    # Only confirmed variants' containers are fetched and counted. An unrelated
    # owner's container is not this company's measurement setup.
    new_ids = sorted({g for p in confirmed for g in p["containers"]} - base_containers)
    parsed = {}
    if do_containers and new_ids:
        def grab(gid):
            try:
                return gid, analyse_container(gid)
            except Exception as e:
                return gid, {"error": f"{type(e).__name__}: {str(e)[:140]}"}
        with ThreadPoolExecutor(max_workers=min(3, len(new_ids))) as ex:
            parsed = dict(ex.map(grab, new_ids))

    for p in probes:
        p["new_containers"] = [g for g in p["containers"] if g in new_ids]
        if not p["served_a_page"]:
            p["verdict"] = ("unreachable (connection or TLS failure)" if not p["status"]
                            else f"HTTP {p['status']}, nothing served")
        elif not p["same_company"]:
            p["verdict"] = "UNCONFIRMED owner -- not counted"
        elif p["new_containers"]:
            p["verdict"] = "DIFFERENT container"
        elif p["redirects_into_base"] and not p["containers"]:
            p["verdict"] = "redirects into the base host"
        elif p["containers"]:
            p["verdict"] = ("same container as base"
                            if set(p["containers"]) <= base_containers else "mixed")
        else:
            p["verdict"] = "theirs, but no container in the served HTML"

    # What the single-site read would have missed, stated as the delta rather
    # than as another inventory.
    roll_new = _container_rollup({g: c for g, c in parsed.items() if isinstance(c, dict)})
    delta = {
        "new_containers": new_ids,
        "new_conversion_actions": sorted(set(roll_new["conversion_actions"])
                                         - set(base["conversion_action_labels"])),
        "new_ga4_events": sorted(set(roll_new["ga4_events"])
                                 - set(base["ga4_event_names"])),
        "new_ads_accounts": sorted(set(roll_new["ads_accounts"])
                                   - set(base["ads_account_ids"])),
        "new_tags": roll_new["tags"],
    }

    # Variant profiles so the same cross-target statistics apply within one
    # company: a regional container with a tenth of the tags is the finding.
    profiles = [base]
    for p in confirmed:
        if not p["containers"]:
            continue
        own = {g: parsed[g] for g in p["containers"] if isinstance(parsed.get(g), dict)}
        if not own:
            continue
        roll = _container_rollup(own)
        profiles.append({
            "label": p["host"], "ok": True, "depth": "one page + its containers",
            "containers": p["containers"], "per_container_tags": roll["per_container_tags"],
            "vendors": p["vendors"], "ga4_event_names": roll["ga4_events"],
            "conversion_action_labels": roll["conversion_actions"],
            "ads_account_ids": roll["ads_accounts"], "notes": [],
            "metrics": {
                "containers": len(p["containers"]),
                "tags": roll["tags"] or None,
                "ga4_events": len(roll["ga4_events"]) or None,
                "ads_accounts": len(roll["ads_accounts"]) or None,
                "conversion_actions": len(roll["conversion_actions"]) or None,
                "consent_share_pct": roll["consent_share_pct"],
                "vendors": len(p["vendors"]),
                "lookup_tables": roll["lookup_tables"] or None,
                "sitemap_urls": None,
            },
        })

    return {
        "mode": "regional",
        "base": base,
        "apex": apex, "brand": brand,
        "candidates_tested": len(cands),
        "candidates_not_resolving": [c["host"] for c in skipped],
        "variants": sorted(probes, key=lambda p: (p["verdict"] != "DIFFERENT container", p["host"])),
        "unconfirmed_owner": sorted(p["host"] for p in unconfirmed),
        "unreachable": sorted(p["host"] for p in unreachable),
        "new_containers_parsed": parsed,
        "delta_vs_single_site": delta,
        "profiles": profiles,
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_stats(profiles, stats, outliers, kind_label="targets"):
    L, a = [], None
    a = L.append
    ok = [p for p in profiles if p.get("ok")]

    # One survivor is not a comparison. Say that once, rather than describing a
    # single site as mid-pack against itself.
    if len(ok) < 2:
        a(f"\n## No comparison possible")
        a(f"  {len(ok)} of {len(profiles)} {kind_label} produced comparable measurements, so")
        a("  there is nothing to read any number against. Fix or replace the failures")
        a("  listed below and re-run; a single profile is what recon.py already prints.")
        return L

    ranked = sorted(stats.values(), key=lambda s: -s["spread"])
    moving = [s for s in ranked if s["max"] != s["min"]]
    flat = [s for s in ranked if s["max"] == s["min"]]

    a(f"\n## What separates these {kind_label}  (widest spread first)")
    if not moving:
        a("  Nothing measured here separates them. Either the set is genuinely alike")
        a("  or the collection was too shallow -- check the gaps below.")
    for s in moving:
        a(f"\n  {s['label']}")
        a(f"    high  {fmt(s['max'], s['kind']):>8}   {', '.join(s['holders_max'])}")
        a(f"    low   {fmt(s['min'], s['kind']):>8}   {', '.join(s['holders_min'])}")
        a(f"    middle {fmt(s['median'], s['kind']):>7}   (median of {s['n']})")
    if flat:
        a("\n  Identical across the set, so they differentiate nothing here: "
          + ", ".join(s["label"] for s in flat))

    a(f"\n## Where each one sits against the set")
    for p in ok:
        rows = outliers.get(p["label"], [])
        if rows:
            bits = [f"{side} {s['short']} ({fmt(s['values'][p['label']], s['kind'])})"
                    for side, _, s in rows[:4]]
            a(f"  {p['label']:<26} {'; '.join(bits)}")
        else:
            a(f"  {p['label']:<26} no extreme on any measurement -- mid-pack throughout")

    a("\n## Reading a single number against the set")
    a("  (what a one-site report cannot tell you)")
    for key, s in sorted(stats.items(), key=lambda kv: -kv[1]["spread"])[:4]:
        for p in ok:
            v = s["values"].get(p["label"])
            if v is None:
                continue
            if v == s["max"] and s["max"] != s["min"]:
                rel = "the top of this set"
            elif v == s["min"] and s["max"] != s["min"]:
                rel = "the bottom of this set"
            elif s["median"] and v > s["median"]:
                rel = f"above the middle ({fmt(s['median'], s['kind'])})"
            elif s["median"] and v < s["median"]:
                rel = f"below the middle ({fmt(s['median'], s['kind'])})"
            else:
                rel = "the middle value itself"
            a(f"  {p['label']:<26} {fmt(v, s['kind']):>8}  {s['label']:<38} {rel}")
        a("")

    if len(ok) < MIN_FOR_STATS:
        a(f"  !! {len(ok)} comparable {kind_label} "
          f"{'is' if len(ok) == 1 else 'are'} too few for the median to be a benchmark.")
        a("     With two or three, the median is simply one of the numbers in the set, and")
        a("     'above the middle' means 'above one other site'. Treat the high/low names as")
        a(f"     the real output here; add targets up to {MIN_FOR_STATS}-6 before calling any of")
        a("     this a baseline.")
    return L


def render_compare(res):
    L, a = [], None
    a = L.append
    profiles = res["profiles"]
    ok = [p for p in profiles if p.get("ok")]
    stats, outliers = res["stats"], res["outliers"]
    skipped_containers = not res["containers_parsed"]

    a(f"# Multi-target comparison: {', '.join(p['label'] for p in profiles)}")
    a(f"{len(ok)} of {len(profiles)} collected; "
      f"{'containers parsed' if res['containers_parsed'] else 'containers SKIPPED (--fast)'}")

    for p in profiles:
        if p.get("identity_warning"):
            a("")
            a(f"!! {p['label']}: {p['identity_warning']}")

    L += render_stats(profiles, stats, outliers, "targets")

    v = res["vendors"]
    if len(ok) < 2:
        return "\n".join(L + ["", "## Gaps"] +
                          [f"  - {p['label']}: {p.get('gap')}" for p in profiles
                           if not p.get("ok")])
    a("\n## Stack: shared versus chosen")
    a(f"  loaded by all {len(ok)} ({len(v['common'])}) -- table stakes, not a differentiator:")
    a("    " + (", ".join(v["common"]) or "none"))
    if v.get("contested"):
        a(f"\n  some but not all ({len(v['contested'])}) -- the contested choices:")
        a("    " + ", ".join(v["contested"]))
    a("\n  unique to one target -- the deliberate bets:")
    for name, only in v["unique"].items():
        a(f"    {name:<26} {', '.join(only) if only else 'nothing of its own'}")

    a("\n## Funnel vocabulary")
    a("  (event names are written by whoever owns the funnel, so shared names mean a")
    a("   shared playbook and unique ones mean a stage the others do not measure)")
    namesets = {p["label"]: set(p["ga4_event_names"]) for p in ok if p["ga4_event_names"]}
    if len(namesets) >= 2:
        shared = set.intersection(*namesets.values())
        a(f"  named by all {len(namesets)} targets with a parsed container: "
          f"{', '.join(sorted(shared)[:25]) or 'nothing in common'}")
        for name, names in namesets.items():
            others = set.union(*[x for k, x in namesets.items() if k != name])
            only = sorted(names - others)
            a(f"  only {name}: {', '.join(only[:18]) or 'nothing of its own'}"
              + (f"  (+{len(only) - 18} more)" if len(only) > 18 else ""))
        runtime = {p["label"]: p.get("ga4_runtime_named_events", 0) for p in ok
                   if p.get("ga4_runtime_named_events")}
        if runtime:
            a("  events whose name is built at runtime, so there is no name to compare: "
              + ", ".join(f"{k} {v}" for k, v in runtime.items()))
    else:
        a("  fewer than two targets have a parsed container with named GA4 events, so")
        a("  there is no vocabulary to compare.")

    a("\n## Per-target inventory")
    for p in profiles:
        if not p.get("ok"):
            a(f"\n  {p['label']}  -- GAP: {p.get('gap')}")
            continue
        m = p["metrics"]
        a(f"\n  {p['label']}  ({p['depth']}, HTTP {p.get('http_status')})")
        cont = ", ".join(f"{g} ({t:,} tags)" if isinstance(t, int) else f"{g} (not parsed)"
                         for g, t in (p.get("per_container_tags") or {}).items())
        a(f"    containers: {cont or 'none found'}")
        if p.get("tags_without_a_container"):
            a(f"    no GTM container: tags directly with "
              f"{', '.join(p.get('google_tags_in_html') or [])}")
        a(f"    ga4 events {fmt(m['ga4_events'], 'count')}   "
          f"ads accounts {fmt(m['ads_accounts'], 'count')}   "
          f"conversion actions {fmt(m['conversion_actions'], 'count')}   "
          f"lookup tables {fmt(m['lookup_tables'], 'count')}")
        a(f"    consent-declaring tags {fmt(m['consent_share_pct'], 'pct')}   "
          f"vendors {fmt(m['vendors'], 'count')}   "
          f"sitemap {fmt(m['sitemap_urls'], 'count')} URLs")
        if p.get("ads_account_ids"):
            a(f"    ads accounts: {', '.join(p['ads_account_ids'])}")
        if p.get("sibling_hosts"):
            a(f"    other hosts on this domain: {', '.join(p['sibling_hosts'][:8])}")

    gaps = [p for p in profiles if not p.get("ok")]
    a("\n## Gaps and caveats")
    if skipped_containers:
        a("  - --fast: containers were not parsed, so tags, GA4 events, conversion actions,")
        a("    consent share and lookup tables are missing for every target. Those are the")
        a("    measurements worth comparing; re-run without --fast before concluding anything.")
    for p in gaps:
        a(f"  - {p['label']}: {p.get('gap')}. Counted as missing, not as zero.")
    for p in ok:
        if p.get("tags_without_a_container"):
            a(f"  - {p['label']} serves no GTM container and tags directly instead, so its")
            a("    container measurements are absent by architecture rather than by failure.")
            a("    Its tag count is not comparable to a container-based target's, and its")
            a("    conversion setup is only visible where the gtag snippet names it.")
    for p in ok:
        # No container means no container measurements, and the architecture note
        # just above already said why. Repeating the list adds nothing.
        m_none = [k for k, _, _, _ in METRICS
                  if p["metrics"].get(k) is None
                  and not (skipped_containers and k in CONTAINER_METRICS)
                  and not (p.get("tags_without_a_container") and k in CONTAINER_METRICS)]
        if m_none:
            a(f"  - {p['label']}: no value for {', '.join(m_none)}.")
    a("  - Each target was read at one page scope (plus any --pages). A site serving")
    a("    separate containers for its app or checkout can look thinner than it is;")
    a("    --regional on a single target is the way to test that.")
    a("  - Conversion actions are distinct Ads labels, not AW- accounts, and a label")
    a("    present in a container is not proof the account behind it still spends.")
    a("  - Tag counts measure configuration, not sophistication: a 400-tag container")
    a("    can be years of unremoved debt and a 60-tag one a deliberate rebuild.")
    return "\n".join(L)


def render_regional(res):
    L, a = [], None
    a = L.append
    base, delta = res["base"], res["delta_vs_single_site"]
    variants = res["variants"]
    diff = [v for v in variants if v["verdict"] == "DIFFERENT container"]

    a(f"# Regional and functional variant sweep: {res['apex']}")
    a(f"base read: {', '.join(base['containers']) or 'no container'}"
      f"  ({fmt(base['metrics']['tags'], 'count')} tags, "
      f"{fmt(base['metrics']['conversion_actions'], 'count')} conversion actions)")
    a(f"{res['candidates_tested']} candidate hosts, "
      f"{len(variants)} resolved and were fetched once each, "
      f"{len(res['candidates_not_resolving'])} do not exist and were never requested")
    if res.get("unconfirmed_owner"):
        a(f"{len(res['unconfirmed_owner'])} served a page with no tie to this company and are"
          f" excluded from every count below")
    if res.get("unreachable"):
        a(f"{len(res['unreachable'])} resolved but served nothing (parked, blocked or dead)")

    a("\n## Variants serving a container the base site does not")
    if not diff:
        a("  None. Every reachable variant serves the same container(s) as the base site,")
        a("  so the single-site read was already the whole picture for this company.")
    for v in diff:
        a(f"\n  {v['host']}  [{v['why']}]")
        a(f"    new containers: {', '.join(v['new_containers'])}")
        for g in v["new_containers"]:
            c = res["new_containers_parsed"].get(g)
            if isinstance(c, dict) and not c.get("error"):
                gs = c.get("google_ads_summary") or {}
                a(f"      {g}: {(c.get('counts') or {}).get('tags', 0):,} tags, "
                  f"{len(c.get('ga4_events') or [])} GA4 events, "
                  f"{len(gs.get('labels') or [])} conversion actions, "
                  f"{len(c.get('lookup_tables') or [])} lookup tables")
            elif isinstance(c, dict):
                a(f"      {g}: not parsed -- {c.get('error')}")
        a(f"    tied to the company by: {'; '.join(v['same_company_evidence'])}")
        if v.get("title"):
            a(f"    title: {v['title']}")

    a("\n## What the single-site read missed")
    if delta["new_containers"]:
        a(f"  containers:         +{len(delta['new_containers'])}  "
          f"({', '.join(delta['new_containers'])})")
        a(f"  tags:               +{delta['new_tags']:,} in those containers")
        a(f"  conversion actions: +{len(delta['new_conversion_actions'])}"
          + (f"  ({', '.join(delta['new_conversion_actions'][:6])}"
             + (" ..." if len(delta["new_conversion_actions"]) > 6 else "") + ")"
             if delta["new_conversion_actions"] else ""))
        a(f"  ads accounts:       +{len(delta['new_ads_accounts'])}"
          + (f"  ({', '.join(delta['new_ads_accounts'])})" if delta["new_ads_accounts"] else ""))
        a(f"  GA4 event names:    +{len(delta['new_ga4_events'])}"
          + (f"  ({', '.join(delta['new_ga4_events'][:12])}"
             + (" ..." if len(delta["new_ga4_events"]) > 12 else "") + ")"
             if delta["new_ga4_events"] else ""))
        a("")
        if delta["new_conversion_actions"] or delta["new_ads_accounts"]:
            a("  Those conversion actions and ad accounts are ones the homepage container")
            a("  never names, which is the point of the sweep: the stage that actually")
            a("  converts often lives on a host the marketing site never loads.")
        else:
            a("  The extra containers reuse the same conversion actions and ad accounts as the")
            a("  base container, so the optimisation surface is unchanged -- what the sweep")
            a("  found is extra measurement, not extra bidding. Worth knowing either way: a")
            a("  separate container is a separate place a tag can be changed or broken.")
    else:
        a("  Nothing. No variant added a container, so nothing here changes the base read.")

    if res.get("unconfirmed_owner"):
        a(f"\n## Resolving hosts with no tie to this company ({len(res['unconfirmed_owner'])})")
        a("  (a brand's ccTLD is frequently somebody else's business, and a stranger's")
        a("   container counted as this company's would be a badly wrong finding)")
        for v in variants:
            if v["same_company"] or not v["served_a_page"]:
                continue
            land = (f"  -> {v['final_host']}"
                    if v["final_host"] and v["final_host"] != v["host"] else "")
            a(f"  {v['host']}{land}")
            if v.get("title"):
                a(f"      \"{v['title']}\"")
            if v["containers"]:
                a(f"      serves {', '.join(v['containers'])} -- somebody else's container,"
                  f" not counted")
        a("  Checked for: a page reference to the base domain, a redirect into it, or a")
        a("  shared container/measurement ID. None of these showed any. If one of them is")
        a("  in fact theirs, confirm it by hand and re-read that host on its own.")

    a("\n## Every variant that resolved")
    for v in variants:
        a(f"  {v['host']:<34} {v['verdict']:<36} "
          f"{', '.join(v['containers']) or '-'}")
        if v["final_host"] and v["final_host"] != v["host"]:
            a(f"  {'':<34} -> landed on {v['final_host']}")

    if res.get("unreachable"):
        a(f"\n## Resolving but served no page ({len(res['unreachable'])})")
        a("  (DNS records with nothing behind them: parked, blocked, or dead. Not evidence")
        a("   of a market, and not evidence against one either)")
        a("  " + ", ".join(res["unreachable"]))

    if res["candidates_not_resolving"]:
        a(f"\n## Candidates that do not resolve ({len(res['candidates_not_resolving'])})")
        a("  (DNS only -- no request was sent to any of these)")
        a("  " + ", ".join(res["candidates_not_resolving"]))

    profiles = res["profiles"]
    if len([p for p in profiles if p.get("ok")]) >= 2:
        stats = stat_block(profiles)
        L += render_stats(profiles, stats, outlier_summary(profiles, stats), "variants")

    a("\n## Caveats")
    a("  - Each variant got one homepage request, not a full collection, so a variant's")
    a("    own extra pages may carry containers this sweep did not see.")
    a("  - A variant serving the same container still behaves differently at runtime:")
    a("    the container's own region conditions decide what fires where.")
    a("  - A resolving ccTLD is not proof of a live market. Some are defensive")
    a("    registrations that redirect, and some belong to an unrelated company.")
    a("  - A variant tied to the company only by 'its page references the base domain'")
    a("    may be a reseller, partner or affiliate rather than the company's own site.")
    a("    A redirect into the base host or a shared container ID is the firmer tie;")
    a("    check the evidence line before counting a page-reference-only variant.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def run_compare(targets, pages=(), do_containers=True, workers=DEFAULT_WORKERS):
    profiles = collect_many(targets, pages, do_containers, workers)
    stats = stat_block(profiles)
    return {
        "mode": "compare",
        "containers_parsed": do_containers,
        "profiles": profiles,
        "stats": stats,
        "outliers": outlier_summary(profiles, stats),
        "vendors": vendor_sets(profiles),
    }


def main():
    # An explicit --help is a request that succeeded, so it exits 0. Running with
    # no arguments at all is a usage error and still exits 1.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    argv = sys.argv[1:]
    if not argv or "--help" in argv or "-h" in argv:
        print(__doc__)
        return 1

    pages, out_json, workers = [], None, DEFAULT_WORKERS
    regional = "--regional" in argv
    do_containers = "--fast" not in argv and "--no-containers" not in argv

    def opt(flag, default=None):
        if flag in argv:
            i = argv.index(flag)
            if len(argv) > i + 1 and not argv[i + 1].startswith("--"):
                return argv[i + 1]
        return default

    if (v := opt("--pages")):
        pages = [p.strip() for p in v.split(",") if p.strip()]
    if "--json" in argv:
        out_json = opt("--json", "compare.json")
    if (v := opt("--workers")):
        try:
            workers = int(v)
        except ValueError:
            pass

    consumed = {"--regional", "--fast", "--no-containers", "--json", "--pages", "--workers"}
    targets, skip = [], False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok.startswith("--"):
            skip = tok in {"--json", "--pages", "--workers"}
            if tok not in consumed:
                print(f"unknown flag {tok}", file=sys.stderr)
                return 1
            continue
        targets.append(tok)
    # Dedupe while keeping order: comparing a target against itself produces a
    # spread of zero and reads as "these sites are identical".
    targets = list(dict.fromkeys(targets))

    if not targets:
        print("give at least one target", file=sys.stderr)
        return 1

    if regional:
        if len(targets) != 1:
            print("--regional sweeps one company; pass exactly one target", file=sys.stderr)
            return 1
        res = sweep_regional(targets[0], pages, do_containers, workers)
        print(render_regional(res))
    else:
        if len(targets) < 2:
            print("comparison needs at least two targets (or use --regional for one "
                  "company's variants)", file=sys.stderr)
            return 1
        res = run_compare(targets, pages, do_containers, workers)
        print(render_compare(res))

    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2, default=str)
        print(f"\n[raw comparison written to {out_json}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
