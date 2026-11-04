#!/usr/bin/env python3
"""Collect every public growth/martech signal a URL serves to an ordinary visitor.

This only requests things the site hands to any browser that loads it: the HTML,
its response headers, the scripts it references, and the well-known paths that
exist to be read (robots.txt, sitemap.xml, ads.txt, security.txt). Nothing is
guessed at, brute-forced, or fetched from behind a login.

Usage:
    python3 recon.py https://example.com                   # collect + print
    python3 recon.py https://example.com --json out.json    # also save raw facts
    python3 recon.py https://example.com --pages /pricing,/demo
    python3 recon.py https://example.com --no-containers    # skip gtm.js parsing

Output is deliberately raw. Reading it and deciding what it means is the analyst's
job, and the SKILL.md workflow covers how to turn this into a report.
"""

import json
import os
import re
import sys
import gzip
import zlib
import socket
import ssl
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gtm_parse import analyse as analyse_container, fetch as _fetch_text, UA  # noqa: E402

TIMEOUT = 25

# ---------------------------------------------------------------------------
# Vendor fingerprints. Each entry: (label, regex, what the hit implies).
# Regexes run against the raw HTML. The "implies" text exists so the report can
# say why a detection matters instead of listing vendor names.
# references/vendor-fingerprints.md has the longer catalogue and reasoning.
# ---------------------------------------------------------------------------
FINGERPRINTS = [
    # --- identity of the measurement setup --------------------------------
    ("GTM container", r"GTM-[A-Z0-9]{4,10}", "tag manager container; fetchable in full", 0),
    ("GA4 measurement ID", r"G-[A-Z0-9]{8,12}", "GA4 property", 0),
    ("Google Tag ID", r"GT-[A-Z0-9]{6,12}", "newer Google tag wrapper", 0),
    ("Google Ads", r"AW-\d{6,12}", "Google Ads conversion account", 0),
    ("Floodlight", r"DC-\d{6,12}", "DV360/CM360 — programmatic display spend", 0),
    ("Universal Analytics", r"UA-\d{4,10}-\d+", "legacy GA; stale tracking left in place", 0),
    ("Google Optimize (dead)", r"OPT-[A-Z0-9]{6,10}", "legacy -- Google sunset Optimize in Sept 2023, so this is container debt, NOT a live testing tool", 0),
    # --- paid social / demand ---------------------------------------------
    ("Meta Pixel", r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{13,17})|connect\.facebook\.net", "Meta ads"),
    ("LinkedIn Insight", r"_linkedin_partner_id\s*=\s*[\"']?(\d+)|snap\.licdn\.com|px\.ads\.linkedin\.com|sjs\.bizographics\.com", "LinkedIn ads — B2B demand gen"),
    ("TikTok Pixel", r"ttq\.load\(\s*['\"]([A-Z0-9]+)|analytics\.tiktok\.com", "TikTok ads"),
    ("Reddit Pixel", r"rdt\(\s*['\"]init['\"]\s*,\s*['\"]([a-z0-9_]+)|events\.redditmedia\.com|alb\.reddit\.com", "Reddit ads"),
    ("X/Twitter Pixel", r"twq\(\s*['\"](?:init|config)['\"]\s*,\s*['\"]([a-z0-9]+)|static\.ads-twitter\.com|analytics\.twitter\.com", "X ads"),
    ("Pinterest Tag", r"pintrk\(\s*['\"]load['\"]\s*,\s*['\"](\d+)|ct\.pinterest\.com|s\.pinimg\.com", "Pinterest ads"),
    ("Quora Pixel", r"qp\(\s*['\"]init['\"]\s*,\s*['\"]([a-f0-9]+)|a\.quora\.com", "Quora ads"),
    ("Bing/Microsoft UET", r"\"ti\"\s*:\s*\"(\d{6,12})\"|bat\.bing\.com", "Microsoft Ads"),
    ("Criteo", r"criteo\.(?:net|com)", "retargeting"),
    ("Taboola", r"_taboola|trc\.taboola\.com", "native/content ads"),
    ("Outbrain", r"outbrain\.com/outbrain\.js|obApi", "native/content ads"),
    # --- ABM / visitor de-anonymisation (reveals outbound + ABM strategy) --
    ("6sense", r"6si\.com|_6si|6sense", "intent data + ABM; enterprise-motion signal"),
    ("Demandbase", r"demandbase|company-target\.com", "ABM platform"),
    ("RB2B", r"rb2b|reb2b", "person-level visitor ID; aggressive outbound motion"),
    ("Clearbit/HubSpot Breeze", r"clearbit|reveal\.clearbit\.com", "firmographic enrichment + form shortening"),
    ("Koala", r"getkoala\.com|ko_track", "product+web intent scoring for sales"),
    ("Warmly", r"warmly\.ai|getwarmly", "visitor de-anonymisation + chat"),
    ("Vector", r"vector\.co/|vectorjs", "visitor intent"),
    ("Apollo tracker", r"apollo\.io/.*tracker|assets\.apollo\.io", "outbound prospecting"),
    ("Leadfeeder/Dealfront", r"leadfeeder|dealfront", "company-level visitor ID"),
    ("Albacross", r"albacross", "company-level visitor ID"),
    ("Factors.ai", r"factors\.ai", "attribution + ABM"),
    ("Influ2", r"influ2", "person-based advertising"),
    # --- MAP / CRM / lifecycle --------------------------------------------
    ("HubSpot", r"js\.hs-scripts\.com/(\d+)|hs-analytics|hubspot\.com/.*portalId", "HubSpot hub"),
    ("Marketo", r"munchkin[^0-9]{0,20}(\d{3}-[A-Z0-9]{3}-\d{3})|munchkin\.marketo\.net", "Marketo MAP — enterprise MQL motion", 0),
    ("Pardot", r"piAId\s*=\s*['\"]?(\d+)|pi\.pardot\.com", "Pardot/Account Engagement"),
    ("Eloqua", r"eloqua|elqCfg", "Oracle Eloqua MAP"),
    ("Salesforce Web-to-Lead", r"webto\.salesforce\.com", "direct-to-SFDC lead capture"),
    ("Braze", r"braze\.com|appboy", "lifecycle messaging"),
    ("Customer.io", r"customer\.io|cioJs", "lifecycle messaging"),
    ("Klaviyo", r"klaviyo\.com|_learnq", "ecommerce lifecycle"),
    ("Iterable", r"iterable\.com", "lifecycle messaging"),
    # --- product analytics / CDP ------------------------------------------
    ("Segment", r"cdn\.segment\.com/analytics\.js/v1/([A-Za-z0-9]+)|analytics\.load\(\s*['\"]([A-Za-z0-9]{10,})", "CDP — events fan out server-side"),
    ("RudderStack", r"rudderlabs|rudderstack", "open-source CDP"),
    ("Amplitude", r"amplitude\.com|amplitude\.getInstance", "product analytics"),
    ("Mixpanel", r"mixpanel\.(?:com|init\(\s*['\"]([a-f0-9]{32}))", "product analytics"),
    ("PostHog", r"posthog\.com|phc_[A-Za-z0-9]{20,}", "product analytics"),
    ("Heap", r"heapanalytics\.com|heap\.load\(\s*['\"]?(\d+)", "autocapture analytics"),
    ("Pendo", r"pendo\.io", "in-product guides + analytics"),
    ("June", r"june\.so", "product analytics"),
    # --- session replay / UX ----------------------------------------------
    ("Hotjar", r"hotjar\.com|hjid\s*:\s*(\d+)", "session replay + surveys"),
    ("Microsoft Clarity", r"clarity\.ms", "free session replay"),
    ("FullStory", r"fullstory\.com|FS\.identify", "session replay"),
    ("LogRocket", r"logrocket", "session replay"),
    ("Contentsquare", r"contentsquare|quantummetric", "enterprise experience analytics"),
    ("Mouseflow", r"mouseflow", "session replay"),
    ("Smartlook", r"smartlook", "session replay"),
    # --- experimentation (reveals their testing velocity) ------------------
    ("Optimizely", r"cdn\.optimizely\.com/(?:js|public|datafiles)/([A-Za-z0-9_-]+)", "A/B testing; datafile may be public"),
    ("VWO", r"visualwebsiteoptimizer\.com|_vwo_code|vwo_\$", "A/B testing"),
    ("LaunchDarkly", r"launchdarkly\.com|ldclient", "feature flags"),
    ("Statsig", r"statsig\.com|statsigsdk", "experimentation + flags"),
    ("Split.io", r"split\.io|splitio", "feature flags"),
    ("GrowthBook", r"growthbook", "open-source experimentation"),
    ("Eppo", r"eppo\.cloud|geteppo", "warehouse-native experimentation"),
    ("AB Tasty", r"abtasty", "A/B testing + personalisation"),
    ("Kameleoon", r"kameleoon", "A/B testing"),
    ("Convert.com", r"cdn-?\d*\.convertexperiments\.com", "A/B testing"),
    ("Mutiny", r"mutinyhq|mutiny\.sh", "landing-page personalisation for ABM"),
    ("Intellimize", r"intellimize", "landing-page personalisation"),
    ("Dynamic Yield", r"dynamicyield", "personalisation"),
    ("Unbounce", r"unbounce", "standalone landing pages — paid-traffic motion"),
    ("Instapage", r"instapage", "standalone landing pages"),
    # --- chat / scheduling (speed-to-lead motion) --------------------------
    ("Qualified", r"qualified\.com|qualifiedjs", "sales chat routing on high-intent pages"),
    ("Drift", r"drift\.com|drift\.load", "conversational marketing"),
    ("Intercom", r"intercom\.(?:io|com)|intercomSettings", "support/chat"),
    ("Chili Piper", r"chilipiper", "instant inbound meeting booking"),
    ("Calendly", r"calendly\.com", "self-serve booking"),
    ("Default", r"default\.com/|usedefault", "inbound routing"),
    ("Crisp/Tidio/Zendesk chat", r"crisp\.chat|tidio|zdassets", "chat"),
    # --- consent (gates everything above; shapes their data quality) -------
    ("OneTrust", r"onetrust|cdn\.cookielaw\.org|data-domain-script", "enterprise CMP"),
    ("Cookiebot", r"cookiebot|consent\.cookiebot\.com", "CMP"),
    ("Usercentrics", r"usercentrics", "CMP"),
    ("Didomi", r"didomi", "CMP"),
    ("Osano", r"osano", "CMP"),
    ("Ketch", r"ketch\.com|ketchcdn", "CMP"),
    ("TrustArc", r"trustarc", "CMP"),
    ("Iubenda", r"iubenda", "CMP"),
    ("Termly", r"termly", "CMP"),
    # --- platform / infra --------------------------------------------------
    ("Next.js", r"/_next/static|__NEXT_DATA__", "Next.js — __NEXT_DATA__ may expose props", 0),
    ("Nuxt", r"__NUXT__|/_nuxt/", "Nuxt", 0),
    ("Gatsby", r"gatsby-|___gatsby", "Gatsby"),
    ("Astro", r"astro-island|/_astro/", "Astro"),
    ("Webflow", r"webflow\.(?:com|io)|wf-", "Webflow — marketing site, fast iteration"),
    ("WordPress", r"/wp-content/|/wp-includes/", "WordPress"),
    ("HubSpot CMS", r"hs-sites\.com|hubspotusercontent", "HubSpot CMS"),
    ("Shopify", r"cdn\.shopify\.com|Shopify\.theme", "Shopify storefront"),
    ("Contentful", r"contentful\.com|ctfassets\.net", "headless CMS"),
    ("Sanity", r"sanity\.io|cdn\.sanity", "headless CMS"),
    ("Storyblok", r"storyblok", "headless CMS"),
    ("Stripe", r"js\.stripe\.com|pk_live_[A-Za-z0-9]{20,}", "self-serve checkout"),
    ("Paddle", r"paddle\.com|paddle_button", "merchant of record — global self-serve"),
    ("Chargebee", r"chargebee", "subscription billing"),
    # --- social proof ------------------------------------------------------
    ("G2", r"g2crowd|g2\.com/products", "G2 review widget"),
    ("Trustpilot", r"trustpilot", "Trustpilot widget"),
    # --- additions commonly only visible via CSP / container -------------
    ("ZoomInfo", r"js\.zi-scripts\.com|zoominfo", "firmographic enrichment + form shortening"),
    ("Capterra", r"ct\.capterra\.com|tr\.capterra\.com", "review-site paid listings"),
    ("Recurly", r"js\.recurly\.com|recurly", "subscription billing"),
    ("Datadog RUM", r"datadoghq-browser-agent|logs\.datadoghq\.com", "real-user monitoring"),
    ("Wistia", r"wistia\.(?:com|net)", "video hosting with lead capture"),
    ("Vidyard", r"vidyard", "video hosting with lead capture"),
    ("Amazon Ads", r"amazon-adsystem\.com", "Amazon DSP"),
    ("Xing", r"static\.xingcdn\.com|xing\.com", "DACH professional network ads"),
    ("Yahoo Japan", r"s\.yjtag\.jp|yahoo\.co\.jp", "Japan market ads"),
    ("TVSquared", r"tvsquared", "TV attribution — offline/brand spend"),
    ("Greenhouse", r"boards\.greenhouse\.io|job-boards\.greenhouse\.io", "ATS — headcount plans readable"),
    ("Lever", r"jobs\.lever\.co", "ATS — headcount plans readable"),
    ("Ashby", r"jobs\.ashbyhq\.com", "ATS — headcount plans readable"),
    ("Spara", r"spara\.co", "AI SDR / inbound agent"),
    ("Jotform", r"jotform|jotfor\.ms", "form tooling outside the main stack"),
]

