#!/usr/bin/env python3
"""Read the two most commercially revealing public pages: pricing and signup.

The container tells you how a company *measures* its funnel. The pricing page
tells you what the funnel is selling, in what units, and -- the single most
useful fact here -- the exact tier where self-serve stops and a salesperson
starts. The signup page tells you what they demand before they will let someone
in. Both are served to every visitor, with no login and no form submission.

The two layers corroborate each other. A container that carries SKU strings like
`org-true-tier-1-premium` is naming plans that the pricing page renders in
English, and a conversion gated on the signup form's `userGoal` field is gated on
a field this script can read off the markup. Run both and reconcile them; a plan
name that appears in one and not the other is a finding.

Usage:
    python3 -I pricing.py example.com
    python3 -I pricing.py example.com --json              # machine-readable, stdout
    python3 -I pricing.py example.com --json out.json     # ... or to a file
    python3 -I pricing.py example.com --pricing /plans --signup /start

READ-ONLY, AND THE SIGNUP BOUNDARY IS NOT NEGOTIABLE.
This script issues GET requests and parses markup. It does not submit the signup
form, create an account, enter data into any field, or POST anything anywhere.
The signup section describes the form's *structure* -- fields, requirements, SSO
options, where it posts -- read off the HTML. Anyone extending this file must
keep it that way: a tool that creates test accounts in someone else's product is
a different and unwelcome tool.

Honest limits, repeated in the output because they change how the numbers read:
  - Modern pricing pages are frequently client-rendered. When the prices are not
    in the served HTML this script says so instead of printing an empty table.
  - Prices are reported verbatim, with whatever currency marker was found. No
    conversion, no normalisation.
  - This vantage point cannot change its apparent geography. Prices gated on IP
    geolocation are invisible here, and "same as base" below means same *from
    here*.
"""

import html as htmllib
import json
import re
import subprocess
import sys
import gzip
import zlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TIMEOUT = 25

# Guessed last. The whole point of trying the sitemap and the nav first is that a
# guess which happens to 200 on a soft-404 marketing page produces a confident
# empty read, which is the worst outcome this script can have.
PRICING_GUESSES = ["/pricing", "/plans", "/pricing-plans", "/price", "/pricing/plans",
                   "/en/pricing", "/pricing/", "/plans-pricing"]

SIGNUP_GUESSES = ["/signup", "/sign-up", "/register", "/free-trial", "/trial",
                  "/get-started", "/start", "/create-account", "/users/sign_up",
                  "/signup/", "/account/signup"]

# Tier vocabulary. Used to anchor card segmentation and to recognise a tier name
# that carries no price (a "Contact sales" top tier has a heading and no number).
# Deliberately excludes words that are common in feature copy -- "platform",
# "core", "company" and "max" each turned a product paragraph into a phantom tier
# on a live run, and a phantom tier is a wrong fact, not a harmless extra row.
TIER_WORDS = (r"free|freemium|starter|basic|essential(?:s)?|individual|personal|standard|"
              r"plus|pro(?:fessional)?|premium|advanced|business|team(?:s)?|growth|scale|"
              r"enterprise\+?|ultimate|custom|hobby|lite|solo|startup|suite")

# The marker for "we want you to buy this one". Only badge-grade signals: a bare
# "highlight" or "featured" class appears all over a modern marketing page and
# marked every tier as promoted on three of three live targets, which is the same
# as marking none.
PROMO_RE = re.compile(
    r"(most[\s_-]?popular|most[\s_-]?valuable|best[\s_-]?value|best[\s_-]?seller|"
    r"recommended|popular[\s_-]?plan|is[\s_-]?popular|our[\s_-]?pick|"
    r"data-popular|badge[\s_-]?popular)", re.I)

# Where self-serve ends. A top tier with a button instead of a number means the
# company has decided that above some size, a human has to be involved.
CONTACT_SALES_RE = re.compile(
    r"(contact\s+(?:us|sales|our\s+sales)|talk\s+to\s+(?:sales|us|an?\s+expert)|"
    r"get\s+(?:a\s+)?(?:quote|custom\s+(?:quote|pricing))|request\s+(?:a\s+)?(?:quote|pricing|demo)|"
    r"custom\s+pricing|let's\s+talk|speak\s+(?:to|with)\s+(?:sales|us)|book\s+a\s+(?:call|demo))", re.I)

CUR = (r"US\$|A\$|C\$|CA\$|NZ\$|HK\$|S\$|R\$|\$|€|£|¥|₹|₪|₩|₺|₽|฿|"
       r"CHF|SEK|NOK|DKK|PLN|CZK|HUF|TRY|AUD|CAD|USD|EUR|GBP|JPY|INR|BRL|MXN|SGD|ZAR|AED|ILS")
NUM = r"\d{1,3}(?:[ .,]\d{3})*(?:[.,]\d{1,2})?"
PRICE_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9])(" + CUR + r")\s?(" + NUM + r")(?![\d])")
PRICE_SUFFIX_RE = re.compile(r"(?<![A-Za-z0-9.])(" + NUM + r")\s?(€|£|kr|zł|Kč|Ft|CHF|USD|EUR|GBP|"
                             r"SEK|NOK|DKK|PLN|JPY|INR|BRL|MXN|TRY)(?![A-Za-z0-9])")

PERIOD_RE = re.compile(
    r"(/\s?mo(?:nth)?\b|per\s+month|monthly|a\s+month|/\s?yr\b|/\s?year\b|per\s+year|"
    r"annual(?:ly)?|a\s+year|per\s+quarter|quarterly|one[\s-]?time|lifetime|forever)", re.I)

SEAT_RE = re.compile(
    r"(per\s+(?:seat|user|member|agent|editor|contributor|person|employee|license|licence)|"
    r"/\s?(?:seat|user|member|agent)\b|each\s+user|per[\s-]user\b|per[\s-]seat\b|"
    # monday.com prints "$ 9 seat / month" with no "per" anywhere, and without
    # this branch a per-seat page reported its billing basis as "not stated".
    r"\b(?:seat|user|member|agent|editor)\s?/\s?(?:mo\b|month|yr\b|year))", re.I)

FLAT_RE = re.compile(r"(flat\s+(?:rate|fee|price)|per\s+(?:workspace|org(?:anisation|anization)?|"
                     r"site|project|account|company|team)\b|unlimited\s+(?:users|seats|members))", re.I)

MIN_SEAT_RES = [
    re.compile(r"minimum\s+(?:of\s+)?(\d{1,4})\s+(?:seat|user|member|agent|licen[cs]e)", re.I),
    re.compile(r"(\d{1,4})\s+(?:seat|user|member|agent|licen[cs]e)s?\s+minimum", re.I),
    re.compile(r"min(?:imum)?\.?\s*(\d{1,4})\s*(?:seat|user|member)", re.I),
    re.compile(r"(?:starts?|starting)\s+(?:at|from)\s+(\d{1,4})\s+(?:seat|user|member)", re.I),
    re.compile(r"(?:multiples?|packs?|increments?)\s+of\s+(\d{1,4})\s*(?:seat|user|member)?", re.I),
    re.compile(r"(\d{1,4})\s*(?:seat|user|member)s?\s+included", re.I),
]

SAVING_RE = re.compile(
    r"(sav(?:e|ings?)\s+(?:up\s+to\s+)?[^.<,;]{0,24}?\d{1,3}\s?%|\d{1,3}\s?%\s+(?:off|discount|cheaper|savings)|"
    r"sav(?:e|ings?)\s+(?:up\s+to\s+)?(?:" + CUR + r")\s?" + NUM + r")", re.I)

SSO_PROVIDERS = [
    ("Google", r"accounts\.google\.com|/auth/google|google[-_ ]?oauth|sign\s*(?:in|up)\s*with\s*google|continue\s+with\s+google"),
    ("Microsoft", r"login\.microsoftonline\.com|/auth/(?:microsoft|azure)|with\s*microsoft|azure[-_ ]?ad"),
    ("Apple", r"appleid\.apple\.com|/auth/apple|with\s*apple"),
    ("GitHub", r"github\.com/login/oauth|/auth/github|with\s*github"),
    ("GitLab", r"gitlab\.com/oauth|/auth/gitlab|with\s*gitlab"),
    ("Slack", r"slack\.com/oauth|/auth/slack|with\s*slack"),
    ("LinkedIn", r"linkedin\.com/oauth|/auth/linkedin|with\s*linkedin"),
    ("Facebook", r"facebook\.com/(?:v\d+\.\d+/)?dialog/oauth|/auth/facebook|with\s*facebook"),
    ("Okta", r"okta\.com|/auth/okta"),
    ("SAML / generic SSO", r"\bsaml\b|/auth/saml|single\s+sign[-\s]?on|\bsso\b"),
    ("Intuit", r"intuit\.com/oauth|with\s*intuit"),
]

FREE_MAIL_RE = re.compile(r"gmail\.com|yahoo\.(?:com|co\.\w+)|hotmail\.com|outlook\.com|aol\.com|"
                          r"icloud\.com|proton(?:mail)?\.(?:com|me)|gmx\.|mail\.ru|qq\.com", re.I)

WORK_EMAIL_RE = re.compile(r"(work\s+e-?mail|business\s+e-?mail|company\s+e-?mail|corporate\s+e-?mail|"
                           r"professional\s+e-?mail|e-?mail\s+(?:profissional|professionnelle|"
                           r"aziendale|empresarial)|arbeits-?e-?mail|gesch[äa]ftliche\s+e-?mail)", re.I)

STEP_RE = re.compile(r"(step\s*\d\s*(?:of|/)\s*\d|data-step|aria-current=[\"']step|"
                     r"class=[\"'][^\"']*(?:stepper|multi-?step|wizard|onboarding-step|progress-step)|"
                     r"\bnext\s+step\b)", re.I)