WELL_KNOWN = [
    "/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/ads.txt", "/app-ads.txt",
    "/.well-known/security.txt", "/security.txt", "/humans.txt",
    "/.well-known/apple-app-site-association", "/.well-known/assetlinks.json",
    "/llms.txt", "/feed", "/rss.xml", "/blog/rss.xml", "/changelog",
]


def norm_url(u):
    if not re.match(r"^https?://", u):
        u = "https://" + u
    return u


def fetch_full(url, timeout=TIMEOUT):
    """Fetch returning body + headers + final URL. Falls back to curl."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate", "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return {"url": r.url, "status": r.status, "body": raw.decode("utf-8", "replace"),
                    "headers": dict(r.headers.items())}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "body": "", "headers": dict(e.headers.items()) if e.headers else {}}
    except Exception:
        return _curl_full(url, timeout)


def _curl_full(url, timeout):
    try:
        p = subprocess.run(
            ["curl", "-sSL", "--compressed", "-D", "-", "--max-time", str(timeout),
             "-A", UA, "-w", "\n__FINAL__%{url_effective}\n__STATUS__%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 15)
        out = p.stdout
        final = re.search(r"\n__FINAL__(.*)", out)
        status = re.search(r"\n__STATUS__(\d+)", out)
        out = re.sub(r"\n__FINAL__.*", "", out, flags=re.S)
        # Last header block then body
        parts = re.split(r"\r?\n\r?\n", out, maxsplit=0)
        headers = {}
        body = out
        for i, blk in enumerate(parts):
            if blk.startswith("HTTP/"):
                headers = {}
                for line in blk.splitlines()[1:]:
                    if ":" in line:
                        k, _, v = line.partition(":")
                        headers[k.strip()] = v.strip()
                body = "\n\n".join(parts[i + 1:])
        return {"url": final.group(1).strip() if final else url,
                "status": int(status.group(1)) if status else 0,
                "body": body, "headers": headers}
    except Exception as e:
        return {"url": url, "status": 0, "body": "", "headers": {}, "error": str(e)[:200]}


# A dotted i18n key like "templateCenter.integration.clearbit.description" looks
# exactly like a hostname unless the suffix is checked against real TLDs, which
# is how an integrations catalogue produced confident vendor detections.
TLD_RE = (r"\.(?:com|net|org|io|ai|co|dev|app|eu|jp|me|tv|cloud|gg|sh|xyz|us|uk|de|fr|"
          r"br|mx|ca|au|in|cn|so|to|id|fm|live|site|online|tech|link|page|run|work|biz|"
          r"info|cc|ly|es|it|nl|se|no|fi|dk|pl|ru|kr|tw|hk|sg|za|ie|at|ch|be|pt|gr|cz|"
          r"com\.br|co\.uk|co\.jp|com\.au|com\.mx|co\.za)(?:[/:?\s\"\'<>]|$)")


def _evidence_strength(matches, contexts=()):
    """Decide whether a detection is load evidence or just a name in the text.

    A bare product name matches anywhere it is mentioned -- including a target's
    own integrations directory or a competitor list -- so monday.com's i18n
    strings produced confident "Clearbit" and "Jotform" detections for vendors
    that load nothing. Treat a host, an ID shape or a JS global as strong, and a
    bare alphabetic name as weak.
    """
    for m in matches:
        if re.search(r"[a-z0-9-]+\.[a-z]{2,}(/|$)", m, re.I):      # a host
            return "strong"
        if re.search(r"[A-Z]{2}-[A-Z0-9]{4,}|\d{6,}|_[a-z]+_|^phc_|^pk_live_", m):  # an ID
            return "strong"
        if re.search(r"(fbq|ttq|twq|pintrk|rdt|qp|_linkedin|munchkin|piAId|obApi|Settings|load\()", m):
            return "strong"
        # An asset path or injected global is served by the thing itself, so it
        # is load evidence even though it contains no host and no ID.
        if re.search(r"(^/|/_)(_?next|_?nuxt|astro|wp-content|wp-includes|gatsby)", m, re.I):
            return "strong"
        if re.search(r"(__NEXT_DATA__|__NUXT__|___gatsby|astro-island|_vwo_|_learnq|"
                     r"intercomSettings|elqCfg|Shopify\.theme|data-domain-script|hjid|"
                     r"_vis_opt|phc_|pk_live_)", m):
            return "strong"

    # A pattern can match only the bare vendor name inside what is actually a
    # hostname -- "contentsquare" out of "app.contentsquare.com", which came from
    # a CSP allowlist and is unambiguous load evidence. So check whether the match
    # sits inside a host or URL before calling it a mere mention. An i18n string
    # in an integrations catalogue has no TLD next to it and stays weak.
    for m in matches:
        token = re.escape(m.strip().strip("./"))
        if not token or len(token) < 3:
            continue
        for ctx in contexts:
            if re.search(r"[a-z0-9-]*" + token + r"[a-z0-9-]*" + TLD_RE, ctx, re.I):
                return "strong"
            if re.search(r"//[^\s\"\'<>]*" + token, ctx, re.I):
                return "strong"
    return "weak"


def fingerprint(html, source="html"):
    """Run the fingerprint catalogue over a blob of text.

    Entries carry explicit regex flags because the ID patterns (GTM-, G-, AW-)
    must stay case-sensitive -- lowercased near-misses like a `g-disabled` CSS
    class or a `gtm-tracking` data attribute otherwise masquerade as real IDs.
    """
    hits = OrderedDict()
    for entry in FINGERPRINTS:
        label, pat, implies = entry[0], entry[1], entry[2]
        flags = entry[3] if len(entry) > 3 else re.I
        ms = list(re.finditer(pat, html, flags))
        if not ms:
            continue
        ids = []
        for m in ms[:20]:
            got = next((g for g in m.groups() if g), None) if m.groups() else m.group(0)
            if got:
                ids.append(got)
        matched = [m.group(0) for m in ms[:20]]
        windows = [html[max(0, m.start() - 60):m.end() + 60] for m in ms[:20]]
        hits[label] = {"implies": implies, "occurrences": len(ms),
                       "ids": sorted(set(ids))[:10], "source": source,
                       "evidence": _evidence_strength(matched + ids, windows)}
    return hits


def third_party_hosts(html, own_host):
    hosts = {}
    for m in re.finditer(r"""(?:src|href)\s*=\s*["']((?:https?:)?//[^"'>\s]+)""", html, re.I):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        h = urllib.parse.urlparse(u).netloc.lower()
        base = own_host.replace("www.", "")
        if h and base not in h:
            hosts[h] = hosts.get(h, 0) + 1
    return [{"host": h, "refs": n} for h, n in sorted(hosts.items(), key=lambda x: -x[1])]


def parse_robots(body):
    """Group robots.txt directives by user-agent and compare AI crawlers to `*`.

    Listing the AI crawler names on their own implied they were blocked. On one
    live site every AI agent's rules were byte-identical to the default group
    and the file ended `Content-Signal: ai-train=yes` -- nothing was restricted
    at all. So report the comparison, not the names.
    """
    groups, current = {}, []
    for raw in body.splitlines():
        line = raw.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if current and groups.get(tuple(current)) is not None:
                current = []
            current.append(value)
            groups.setdefault(tuple(current), [])
        elif current:
            key = tuple(current)
            groups.setdefault(key, []).append((field, value))
        if field in ("disallow", "allow", "crawl-delay") and current:
            pass

    per_agent = {}
    for agents, rules in groups.items():
        norm = tuple(sorted((f, v) for f, v in rules if f in ("disallow", "allow")))
        for a in agents:
            per_agent[a.lower()] = norm
    default = per_agent.get("*", ())

    ai = {}
    for a, rules in per_agent.items():
        if not re.search(r"(gptbot|oai-|openai|chatgpt|claude|anthropic|perplexity|"
                         r"bytespider|ccbot|google-extended|applebot-extended|"
                         r"meta-external|cohere|diffbot|omgili|timpibot|youbot)", a, re.I):
            continue
        if any(f == "disallow" and v == "/" for f, v in rules):
            verdict = "BLOCKED (disallow: /)"
        elif rules == default:
            verdict = "same-as-default (NOT blocked)"
        elif len(rules) > len(default):
            verdict = "more restrictive than default"
        else:
            verdict = "more permissive than default"
        ai[a] = verdict
    return {
        "default_group_rules": len(default),
        "ai_crawler_verdicts": ai,
        "content_signal": (re.search(r"(?im)^\s*Content-Signal:\s*(.+)$", body).group(1).strip()
                           if re.search(r"(?im)^\s*Content-Signal:", body) else None),
    }


def verify_identity(requested_host, final_url, html):
    """Confirm the page we got actually belongs to the host we asked for.

    A CDN edge once answered a request for one company's homepage with another
    company's cached page, complete with the wrong GTM container ID. Silently
    reporting on the wrong company is the worst failure this tool can have, so
    compare the canonical/og:url/title against what was requested and say so
    loudly on a mismatch.
    """
    base = requested_host.lower().replace("www.", "")
    seen = {}
    for name, pat in (("canonical", r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)'),
                      ("og:url", r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)')):
        m = re.search(pat, html, re.I)
        if m:
            seen[name] = urllib.parse.urlparse(m.group(1)).netloc.lower().replace("www.", "")
    final_host = urllib.parse.urlparse(final_url or "").netloc.lower().replace("www.", "")
    mismatches = {k: v for k, v in seen.items() if v and base not in v and v not in base}
    ok = not mismatches and (not final_host or base in final_host or final_host in base)
    return {
        "requested_host": requested_host, "final_host": final_host,
        "declared_hosts": seen, "match": ok,
        "warning": (None if ok else
                    f"IDENTITY MISMATCH: requested {requested_host} but the page declares "
                    f"{mismatches or final_host}. A CDN edge may have served a cached page for a "
                    f"different site. Re-fetch (try the apex domain) and confirm before using any "
                    f"of this collection -- container IDs in it may belong to another company."),
    }


def parse_csp(headers):
    """A CSP is the site's own allowlist of every third party it talks to --
    often a more complete stack inventory than the HTML, because it includes
    vendors loaded later or only on other pages."""
    csp = headers.get("Content-Security-Policy") or headers.get("content-security-policy") or ""
    if not csp:
        return None
    domains = sorted(set(re.findall(r"[*.]?([a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,})", csp, re.I)))
    return {"present": True, "length": len(csp), "allowed_domains": domains}


def parse_sitemaps(base, body, limit=3):
    """Expand a sitemap index one level and return the URL inventory."""
    out = {"child_sitemaps": [], "sample_urls": [], "total_urls": 0, "path_prefixes": {}}
    children = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", body, re.S)
    urls = re.findall(r"<url>.*?<loc>(.*?)</loc>", body, re.S) or re.findall(r"<loc>(.*?)</loc>", body)
    if children:
        out["child_sitemaps"] = children[:40]
        for c in children[:limit]:
            r = fetch_full(c.strip())
            urls += re.findall(r"<loc>(.*?)</loc>", r["body"])
    urls = [u.strip() for u in urls if u.strip().startswith("http")]
    out["total_urls"] = len(urls)
    out["sample_urls"] = urls[:150]
    pref = {}
    for u in urls:
        seg = urllib.parse.urlparse(u).path.strip("/").split("/")
        key = "/" + (seg[0] if seg and seg[0] else "")
        pref[key] = pref.get(key, 0) + 1
    out["path_prefixes"] = dict(sorted(pref.items(), key=lambda x: -x[1])[:40])
    return out


def dns_facts(host):
    facts = {}
    try:
        facts["a_records"] = sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443)})[:6]
    except Exception:
        pass
    for rr in ("MX", "TXT", "NS"):
        try:
            p = subprocess.run(["dig", "+short", rr, host], capture_output=True, text=True, timeout=12)
            vals = [l.strip() for l in p.stdout.splitlines() if l.strip()]
            if vals:
                facts[rr.lower()] = vals[:12]
        except Exception:
            pass
    # TXT records name the SaaS vendors a company has verified a domain with --
    # a quiet but reliable read on their stack and on pending migrations.
    txt = " ".join(facts.get("txt", []))
    vendors = sorted(set(re.findall(
        r"(google-site-verification|atlassian|facebook-domain-verification|"
        r"ms=|MS=|adobe-idp|docusign|zoom|slack|miro|notion|asana|hubspot|"
        r"pardot|marketo|stripe|shopify|zendesk|intercom|dropbox|canva|"
        r"atlassian-domain-verification|apple-domain-verification|amazonses|"
        r"sendgrid|mailgun|postmark|klaviyo|segment|logmein|webex|citrix|"
        r"onetrust|workday|servicenow|loom|figma|github|gitlab|openai|anthropic)",
        txt, re.I)))
    if vendors:
        facts["txt_vendor_hints"] = vendors
    return facts


def fetch_vwo(account_id, page_url):
    """Fetch VWO's public settings file and pull live test names out of it.

    This is the same request a visitor's browser makes. It is the highest-value
    artefact for "what are they testing", because campaign names come back in
    plain text -- and it was documented in the references while the collector
    only handled Optimizely, so every run had to build it by hand.
    """
    url = (f"https://dev.visualwebsiteoptimizer.com/j.php?a={account_id}"
           f"&u={urllib.parse.quote(page_url, safe='')}&f=1&r={0.123456}")
    try:
        body, status = _fetch_text(url, 20)
    except Exception as e:
        return {"url": url, "error": str(e)[:160]}
    names = sorted({n.replace("\\/", "/").replace('\\"', '"')
                    for n in re.findall(r'"name"\s*:\s*"((?:[^"\\]|\\.){3,120})"', body)})
    types = sorted(set(re.findall(r'"type"\s*:\s*"([A-Z_]+)"', body)))
    goals = len(re.findall(r'"goals"', body))
    return {
        "url": url, "status": status, "bytes": len(body),
        "campaign_names": names[:60], "campaign_types": types,
        "goal_blocks": goals,
        "note": ("campaign names are the live test inventory" if names else
                 "no plain-text campaign names in the response (account may have no running tests)"),
    }