PROMISE_RES = [
    ("free trial", r"\b(?:free|kostenlos\w*|gratis)\s+trial|trial\s+(?:for\s+)?free|start\s+(?:your\s+)?free\s+trial"),
    ("trial length", r"\b(\d{1,3})[\s-]day\s+(?:free\s+)?trial|trial\s+(?:of\s+)?(\d{1,3})\s+days"),
    ("no credit card", r"no\s+credit\s+card|credit\s+card\s+not\s+required|without\s+a\s+credit\s+card"),
    ("free forever / freemium", r"free\s+forever|forever\s+free|free\s+plan|always\s+free|free\s+for(?:ever)?\s+up\s+to"),
    ("demo / sales-assisted", r"book\s+a\s+demo|request\s+a\s+demo|get\s+a\s+demo|schedule\s+a\s+(?:demo|call)|talk\s+to\s+sales"),
    ("card required up front", r"card\s+(?:details|required)|payment\s+(?:details|method)\s+required|enter\s+(?:your\s+)?card"),
]

# A SKU identifier hardcoded into page JSON is the plan's internal name, which is
# exactly the string a GTM lookup table or a conversion label is keyed on. These
# are worth surfacing on their own so they can be grepped against gtm_parse.py
# output -- that reconciliation is the point of running both scripts.
SKU_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[-_][a-z0-9]+){2,7}\b")
SKU_HINT_RE = re.compile(r"(tier|plan|sku|premium|basic|pro\b|standard|enterprise|starter|business|"
                         r"seat|annual|monthly|yearly|subscription|price|package|bundle)", re.I)


def norm_url(u):
    if not re.match(r"^https?://", u):
        u = "https://" + u
    return u