def discover_vwo_account(*blobs):
    for b in blobs:
        if not b:
            continue
        m = (re.search(r"visualwebsiteoptimizer\.com/j\.php\?a=(\d{4,10})", b)
             or re.search(r"\baccount_id\s*[:=]\s*['\"]?(\d{4,10})", b)
             or re.search(r"_vwo_code[\s\S]{0,400}?\ba\s*=\s*(\d{4,10})", b))
        if m:
            return m.group(1)
    return None


def collect(target, extra_pages=(), do_containers=True):
    url = norm_url(target)
    root = fetch_full(url)
    host = urllib.parse.urlparse(root["url"] or url).netloc
    origin = f"{urllib.parse.urlparse(root['url'] or url).scheme}://{host}"
    html = root["body"]

    report = {
        "target": url,
        "final_url": root["url"],
        "http_status": root["status"],
        "html_bytes": len(html),
        "response_headers": {k: v for k, v in root["headers"].items()
                             if k.lower() in {
                                 "server", "x-powered-by", "via", "x-vercel-id", "x-vercel-cache",
                                 "cf-ray", "cf-cache-status", "x-amz-cf-id", "x-cache",
                                 "x-served-by", "x-nextjs-cache", "x-shopify-stage",
                                 "x-hubspot-correlation-id", "set-cookie", "strict-transport-security",
                                 "content-security-policy", "report-to", "permissions-policy",
                                 "x-robots-tag", "x-drupal-cache", "x-generator"}},
        "identity": verify_identity(host, root["url"], html),
        "csp": parse_csp(root["headers"]),
        "cookies_set_on_first_load": sorted(set(re.findall(
            r"^([^=;]+)=", root["headers"].get("Set-Cookie", "") or "", re.M))),
        "meta": {
            "title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I).group(1).strip()[:200]
                      if re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) else None),
            "description": (re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,400})', html, re.I).group(1)
                            if re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{0,400})', html, re.I) else None),
            "generator": (re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html, re.I).group(1)
                          if re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html, re.I) else None),
            "hreflang_locales": sorted(set(re.findall(r'hreflang=["\']([a-zA-Z-]+)["\']', html)))[:40],
        },
        "fingerprints": fingerprint(html),
        "third_party_hosts": third_party_hosts(html, host)[:60],
        "well_known": {},
        "sitemap": None,
        "gtm_containers": {},
        "gtag": {},
        "experimentation": {},
        "dns": dns_facts(host.replace("www.", "")) if host else {},
        "extra_pages": {},
        "collection_notes": [],
    }

    # --- well-known paths, in parallel -----------------------------------
    def grab(path):
        return path, fetch_full(origin + path, timeout=15)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for path, r in ex.map(grab, WELL_KNOWN):
            if r["status"] == 200 and r["body"].strip():
                body = r["body"]
                entry = {"status": 200, "bytes": len(body)}
                if path == "/robots.txt":
                    entry["disallow"] = sorted(set(re.findall(r"(?im)^\s*Disallow:\s*(\S+)", body)))[:80]
                    entry["sitemaps"] = sorted(set(re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", body)))
                    entry["user_agents"] = sorted(set(re.findall(r"(?im)^\s*User-agent:\s*(\S+)", body)))[:40]
                    entry.update(parse_robots(body))
                elif path in ("/ads.txt", "/app-ads.txt"):
                    entry["sellers"] = sorted({l.split(",")[0].strip() for l in body.splitlines()
                                               if "," in l and not l.strip().startswith("#")})[:40]
                elif "security.txt" in path:
                    entry["excerpt"] = body[:600]
                elif path == "/llms.txt":
                    entry["excerpt"] = body[:1200]
                else:
                    entry["excerpt"] = body[:500]
                report["well_known"][path] = entry

    # --- sitemap inventory ------------------------------------------------
    sm_urls = report["well_known"].get("/robots.txt", {}).get("sitemaps") or [origin + "/sitemap.xml"]
    for sm in sm_urls[:1]:
        r = fetch_full(sm)
        if r["status"] == 200 and "<" in r["body"]:
            report["sitemap"] = {"source": sm, **parse_sitemaps(origin, r["body"])}

    # --- extra pages first, so container discovery can use them -----------
    # Scope is a page, not a domain: one site can serve several containers, split
    # by page template or by app vs marketing site. A homepage-only read found 1
    # of monday.com's 3. Collect IDs from every page fetched.
    page_blobs = [html]
    for pg in extra_pages:
        pg = pg if pg.startswith("http") else origin + ("" if pg.startswith("/") else "/") + pg
        r = fetch_full(pg)
        if r["status"] == 200:
            page_blobs.append(r["body"])
            pg_fp = fingerprint(r["body"])
            new_strong = [k for k, v in pg_fp.items()
                          if k not in report["fingerprints"] and v.get("evidence") != "weak"]
            new_weak = [k for k, v in pg_fp.items()
                        if k not in report["fingerprints"] and v.get("evidence") == "weak"]
            report["extra_pages"][pg] = {
                "status": r["status"], "bytes": len(r["body"]),
                # Split here too: an integrations or comparison page is exactly
                # where bare vendor names show up for tools the site never loads.
                "new_fingerprints": new_strong,
                "name_only_no_load_evidence": new_weak,
                "forms": sorted(set(re.findall(r'<form[^>]+action=["\']([^"\']+)', r["body"], re.I)))[:15],
                "gtm_ids": sorted(set(re.findall(r"GTM-[A-Z0-9]{4,10}", r["body"]))),
            }
        else:
            report["extra_pages"][pg] = {"status": r["status"]}

    # --- GTM containers ---------------------------------------------------
    gtm_ids = sorted({g for b in page_blobs for g in re.findall(r"GTM-[A-Z0-9]{4,10}", b)})
    report["container_discovery"] = {"from_pages": sorted(gtm_ids), "from_zones": []}
    if do_containers:
        queue, seen = list(gtm_ids), set()
        while queue and len(seen) < 6:
            gid = queue.pop(0)
            if gid in seen:
                continue
            seen.add(gid)
            try:
                c = analyse_container(gid)
                report["gtm_containers"][gid] = c
                # A Zone loads another container conditionally; follow it once.
                for z in c.get("zones", []):
                    for child in re.findall(r"GTM-[A-Z0-9]{4,10}", json.dumps(z)):
                        if child not in seen:
                            queue.append(child)
                            report["container_discovery"]["from_zones"].append(child)
            except Exception as e:
                report["gtm_containers"][gid] = {"error": str(e)[:300]}
                report["collection_notes"].append(f"container {gid} failed: {str(e)[:120]}")
        # Form actions and links routinely point at the auth/app subdomain, which
        # tends to host its own container. Naming those hosts turns "look harder"
        # into a concrete next command.
        root_domain = ".".join(host.replace("www.", "").split(".")[-2:])
        sibling = sorted({
            h for b in page_blobs
            for h in re.findall(r"//([a-z0-9][a-z0-9.-]*\." + re.escape(root_domain) + r")", b, re.I)
            if h.lower().replace("www.", "") != host.lower().replace("www.", "")
        })[:12]
        if sibling:
            report["container_discovery"]["sibling_hosts"] = sibling
            report["collection_notes"].append(
                "Other hosts on this domain appear in the pages collected: "
                + ", ".join(sibling)
                + ". Each can serve its own container; re-run against them to complete the picture.")
        if len(report["gtm_containers"]) == 1:
            report["collection_notes"].append(
                "Only one container found. Sites commonly serve separate containers for the app "
                "or signup subdomain and for other page templates; pass --pages, or try the app "
                "subdomain, before treating this as the whole picture.")
    else:
        report["gtm_containers"] = {g: "skipped (--no-containers)" for g in gtm_ids}

    # --- gtag/js for Google Tag + GA4 IDs ---------------------------------
    for gid in sorted(set(re.findall(r"G[T]?-[A-Z0-9]{8,12}", html)))[:3]:
        try:
            body, status = _fetch_text(f"https://www.googletagmanager.com/gtag/js?id={gid}", 25)
            report["gtag"][gid] = {
                "status": status, "bytes": len(body),
                "linked_ids": sorted(set(re.findall(r"(?:AW|G|GT|DC|MC)-[A-Z0-9]{6,15}", body)))[:20],
            }
        except Exception as e:
            report["gtag"][gid] = {"error": str(e)[:200]}

    # --- experimentation datafiles ---------------------------------------
    opt = report["fingerprints"].get("Optimizely", {}).get("ids", [])
    for key in opt[:2]:
        for tmpl in (f"https://cdn.optimizely.com/datafiles/{key}.json",
                     f"https://cdn.optimizely.com/public/{key}/s/_.json",
                     f"https://cdn.optimizely.com/js/{key}.js"):
            try:
                body, status = _fetch_text(tmpl, 20)
                if status == 200 and not body.strip().startswith("{"):
                    # Web Experimentation ships a JS snippet rather than a datafile.
                    # Whether readable experiment names survive minification varies
                    # by project, so report what is there and stay quiet otherwise.
                    names = sorted(set(re.findall(
                        r'"(?:experimentName|campaignName|audienceName)"\s*:\s*"([^"]{2,80})"', body)))
                    report["experimentation"][tmpl] = {
                        "format": "web-experimentation snippet",
                        "bytes": len(body),
                        "readable_names": names[:60],
                        "note": ("no plain-text experiment names in this snippet"
                                 if not names else "names read from snippet metadata"),
                    }
                    break
                if status == 200 and body.strip().startswith("{"):
                    d = json.loads(body)
                    report["experimentation"][tmpl] = {
                        "experiments": [e.get("key") for e in d.get("experiments", [])][:80],
                        "feature_flags": [f.get("key") for f in d.get("featureFlags", [])][:80],
                        "events": [e.get("key") for e in d.get("events", [])][:80],
                        "audiences": [a.get("name") for a in d.get("audiences", [])][:40],
                    }
                    break
            except Exception:
                continue

    # --- widen fingerprinting beyond the HTML -----------------------------
    # Most vendors are injected by GTM at runtime, so they never appear in the
    # served HTML. Two places do see them: the CSP allowlist (the site had to
    # permit the domain) and the container's own Custom HTML hosts. Folding those
    # in is what turns "6 vendors" into the real stack.
    widened = {}
    if report["csp"]:
        widened.update(fingerprint(" ".join(report["csp"]["allowed_domains"]), source="csp"))
    container_hosts = []
    for gid, c in report["gtm_containers"].items():
        if isinstance(c, dict) and not c.get("error"):
            container_hosts += [h["host"] for h in c.get("custom_html_hosts", [])]
            for t in c.get("custom_templates", []):
                container_hosts += t.get("fingerprint", {}).get("hosts", [])
                container_hosts += t.get("fingerprint", {}).get("globals", [])
    if container_hosts:
        for k, v in fingerprint(" ".join(container_hosts), source="gtm-container").items():
            widened.setdefault(k, v)
    for k, v in widened.items():
        if k not in report["fingerprints"]:
            report["fingerprints"][k] = v
    report["stack_sources_note"] = (
        "A vendor seen only in 'csp' or 'gtm-container' is still live -- it is "
        "loaded at runtime rather than written into the served HTML.")

    # --- VWO settings file (live test names) ------------------------------
    # Runs last, because VWO is usually detected from the container rather than
    # the HTML, so the fingerprint set has to be widened first.
    if "VWO" in report["fingerprints"]:
        acct = None
        for c in report["gtm_containers"].values():
            if isinstance(c, dict):
                got = (c.get("vendor_account_ids") or {}).get("vwo")
                if got:
                    acct = got[0]
                    break
        acct = acct or discover_vwo_account(html, json.dumps(report["gtm_containers"], default=str))
        if acct:
            report["experimentation"][f"vwo:{acct}"] = fetch_vwo(acct, report["final_url"] or url)
        else:
            report["collection_notes"].append(
                "VWO detected but no account ID found; grep the raw gtm.js for _vwo_code "
                "or visualwebsiteoptimizer.com/j.php?a=")

    return report


def render(r):
    L = []
    a = L.append
    a(f"# Public recon: {r['final_url']}   (HTTP {r['http_status']}, {r['html_bytes']:,} bytes HTML)")
    ident = r.get("identity") or {}
    if ident.get("warning"):
        a("")
        a("!! " + ident["warning"])
        a("")
    if r["meta"]["title"]:
        a(f"title: {r['meta']['title']}")
    if r["meta"]["generator"]:
        a(f"generator: {r['meta']['generator']}")
    if r["meta"]["hreflang_locales"]:
        a(f"locales (hreflang): {', '.join(r['meta']['hreflang_locales'])}")

    a("\n## Response headers")
    for k, v in r["response_headers"].items():
        a(f"  {k}: {str(v)[:220]}")
    if r["cookies_set_on_first_load"]:
        a(f"  cookies set pre-consent: {', '.join(r['cookies_set_on_first_load'])}")

    if r["csp"]:
        doms = r["csp"]["allowed_domains"]
        a(f"\n## CSP allowlist ({len(doms)} domains) - their own inventory of third parties")
        if len(doms) < 3:
            a("  PERMISSIVE OR EMPTY CSP (e.g. default-src *): this says nothing about their stack.")
            a("  Read it as 'no information', not as a small vendor footprint.")
        a("  " + ", ".join(doms[:80]))
    else:
        a("\n## CSP: none served")
        a("  No CSP means the stack-enumeration shortcut is unavailable here, not that the")
        a("  stack is thin. Fall back to container hosts and the --pages sweep.")

    strong = {k: v for k, v in r["fingerprints"].items() if v.get("evidence") != "weak"}
    weak = {k: v for k, v in r["fingerprints"].items() if v.get("evidence") == "weak"}
    a(f"\n## Vendor fingerprints ({len(strong)} with load evidence)")
    for k, v in strong.items():
        ids = f"  ids={v['ids']}" if v["ids"] else ""
        src = v.get("source", "html")
        tag = "" if src == "html" else f"  [seen in {src}]"
        a(f"  {k:<28} x{v['occurrences']:<3} {v['implies']}{ids}{tag}")
    if r.get("stack_sources_note"):
        a(f"  note: {r['stack_sources_note']}")
    if weak:
        a(f"\n## Name mentioned, no load evidence ({len(weak)}) -- verify before reporting")
        a("   (a bare product name also matches an integrations directory or a competitor list)")
        for k, v in weak.items():
            a(f"  {k:<28} x{v['occurrences']:<3} matched text only")

    if r["third_party_hosts"]:
        a("\n## Third-party hosts referenced in HTML")
        for h in r["third_party_hosts"][:35]:
            a(f"  {h['refs']:>3}  {h['host']}")

    for path, e in r["well_known"].items():
        a(f"\n## {path}  ({e['bytes']:,} bytes)")
        if e.get("ai_crawler_verdicts"):
            a("  AI crawlers:")
            for agent, verdict in sorted(e["ai_crawler_verdicts"].items()):
                a(f"    {agent:<28} {verdict}")
        elif path == "/robots.txt":
            a("  AI crawlers: none named in robots.txt")
        if e.get("content_signal"):
            a(f"  Content-Signal: {e['content_signal']}")
        for key in ("disallow", "sitemaps", "sellers", "user_agents"):
            if e.get(key):
                a(f"  {key}: {', '.join(map(str, e[key][:40]))}")
        if e.get("excerpt"):
            a("  " + e["excerpt"][:400].replace("\n", "\n  "))

    if r["sitemap"]:
        s = r["sitemap"]
        a(f"\n## Sitemap ({s['total_urls']:,} URLs from {s['source']})")
        a("  top path prefixes: " + ", ".join(f"{k} ({v})" for k, v in list(s["path_prefixes"].items())[:25]))

    if r["gtag"]:
        a("\n## gtag/js")
        for gid, v in r["gtag"].items():
            a(f"  {gid}: linked -> {', '.join(v.get('linked_ids', [])) or v.get('error', 'none')}")

    if r["experimentation"]:
        a("\n## Experimentation datafiles (public)")
        for u, v in r["experimentation"].items():
            a(f"  {u}")
            for key in ("experiments", "feature_flags", "events", "audiences",
                        "readable_names", "campaign_names", "campaign_types"):
                if v.get(key):
                    a(f"    {key} ({len(v[key])}): {', '.join(map(str, v[key][:30]))}")
            if v.get("note"):
                a(f"    note: {v['note']}  ({v.get('format','')}, {v.get('bytes',0):,} bytes)")

    if r["dns"]:
        a("\n## DNS")
        for k, v in r["dns"].items():
            a(f"  {k}: {', '.join(map(str, v[:8]))[:400]}")

    if r["extra_pages"]:
        a("\n## Extra pages")
        for p, v in r["extra_pages"].items():
            a(f"  {p}  HTTP {v['status']}")
            if v.get("new_fingerprints"):
                a(f"    page-only vendors: {', '.join(v['new_fingerprints'])}")
            if v.get("name_only_no_load_evidence"):
                a(f"    name-only on this page (verify): {', '.join(v['name_only_no_load_evidence'])}")
            if v.get("forms"):
                a(f"    form actions: {', '.join(v['forms'][:8])}")

    cd_ = r.get("container_discovery") or {}
    a(f"\n## GTM containers found: {', '.join(r['gtm_containers'].keys()) or 'none'}")
    if cd_:
        a(f"   from pages: {', '.join(cd_.get('from_pages', [])) or 'none'}"
          f"   from zones: {', '.join(cd_.get('from_zones', [])) or 'none'}")
        if cd_.get("sibling_hosts"):
            a(f"   other hosts on this domain (may have their own containers): "
              f"{', '.join(cd_['sibling_hosts'])}")
    a("   (run scripts/gtm_parse.py <ID> for the full container read, or see --json)")
    if r["collection_notes"]:
        a("\n## Collection notes")
        for n in r["collection_notes"]:
            a(f"  - {n}")
    return "\n".join(L)


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    target = argv[0]
    pages = []
    out_json = None
    if "--pages" in argv:
        pages = [p.strip() for p in argv[argv.index("--pages") + 1].split(",") if p.strip()]
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("--") else "recon.json"
    rep = collect(target, pages, do_containers="--no-containers" not in argv)
    print(render(rep))
    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print(f"\n[raw facts written to {out_json}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