def fetch_full(url, timeout=TIMEOUT):
    """Fetch body + headers + final URL, falling back to curl.

    Same shape as recon.py's fetcher, and the curl fallback exists for the same
    reason: a stock macOS Python frequently has no usable CA store, and silently
    reporting "page not found" because of a local certificate problem would look
    identical to a site that has no pricing page.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate", "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        ctx = ssl.create_default_context()
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
        return {"url": url, "status": e.code, "body": "",
                "headers": dict(e.headers.items()) if e.headers else {}}
    except Exception:
        return _curl_full(url, timeout)


def _curl_full(url, timeout):
    try:
        p = subprocess.run(
            ["curl", "-sSL", "--compressed", "-D", "-", "--max-time", str(timeout),
             "-A", UA, "-H", "Accept-Language: en-US,en;q=0.9",
             "-w", "\n__FINAL__%{url_effective}\n__STATUS__%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 15)
        out = p.stdout
        final = re.search(r"\n__FINAL__(.*)", out)
        status = re.search(r"\n__STATUS__(\d+)", out)
        out = re.sub(r"\n__FINAL__.*", "", out, flags=re.S)
        parts = re.split(r"\r?\n\r?\n", out)
        headers, body = {}, out
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


def strip_tags(frag):
    frag = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", frag)
    frag = re.sub(r"(?s)<[^>]+>", " ", frag)
    return re.sub(r"\s+", " ", htmllib.unescape(frag)).strip()


def visible_text(doc):
    return strip_tags(doc)


def markup_only(doc):
    """Drop inline <style> and <script> before segmenting the page.

    Not cosmetic: 420KB of monday.com's 1.4MB pricing page is inline CSS, and the
    class names inside it contain `is-most-popular`, so the promotion detector
    fired on a stylesheet rule instead of the badge and then could not attribute
    it to any tier. Embedded JSON is mined from the raw document separately.
    """
    doc = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", doc)
    return re.sub(r"(?is)<svg[^>]*>.*?</svg>", " ", doc)


# ---------------------------------------------------------------------------
# Discovery. Sitemap and nav evidence beat a guess, because a guessed path that
# returns 200 on a catch-all route reads exactly like a real pricing page with
# no prices on it.
# ---------------------------------------------------------------------------

def nav_pricing_links(doc, origin, own_host):
    """Links the site itself labels as pricing, which is stronger than a guess."""
    out = []
    for m in re.finditer(r"""<a[^>]+href=["']([^"'#?]+)[^>]*>(.{0,120}?)</a>""", doc, re.I | re.S):
        href, label = m.group(1), strip_tags(m.group(2))
        if not re.search(r"(?i)(pricing|^/plans?$|/plans/|preise|precios|prix|prezzi|pre[cç]os|"
                         r"pricing-plans|price-list)", href) and not re.search(
                r"(?i)^(pricing|plans|preise|precios|tarifs?|prezzi|pre[cç]os)$", label.strip()):
            continue
        u = urllib.parse.urljoin(origin, href)
        if urllib.parse.urlparse(u).netloc.replace("www.", "") != own_host.replace("www.", ""):
            continue
        out.append({"url": u.rstrip("/") or u, "label": label[:60] or None, "source": "nav/body link"})
    seen, uniq = set(), []
    for c in out:
        if c["url"] not in seen:
            seen.add(c["url"])
            uniq.append(c)
    return uniq[:8]


def sitemap_pricing_urls(origin):
    """Pull pricing-looking URLs out of robots.txt's declared sitemaps.

    A sitemap hit is the strongest discovery signal available: the company
    published the URL itself, and it also exposes the locale-prefixed variants
    without having to guess which locale prefixes exist.
    """
    found, notes = [], []
    rb = fetch_full(origin + "/robots.txt", timeout=15)
    maps = sorted(set(re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", rb["body"]))) if rb["status"] == 200 else []
    if not maps:
        maps = [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
        notes.append("robots.txt declared no sitemap; tried /sitemap.xml directly")
    locs = []
    for sm in maps[:2]:
        r = fetch_full(sm, timeout=20)
        if r["status"] != 200 or "<" not in r["body"]:
            continue
        children = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", r["body"], re.S)
        locs += re.findall(r"<loc>(.*?)</loc>", r["body"])
        # One level of index expansion, and only into children whose own name
        # suggests marketing pages -- a 40-file sitemap index is mostly blog.
        for c in children:
            if re.search(r"(?i)(page|pricing|product|marketing|site|main|en|www)", c):
                rc = fetch_full(c.strip(), timeout=20)
                if rc["status"] == 200:
                    locs += re.findall(r"<loc>(.*?)</loc>", rc["body"])
            if len(locs) > 20000:
                break
    for u in locs:
        u = htmllib.unescape(u.strip())
        path = urllib.parse.urlparse(u).path
        if re.search(r"(?i)(^|/)(pricing|plans|preise|precios|tarifs|prezzi|pre[cç]os)(/|$|-plans)", path):
            found.append(u)
    return sorted(set(found))[:60], notes


def pick_pricing(origin, home_doc, own_host):
    """Rank candidates, fetch in order, and return the first that looks real."""
    nav = nav_pricing_links(home_doc, origin, own_host)
    sm, notes = sitemap_pricing_urls(origin)

    # Shortest sitemap path first: /pricing beats /pricing/enterprise-faq.
    sm_short = sorted(sm, key=lambda u: (len(urllib.parse.urlparse(u).path), u))
    nav_urls = [c["url"] for c in nav]

    # Shortest nav path first for the same reason as the sitemap list: HubSpot's
    # nav links /pricing/marketing before /pricing, and the product-specific page
    # is a narrower read of the company than the index.
    nav_urls = sorted(nav_urls, key=lambda u: (len(urllib.parse.urlparse(u).path), u))
    cands = []
    for u in nav_urls:
        src = "nav link + sitemap" if any(u.rstrip("/") == s.rstrip("/") for s in sm) else "nav link"
        cands.append({"url": u, "source": src})
    for u in sm_short[:6]:
        if not any(c["url"].rstrip("/") == u.rstrip("/") for c in cands):
            cands.append({"url": u, "source": "sitemap"})
    for p in PRICING_GUESSES:
        u = origin + p
        if not any(c["url"].rstrip("/") == u.rstrip("/") for c in cands):
            cands.append({"url": u, "source": "guessed path"})

    tried = []
    for c in cands[:12]:
        r = fetch_full(c["url"])
        looks = bool(PRICE_PREFIX_RE.search(r["body"]) or PRICE_SUFFIX_RE.search(r["body"])
                     or re.search(r"(?i)(pricing|plans?|per\s+(?:month|seat|user))", visible_text(r["body"])[:6000]))
        tried.append({"url": c["url"], "source": c["source"], "status": r["status"], "plausible": looks})
        if r["status"] == 200 and looks:
            return {"chosen": c["url"], "final_url": r["url"], "source": c["source"],
                    "tried": tried, "notes": notes, "sitemap_pricing_urls": sm,
                    "nav_links": nav}, r
    return {"chosen": None, "final_url": None, "source": None, "tried": tried,
            "notes": notes + ["no pricing page responded with pricing-shaped content"],
            "sitemap_pricing_urls": sm, "nav_links": nav}, None


# ---------------------------------------------------------------------------
# Rendering verdict. This runs before anything is reported, because the whole
# output means something different when the prices were never in the HTML.
# ---------------------------------------------------------------------------

SPA_MARKERS = [
    ("__NEXT_DATA__", r"__NEXT_DATA__"),
    ("Next.js streamed payload", r"self\.__next_f|__next_f\.push"),
    ("Nuxt", r"__NUXT__|/_nuxt/"),
    ("Remix", r"__remixContext"),
    ("Astro islands", r"astro-island"),
    ("React root", r"id=[\"']root[\"']|data-reactroot"),
    ("SvelteKit", r"__sveltekit_"),
    ("Angular", r"ng-version|<app-root"),
    ("Gatsby", r"___gatsby|window\.___chunkMapping"),
]


def rendering_verdict(doc, prices, json_price_count):
    markers = [name for name, pat in SPA_MARKERS if re.search(pat, doc, re.I)]
    text_len = len(visible_text(doc))
    if prices:
        verdict = "server-rendered: prices are present in the served HTML"
    elif json_price_count:
        verdict = ("client-rendered page, but prices were recovered from an embedded JSON "
                   "payload (__NEXT_DATA__ / JSON-LD) rather than from the markup")
    else:
        verdict = ("CLIENT-RENDERED OR GATED: no prices in the served HTML and none in any "
                   "embedded JSON payload. The prices could NOT be read. This is not evidence "
                   "that the page has no tiers -- load it in a browser to read them.")
    return {"verdict": verdict, "framework_markers": markers,
            "visible_text_chars": text_len,
            "thin_html": text_len < 1200,
            "prices_in_markup": len(prices), "prices_in_json": json_price_count}


# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------

def find_prices(doc):
    """Every currency-marked number in the page, verbatim, with its context.

    Literals are never converted or normalised: `€9` and `$9` are different
    facts, and a script that silently harmonised them would destroy the one thing
    the locale sweep below is looking for.
    """
    text = visible_text(doc)
    out = []
    # Suffix-currency pages are parsed first so their markers are claimed. On a
    # German page reading "24,00 € ... 65,00 €", the prefix pattern otherwise
    # pairs one price's trailing euro sign with the NEXT price's number and
    # invents "€ 65,00" as a separate price.
    claimed = []
    for m in PRICE_SUFFIX_RE.finditer(text):
        out.append((m.start(), m.group(2), m.group(1), m.group(0)))
        claimed.append((m.start(), m.end()))
    for m in PRICE_PREFIX_RE.finditer(text):
        if any(lo <= m.start(1) < hi for lo, hi in claimed):
            continue
        out.append((m.start(), m.group(1), m.group(2), m.group(0)))
    seen, prices = set(), []
    for pos, cur, num, literal in sorted(out):
        ctx = text[max(0, pos - 90):pos + 120]
        per = PERIOD_RE.search(text[pos:pos + 90]) or PERIOD_RE.search(ctx)
        seat = SEAT_RE.search(ctx)
        key = (literal, ctx[:40])
        if key in seen:
            continue
        seen.add(key)
        prices.append({
            "literal": literal.strip(), "currency_marker": cur, "amount_as_written": num,
            "period": per.group(0).strip().lower() if per else None,
            "per_unit": seat.group(0).strip().lower() if seat else None,
            "context": ctx.strip()[:160],
            # The words that FOLLOW a price are the ones that qualify it. The
            # wider context bleeds in the previous tier's "billed annually" and
            # then both prices in a card classify as the same kind, which loses
            # the monthly/annual pair entirely.
            "trailing": text[pos:pos + 90].strip(),
        })
    return prices


def price_literals(doc):
    """Just the distinct literals, for comparing one locale page against another."""
    return sorted({p["literal"] for p in find_prices(markup_only(doc))})


def price_amounts(doc):
    """The numeric amounts, used only to tell a real price difference from a
    formatting one.

    Asana's German page writes `$10,99` where its English page writes `$10.99`:
    same price, local decimal separator. Comparing raw literals called that a
    market with different pricing, which is a wrong finding. The literals are
    still what gets reported; this set only decides the verdict.
    """
    out = set()
    for p in find_prices(markup_only(doc)):
        v = _to_float(p["amount_as_written"])
        if v is not None:
            out.add(round(v, 2))
    return out


def walk_json(node, path="", out=None, depth=0):
    if out is None:
        out = []
    if depth > 14:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            walk_json(v, f"{path}.{k}" if path else str(k), out, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:400]):
            walk_json(v, f"{path}[{i}]", out, depth + 1)
    else:
        out.append((path, node))
    return out


PRICE_KEY_RE = re.compile(r"(?i)(price|amount|cost|msrp|rate|fee)(?!less)")
PLANKEY_RE = re.compile(r"(?i)(plan|tier|sku|product|package|offer|subscription|edition)")
CUR_KEY_RE = re.compile(r"(?i)(currency|currencyCode|priceCurrency|iso)")


def mine_embedded_json(doc):
    """Mine __NEXT_DATA__, JSON-LD and other inline JSON blobs for real numbers.

    This is where a client-rendered pricing page usually gives itself away: the
    page body is empty but the props the client will render are serialised into
    the HTML in full, SKU identifiers and all.
    """
    found = {"sources": [], "price_fields": [], "offers": [], "plan_names": [], "currencies": []}
    blobs = []

    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', doc, re.S)
    if m:
        blobs.append(("__NEXT_DATA__", m.group(1)))
    for mm in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', doc, re.S):
        blobs.append(("JSON-LD", mm.group(1)))
    for pat, name in ((r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>', "__NUXT__"),
                      (r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>', "__INITIAL_STATE__"),
                      (r'window\.__APOLLO_STATE__\s*=\s*(\{.*?\});?\s*</script>', "__APOLLO_STATE__")):
        mm = re.search(pat, doc, re.S)
        if mm:
            blobs.append((name, mm.group(1)))

    for name, raw in blobs:
        raw = raw.strip()
        try:
            data = json.loads(htmllib.unescape(raw) if "&quot;" in raw[:400] else raw)
        except Exception:
            continue
        found["sources"].append(name)
        pairs = walk_json(data)
        for path, val in pairs:
            leaf = path.split(".")[-1].split("[")[0]
            if CUR_KEY_RE.fullmatch(leaf) and isinstance(val, str) and 2 <= len(val) <= 4:
                found["currencies"].append(val)
            if PRICE_KEY_RE.search(leaf) and isinstance(val, (int, float, str)):
                s = str(val)
                if re.fullmatch(r"\d{1,7}(?:[.,]\d{1,2})?", s) and s not in ("0",) or \
                        (isinstance(val, str) and re.search(r"\d", s) and len(s) < 24):
                    found["price_fields"].append({"json_path": path, "value_as_written": s})
            if PLANKEY_RE.search(leaf) and isinstance(val, str) and 2 <= len(val) <= 48:
                # Anchored prefix matching turned "Product Management" into a
                # "Pro" tier, so a plan name must either BE a tier name or look
                # like a hyphenated internal identifier.
                if TIER_NAME_RE.match(val.strip()) or (
                        SKU_RE.fullmatch(val.strip()) and SKU_HINT_RE.search(val)):
                    found["plan_names"].append(val.strip())

        # JSON-LD Offer/Product is a declared, structured price: the strongest
        # form available, and on one live target the only place the real numbers
        # appear. The numbers are often not in `price` but inside a
        # priceSpecification list that carries the monthly and annual rate as
        # separate entries, which is exactly the pair the delta question needs.
        for node in _iter_dicts(data):
            t = node.get("@type") or node.get("type")
            types = [t] if isinstance(t, str) else (t if isinstance(t, list) else [])
            if not any(str(x).lower() in ("offer", "aggregateoffer", "product", "service",
                                          "softwareapplication") for x in types):
                continue
            offer = {k: node.get(k) for k in
                     ("name", "price", "priceCurrency", "lowPrice", "highPrice",
                      "billingDuration", "unitText", "sku", "availability", "description")
                     if node.get(k) is not None}
            spec = node.get("priceSpecification")
            specs = []
            for s in (spec if isinstance(spec, list) else [spec] if isinstance(spec, dict) else []):
                if not isinstance(s, dict):
                    continue
                keep = {k: s.get(k) for k in
                        ("price", "priceCurrency", "billingDuration", "billingIncrement",
                         "unitCode", "unitText", "name", "referenceQuantity", "description")
                        if s.get(k) is not None}
                if keep.get("price") is not None:
                    specs.append(keep)
            if specs:
                offer["priceSpecification"] = specs
            if offer.get("description"):
                offer["description"] = str(offer["description"])[:120]
            if offer.get("price") is not None or offer.get("lowPrice") is not None or specs:
                offer["@type"] = types
                found["offers"].append(offer)

    for k in ("price_fields", "plan_names", "currencies"):
        if k == "price_fields":
            seen, uniq = set(), []
            for p in found[k]:
                key = (p["json_path"], p["value_as_written"])
                if key not in seen:
                    seen.add(key)
                    uniq.append(p)
            found[k] = uniq[:60]
        else:
            found[k] = sorted(set(found[k]))[:40]
    found["offers"] = found["offers"][:20]
    return found


def _iter_dicts(node, depth=0):
    if depth > 14:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v, depth + 1)
    elif isinstance(node, list):
        for v in node[:400]:
            yield from _iter_dicts(v, depth + 1)


# ---------------------------------------------------------------------------
# Card segmentation. Tier headings anchor the page: everything between one
# heading and the next belongs to that tier, which is how a price, a CTA and a
# "most popular" badge get attributed to the right plan.
# ---------------------------------------------------------------------------

HEADING_RE = re.compile(
    r"<(h[1-6]|dt|strong)[^>]*>(.{0,90}?)</\1>|"
    r"<[a-z]+[^>]*class=[\"'][^\"']*(?:plan-?(?:name|title)|tier-?(?:name|title)|"
    r"pricing-?(?:card|plan)-?title)[^\"']*[\"'][^>]*>(.{0,90}?)<", re.I | re.S)

# Plan names are often not headings at all -- monday.com renders "Free", "Basic",
# "Standard", "Pro", "Enterprise" in plain spans, so a heading-only sweep found
# three CTA sentences and none of the five tiers. This second pass accepts any
# short element whose entire text is a tier name.
EXACT_TIER_EL_RE = re.compile(
    r"<(span|div|p|h[1-6]|strong|b|li)[^>]*>\s*((?:[A-Z][A-Za-z+]{0,14}\s){0,2}"
    r"(?i:" + TIER_WORDS + r"))\s*</\1>")

TIER_NAME_RE = re.compile(r"(?i)^(?:the\s+)?(?:[a-z][a-z+]{0,14}\s){0,2}(" + TIER_WORDS +
                          r")(?:\s+(?:plan|tier|edition))?$")

# Notion renders its badge inside the plan heading, so the label comes back as
# "Business Recommended" and splits one plan into two cards. Strip the badge off
# the label; the proximity pass below still credits the badge to that tier.
PROMO_SUFFIX_RE = re.compile(r"\s*(most\s+popular|recommended|best\s+value|popular|our\s+pick)\s*$", re.I)


def _is_tier_label(label):
    """Filter out the marketing copy that merely contains a tier word.

    Every rejection here was a real false positive on a live page: FAQ questions
    ("Can I purchase enterprise plans online?"), feature headings ("Control
    access with custom permissions") and section straplines ("Trusted by teams
    everywhere.") all contain tier vocabulary and none of them is a tier.
    """
    if not label or len(label) > 36:
        return False
    if label[-1] in "?.:!,":
        return False
    if len(label.split()) > 4:
        return False
    if TIER_NAME_RE.match(label):
        return True
    return bool(re.search(r"(?i)\b(" + TIER_WORDS + r")\b", label))


def segment_cards(doc):
    """Attribute prices, CTAs, features and badges to the tier name above them.

    Cards with the same label are merged rather than listed twice: a monthly /
    annual toggle renders both price sets into the HTML, so every tier appears
    two or three times, and reporting "Starter" three times would read as three
    products.
    """
    anchors = {}
    for m in HEADING_RE.finditer(doc):
        label = PROMO_SUFFIX_RE.sub("", strip_tags(m.group(2) or m.group(3) or "")).strip()
        if _is_tier_label(label):
            anchors[m.start()] = label
    for m in EXACT_TIER_EL_RE.finditer(doc):
        label = PROMO_SUFFIX_RE.sub("", strip_tags(m.group(2))).strip()
        if _is_tier_label(label):
            anchors.setdefault(m.start(), label)

    positions = sorted(anchors)[:120]
    merged = OrderedDict()
    for i, pos in enumerate(positions):
        label = anchors[pos]
        end = positions[i + 1] if i + 1 < len(positions) else min(len(doc), pos + 4000)
        # A card window is capped, and it stops at the comparison table or the
        # footer. Without that the final anchor swallowed a 4-row comparison
        # table and the top tier was reported as carrying every price on the
        # page -- which inverted the self-serve verdict.
        stop = min(end, pos + 4000)
        for boundary in (r"<table\b", r"<footer\b"):
            bm = re.search(boundary, doc[pos:stop], re.I)
            if bm and bm.start() > 40:
                stop = pos + bm.start()
        frag = doc[pos:stop]
        text = visible_text(frag)
        ctas = []
        for mm in re.finditer(r"""<(?:a|button)\b([^>]*)>(.{0,140}?)</(?:a|button)>""", frag, re.I | re.S):
            attrs, inner = mm.group(1), strip_tags(mm.group(2))
            href = re.search(r"""href=["']([^"']+)""", attrs)
            if not inner or len(inner) > 60:
                continue
            if re.search(r"(?i)^(log\s?in|sign\s?in|learn\s+more|compare|see\s+(?:all|more)|"
                         r"privacy|cookie|terms|help\s+cent(?:er|re))", inner.strip()):
                continue
            ctas.append({"text": inner.strip(),
                         "href": htmllib.unescape(href.group(1)) if href else None})

        key = label.lower()
        card = merged.setdefault(key, {
            "tier_label": label, "prices": [], "cta": [], "promoted_marker": None,
            "contact_sales": None, "per_seat_language": None, "features_listed": [],
            "saving_copy": None, "occurrences": 0,
        })
        card["occurrences"] += 1
        seen_lit = {(p["literal"], p["period"]) for p in card["prices"]}
        for p in find_prices(frag):
            if (p["literal"], p["period"]) not in seen_lit:
                seen_lit.add((p["literal"], p["period"]))
                card["prices"].append(p)
        seen_cta = {(c["text"], c["href"]) for c in card["cta"]}
        for c in ctas:
            if (c["text"], c["href"]) not in seen_cta:
                seen_cta.add((c["text"], c["href"]))
                card["cta"].append(c)
        if not card["contact_sales"] and CONTACT_SALES_RE.search(text):
            card["contact_sales"] = CONTACT_SALES_RE.search(text).group(0)
        if not card["per_seat_language"] and SEAT_RE.search(text):
            card["per_seat_language"] = SEAT_RE.search(text).group(0).lower()
        if not card["saving_copy"] and SAVING_RE.search(text):
            card["saving_copy"] = SAVING_RE.search(text).group(0)
        feats = [strip_tags(x) for x in re.findall(r"(?is)<li[^>]*>(.{0,180}?)</li>", frag)]
        for f in feats:
            if 3 < len(f) < 160 and f not in card["features_listed"]:
                card["features_listed"].append(f)

    # Badges are attributed in one proximity pass over the whole page rather than
    # per card window. A "Most Popular" badge sits ABOVE the plan name in the
    # DOM, and a responsive page renders the same card set two or three times, so
    # a per-window search credited monday.com's single badge to all six tiers.
    # Here each badge votes for the nearest plan name after it, and the tier with
    # the most votes is the promoted one.
    votes = Counter()
    for m in PROMO_RE.finditer(doc):
        # Nearest by distance in either direction: a badge sits above the plan
        # name on monday.com and below it on other pages, so preferring one
        # direction credited the badge to the neighbouring tier half the time.
        near = [(min(abs(pos_ - m.end()), abs(m.start() - pos_)), pos_) for pos_ in positions]
        near = [(d, pos_) for d, pos_ in near if d <= 400]
        after = min(near)[1] if near else None
        if after is not None:
            key = anchors[after].lower()
            votes[key] += 1
            if key in merged:
                merged[key]["promoted_marker"] = m.group(0)
                merged[key]["promo_votes"] = votes[key]

    top = max(votes.values()) if votes else 0
    for c in merged.values():
        c["prices"] = c["prices"][:8]
        c["cta"] = c["cta"][:4]
        c["features_listed"] = c["features_listed"][:16]
        # Keep the badge only on the tier (or tied tiers) it was nearest to most
        # often; a single stray match on another card is noise, not a second badge.
        if c.get("promoted_marker") and c.get("promo_votes", 0) < top:
            c["promoted_marker"] = None

    # A tier has to carry commerce. A heading that mentions "enterprise" with no
    # price, no sales CTA and no plan CTA is prose, not a plan.
    kept = [c for c in merged.values()
            if c["prices"] or c["contact_sales"]
            or any(re.search(r"(?i)(sign[-\s]?up|get[-\s]?started|try|buy|start|trial|create[-\s]?account)",
                             (x["text"] or "") + " " + (x["href"] or "")) for x in c["cta"])]
    return kept[:16]


def feature_matrix(doc):
    """Read a plan-comparison table only when it is genuinely machine-readable.

    Where the comparison is drawn with divs, icons and no text, there is nothing
    honest to extract, so this returns nothing rather than a half-guessed matrix.
    """
    best = None
    for tm in re.finditer(r"(?is)<table[^>]*>(.*?)</table>", doc):
        tbl = tm.group(1)
        rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", tbl)
        if len(rows) < 3:
            continue
        parsed = []
        for row in rows:
            cells = [strip_tags(c) for c in re.findall(r"(?is)<t[hd][^>]*>(.*?)</t[hd]>", row)]
            if cells:
                parsed.append(cells)
        if not parsed:
            continue
        header = parsed[0]
        if len(header) < 3:
            continue
        plan_cols = [h for h in header[1:] if re.search(r"(?i)\b(" + TIER_WORDS + r")\b", h)]
        if len(plan_cols) < 2:
            continue
        # Rows where the cells differ are the actual gates; rows identical across
        # every plan are marketing filler.
        gates = []
        for r_ in parsed[1:]:
            if len(r_) < 2:
                continue
            vals = r_[1:]
            if len(set(v.strip().lower() for v in vals)) > 1:
                gates.append({"feature": r_[0][:90], "by_plan": dict(zip(header[1:], vals))})
        cand = {"header": header, "plan_columns": plan_cols,
                "rows_total": len(parsed) - 1, "differing_rows": len(gates),
                "gates": gates[:40]}
        if best is None or cand["differing_rows"] > best["differing_rows"]:
            best = cand
    return best


def pricing_shape(doc):
    text = visible_text(doc)
    mins = []
    for rx in MIN_SEAT_RES:
        for m in rx.finditer(text):
            mins.append({"stated": m.group(0).strip()[:90], "seats": m.group(1)})
    seat_hits = [m.group(0).lower() for m in SEAT_RE.finditer(text)][:8]
    flat_hits = [m.group(0).lower() for m in FLAT_RE.finditer(text)][:8]
    if seat_hits and not flat_hits:
        basis = "per seat / per user"
    elif flat_hits and not seat_hits:
        basis = "flat (per workspace / org / account)"
    elif seat_hits and flat_hits:
        basis = "mixed — both per-seat and flat language present; check per tier"
    else:
        basis = "not stated in the served HTML"
    return {
        "billing_basis": basis,
        "per_seat_phrases": sorted(set(seat_hits)),
        "flat_phrases": sorted(set(flat_hits)),
        "minimum_seats": mins[:6],
        "billing_periods_mentioned": sorted({m.group(0).strip().lower() for m in PERIOD_RE.finditer(text)}),
        "saving_copy": sorted({m.group(0).strip() for m in SAVING_RE.finditer(text)})[:8],
        "toggle_present": bool(re.search(r"(?i)(monthly|annual(?:ly)?)[^<]{0,40}(annual(?:ly)?|monthly)", text)
                               or re.search(r"(?i)class=[\"'][^\"']*(billing-)?toggle", doc)),
    }


def _to_float(s):
    """Parse a written amount without deciding what currency it is in.

    Separator conventions have to be handled properly, not approximately: the
    naive version read GitLab's German `$ 12.000` as 12.0 and then declared the
    German page's amounts different from the English page's, which is a wrong
    market finding produced entirely by a decimal point.
    """
    s = s.replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        # Whichever separator comes last is the decimal one.
        sep = "." if s.rfind(".") > s.rfind(",") else ","
        s = s.replace("," if sep == "." else ".", "").replace(sep, ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    elif "." in s:
        # A dot before exactly three trailing digits is a thousands separator in
        # continental Europe and Brazil; two digits is a decimal everywhere.
        s = s.replace(".", "") if re.search(r"\.\d{3}$", s) else s
    try:
        return float(s)
    except ValueError:
        return None


def _price_kind(p):
    """Classify a price as a monthly rate on an annual plan, a monthly rate on a
    monthly plan, or an annual total.

    This distinction is the whole difficulty of the monthly-vs-annual question.
    Almost every SaaS page prints the annual price already divided by twelve and
    labels it "per month, billed annually", so subtracting two on-page numbers
    can invent a 90% discount out of a 24% one. Only two prices of the SAME kind
    can be compared arithmetically.
    """
    ctx = (p.get("context") or "").lower()
    trail = (p.get("trailing") or "").lower()
    per = (p.get("period") or "").lower()
    cue = r"(billed annually|billed yearly|annual|yearly|per year|/yr|a year)"
    mcue = r"(billed monthly|month-to-month|monthly)"
    # Trailing text decides; only when it says nothing either way does the wider
    # context get a vote.
    annual_ctx = (re.search(cue, trail) or
                  (not re.search(mcue, trail) and re.search(cue, ctx)))
    if per and re.search(r"(mo\b|month)", per):
        return "monthly_rate_annual_plan" if annual_ctx else "monthly_rate_monthly_plan"
    if per and re.search(r"(yr|year|annual)", per):
        return "annual_total"
    return None


def _json_ld_pairs(emb):
    """Pull the monthly/annual pair out of JSON-LD priceSpecification entries.

    This is the cleanest source of the delta there is, because the markup version
    of the same page is ambiguous by design: monday.com renders one per-seat
    number plus a struck-through total, while its JSON-LD states both rates with
    `unitCode` MON and ANN and a description that says which is which.
    """
    pairs = []
    for o in emb.get("offers", []):
        specs = o.get("priceSpecification") or []
        kinds = {}
        for s in specs:
            tag = " ".join(str(s.get(k, "")) for k in
                           ("description", "unitCode", "unitText", "billingDuration", "name")).lower()
            kind = ("annual" if re.search(r"annual|yearly|\bann\b|\byear\b|p1y", tag) else
                    "monthly" if re.search(r"month|\bmon\b|p1m", tag) else None)
            if kind and kind not in kinds:
                kinds[kind] = s
        if "annual" not in kinds or "monthly" not in kinds:
            continue
        an, mo = kinds["annual"], kinds["monthly"]
        if an.get("priceCurrency") != mo.get("priceCurrency"):
            continue
        a, m = _to_float(str(an["price"])), _to_float(str(mo["price"]))
        if not a or not m or m <= 0 or a > m:
            continue
        pairs.append({
            "plan": o.get("name") or o.get("sku") or "(unnamed offer)",
            "currency": mo.get("priceCurrency"),
            "monthly_price": str(mo["price"]), "annual_price": str(an["price"]),
            "discount_for_paying_annually": f"{(1 - a / m) * 100:.0f}%",
            "source": "JSON-LD priceSpecification (the page's own structured data)",
            "monthly_label": mo.get("description") or mo.get("unitCode"),
            "annual_label": an.get("description") or an.get("unitCode"),
        })
    return pairs


def _table_pairs(fg):
    """Pair the "Annual subscription" and "Monthly subscription" rows of a
    comparison table.

    Zendesk publishes both per-agent-per-month rates as two rows of the same
    table -- $19 against $25 -- which is the cleanest statement of the annual
    discount on the page, and it is nowhere in the tier cards.
    """
    if not fg:
        return []
    gates = fg.get("gates") or []
    ann = next((g for g in gates if re.search(r"(?i)annual|yearly", g["feature"])), None)
    # The annual row is itself labelled "(per agent/month)", so a plain /month/
    # search picks it twice and reports a 0% discount. The monthly row is the one
    # that mentions a month and does NOT mention a year.
    mon = next((g for g in gates if re.search(r"(?i)month", g["feature"])
                and not re.search(r"(?i)annual|yearly", g["feature"])), None)
    if not ann or not mon or ann is mon:
        return []
    out = []
    for col, av in ann["by_plan"].items():
        mv = mon["by_plan"].get(col)
        if not mv:
            continue
        ma, mm_ = PRICE_PREFIX_RE.search(str(av)), PRICE_PREFIX_RE.search(str(mv))
        if not ma or not mm_ or ma.group(1) != mm_.group(1):
            continue
        a_, m_ = _to_float(ma.group(2)), _to_float(mm_.group(2))
        if not a_ or not m_ or m_ <= 0 or a_ > m_:
            continue
        out.append({
            "plan": col.strip()[:40],
            "monthly_rate": mm_.group(0), "annual_rate": ma.group(0),
            "discount_for_paying_annually": f"{(1 - a_ / m_) * 100:.0f}%",
            "source": f"comparison-table rows {mon['feature']!r} / {ann['feature']!r}",
        })
    return out


def monthly_annual_delta(cards, shape, emb=None, fg=None):
    """Report the monthly-vs-annual gap, and be explicit about its basis.

    Savings copy the company wrote itself is the observed fact and leads. A
    percentage is computed only between two same-unit monthly rates; an annual
    total beside a monthly rate is reported verbatim with no percentage, because
    the page did not say whether it is a yearly charge or a yearly-billed rate.
    """
    out = {"stated_savings": shape["saving_copy"], "comparable_pairs": [],
           "ambiguous_pairs": [], "structured_data_pairs": _json_ld_pairs(emb or {}),
           "comparison_table_pairs": _table_pairs(fg), "basis": None}
    for c in cards:
        kinds = {}
        for p in c["prices"]:
            k = _price_kind(p)
            if k and k not in kinds:
                kinds[k] = p
        ann_rate, mo_rate = kinds.get("monthly_rate_annual_plan"), kinds.get("monthly_rate_monthly_plan")
        if ann_rate and mo_rate and ann_rate["currency_marker"] == mo_rate["currency_marker"]:
            a, m = _to_float(ann_rate["amount_as_written"]), _to_float(mo_rate["amount_as_written"])
            if a and m and m > 0 and a <= m:
                out["comparable_pairs"].append({
                    "tier": c["tier_label"],
                    "monthly_plan_rate": mo_rate["literal"],
                    "annual_plan_rate": ann_rate["literal"],
                    "discount_for_paying_annually": f"{(1 - a / m) * 100:.0f}%",
                    "basis": "both figures are per-month rates in the same currency [observed]",
                })
                continue
        tot = kinds.get("annual_total")
        any_mo = ann_rate or mo_rate
        if tot and any_mo:
            out["ambiguous_pairs"].append({
                "tier": c["tier_label"],
                "monthly_literal": any_mo["literal"], "annual_literal": tot["literal"],
                "note": ("no percentage computed: the page did not say whether the annual figure "
                         "is a full-year charge or a yearly-billed monthly rate"),
            })
    if out["structured_data_pairs"] or out["comparison_table_pairs"]:
        out["basis"] = ("the page states both rates itself, in structured data or a comparison "
                        "table [observed] -- the strongest basis available for this question")
    elif out["stated_savings"]:
        out["basis"] = "savings copy written by the company [observed]"
    elif out["comparable_pairs"]:
        out["basis"] = "arithmetic on two same-unit per-month literals [observed inputs]"
    elif out["ambiguous_pairs"]:
        out["basis"] = "pairs found but not comparable; reported verbatim without a percentage"
    else:
        out["basis"] = "no monthly/annual delta readable from the served HTML"
    return out


def tiers_from_structured_data(emb):
    """Build a tier list out of JSON-LD offers.

    Worth its own section because it is the only thing that works on a fully
    client-rendered page: Atlassian's Jira pricing page serves 1.4MB of HTML with
    61 characters of visible text, and its JSON-LD still names Free, Standard and
    Premium with per-user prices. Labelled by source so no reader mistakes it for
    something that was visible on the page.
    """
    out = []
    for o in emb.get("offers", []):
        name = o.get("name") or o.get("sku")
        if not name:
            continue
        prices = []
        if o.get("price") is not None:
            prices.append({"price": str(o["price"]), "currency": o.get("priceCurrency"),
                           "label": o.get("billingDuration") or o.get("unitText")})
        for sp in o.get("priceSpecification") or []:
            prices.append({"price": str(sp["price"]), "currency": sp.get("priceCurrency"),
                           "label": sp.get("description") or sp.get("billingDuration")
                           or sp.get("unitCode")})
        if o.get("lowPrice") is not None:
            prices.append({"price": f"{o['lowPrice']}-{o.get('highPrice', '?')}",
                           "currency": o.get("priceCurrency"), "label": "price range"})
        if prices:
            out.append({"plan": str(name)[:48], "prices": prices[:4],
                        "description": o.get("description"),
                        "source": "JSON-LD structured data, not visible markup"})
    return out[:16]


def promoted_tier(cards, doc):
    hits = [{"tier": c["tier_label"], "marker": c["promoted_marker"],
             "votes": c.get("promo_votes")} for c in cards if c["promoted_marker"]]
    if len(hits) > 1:
        # More than one tier can genuinely be badged: a page carrying several
        # product lines promotes one tier in each. Say so rather than picking.
        return {"tier": None, "marker": hits[0]["marker"], "all_marked": hits,
                "note": ("several tiers carry a promotion badge, which usually means the page "
                         "lists more than one product line; check which block each badge sits in")}
    if hits:
        return {"tier": hits[0]["tier"], "marker": hits[0]["marker"], "all_marked": hits}
    m = PROMO_RE.search(doc)
    return {"tier": None, "marker": m.group(0) if m else None,
            "all_marked": [],
            "note": ("a promotion marker exists on the page but could not be attributed to a tier"
                     if m else "no visual-promotion marker found in the served HTML")}


def self_serve_boundary(cards, doc):
    """Where the price list stops and a salesperson starts.

    This is the most useful single fact on a pricing page: it is the company's
    own statement of the deal size it will not transact without a human, and it
    predicts which funnel stages their container will bother to optimise.
    """
    priced = [c["tier_label"] for c in cards if c["prices"]]
    sales = [c["tier_label"] for c in cards if c["contact_sales"] and not c["prices"]]
    both = [c["tier_label"] for c in cards if c["contact_sales"] and c["prices"]]
    verdict = None
    if priced and sales:
        # Document order matters: the boundary is the last priced tier that
        # appears BEFORE the first unpriced one, not the last priced tier on the
        # page (a footer plan card would otherwise move the line).
        first_sales_idx = next(i for i, c in enumerate(cards)
                               if c["contact_sales"] and not c["prices"])
        before = [c["tier_label"] for c in cards[:first_sales_idx] if c["prices"]]
        verdict = (f"self-serve through {(before or priced)[-1]!r}; "
                   f"{cards[first_sales_idx]['tier_label']!r} and above are sales-assisted "
                   f"(no price published)")
    elif priced and not sales:
        verdict = "every tier found carries a published price — fully self-serve as read here"
    elif sales and not priced:
        verdict = "no published prices at all; every tier routes to sales"
    return {"priced_tiers": priced, "contact_sales_tiers": sales,
            "priced_but_also_sales_cta": both,
            "top_tier_is_contact_sales": bool(sales),
            "verdict": verdict or "could not determine — no tiers were segmented"}


def sku_identifiers(doc):
    """Internal plan identifiers left in page JSON or data attributes.

    These are the strings that show up on the measurement side as lookup-table
    keys and conversion labels, which is what makes them worth pulling out
    separately: `org-true-tier-1-premium` in a container and the same string in a
    pricing payload is the same plan seen from two directions.
    """
    cands = set()
    for m in SKU_RE.finditer(doc):
        s = m.group(0)
        if len(s) > 60 or not SKU_HINT_RE.search(s):
            continue
        if re.search(r"(?i)(^|-)(css|svg|aria|data|btn|icon|img|font|grid|flex|text|bg|mb|mt|px|py|"
                     r"rounded|hover|focus|lg|md|sm|xl)(-|$)", s):
            continue
        cands.add(s)
    return sorted(cands)[:40]


# ---------------------------------------------------------------------------
# Locale / currency variants
# ---------------------------------------------------------------------------

LOCALE_GUESSES = ["de", "fr", "es", "pt-br", "ja", "it", "nl", "en-gb", "en-au", "in"]


def locale_variants(pricing_url, doc, base_literals, base_amounts, max_variants=6):
    """Fetch locale-prefixed pricing pages and report where the numbers differ.

    THE REAL LIMIT, stated here and in the output: this process cannot change its
    apparent geography. It sees what this vantage point is served. Prices chosen
    by IP geolocation, or by a currency the site picks for the visitor, are not
    visible, and a row below saying "same prices as base" means same *from here*
    -- not the same for a buyer in that market.
    """
    parsed = urllib.parse.urlparse(pricing_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_path = parsed.path or "/pricing"

    # hreflang alternates are the site's own list of its locale URLs, so they
    # beat guessing a prefix that may not exist.
    alts = []
    for m in re.finditer(r"""<link[^>]+rel=["']alternate["'][^>]*>""", doc, re.I):
        tag = m.group(0)
        hl = re.search(r"""hreflang=["']([^"']+)""", tag, re.I)
        hr = re.search(r"""href=["']([^"']+)""", tag, re.I)
        if hl and hr and hl.group(1).lower() not in ("x-default",):
            alts.append((hl.group(1), urllib.parse.urljoin(pricing_url, hr.group(1))))
    seen = set()
    cands = []
    for lang, u in alts:
        if u.rstrip("/") == pricing_url.rstrip("/") or u in seen:
            continue
        seen.add(u)
        cands.append({"locale": lang, "url": u, "source": "hreflang alternate"})
    if not cands:
        stripped = re.sub(r"^/[a-z]{2}(?:-[a-z]{2})?(/|$)", "/", base_path, flags=re.I)
        for loc in LOCALE_GUESSES:
            u = origin + "/" + loc + stripped
            if u not in seen:
                seen.add(u)
                cands.append({"locale": loc, "url": u, "source": "guessed locale prefix"})

    def one(c):
        r = fetch_full(c["url"], timeout=20)
        if r["status"] != 200:
            return {**c, "status": r["status"], "prices": [], "verdict": "not served here"}
        lits = price_literals(r["body"])
        amounts = price_amounts(r["body"])
        new_amounts = sorted(amounts - base_amounts)
        if not lits:
            verdict = "200 but no prices in the served HTML (client-rendered variant)"
        elif new_amounts:
            verdict = "DIFFERENT AMOUNTS"
        elif set(lits) - set(base_literals):
            verdict = "same amounts, different currency marker or number format"
        else:
            verdict = "identical to base"
        return {**c, "status": 200, "final_url": r["url"], "prices": lits[:14],
                "amounts_not_on_base_page": new_amounts[:10], "verdict": verdict}

    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for res in ex.map(one, cands[:max_variants]):
            results.append(res)

    differing = [r for r in results if r.get("verdict") == "DIFFERENT AMOUNTS"]
    return {
        "candidates_checked": len(results),
        "variants": results,
        "markets_with_different_prices": [r["locale"] for r in differing],
        "geo_limit": ("This run saw only what this vantage point is served. Apparent geography "
                      "cannot be changed from here, so prices varied by IP geolocation are NOT "
                      "visible in this output. 'Same as base' means same from here."),
    }


def currency_switcher(doc):
    """Does the page let a visitor choose a currency, and which ones are offered."""
    codes = set()
    sel = None
    for m in re.finditer(r"(?is)<select[^>]*>(.*?)</select>", doc):
        opts = re.findall(r"""(?is)<option[^>]*value=["']([^"']{2,8})["'][^>]*>(.{0,40}?)</option>""", m.group(1))
        got = {v.upper() for v, _ in opts if re.fullmatch(r"[A-Za-z]{3}", v)}
        if len(got & {"USD", "EUR", "GBP", "AUD", "CAD", "JPY", "INR", "BRL", "SGD", "CHF",
                      "SEK", "MXN", "ZAR", "NZD", "PLN", "DKK", "NOK", "AED", "ILS", "HKD"}) >= 2:
            codes |= got
            sel = "<select> of currency codes"
    for m in re.finditer(r"(?i)[?&](?:currency|curr|cur)=([A-Za-z]{3})\b", doc):
        codes.add(m.group(1).upper())
        sel = sel or "currency query parameter in links"
    for m in re.finditer(r"""(?i)data-currency=["']([A-Za-z]{3})["']""", doc):
        codes.add(m.group(1).upper())
        sel = sel or "data-currency attributes"
    symbols = sorted({p["currency_marker"] for p in find_prices(doc)})
    return {"switcher": sel, "currency_codes_offered": sorted(codes)[:20],
            "currency_markers_on_page": symbols,
            "note": ("more than one currency marker appears on this one page; check whether the "
                     "page lists several currencies or whether this vantage point was assigned one"
                     if len(symbols) > 1 else None)}


# ---------------------------------------------------------------------------
# Signup / trial page — STRUCTURE ONLY. Nothing here submits anything.
# ---------------------------------------------------------------------------

def pick_signup(origin, pricing_doc, home_doc, own_host, override=None):
    """Prefer the destination the pricing CTAs actually point at."""
    cands = []
    for doc, src in ((pricing_doc or "", "pricing CTA destination"), (home_doc or "", "homepage CTA")):
        for m in re.finditer(r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.{0,100}?)</a>""", doc, re.I | re.S):
            href, label = m.group(1), strip_tags(m.group(2))
            if not re.search(r"(?i)(sign[-_]?up|signup|register|free[-_]?trial|get[-_]?started|"
                             r"start[-_]?(?:free|trial|now)|create[-_]?account|/trial)", href + " " + label):
                continue
            u = urllib.parse.urljoin(origin, href)
            if "://" in u and urllib.parse.urlparse(u).netloc.split(".")[-2:] != own_host.split(".")[-2:]:
                # An auth subdomain is normal and still theirs; a third-party host is not.
                if not re.search(r"(?i)(auth|login|app|signup|secure|accounts?|try|start)\.", u):
                    continue
            cands.append({"url": u.split("#")[0], "source": src, "label": label[:60]})
    out, seen = [], set()
    if override:
        out.append({"url": urllib.parse.urljoin(origin, override), "source": "--signup override", "label": None})
    for c in cands:
        if c["url"] not in seen:
            seen.add(c["url"])
            out.append(c)
    for p in SIGNUP_GUESSES:
        u = origin + p
        if u not in seen:
            seen.add(u)
            out.append({"url": u, "source": "guessed path", "label": None})

    tried, reachable = [], None
    for c in out[:12]:
        r = fetch_full(c["url"])
        has_form = bool(re.search(r"(?i)<input[^>]+type=[\"']?(?:email|password|text)", r["body"]))
        sso = bool(re.search(r"(?i)(continue|sign\s?(?:in|up))\s+with\s+(google|microsoft|apple|github)", r["body"]))
        tried.append({"url": c["url"], "source": c["source"], "status": r["status"],
                      "form_or_sso": has_form or sso})
        if r["status"] == 200 and (has_form or sso):
            return {**c, "final_url": r["url"], "tried": tried}, r
        if r["status"] == 200 and reachable is None:
            reachable = c["url"]
    # "No signup page found" and "the signup page is a JS app" are different
    # facts, and only the second one is true for most product-led companies.
    return {"url": None, "source": None, "tried": tried,
            "reachable_but_unreadable": reachable}, None


def signup_structure(doc, page_url):
    """Describe the form. Do not fill it, do not send it.

    Field names are the half of the measurement story the container cannot show:
    a conversion gated on `userGoal` is gated on one of these inputs, and the
    field list is where that gate becomes legible.
    """
    labels = {}
    for m in re.finditer(r"""(?is)<label[^>]*for=["']([^"']+)["'][^>]*>(.{0,120}?)</label>""", doc):
        labels[m.group(1)] = strip_tags(m.group(2))[:80]

    fields = []
    for m in re.finditer(r"(?i)<(input|select|textarea)\b([^>]*)>", doc):
        kind, attrs = m.group(1).lower(), m.group(2)

        def attr(name):
            mm = re.search(name + r"""\s*=\s*["']([^"']*)""", attrs, re.I)
            return mm.group(1) if mm else None

        itype = (attr("type") or ("select" if kind == "select" else "text")).lower()
        if itype in ("hidden", "submit", "button", "image", "reset"):
            continue
        name = attr("name") or attr("id") or attr("data-testid")
        if not name and not attr("placeholder") and not attr("aria-label"):
            continue
        fields.append({
            "name": name, "type": itype,
            "required": bool(re.search(r"(?i)\brequired\b|aria-required=[\"']true", attrs)),
            "autocomplete": attr("autocomplete"),
            "label": labels.get(attr("id") or "", None) or attr("aria-label") or attr("placeholder"),
            "pattern": attr("pattern"),
        })
    seen, uniq = set(), []
    for f in fields:
        key = (f["name"], f["type"], f["label"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(f)

    text = visible_text(doc)
    # "no credit card required" contains "card required", and matching it there
    # reported the exact opposite of what the page promises. Strip the negated
    # phrasings before looking for a card demand.
    text_card = re.sub(r"(?i)(no\s+credit\s+card[^.,;]{0,24}|credit\s+card\s+not\s+required|"
                       r"without\s+a\s+credit\s+card)", " ", text)
    sso = [name for name, pat in SSO_PROVIDERS if re.search(pat, doc, re.I)]

    forms = []
    for m in re.finditer(r"(?i)<form\b([^>]*)>", doc):
        attrs = m.group(1)
        act = re.search(r"""action\s*=\s*["']([^"']*)""", attrs, re.I)
        meth = re.search(r"""method\s*=\s*["']([^"']*)""", attrs, re.I)
        forms.append({"action": urllib.parse.urljoin(page_url, act.group(1)) if act and act.group(1) else None,
                      "method": (meth.group(1).upper() if meth else None)})
    api = sorted(set(re.findall(r"""["'](/(?:api|v\d)/[A-Za-z0-9/_.-]{2,60})["']""", doc)))[:12]

    free_mail = sorted({m.group(0).lower() for m in FREE_MAIL_RE.finditer(doc)})[:12]
    promises = []
    for name, pat in PROMISE_RES:
        mm = re.search(pat, text_card if name == "card required up front" else text, re.I)
        if mm:
            promises.append({"promise": name, "as_written": mm.group(0).strip()[:80]})

    step_hits = sorted({m.group(0).strip()[:60] for m in STEP_RE.finditer(doc)})[:8]
    fieldsets = len(re.findall(r"(?i)<fieldset\b", doc))

    return {
        "fields": uniq[:40],
        "required_fields": [f["name"] or f["label"] for f in uniq if f["required"]][:20],
        "field_count": len(uniq),
        "sso_providers": sso,
        "work_email_demanded": {
            "copy": (WORK_EMAIL_RE.search(text).group(0) if WORK_EMAIL_RE.search(text) else None),
            "free_domain_blocklist_in_page": free_mail,
            "verdict": ("work email demanded in copy" if WORK_EMAIL_RE.search(text) else
                        "free-mail domains listed in page code, which usually means a blocklist"
                        if free_mail else "no work-email requirement visible in the served HTML"),
        },
        "progressive_profiling": {
            "markers": step_hits, "fieldsets": fieldsets,
            "verdict": ("multi-step signup: later steps are not in this HTML, so the full field "
                        "list is longer than the one above" if step_hits or fieldsets > 1 else
                        "single step as served; later profiling after account creation is not "
                        "visible from outside and is not ruled out"),
        },
        "posts_to": forms[:6],
        "api_endpoints_referenced": api,
        "cta_promise": promises,
        "boundary": ("Structure read from markup only. Nothing was submitted, no account was "
                     "created, and no field was filled."),
    }


# ---------------------------------------------------------------------------

def collect(target, pricing_override=None, signup_override=None, do_locales=True):
    url = norm_url(target)
    home = fetch_full(url)
    parsed = urllib.parse.urlparse(home["url"] or url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    own_host = parsed.netloc.lower()

    rep = {"target": url, "origin": origin, "home_status": home["status"], "notes": []}

    if pricing_override:
        pu = urllib.parse.urljoin(origin, pricing_override)
        pr = fetch_full(pu)
        disc = {"chosen": pu, "final_url": pr["url"], "source": "--pricing override",
                "tried": [{"url": pu, "status": pr["status"]}], "notes": [],
                "sitemap_pricing_urls": [], "nav_links": []}
    else:
        disc, pr = pick_pricing(origin, home["body"], own_host)

    rep["pricing_discovery"] = disc

    if not pr or pr["status"] != 200:
        rep["pricing"] = {"found": False,
                          "note": ("No pricing page could be fetched. Paths tried are listed under "
                                   "pricing_discovery.tried. Absence of a URL is not evidence of "
                                   "absence of pricing — some companies publish prices only inside "
                                   "the product or in a PDF.")}
        rep["notes"].append("pricing page not reachable; nothing below describes their tiers")
    else:
        doc = pr["body"]
        # Embedded JSON is mined from the raw document; everything structural is
        # read from the markup with inline CSS and scripts removed.
        emb = mine_embedded_json(doc)
        doc_m = markup_only(doc)
        prices = find_prices(doc_m)
        cards = segment_cards(doc_m)
        shape = pricing_shape(doc_m)
        gates = feature_matrix(doc_m)
        rep["pricing"] = {
            "found": True,
            "url": pr["url"],
            "html_bytes": len(doc),
            "rendering": rendering_verdict(doc_m, prices, len(emb["price_fields"]) + len(emb["offers"])),
            "tiers": cards,
            "tier_names": [c["tier_label"] for c in cards],
            "tiers_from_structured_data": tiers_from_structured_data(emb),
            "markup_readable": bool(prices or cards),
            "prices_in_markup": prices[:40],
            "embedded_json": emb,
            "shape": shape,
            "monthly_vs_annual": monthly_annual_delta(cards, shape, emb, gates),
            "promoted_tier": promoted_tier(cards, doc_m),
            "self_serve_boundary": self_serve_boundary(cards, doc),
            "feature_gates": gates,
            "sku_identifiers": sku_identifiers(doc),
            "currency": currency_switcher(doc_m),
        }
        if rep["pricing"]["feature_gates"] is None and rep["pricing"]["markup_readable"]:
            rep["notes"].append(
                "No machine-readable plan-comparison table. Feature gates on this page are drawn "
                "with icons or divs, so per-tier differences could not be extracted; the per-tier "
                "feature lists above are whatever each card published as text.")
        if not prices and not emb["price_fields"] and not emb["offers"]:
            rep["notes"].append(
                "No prices were readable. Treat the tier list as incomplete and do NOT report an "
                "empty table as 'they publish no prices'.")
        if do_locales:
            rep["locales"] = locale_variants(pr["url"], doc, price_literals(doc), price_amounts(doc))

    sig_disc, sr = pick_signup(origin, pr["body"] if pr else "", home["body"], own_host, signup_override)
    rep["signup_discovery"] = sig_disc
    if sr and sr["status"] == 200:
        rep["signup"] = {"found": True, "url": sr["url"], "html_bytes": len(sr["body"]),
                         **signup_structure(sr["body"], sr["url"])}
        if rep["signup"]["field_count"] == 0 and not rep["signup"]["sso_providers"]:
            rep["notes"].append(
                "Signup page fetched but no fields or SSO buttons in the served HTML: the form is "
                "client-rendered. Its structure could not be read.")
    else:
        unreadable = sig_disc.get("reachable_but_unreadable")
        rep["signup"] = {
            "found": False,
            "reachable_but_unreadable": unreadable,
            "note": ((f"{unreadable} responded 200 but served no form fields and no SSO buttons: "
                      "the signup form is client-rendered, so its structure could not be read. "
                      "That is not the same as there being no signup.")
                     if unreadable else
                     "No signup or trial page responded at all. It may be on another host, or "
                     "behind a demo request instead of self-serve.")}
    return rep


def render(r):
    L = []
    a = L.append
    a(f"# Pricing & signup: {r['target']}")
    a("  (read-only: GETs and markup parsing. Nothing was submitted and no account was created.)")

    d = r["pricing_discovery"]
    a("\n## Pricing page discovery")
    a(f"  chosen: {d['chosen'] or 'NONE'}   via: {d.get('source') or '-'}")
    for t in d.get("tried", [])[:10]:
        a(f"    tried {t['url']:<60.60} HTTP {t.get('status')}  [{t.get('source','')}]")
    if d.get("sitemap_pricing_urls"):
        a(f"  pricing-shaped URLs in sitemap ({len(d['sitemap_pricing_urls'])}): "
          + ", ".join(d["sitemap_pricing_urls"][:10]))
    for n in d.get("notes", []):
        a(f"  note: {n}")

    p = r.get("pricing") or {}
    if not p.get("found"):
        a("\n## Pricing: NOT READ")
        a("  " + p.get("note", ""))
    else:
        rv = p["rendering"]
        a(f"\n## Rendering ({p['html_bytes']:,} bytes HTML, {rv['visible_text_chars']:,} chars visible text)")
        a(f"  {rv['verdict']}")
        if rv["framework_markers"]:
            a(f"  framework markers: {', '.join(rv['framework_markers'])}")
        a(f"  prices in markup: {rv['prices_in_markup']}   prices in embedded JSON: {rv['prices_in_json']}")
        if not p["markup_readable"]:
            a("")
            a("  !! THE MARKUP COULD NOT BE READ. Every 'not stated' and 'none detected' below")
            a("     means NOT READABLE FROM HERE, not absent from the page. Load the page in a")
            a("     browser before reporting anything about this company's tiers.")

        if p["tiers_from_structured_data"]:
            a(f"\n## Tiers declared in structured data ({len(p['tiers_from_structured_data'])})")
            a("  (the page's own JSON-LD. On a client-rendered page this is often the only")
            a("   readable source of the real numbers -- cite it as structured data, not as")
            a("   something a visitor sees)")
            for t in p["tiers_from_structured_data"]:
                prices = "; ".join(
                    f"{x['currency'] or ''} {x['price']}".strip()
                    + (f" ({str(x['label'])[:34]})" if x["label"] else "")
                    for x in t["prices"])
                a(f"  {t['plan'][:24]:<24} {prices[:92]}")

        a(f"\n## Tiers in the markup ({len(p['tiers'])} segmented)")
        if not p["tiers"]:
            a("  No tier cards could be segmented from this HTML. Do not read that as 'no tiers'.")
        for c in p["tiers"]:
            price_s = "; ".join(f"{x['literal']}{(' ' + x['period']) if x['period'] else ''}"
                                for x in c["prices"][:3]) or ("contact sales" if c["contact_sales"] else "-")
            a(f"  {c['tier_label'][:28]:<28} {price_s[:46]:<46}"
              + (f"  [{c['promoted_marker']}]" if c["promoted_marker"] else ""))
            for cta in c["cta"][:2]:
                a(f"      CTA: {cta['text'][:40]:<40} -> {cta['href'] or '(no href)'}")
            if c["contact_sales"]:
                a(f"      sales-assisted: \"{c['contact_sales']}\"")
            if c["features_listed"]:
                a(f"      features listed: {len(c['features_listed'])}"
                  f" (first: {c['features_listed'][0][:60]})")

        b = p["self_serve_boundary"]
        a("\n## Where self-serve ends")
        a(f"  {b['verdict']}")
        a(f"  priced: {', '.join(b['priced_tiers']) or 'none'}")
        a(f"  contact-sales only: {', '.join(b['contact_sales_tiers']) or 'none'}")

        s = p["shape"]
        a("\n## Billing shape")
        a(f"  basis: {s['billing_basis']}")
        if s["per_seat_phrases"]:
            a(f"  per-seat language: {', '.join(s['per_seat_phrases'][:6])}")
        if s["flat_phrases"]:
            a(f"  flat-rate language: {', '.join(s['flat_phrases'][:6])}")
        a(f"  minimum seats: " + (", ".join(f"{m['seats']} (\"{m['stated']}\")" for m in s["minimum_seats"][:3])
                                  or "not stated in the served HTML"))
        a(f"  periods mentioned: {', '.join(s['billing_periods_mentioned'][:10]) or 'none'}")
        a(f"  monthly/annual toggle: {'present' if s['toggle_present'] else 'not detected'}")

        ma = p["monthly_vs_annual"]
        a("\n## Monthly vs annual")
        a(f"  basis: {ma['basis']}")
        for q in ma["stated_savings"][:5]:
            a(f"  stated: \"{q}\"")
        for c in ma["comparison_table_pairs"][:8]:
            a(f"  {c['plan'][:24]:<24} monthly {c['monthly_rate']} vs annual {c['annual_rate']}"
              f"  -> {c['discount_for_paying_annually']} cheaper annually")
            a(f"      from {c['source']}")
        for c in ma["structured_data_pairs"][:10]:
            a(f"  {str(c['plan'])[:20]:<20} {c['currency']} {c['monthly_price']} monthly vs "
              f"{c['annual_price']} annual  -> {c['discount_for_paying_annually']} cheaper annually")
            a(f"      from {c['source']}; labels: {c['monthly_label']} / {c['annual_label']}")
        names = [str(c["plan"]) for c in ma["structured_data_pairs"]]
        if len(names) != len(set(names)):
            # monday.com declares a Basic, Standard and Pro for each of its three
            # product lines, at different prices. Quoting one as "their Basic
            # plan" would be wrong for two thirds of their catalogue.
            a("  note: a plan name repeats above at different prices -- this page carries several")
            a("        product lines. Match each figure to its product before quoting it.")
        for c in ma["comparable_pairs"][:8]:
            a(f"  {c['tier'][:24]:<24} monthly {c['monthly_plan_rate']} vs annual-billed "
              f"{c['annual_plan_rate']}  -> {c['discount_for_paying_annually']} cheaper annually")
        for c in ma["ambiguous_pairs"][:6]:
            a(f"  {c['tier'][:24]:<24} {c['monthly_literal']} and {c['annual_literal']}")
            a(f"      {c['note']}")

        pt = p["promoted_tier"]
        a("\n## Visually promoted tier")
        a(f"  {pt['tier'] or '(not a single tier)'}"
          + (f"   marker: {pt['marker']}" if pt.get("marker") else ""))
        if len(pt.get("all_marked") or []) > 1:
            a("  badged: " + ", ".join(f"{h['tier']} (x{h.get('votes') or 1})" for h in pt["all_marked"]))
        if pt.get("note"):
            a(f"  note: {pt['note']}")

        fg = p["feature_gates"]
        a("\n## Feature gates between tiers")
        if not fg:
            a("  No machine-readable comparison table. The differences on this page are drawn")
            a("  with icons or divs, so nothing honest can be extracted from the markup.")
        else:
            a(f"  comparison table: {fg['rows_total']} rows, {fg['differing_rows']} differ between plans")
            a(f"  plan columns: {', '.join(fg['plan_columns'])}")
            for g in fg["gates"][:12]:
                vals = "  ".join(f"{k.strip()[:14]}={str(v).strip()[:18]}" for k, v in g["by_plan"].items())
                a(f"    {g['feature'][:40]:<40} {vals[:110]}")

        cu = p["currency"]
        a("\n## Currency")
        a(f"  markers seen on the page: {', '.join(cu['currency_markers_on_page']) or 'none'}")
        a(f"  switcher: {cu['switcher'] or 'none detected'}")
        if cu["currency_codes_offered"]:
            a(f"  codes offered: {', '.join(cu['currency_codes_offered'])}")
        if cu["note"]:
            a(f"  note: {cu['note']}")

        emb = p["embedded_json"]
        if emb["sources"]:
            a(f"\n## Embedded JSON ({', '.join(emb['sources'])})")
            for o in emb["offers"][:8]:
                a(f"  offer: {json.dumps(o, default=str)[:200]}")
            for f in emb["price_fields"][:14]:
                a(f"  {f['json_path'][:70]:<70} = {f['value_as_written']}")
            if emb["plan_names"]:
                a(f"  plan/tier strings: {', '.join(emb['plan_names'][:16])}")
            if emb["currencies"]:
                a(f"  currency codes in payload: {', '.join(emb['currencies'][:10])}")

        if p["sku_identifiers"]:
            a("\n## Internal plan/SKU identifiers in the page")
            a("  (grep these against gtm_parse.py lookup-table rows and conversion labels --")
            a("   the same plan named on both sides is the two layers corroborating each other.")
            a("   This list is mechanical and includes i18n keys: read it, do not quote it whole.")
            for s_ in p["sku_identifiers"][:24]:
                a(f"    {s_}")

    loc = r.get("locales")
    if loc:
        a(f"\n## Locale / market variants ({loc['candidates_checked']} checked)")
        for v in loc["variants"]:
            if v["status"] != 200:
                a(f"  {v['locale']:<10} HTTP {v['status']:<5} {v['url'][:60]}")
                continue
            a(f"  {v['locale']:<10} {v['verdict']}")
            if v["prices"]:
                a(f"             prices: {', '.join(v['prices'][:8])}")
        a(f"  markets with genuinely different amounts from here: "
          f"{', '.join(loc['markets_with_different_prices']) or 'none found'}")
        a("  LIMIT: " + loc["geo_limit"])

    sd = r.get("signup_discovery") or {}
    sg = r.get("signup") or {}
    a("\n## Signup / trial page")
    a(f"  chosen: {sd.get('url') or 'NONE'}   via: {sd.get('source') or '-'}")
    for t in sd.get("tried", [])[:8]:
        a(f"    tried {t['url']:<60.60} HTTP {t.get('status')}")
    if not sg.get("found"):
        a("  " + sg.get("note", ""))
    else:
        a(f"\n  STRUCTURE ONLY — {sg['boundary']}")
        a(f"  fields ({sg['field_count']}):")
        for f in sg["fields"][:20]:
            req = "required" if f["required"] else "optional"
            a(f"    {str(f['name'])[:26]:<26} {f['type']:<10} {req:<9} {str(f['label'] or '')[:40]}")
        a(f"  required: {', '.join(map(str, sg['required_fields'])) or 'none marked in markup'}")
        a(f"  SSO: {', '.join(sg['sso_providers']) or 'none found in HTML'}")
        we = sg["work_email_demanded"]
        a(f"  work email: {we['verdict']}"
          + (f"  (\"{we['copy']}\")" if we["copy"] else ""))
        if we["free_domain_blocklist_in_page"]:
            a(f"    free-mail domains named in page code: {', '.join(we['free_domain_blocklist_in_page'][:8])}")
        pp = sg["progressive_profiling"]
        a(f"  progressive profiling: {pp['verdict']}")
        if pp["markers"]:
            a(f"    markers: {', '.join(pp['markers'][:5])}")
        a("  posts to: " + (", ".join(f"{f['method'] or 'GET'} {f['action'] or '(no action attr — JS submit)'}"
                                      for f in sg["posts_to"][:4]) or "no <form> in the served HTML"))
        if sg["api_endpoints_referenced"]:
            a(f"  api paths referenced: {', '.join(sg['api_endpoints_referenced'][:8])}")
        a("  CTA promises: " + (", ".join(f"{q['promise']} (\"{q['as_written']}\")"
                                          for q in sg["cta_promise"][:5]) or "none matched"))

    if r["notes"]:
        a("\n## Collection notes")
        for n in r["notes"]:
            a(f"  - {n}")

    a("\n## How to read this")
    a("  - Prices are verbatim with the currency marker found. Nothing was converted.")
    a("  - A client-rendered verdict means the prices could not be read, NOT that there are none.")
    a("  - Locale rows reflect what this vantage point was served; IP-geolocated prices are invisible.")
    a("  - The signup section is markup structure. No form was submitted and no account was created.")
    return "\n".join(L)


def main():
    # An explicit --help is a request that succeeded, so it exits 0. Running with
    # no arguments at all is a usage error and still exits 1.
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("--"):
        print(__doc__)
        return 1
    target = argv[0]
    pricing = signup = None
    out_json = None
    want_json = "--json" in argv
    if "--pricing" in argv:
        i = argv.index("--pricing")
        pricing = argv[i + 1] if len(argv) > i + 1 else None
    if "--signup" in argv:
        i = argv.index("--signup")
        signup = argv[i + 1] if len(argv) > i + 1 else None
    if want_json:
        i = argv.index("--json")
        if len(argv) > i + 1 and not argv[i + 1].startswith("--"):
            out_json = argv[i + 1]

    rep = collect(target, pricing, signup, do_locales="--no-locales" not in argv)

    if want_json and not out_json:
        print(json.dumps(rep, indent=2, default=str))
        return 0
    print(render(rep))
    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print(f"\n[raw facts written to {out_json}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
