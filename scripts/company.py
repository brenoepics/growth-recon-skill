#!/usr/bin/env python3
"""Collect the company-level public signals a website itself does not carry.

`recon.py` reads the *site*. This reads the *company*: who they are hiring and
for which stack, which apps they ship and how often they ship them, what they
file with the SEC, and where buyers review them.

These sources are forward-looking in a way a tag container is not. A container
records what a team already built. A req for a "Lifecycle Marketing Manager
(Marketo)" states both that Marketo is in the stack and that lifecycle is the
motion they are staffing next -- and an app portfolio that gained three new
listings this quarter says where engineering money went.

Usage:
    python3 -I scripts/company.py example.com
    python3 -I scripts/company.py example.com --json              # JSON to stdout
    python3 -I scripts/company.py example.com --json out.json     # report + file
    python3 -I scripts/company.py example.com --board ashby:monday.com
    python3 -I scripts/company.py example.com --cik 1477720

Public and unauthenticated, on the same terms as the rest of this skill:
published job-board APIs, Apple's lookup endpoint, EDGAR's JSON, and review
pages as served. Nothing signs in, nothing is submitted, no bot protection is
worked around. Where a source refuses an automated request, the refusal is the
finding and gets reported as one -- that is the correct outcome, not a failure.

Deliberately not attempted: parsing financial statements out of a filing. The
useful output is the right document plus what to read in it. A script that
half-parses an income statement produces confident wrong numbers, which is worse
than a link.

Every line of the report is marked [obs] for something a source states outright
or [inf] for a reading of it, because the two get quoted very differently.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

# -I keeps the script's own directory off sys.path, so the shared fetchers have
# to be put back in reach explicitly -- same reason recon.py does this.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recon import fetch_full, norm_url  # noqa: E402
from gtm_parse import fetch as fetch_text, UA  # noqa: E402

# The SEC asks automated clients to identify themselves. A generic descriptive
# string satisfies that; a personal email address in a UA sent to a public
# endpoint is a privacy leak with no upside, so there is never one here.
SEC_UA = "growth-recon/1.0 (public data research)"

# ---------------------------------------------------------------------------
# Applicant tracking systems with a public JSON board.
#
# `probe` is the cheap call used to test a candidate token (a wrong token 404s
# in a few bytes); `jobs` is the fuller call made only once a token is
# confirmed. Fetching descriptions is what makes the tool names readable, and
# it is the expensive part, so it happens exactly once.
# ---------------------------------------------------------------------------
ATS = OrderedDict([
    ("greenhouse", {
        "label": "Greenhouse",
        "probe": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
        "jobs": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true",
        "board": "https://job-boards.greenhouse.io/{t}",
        "site_patterns": [
            r"boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9][A-Za-z0-9_.-]{1,40})",
            r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9][A-Za-z0-9_.-]{1,40})",
            r"(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9][A-Za-z0-9_.-]{1,40})",
        ],
        # `greenhouse[-_]job` catches an embed container or an i18n key, which is
        # how asana.com names it ("greenhouse_jobs_list.filters..."). Bare
        # "greenhouse" is left out on purpose: it matches "greenhouse gas".
        "vendor_patterns": [r"greenhouse\.io", r"gh_jid=", r"gh_src=",
                            r"greenhouse[-_]job", r"greenhouse[-_]embed"],
    }),
    ("lever", {
        "label": "Lever",
        "probe": "https://api.lever.co/v0/postings/{t}?mode=json",
        "jobs": "https://api.lever.co/v0/postings/{t}?mode=json",
        "board": "https://jobs.lever.co/{t}",
        "site_patterns": [
            r"jobs\.lever\.co/([A-Za-z0-9][A-Za-z0-9_.-]{1,40})",
            r"api\.lever\.co/v0/postings/([A-Za-z0-9][A-Za-z0-9_.-]{1,40})",
        ],
        # metabase.com names Lever only as `<div id="lever-jobs-container">` and
        # a `lever-jobs-embed` script, so matching lever.co alone missed a live
        # board. `\blever\b` is safe here -- it does not match "leverage".
        "vendor_patterns": [r"lever\.co", r"\blever\b", r"lever[-_]job"],
    }),
    ("ashby", {
        "label": "Ashby",
        "probe": "https://api.ashbyhq.com/posting-api/job-board/{t}",
        "jobs": "https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true",
        "board": "https://jobs.ashbyhq.com/{t}",
        "site_patterns": [
            r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9_.%-]{1,40})",
            r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9][A-Za-z0-9_.%-]{1,40})",
        ],
        # monday.com's careers page carries no Ashby URL at all -- the board is
        # rendered from embedded JSON whose rows are tagged `"source":"ashby"`.
        # That names the vendor without naming the token, which is exactly the
        # case the candidate probe below exists for.
        "vendor_patterns": [r"ashbyhq\.com", r'"source"\s*:\s*"ashby"', r"\bashby\b"],
    }),
])

# A path segment that is part of the vendor's own URL structure, never a board
# token: `boards.greenhouse.io/embed/job_board/js` would otherwise yield "embed".
TOKEN_STOPLIST = {
    "embed", "api", "v0", "v1", "static", "assets", "js", "css", "img", "images",
    "job_board", "jobs", "posting-api", "login", "signup", "favicon.ico", "robots.txt",
    "applications", "apply", "search", "boards", "company", "www",
}

# Other ATSes seen on marketing sites. None of these publishes a board as plain
# JSON on a documented unauthenticated path, so they are reported as a link to
# open rather than pretended to be collected.
OTHER_ATS = [
    ("Workday", r"([a-z0-9-]+)\.(?:wd\d+)\.myworkdayjobs\.com", "myworkdayjobs.com board"),
    ("SmartRecruiters", r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)", "SmartRecruiters careers page"),
    ("Workable", r"([a-z0-9-]+)\.workable\.com", "Workable board"),
    ("Teamtailor", r"([a-z0-9-]+)\.teamtailor\.com", "Teamtailor board"),
    ("Personio", r"([a-z0-9-]+)\.jobs\.personio\.(?:com|de)", "Personio board"),
    ("BambooHR", r"([a-z0-9-]+)\.bamboohr\.com", "BambooHR board"),
    ("Recruitee", r"([a-z0-9-]+)\.recruitee\.com", "Recruitee board"),
    ("SuccessFactors", r"([a-z0-9-]+)\.(?:jobs|career)\.sapsf\.com", "SuccessFactors board"),
    ("Rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)", "Rippling board"),
    ("Pinpoint", r"([a-z0-9-]+)\.pinpointhq\.com", "Pinpoint board"),
]

# ---------------------------------------------------------------------------
# Function mapping.
#
# Order matters: the specific operations functions are tested before the broad
# ones, so "Marketing Operations Manager" lands in revenue operations rather
# than being swallowed by /marketing/. The ATS department field is checked
# first because it is the company's own org chart; the title is the fallback.
# ---------------------------------------------------------------------------
FUNCTIONS = [
    ("Revenue / marketing ops", r"revenue operation|rev ?ops|sales operation|marketing operation|"
                                r"gtm operation|go.to.market operation|deal desk|marketing technolog|"
                                r"martech|sales enablement|revenue enablement"),
    ("Marketing & growth", r"marketing|growth|demand gen|demand.generation|brand|content|communicat|"
                           r"\bseo\b|\bsem\b|paid (?:media|search|social|acquisition)|lifecycle|"
                           r"campaign|\bpr\b|public relations|events|field marketing|product marketing|"
                           r"community|social media|creative|copywrit"),
    ("Sales & partnerships", r"\bsales\b|account executive|\bae\b|account manager|\bsdr\b|\bbdr\b|"
                             r"business development|partnership|channel|alliance|reseller|"
                             r"commercial|new business|expansion|\brevenue\b"),
    ("Customer success & support", r"customer success|customer experience|\bcsm\b|support|onboarding|"
                                   r"professional services|solution (?:architect|engineer|consultant)|"
                                   r"implementation|renewal|technical account"),
    ("Data & analytics", r"\bdata\b|analytic|analyst|data scien|machine learning|\bml\b|"
                         r"\bai\b|research scientist|business intelligence|\bbi\b|insight"),
    ("Design", r"\bdesign|\bux\b|\buxr\b|\bui\b|user research|user experience research|brand studio"),
    # Engineering before Product on purpose: "Product Engineering" is an
    # engineering org, and asana.com has 16 reqs in one. Design stays ahead of
    # both so "Product Design" does not land in engineering either.
    ("Engineering", r"engineer|software|developer|infrastructure|platform|devops|\bsre\b|"
                    r"security|architect|\bqa\b|quality assurance|technical"),
    ("Product", r"product manage|product owner|\bproduct\b|program manage"),
    ("Finance", r"finance|financial|accounting|accountant|controller|fp&a|treasury|\btax\b|"
                r"audit|procurement|billing"),
    ("People & recruiting", r"people|recruit|talent|\bhr\b|human resource|workplace|"
                            r"compensation|benefits|learning"),
    ("Legal & compliance", r"legal|counsel|compliance|privacy|regulatory|\bgrc\b|trust & safety"),
    ("Operations & G&A", r"operation|strategy|chief of staff|business operation|administrat|"
                         r"facilities|\bit\b|workplace|executive assistant"),
]

# Titles that place a role on the growth / marketing / data surface this skill
# cares about. These are the reqs worth reading in full: they name the tools.
GROWTH_ROLE_RE = re.compile(
    r"marketing|growth|demand gen|demand.generation|lifecycle|crm|campaign|\bseo\b|\bsem\b|"
    r"paid (?:media|search|social|acquisition)|performance|acquisition|\bppc\b|"
    r"rev ?ops|revenue operation|sales operation|marketing operation|martech|"
    r"analytic|\bdata\b|experimentation|conversion|\bcro\b|web (?:engineer|developer|manager)|"
    r"product marketing|brand|content|community|partner marketing|field marketing|abm|"
    r"customer marketing|email|retention|monetization|pricing", re.I)

# ---------------------------------------------------------------------------
# Tool catalogue for job text.
#
# The entries are deliberately strict about generic English words. "Segment",
# "Mode" and "Sigma" are ordinary words in a marketing job description, so each
# requires a disambiguating neighbour -- the same trap recon.py's weak/strong
# evidence split exists for, in a different source.
# ---------------------------------------------------------------------------
STACK_TOOLS = [
    # marketing automation / CRM
    ("Marketo", r"\bmarketo\b", "MAP — enterprise MQL motion"),
    ("Eloqua", r"\beloqua\b", "MAP — enterprise, older vintage"),
    ("Pardot", r"\bpardot\b|account engagement", "MAP — Salesforce-centric"),
    ("HubSpot", r"\bhubspot\b", "integrated marketing + CRM"),
    ("Salesforce", r"\bsalesforce\b|\bsfdc\b", "CRM of record"),
    ("Braze", r"\bbraze\b", "lifecycle messaging at scale"),
    ("Iterable", r"\biterable\b", "lifecycle messaging"),
    ("Customer.io", r"customer\.io", "lifecycle messaging"),
    ("Klaviyo", r"\bklaviyo\b", "ecommerce lifecycle"),
    ("Intercom", r"\bintercom\b", "in-product messaging / support"),
    ("Dynamics 365", r"dynamics 365", "Microsoft CRM"),
    # CDP / analytics
    ("Segment", r"twilio segment|segment\.com|\bsegment cdp\b", "CDP — events fan out server-side"),
    ("RudderStack", r"rudderstack", "CDP, warehouse-first"),
    ("mParticle", r"mparticle", "CDP, mobile-heavy"),
    ("Snowplow", r"snowplow", "self-hosted event pipeline"),
    ("Amplitude", r"\bamplitude\b", "product analytics"),
    ("Mixpanel", r"\bmixpanel\b", "product analytics"),
    ("PostHog", r"\bposthog\b", "engineering-led analytics"),
    ("Heap", r"\bheap\b(?! of)", "autocapture analytics"),
    ("Pendo", r"\bpendo\b", "in-product guides + analytics"),
    ("GA4", r"\bga4\b|google analytics", "web analytics"),
    ("Adobe Analytics", r"adobe analytics|\bomniture\b", "enterprise web analytics"),
    ("Hotjar", r"\bhotjar\b", "qualitative UX"),
    ("Contentsquare", r"contentsquare", "enterprise experience analytics"),
    ("FullStory", r"fullstory", "session replay"),
    ("Quantum Metric", r"quantum metric", "enterprise experience analytics"),
    # experimentation
    ("Optimizely", r"\boptimizely\b", "experimentation platform"),
    ("VWO", r"\bvwo\b|visual website optimizer", "experimentation platform"),
    ("AB Tasty", r"ab ?tasty", "experimentation platform"),
    ("Kameleoon", r"kameleoon", "experimentation platform"),
    ("Statsig", r"\bstatsig\b", "warehouse-native experimentation"),
    ("Eppo", r"\beppo\b", "warehouse-native experimentation"),
    ("GrowthBook", r"growthbook", "open-source experimentation"),
    ("LaunchDarkly", r"launchdarkly", "feature flags"),
    ("Split", r"split\.io", "feature flags + experimentation"),
    ("Mutiny", r"\bmutiny\b", "landing-page personalisation (usually ABM)"),
    # data stack / BI
    ("Snowflake", r"\bsnowflake\b", "cloud warehouse"),
    ("BigQuery", r"\bbigquery\b", "cloud warehouse"),
    ("Databricks", r"databricks", "lakehouse"),
    ("Redshift", r"\bredshift\b", "cloud warehouse"),
    ("dbt", r"\bdbt\b", "transformation layer — modelled metrics"),
    ("Fivetran", r"\bfivetran\b", "managed ingestion"),
    ("Airbyte", r"\bairbyte\b", "ingestion"),
    ("Hightouch", r"hightouch", "reverse ETL — warehouse drives the tools"),
    ("Census", r"\bcensus\b(?! bureau)", "reverse ETL"),
    ("Looker", r"\blooker\b", "BI, semantic layer"),
    ("Tableau", r"\btableau\b", "BI"),
    ("Power BI", r"power ?bi", "BI"),
    ("Mode", r"mode analytics", "SQL-first BI"),
    ("Sigma", r"sigma computing", "spreadsheet-style BI"),
    ("Airflow", r"\bairflow\b", "orchestration"),
    # paid media
    ("Google Ads", r"google ads|adwords", "paid search"),
    # `\bgtm\b` is NOT usable here: in a B2B req "GTM" means go-to-market, and
    # asana.com's board turned "AI GTM Account Executive" into a tag-manager hit.
    ("Google Tag Manager", r"google tag manager|\btag manager\b", "tag management"),
    ("DV360", r"\bdv360\b|display & video 360|campaign manager 360|\bcm360\b", "programmatic — real media budget"),
    ("Meta Ads", r"meta ads|facebook ads|\bmeta business\b", "paid social"),
    ("LinkedIn Ads", r"linkedin ads|linkedin campaign manager", "B2B paid social"),
    ("Microsoft Ads", r"microsoft (?:ads|advertising)|bing ads", "paid search, B2B skew"),
    ("TikTok Ads", r"tiktok ads", "consumer paid social"),
    ("Reddit Ads", r"reddit ads", "community-led paid"),
    ("StackAdapt", r"stackadapt", "programmatic"),
    ("Criteo", r"\bcriteo\b", "retargeting / retail media"),
    ("Capterra / G2 paid", r"capterra|g2crowd|g2\.com", "review-site paid listings"),
    # ABM / intent / outbound
    ("6sense", r"6sense", "intent data, enterprise ABM"),
    ("Demandbase", r"demandbase", "intent data + ad targeting"),
    ("ZoomInfo", r"zoominfo", "enterprise contact data"),
    ("Clearbit", r"clearbit|breeze intelligence", "firmographic enrichment, short forms"),
    ("Apollo", r"apollo\.io", "outbound prospecting"),
    ("Outreach", r"outreach\.io", "sales engagement"),
    ("Salesloft", r"salesloft", "sales engagement"),
    ("Gong", r"\bgong\b", "call intelligence"),
    ("Clari", r"\bclari\b", "forecasting / revenue ops"),
    ("Chili Piper", r"chili ?piper", "speed-to-lead routing"),
    ("Qualified", r"qualified\.com", "sales chat on high-intent pages"),
    # Not `\bdrift\b`: "data drift" and "model drift" are standard ML phrasing.
    ("Drift", r"drift\.com|\bdrift chat\b", "conversational marketing"),
    ("Marketo Measure", r"marketo measure|\bbizible\b", "multi-touch attribution"),
    ("Dreamdata", r"dreamdata", "B2B attribution"),
    # SEO / content / web
    ("Ahrefs", r"\bahrefs\b", "SEO toolkit"),
    ("Semrush", r"semrush", "SEO toolkit"),
    ("Screaming Frog", r"screaming frog", "technical SEO"),
    ("Botify", r"\bbotify\b", "enterprise technical SEO"),
    ("Conductor", r"\bconductor\b", "enterprise SEO"),
    ("Webflow", r"webflow", "marketing-owned site"),
    ("WordPress", r"wordpress", "marketing-owned site"),
    ("Contentful", r"contentful", "headless CMS"),
    ("Sanity", r"sanity\.io", "headless CMS"),
    ("Storyblok", r"storyblok", "headless CMS"),
    ("Figma", r"\bfigma\b", "design system"),
    # mobile growth
    ("AppsFlyer", r"appsflyer", "mobile attribution"),
    ("Adjust", r"adjust\.com", "mobile attribution"),
    ("Branch", r"branch\.io", "deep links + mobile attribution"),
    ("Firebase", r"firebase", "mobile analytics / messaging"),
    # consent / billing
    ("OneTrust", r"onetrust", "enterprise consent — legal involvement"),
    ("Cookiebot", r"cookiebot", "mid-market consent"),
    ("Usercentrics", r"usercentrics", "mid-market consent"),
    ("Stripe", r"\bstripe\b", "self-serve checkout"),
    ("Recurly", r"\brecurly\b", "subscription billing"),
    ("Chargebee", r"chargebee", "subscription billing"),
    ("Zuora", r"\bzuora\b", "enterprise subscription billing"),
    ("NetSuite", r"netsuite", "ERP"),
    # automation
    ("Zapier", r"\bzapier\b", "glue automation"),
    ("Workato", r"workato", "enterprise iPaaS"),
    ("Tray.io", r"tray\.io", "iPaaS"),
]

CAREERS_PATHS = ["/careers", "/jobs", "/company/careers", "/about/careers",
                 "/company/jobs", "/careers/jobs", "/en/careers"]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def plain(s):
    """HTML (or HTML-escaped HTML) to flat text.

    Greenhouse returns job bodies double-escaped (`&lt;p&gt;`), so unescaping
    once leaves tags behind and unescaping after the strip catches entities in
    the prose. Doing both is cheaper than guessing which board it came from.
    """
    s = html.unescape(s or "")
    s = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def brand_of(host):
    """Registrable label of a host: news.ycombinator.com -> ycombinator."""
    base = host.lower().replace("www.", "")
    labels = [l for l in base.split(".") if l]
    two_part = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(labels) >= 3 and labels[-2] in two_part:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else base)


def get_json(url, timeout=30, ua=None):
    """Fetch and parse JSON, returning (data, status, note).

    A soft 404 -- HTTP 200 carrying the site's HTML error page -- is the normal
    failure mode for /.well-known paths on a SPA, so the body is sniffed rather
    than trusted. asana.com answers its apple-app-site-association path with the
    marketing page, and parsing that as JSON is how a run invents an app.
    """
    try:
        body, status = fetch_text(url, timeout, headers={"User-Agent": ua} if ua else None)
    except urllib.error.HTTPError as e:
        # A 404 here is information (the file is not published), so it is worth
        # reporting as a status rather than as a generic failure.
        return None, e.code, f"HTTP {e.code}"
    except Exception as e:
        msg = str(e)
        if re.search(r"could not resolve host|name or service not known|nodename nor servname", msg, re.I):
            return None, 0, "no such host"
        return None, 0, f"request failed: {msg[:110]}"
    head = (body or "").lstrip()[:200].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return None, status, f"HTTP {status} but the body is HTML, not JSON (soft 404)"
    try:
        return json.loads(body), status, None
    except Exception:
        return None, status, f"HTTP {status}, {len(body or '')} bytes, not parseable as JSON"


def dedupe_locations(locs):
    """One posting, one count per place.

    Greenhouse returns a posting location *and* its office names, so a single
    req arrives as ["Vancouver, BC", "Vancouver"] and inflates both rows. Keep
    the most specific string and drop anything contained in it.
    """
    clean = [l.strip() for l in locs if l and l.strip()]
    out = []
    for l in sorted(set(clean), key=len, reverse=True):
        if not any(l.lower() in k.lower() for k in out):
            out.append(l)
    return out


def classify_function(*fields):
    for name, pat in FUNCTIONS:
        for f in fields:
            if f and re.search(pat, f, re.I):
                return name
    return "Unclassified"


def iso_day(s):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(s or ""))
    return m.group(1) if m else None


def days_ago(day):
    if not day:
        return None
    try:
        t = time.mktime(time.strptime(day, "%Y-%m-%d"))
    except Exception:
        return None
    return int((time.time() - t) / 86400)


# ---------------------------------------------------------------------------
# 1. Hiring
# ---------------------------------------------------------------------------
def discover_site_text(origin, host):
    """Fetch the homepage and the careers pages, returning text to search.

    The board token is usually written somewhere a browser can see it: a link on
    the careers page, or the CSP header, which has to allowlist the ATS before
    the embed can load. Both are collected so the token has two chances to
    appear, and the careers links on the homepage are followed rather than
    guessed at where they exist.
    """
    blobs, pages, csp = [], {}, ""
    root = fetch_full(origin + "/")
    if root["body"]:
        blobs.append(root["body"])
        pages[origin + "/"] = root["status"]
    csp = (root["headers"].get("Content-Security-Policy")
           or root["headers"].get("content-security-policy") or "")

    # Careers links the homepage actually publishes, before any guessed path.
    linked = []
    for m in re.finditer(r'href=["\']([^"\']*(?:career|jobs|join-us|life-at)[^"\']*)["\']',
                         root["body"] or "", re.I):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = origin + u
        elif not u.startswith("http"):
            continue
        if host.replace("www.", "") in u and u not in linked:
            linked.append(u)
    candidates = linked[:3] + [origin + p for p in CAREERS_PATHS]
    seen = set()
    todo = [u for u in candidates if not (u in seen or seen.add(u))][:8]

    def grab(u):
        return u, fetch_full(u, timeout=20)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for u, r in ex.map(grab, todo):
            pages[u] = r["status"]
            if r["status"] == 200 and r["body"]:
                blobs.append(r["body"])
    return blobs, pages, csp


def find_board_tokens(blobs, csp, host):
    """Pull ATS tokens out of the collected pages, then probe for a missing one.

    Two outcomes are reported differently on purpose. A token read off the page
    or the CSP is observed. A token that had to be guessed from the domain and
    then confirmed by the board API is an inference that happened to verify --
    monday.com's Ashby board is literally `monday.com`, which no amount of
    reading their careers HTML would have told you, because the page renders
    from embedded JSON that names the vendor and not the board.
    """
    corpus = "\n".join(blobs) + "\n" + csp
    found, vendors, notes = [], [], []

    for key, spec in ATS.items():
        for pat in spec["site_patterns"]:
            for m in re.finditer(pat, corpus, re.I):
                tok = urllib.parse.unquote(m.group(1)).strip("/")
                if tok.lower() in TOKEN_STOPLIST or len(tok) < 2:
                    continue
                if not any(f["ats"] == key and f["token"] == tok for f in found):
                    found.append({"ats": key, "token": tok, "how": "read from the site or its CSP",
                                  "provenance": "observed"})
        if any(re.search(p, corpus, re.I) for p in spec["vendor_patterns"]):
            vendors.append(key)

    # Candidate tokens, best first. The full host is included because several
    # ATS accounts are registered under the domain rather than the brand.
    base = host.lower().replace("www.", "")
    brand = brand_of(host)
    cands = [base, brand, brand.replace("-", ""), base.replace(".", "")]
    cands = [c for i, c in enumerate(cands) if c and c not in cands[:i]]

    targets = [k for k in vendors if not any(f["ats"] == k for f in found)]
    if not found and not targets:
        # Deliberately NOT probing the boards on spec here. Tried that: with no
        # ATS named anywhere on example.com, `example` resolved to a real
        # Greenhouse board belonging to someone else and 21 of a stranger's reqs
        # were reported as theirs. A guess is only allowed once the site has
        # named the vendor itself.
        notes.append("No ATS is named on the pages collected and no board was looked up — "
                     "guessing a token against a live board attributes other companies' reqs, "
                     "so it is not attempted. If they do hire publicly, the board is linked "
                     "from a careers subdomain this pass did not fetch; find it and re-run "
                     "with --board <greenhouse|lever|ashby>:<token>.")

    for key in targets:
        for tok in cands[:4]:
            data, status, _ = get_json(ATS[key]["probe"].format(t=urllib.parse.quote(tok)), 25)
            n = len(data.get("jobs", []) if isinstance(data, dict) else (data or []))
            if status == 200 and isinstance(data, (dict, list)) and n:
                found.append({"ats": key, "token": tok, "provenance": "inferred, then verified",
                              "how": f"not published on the site; '{tok}' matched the domain and "
                                     f"the public {ATS[key]['label']} board answered with {n} roles"})
                break
        else:
            if key in vendors:
                notes.append(f"{ATS[key]['label']} is named on the site but no board token was "
                             f"readable and none of {cands[:4]} resolved. The token is in the "
                             f"careers page's client-side bundle; open the board in a browser "
                             f"and take it from the URL.")

    other = []
    for label, pat, what in OTHER_ATS:
        m = re.search(pat, corpus, re.I)
        if m:
            other.append({"vendor": label, "account": m.group(1), "shows": what,
                          "url": "https://" + m.group(0).split("/")[0].lower()
                                 if "." in m.group(0) else None})
    return found, other, notes


def normalise_jobs(ats, data):
    """Flatten three board shapes into one list.

    Each vendor names the same things differently (`text`/`title`,
    `categories.team`/`department`), and the location fields differ most of all:
    Greenhouse has one string, Ashby has a primary plus secondaries, Lever has
    an `allLocations` array. Keeping all of them matters because where a company
    hires is the market-entry read.
    """
    out = []
    if ats == "greenhouse":
        for j in (data or {}).get("jobs", []):
            depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
            offices = [o.get("name") for o in j.get("offices") or [] if o.get("name")]
            locs = [(j.get("location") or {}).get("name")] + offices
            out.append({
                "title": (j.get("title") or "").strip(),
                "dept": depts[0] if depts else None,
                "team": depts[1] if len(depts) > 1 else None,
                "locations": [l for l in locs if l],
                "url": j.get("absolute_url"),
                "published": iso_day(j.get("first_published") or j.get("updated_at")),
                "employment": None, "comp": None,
                "text": plain(j.get("content")),
            })
    elif ats == "lever":
        for j in data or []:
            c = j.get("categories") or {}
            locs = list(c.get("allLocations") or []) or ([c.get("location")] if c.get("location") else [])
            out.append({
                "title": (j.get("text") or "").strip(),
                "dept": c.get("department") or c.get("team"),
                "team": c.get("team"),
                "locations": [l for l in locs if l],
                "url": j.get("hostedUrl"),
                "published": (time.strftime("%Y-%m-%d", time.gmtime(j["createdAt"] / 1000))
                              if isinstance(j.get("createdAt"), (int, float)) else None),
                "employment": c.get("commitment"), "comp": None,
                "text": plain(j.get("descriptionPlain") or j.get("description")) + " "
                        + plain(j.get("additionalPlain") or j.get("additional")),
            })
    elif ats == "ashby":
        for j in (data or {}).get("jobs", []):
            locs = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
            comp = (j.get("compensation") or {}).get("compensationTierSummary")
            out.append({
                "title": (j.get("title") or "").strip(),
                "dept": j.get("department"), "team": j.get("team"),
                "locations": [l for l in locs if l],
                "url": j.get("jobUrl") or j.get("applyUrl"),
                "published": iso_day(j.get("publishedAt")),
                "employment": j.get("employmentType"), "comp": comp,
                "text": plain(j.get("descriptionPlain") or j.get("descriptionHtml")),
            })
    return [j for j in out if j["title"]]


def tools_in_jobs(jobs):
    """Which named tools appear in the reqs, split by title vs body.

    The split is the whole point. A tool in the title -- "Lifecycle Marketing
    Manager (Marketo)" -- is the system the hire will own. A tool in the body is
    often a nice-to-have, or belongs to the team next door. Both are observed
    strings; only the first supports a confident claim about the stack.
    """
    hits = OrderedDict()
    for label, pat, implies in STACK_TOOLS:
        rx = re.compile(pat, re.I)
        in_title, in_body = [], []
        for j in jobs:
            if rx.search(j["title"]):
                in_title.append(j["title"])
            elif j["text"] and rx.search(j["text"]):
                in_body.append(j["title"])
        if in_title or in_body:
            hits[label] = {"implies": implies, "in_title": in_title[:8],
                           "postings_naming_it": len(in_title) + len(in_body),
                           "in_body_examples": in_body[:6]}
    return OrderedDict(sorted(hits.items(), key=lambda kv: (-len(kv[1]["in_title"]),
                                                            -kv[1]["postings_naming_it"])))


def corroborate_board(ats, raw, jobs, brand, name_hints):
    """Check the board actually belongs to this company before reporting its reqs.

    This exists because a guessed token can resolve to somebody else's board.
    Greenhouse states the owner outright in `company_name`; every board also
    leaks it through apply URLs on the company's own domain and through the
    "about us" paragraph at the top of each req. Agreement is reported, and
    disagreement is reported louder -- a confident report on the wrong company's
    hiring plan is the worst output this script can produce.
    """
    hints = {h.lower() for h in list(name_hints) + [brand] if h}
    stated = None
    if ats == "greenhouse" and isinstance(raw, dict):
        for j in raw.get("jobs", [])[:5]:
            if j.get("company_name"):
                stated = j["company_name"]
                break
    sample = jobs[:25]
    mentions = sum(1 for j in sample
                   if brand in (j.get("text") or "").lower()
                   or brand in (j.get("url") or "").lower())
    ok_stated = stated is not None and any(h in stated.lower() or stated.lower() in h for h in hints)
    ok_mentions = bool(sample) and mentions >= max(1, len(sample) // 4)
    if ok_stated:
        return {"ok": True, "how": f"the board states company_name '{stated}'"}
    if stated is not None and not ok_stated:
        return {"ok": False, "how": f"the board states company_name '{stated}', which is not "
                                    f"{sorted(hints)[:3]} — this is somebody else's board"}
    if ok_mentions:
        return {"ok": True, "how": f"'{brand}' appears in {mentions} of {len(sample)} sampled "
                                   f"reqs or their apply URLs"}
    return {"ok": False, "how": f"'{brand}' appears in {mentions} of {len(sample)} sampled reqs "
                                f"and the board names no company — ownership is unconfirmed"}


def hiring(host, blobs, csp, pages, board_override=None, brand=None, name_hints=()):
    brand = brand or brand_of(host)
    out = {"boards": [], "other_ats": [], "notes": [], "pages_fetched": pages,
           "total_open_roles": 0, "by_function": {}, "by_department_as_they_name_it": {},
           "by_location": {}, "growth_stack_roles": [], "tools_named": {},
           "possible_new_markets": []}
    if board_override:
        ats, _, tok = board_override.partition(":")
        boards = [{"ats": ats, "token": tok, "provenance": "observed",
                   "how": "supplied on the command line"}]
        other, notes = [], []
    else:
        boards, other, notes = find_board_tokens(blobs, csp, host)
    out["other_ats"], out["notes"] = other, notes

    all_jobs = []
    for b in boards:
        spec = ATS.get(b["ats"])
        if not spec:
            out["notes"].append(f"unknown ATS '{b['ats']}' in --board")
            continue
        url = spec["jobs"].format(t=urllib.parse.quote(b["token"]))
        data, status, note = get_json(url, 45)
        jobs = normalise_jobs(b["ats"], data)
        entry = dict(b, label=spec["label"], api=url,
                     board_url=spec["board"].format(t=b["token"]),
                     status=status, roles=len(jobs))
        if note:
            entry["error"] = note
        if jobs:
            entry["corroboration"] = corroborate_board(b["ats"], data, jobs, brand, name_hints)
        out["boards"].append(entry)
        if jobs and (not entry.get("corroboration") or entry["corroboration"]["ok"]):
            all_jobs += jobs
        elif jobs:
            out["notes"].append(
                f"{spec['label']} board '{b['token']}' returned {len(jobs)} roles but they were "
                f"NOT counted: {entry['corroboration']['how']}. Confirm the token before using "
                f"any of it.")

    out["total_open_roles"] = len(all_jobs)
    if not all_jobs:
        return out, []

    funcs = Counter(classify_function(j["dept"], j["team"], j["title"]) for j in all_jobs)
    out["by_function"] = dict(funcs.most_common())
    # Their own department names, kept verbatim. Event names in a container are
    # worth reading because the funnel's owners wrote them; department names are
    # worth reading for the same reason.
    out["by_department_as_they_name_it"] = dict(
        Counter(j["dept"] for j in all_jobs if j["dept"]).most_common(30))

    for j in all_jobs:
        j["locations"] = dedupe_locations(j["locations"])
    locs = Counter(l for j in all_jobs for l in j["locations"])
    out["by_location"] = dict(locs.most_common(30))
    out["remote_postings"] = sum(1 for j in all_jobs
                                 if any("remote" in (l or "").lower() for l in j["locations"]))

    # A location whose whole req set is recent *and* small reads as a market
    # being entered. The size test is not optional: a headquarters refreshes its
    # board continuously, so monday.com's Tel Aviv (63 of 126 reqs, none older
    # than 94 days) topped this list before the share cap went in, which is the
    # opposite of a new market.
    total = len(all_jobs)
    for loc, n in locs.items():
        if n < 2 or n > max(3, 0.2 * total):
            continue
        dates = [j["published"] for j in all_jobs
                 if loc in j["locations"] and j["published"]]
        if not dates:
            continue
        oldest = days_ago(min(dates))
        if oldest is not None and oldest <= 90:
            out["possible_new_markets"].append({"location": loc, "roles": n,
                                                "oldest_posting_days": oldest,
                                                "share_of_open_roles": round(100.0 * n / total, 1)})
    out["possible_new_markets"].sort(key=lambda x: -x["roles"])

    growth = [j for j in all_jobs if GROWTH_ROLE_RE.search(j["title"])]
    out["growth_stack_roles"] = [{
        "title": j["title"], "dept": j["dept"], "locations": j["locations"][:3],
        "published": j["published"], "comp": j["comp"], "url": j["url"],
    } for j in sorted(growth, key=lambda j: (j["published"] or ""), reverse=True)[:40]]
    out["growth_stack_role_count"] = len(growth)
    out["tools_named"] = tools_in_jobs(all_jobs)
    return out, all_jobs


# ---------------------------------------------------------------------------
# 2. Mobile apps
# ---------------------------------------------------------------------------
def app_identifiers(origin, host):
    """Read app IDs out of the two well-known files that exist to publish them.

    Both are served to any browser because the OS fetches them to verify deep
    links. The iOS entry is `TEAMID.bundle.id`, so the team prefix comes off
    before anything is looked up.
    """
    ids = {"ios_bundles": [], "android_packages": [], "sources": {}, "notes": []}
    apex = host.replace("www.", "")
    targets = [
        (origin + "/.well-known/apple-app-site-association", "aasa"),
        (origin + "/apple-app-site-association", "aasa"),
        (origin + "/.well-known/assetlinks.json", "assetlinks"),
        (f"https://app.{apex}/.well-known/apple-app-site-association", "aasa"),
    ]
    for url, kind in targets:
        data, status, note = get_json(url, 20)
        ids["sources"][url] = f"HTTP {status}" + (f" — {note}" if note else "")
        if not data:
            continue
        if kind == "aasa":
            blob = json.dumps(data)
            for raw in re.findall(r'"([A-Z0-9]{8,12}\.[A-Za-z0-9][A-Za-z0-9._-]+)"', blob):
                bundle = raw.split(".", 1)[1]
                if bundle not in ids["ios_bundles"]:
                    ids["ios_bundles"].append(bundle)
            # Some files list a bare appID with no team prefix.
            for raw in re.findall(r'"appIDs?"\s*:\s*\[?\s*"([A-Za-z0-9][A-Za-z0-9._-]+)"', blob):
                bundle = raw.split(".", 1)[1] if re.match(r"^[A-Z0-9]{8,12}\.", raw) else raw
                if "." in bundle and bundle not in ids["ios_bundles"]:
                    ids["ios_bundles"].append(bundle)
        else:
            for entry in data if isinstance(data, list) else [data]:
                pkg = ((entry or {}).get("target") or {}).get("package_name")
                if pkg and pkg not in ids["android_packages"]:
                    ids["android_packages"].append(pkg)
    return ids


def apple_lookup(params):
    data, status, note = get_json("https://itunes.apple.com/lookup?" + urllib.parse.urlencode(params), 25)
    if not data:
        return [], note or f"HTTP {status}"
    return data.get("results", []), None


def app_fields(r):
    day = iso_day(r.get("currentVersionReleaseDate"))
    return {
        "name": r.get("trackName"), "seller": r.get("sellerName"),
        "bundle_id": r.get("bundleId"), "track_id": r.get("trackId"),
        "rating": r.get("averageUserRating"), "rating_count": r.get("userRatingCount"),
        "version": r.get("version"), "last_update": day,
        "days_since_update": days_ago(day),
        "first_release": iso_day(r.get("releaseDate")),
        "genres": r.get("genres"), "price": r.get("price"),
        "listing": r.get("trackViewUrl"),
    }


def mobile(origin, host):
    out = {"ios": [], "android": [], "portfolio": [], "identifiers": {}, "notes": []}
    brand = brand_of(host)
    ids = app_identifiers(origin, host)
    out["identifiers"] = ids

    # asana.com's file lists com.asana.Asana-debug and -nightly alongside the
    # shipped bundle. Those are internal builds, never on the store, so looking
    # them up only manufactures "no result" noise -- but their presence is a
    # (small) real finding: internal builds handle the same deep links.
    variants = [b for b in ids["ios_bundles"]
                if re.search(r"-(?:debug|nightly|dev|staging|internal|alpha|beta)$", b, re.I)]
    shipped = [b for b in ids["ios_bundles"] if b not in variants]
    if variants:
        out["internal_build_bundles"] = variants
        out["notes"].append(
            "Their app-site-association also publishes non-production bundle IDs ("
            + ", ".join(variants) + "), which is why those are not looked up. It does tell you "
            "internal builds are wired to the same deep links.")
    for bundle in shipped[:6]:
        results, err = apple_lookup({"bundleId": bundle})
        if err or not results:
            out["notes"].append(f"iOS bundle {bundle} published by the site returned no App Store "
                                f"result ({err or 'resultCount 0'}); it may be unreleased or "
                                f"region-restricted.")
            continue
        app = app_fields(results[0])
        app["bundle_source"] = "apple-app-site-association (observed)"
        app["artist_id"] = results[0].get("artistId")
        out["ios"].append(app)

    # Only bundle IDs the site actually published get looked up. Reusing the
    # Android package as an iOS bundle looks harmless and is not: asana.com
    # publishes `com.asana.app` in assetlinks.json, and that string on the App
    # Store belongs to "ASANA 360 Global Limited", an unrelated company. A run
    # that skipped the check would report a stranger's app as theirs.
    if ids["android_packages"] and not shipped and not out["ios"]:
        out["notes"].append(
            "An Android package is published but no iOS bundle is. The package name is NOT "
            "looked up on the App Store: bundle namespaces are independent, and a same-looking "
            "string there can belong to a different company.")

    # Fall back to Apple's name search, filtered by developer name. This finds
    # the real app when the site publishes no AASA, and it is an inference: the
    # match is on the seller's name, not on anything the company published.
    if not out["ios"]:
        results, err = apple_lookup({"term": brand, "entity": "software", "limit": 12})
        for r in results:
            seller = (r.get("sellerName") or "").lower()
            bundle = (r.get("bundleId") or "").lower()
            if brand in seller.replace(" ", "") or brand in seller or f".{brand}." in f".{bundle}.":
                app = app_fields(r)
                app["bundle_source"] = f"matched developer name on an App Store search for '{brand}' (inferred)"
                app["artist_id"] = r.get("artistId")
                out["ios"].append(app)
        if err:
            out["notes"].append(f"App Store name search failed: {err}")
        if not out["ios"]:
            out["notes"].append(f"No iOS app found by published bundle ID or by a developer-name "
                                f"search for '{brand}'.")

    droid_variants = [p for p in ids["android_packages"]
                      if re.search(r"\.(?:dev|staging|internal|debug|qa|beta)$", p, re.I)]
    for pkg in [p for p in ids["android_packages"] if p not in droid_variants][:6]:
        out["android"].append({
            "package": pkg,
            "listing": f"https://play.google.com/store/apps/details?id={urllib.parse.quote(pkg)}",
            "source": "assetlinks.json (observed)",
        })
    if droid_variants:
        out["android_internal_packages"] = droid_variants
    if out["android"]:
        out["android_note"] = ("Play has no public lookup API. The listing carries install count, "
                               "rating and last-updated date; open it rather than scraping it.")

    # The developer's whole portfolio, which is the actual investment signal: one
    # app's version number says little, seven listings with their update dates
    # say which product lines are live and which were shipped this quarter.
    artist = next((a.get("artist_id") for a in out["ios"] if a.get("artist_id")), None)
    if artist:
        results, err = apple_lookup({"id": artist, "entity": "software", "limit": 50})
        seen = set()
        for r in results:
            if r.get("wrapperType") == "artist" or not r.get("bundleId"):
                continue
            if r["bundleId"] in seen:
                continue
            seen.add(r["bundleId"])
            out["portfolio"].append(app_fields(r))
        out["portfolio"].sort(key=lambda a: (a["last_update"] or ""), reverse=True)
        fresh = [a for a in out["portfolio"] if (a["days_since_update"] or 999) <= 30]
        n_apps = len(out["portfolio"])
        out["portfolio_note"] = (f"{n_apps} app{'' if n_apps == 1 else 's'} under this developer "
                                 f"account, {len(fresh)} updated in the last 30 days")
        if err:
            out["notes"].append(f"developer portfolio lookup failed: {err}")

    if out["ios"]:
        out["notes"].append(
            "Rating counts are per storefront. These are the US store's numbers (the lookup "
            "default); monday.com reads 48,411 ratings in the US and 6,824 in GB, so a "
            "single figure is a market's rating count, not a global one. Add &country=gb "
            "to the lookup URL to compare markets.")
    return out


# ---------------------------------------------------------------------------
# 3. Public filings
# ---------------------------------------------------------------------------
FILING_READING_GUIDE = [
    "Sales and marketing expense as a share of revenue, over three years. The trend is the "
    "whole story: rising means they are buying growth, falling means they are being asked to "
    "show leverage, and the second one changes how they bid.",
    "The revenue growth rate next to that S&M line. Spend up and growth flat is a company "
    "whose acquisition engine has stopped scaling, which is when they get aggressive on price.",
    "Customer cohort disclosures: dollar-based net retention, customers above a spend "
    "threshold ($50k, $100k ARR bands), and how those bands grew. This is the clearest public "
    "statement of whether the enterprise motion is working.",
    "Revenue by geography and by segment, which tells you which markets the hiring plan is "
    "actually funding.",
    "MD&A on go-to-market: partner and channel language, sales-capacity commentary, and any "
    "named change in motion (self-serve to sales-led, or the reverse).",
    "Risk factors naming competitors or customer-acquisition cost. Legal has to be candid "
    "there in a way marketing never is.",
]


def resolve_cik(name_hints, brand):
    """Find a CIK for the company, preferring the official ticker map.

    `sec.gov/files/company_tickers.json` is the right source and the documented
    one. It is also served from www.sec.gov, which rate-limits by IP and answers
    403 for hours at a time, so EDGAR's full-text search index is kept as a
    fallback -- it returns the CIK inside each hit's display name. The fallback
    is noisier, so a candidate carrying a ticker always outranks one without.
    """
    out = {"candidates": [], "notes": []}
    hints = [h for h in name_hints if h]

    data, status, note = get_json("https://www.sec.gov/files/company_tickers.json", 30, ua=SEC_UA)
    if isinstance(data, dict):
        rows = list(data.values())
        for row in rows:
            title = (row.get("title") or "")
            tkr = (row.get("ticker") or "")
            low = title.lower()
            for h in hints:
                hl = h.lower()
                if low == hl or low.startswith(hl + " ") or hl in low or tkr.lower() == hl:
                    out["candidates"].append({
                        "cik": str(row.get("cik_str")).zfill(10), "name": title,
                        "ticker": tkr, "source": "sec.gov ticker map", "confidence": "strong"})
                    break
    else:
        out["notes"].append(f"sec.gov/files/company_tickers.json unavailable ({note or status}). "
                            f"www.sec.gov rate-limits by IP; EDGAR full-text search was used "
                            f"instead, which matches on entity name and is less exact.")

    if not out["candidates"]:
        for h in hints[:2]:
            url = ("https://efts.sec.gov/LATEST/search-index?q=&entityName="
                   + urllib.parse.quote(h))
            data, status, note = get_json(url, 30, ua=SEC_UA)
            if not isinstance(data, dict):
                out["notes"].append(f"EDGAR entity search for '{h}' failed ({note or status}).")
                continue
            seen = set()
            for hit in (data.get("hits", {}) or {}).get("hits", []):
                for disp in hit.get("_source", {}).get("display_names", []):
                    m = re.match(r"(.+?)\s{2,}(?:\(([A-Z.]{1,6})\)\s{2,})?\(CIK (\d{10})\)", disp)
                    if not m:
                        continue
                    nm, tkr, cik = m.group(1).strip(), m.group(2), m.group(3)
                    low = nm.lower()
                    # The index returns every entity on a matching filing, which
                    # for a Form 4 means the reporting officer as well as the
                    # issuer. Only entities whose own name carries the brand are
                    # candidates, or every insider becomes one.
                    if cik in seen or (brand not in low and brand not in low.replace(" ", "")):
                        continue
                    seen.add(cik)
                    out["candidates"].append({
                        "cik": cik, "name": nm, "ticker": tkr or None,
                        "source": "EDGAR full-text search entity name",
                        # No ticker means it is not a listed reporting company --
                        # often a Form D filer, often an unrelated namesake.
                        "confidence": "strong" if tkr else "name match only, unverified"})
            if out["candidates"]:
                break

    out["candidates"].sort(key=lambda c: (0 if c.get("ticker") else 1, len(c["name"])))
    return out


def filings(name_hints, brand, cik_override=None):
    out = {"resolution": {}, "entity": None, "key_filings": [], "notes": [],
           "what_to_read": FILING_READING_GUIDE}
    if cik_override:
        cik = str(cik_override).zfill(10)
        out["resolution"] = {"candidates": [{"cik": cik, "name": "(supplied)", "ticker": None,
                                             "source": "command line", "confidence": "supplied"}],
                             "notes": []}
    else:
        out["resolution"] = resolve_cik(name_hints, brand)
    cands = out["resolution"]["candidates"]
    if not cands:
        out["notes"].append("No EDGAR entity matched. Most likely they are private or "
                            "foreign-listed outside the SEC's remit; a name that differs from "
                            "the brand (a holding company) would also miss, so check the "
                            "legal name in their terms of service before concluding.")
        return out

    pick = cands[0]
    cik = pick["cik"]
    data, status, note = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json", 40, ua=SEC_UA)
    if not isinstance(data, dict):
        out["notes"].append(f"submissions JSON for CIK {cik} unavailable ({note or status}).")
        return out

    out["entity"] = {
        "cik": cik, "name": data.get("name"), "tickers": data.get("tickers"),
        "exchanges": data.get("exchanges"), "sic": data.get("sicDescription"),
        "fiscal_year_end": data.get("fiscalYearEnd"), "category": data.get("category"),
        "state_of_incorporation": data.get("stateOfIncorporation"),
        "edgar": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K",
        "matched_as": pick["confidence"],
    }

    f = data.get("filings", {}).get("recent", {}) or {}
    forms = f.get("form", []) or []
    out["form_counts"] = dict(Counter(forms).most_common(12))
    # 10-K/10-Q are domestic; a foreign private issuer files 20-F annually and
    # reports quarterly results on 6-K instead, so leaving 6-K out would make a
    # company like monday.com look as if it stopped reporting between annuals.
    wanted = ["10-K", "10-K/A", "20-F", "40-F", "10-Q", "6-K", "S-1", "F-1", "D"]
    per_form = {}
    for i, form in enumerate(forms):
        if form not in wanted or form in per_form:
            continue
        acc = (f.get("accessionNumber") or [None] * len(forms))[i]
        doc = (f.get("primaryDocument") or [None] * len(forms))[i]
        nodash = (acc or "").replace("-", "")
        per_form[form] = {
            "form": form,
            "filed": (f.get("filingDate") or [None] * len(forms))[i],
            "period": (f.get("reportDate") or [None] * len(forms))[i],
            "accession": acc,
            "document": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/{doc}"
                         if acc and doc else None),
            "filing_index": (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/"
                             f"{acc}-index.htm" if acc else None),
        }
    out["key_filings"] = [per_form[w] for w in wanted if w in per_form]

    if len(cands) > 1:
        out["notes"].append("More than one EDGAR entity matched the name; the one reported is "
                            "the best match. Others: "
                            + "; ".join(f"{c['name']} (CIK {c['cik']}, {c['confidence']})"
                                        for c in cands[1:4]))
    if not pick.get("ticker") and not (data.get("tickers") or []):
        out["notes"].append("This entity carries no ticker, so it is not a listed reporting "
                            "company — the filings here are likely Form D private placements, "
                            "and the name match may be a different company entirely. Confirm "
                            "against the legal name on their site before quoting it.")
    return out


# ---------------------------------------------------------------------------
# 4. Review sites
# ---------------------------------------------------------------------------
def self_reported_ratings(blobs):
    """Ratings the company publishes about itself, from its own markup.

    This is the cheap and reliable read the review sites themselves refuse: a
    JSON-LD aggregateRating, a Trustpilot business-unit ID, a G2 or Capterra
    product URL in a badge. It is self-reported and usually chosen to flatter,
    so it is reported as what they claim, not as the site's current score.
    """
    corpus = "\n".join(blobs)
    out = {"aggregate_ratings": [], "product_urls": {}, "trustpilot_business_unit": None}
    for m in re.finditer(r'"aggregateRating"\s*:\s*\{(.{0,400}?)\}', corpus, re.S):
        blk = m.group(1)
        val = re.search(r'"ratingValue"\s*:\s*"?([0-9.]+)', blk)
        cnt = re.search(r'"(?:ratingCount|reviewCount)"\s*:\s*"?(\d+)', blk)
        if val:
            entry = {"rating": val.group(1), "count": cnt.group(1) if cnt else None}
            if entry not in out["aggregate_ratings"]:
                out["aggregate_ratings"].append(entry)
    for key, pat in (("g2", r"g2\.com/products/([a-z0-9][a-z0-9._-]{1,60})"),
                     ("capterra", r"capterra\.com/p/(\d+)/([A-Za-z0-9-]+)"),
                     ("trustpilot", r"trustpilot\.com/review/([a-z0-9][a-z0-9.-]{2,60})")):
        m = re.search(pat, corpus, re.I)
        if m:
            out["product_urls"][key] = m.group(0) if m.group(0).startswith("http") else "https://www." + m.group(0)
    m = re.search(r'businessunit[iI]d["\':= ]{1,6}([a-f0-9]{16,32})', corpus)
    if m:
        out["trustpilot_business_unit"] = m.group(1)
    return out


def reviews(host, brand, blobs):
    """Scoped links, plus one plain request each to see whether reading is possible.

    One ordinary GET per site, no retries and no UA games. All three front their
    pages with bot protection, and the honest output is the refusal plus the
    link -- working around a challenge is not in this skill's remit.
    """
    apex = host.replace("www.", "")
    q = urllib.parse.quote_plus
    self_rep = self_reported_ratings(blobs)
    targets = OrderedDict([
        ("trustpilot", {
            "url": self_rep["product_urls"].get("trustpilot") or f"https://www.trustpilot.com/review/{apex}",
            "scoped_by": "domain — exact, nothing to disambiguate",
            "shows": "TrustScore, review count and the recent reviews, which skew to support "
                     "complaints and billing rather than evaluation",
        }),
        ("g2", {
            "url": self_rep["product_urls"].get("g2") or f"https://www.g2.com/search?query={q(brand)}",
            "scoped_by": ("product page found in their own markup"
                          if self_rep["product_urls"].get("g2") else
                          "a brand search — pick the product, G2 slugs are not derivable"),
            "shows": "category rank, grid position, and the comparison pages buyers land on; "
                     "the 'compared to' list is their real competitive set as buyers see it",
        }),
        ("capterra", {
            "url": self_rep["product_urls"].get("capterra") or f"https://www.capterra.com/search/?query={q(brand)}",
            "scoped_by": ("product page found in their own markup"
                          if self_rep["product_urls"].get("capterra") else
                          "a brand search — Capterra product IDs are numeric and not derivable"),
            "shows": "rating, review count and the alternatives panel; Capterra and G2 both sell "
                     "paid placement, so presence high in a category may be bought",
        }),
    ])
    for name, t in targets.items():
        r = fetch_full(t["url"], timeout=20)
        t["http_status"] = r["status"]
        body = r["body"] or ""
        if r["status"] == 200 and body:
            val = re.search(r'"ratingValue"\s*:\s*"?([0-9.]+)', body)
            cnt = re.search(r'"(?:ratingCount|reviewCount)"\s*:\s*"?(\d+)', body)
            if val:
                t["read"] = {"rating": val.group(1), "count": cnt.group(1) if cnt else None,
                             "from": "JSON-LD in the served page"}
            else:
                t["read"] = None
                t["note"] = "page served but no rating in the static markup (rendered client-side)"
        elif r["status"] in (403, 429, 503):
            t["read"] = None
            t["note"] = (f"refused the automated request (HTTP {r['status']}"
                         + ("; bot-protection interstitial" if "challenge" in body.lower()
                            or "verifying" in body.lower() else "")
                         + "). Correct behaviour here is to open the link in a browser; the "
                           "challenge is not something to work around.")
        else:
            t["read"] = None
            t["note"] = f"HTTP {r['status']} — link emitted, nothing read"
    return {"sites": targets, "self_reported_on_their_own_site": self_rep}


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------
def collect(target, board_override=None, cik_override=None):
    url = norm_url(target)
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    origin = f"{parsed.scheme}://{host}"
    brand = brand_of(host)

    rep = {"target": url, "host": host, "brand": brand,
           "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # One pass over the homepage and careers pages serves three sections: the
    # board token, the legal name EDGAR is resolved by, and the review badges.
    # Fetching them once is also the difference between a 30-second run and a
    # two-minute one.
    blobs, pages, csp = discover_site_text(origin, host)

    names = []
    for pat in (r'"legalName"\s*:\s*"([^"]{2,80})"',
                r'"name"\s*:\s*"([^"]{2,80})"\s*,\s*"url"\s*:\s*"https?://[^"]*' + re.escape(brand),
                r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']{2,60})'):
        for b in blobs:
            m = re.search(pat, b, re.I)
            if m and m.group(1).strip() not in names:
                names.append(m.group(1).strip())
    rep["name_hints"] = names + [brand]

    rep["hiring"], _jobs = hiring(host, blobs, csp, pages, board_override, brand, rep["name_hints"])
    rep["mobile"] = mobile(origin, host)
    rep["filings"] = filings(rep["name_hints"], brand, cik_override)
    rep["reviews"] = reviews(host, brand, blobs)
    return rep


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def render(r):
    L = []
    a = L.append
    a(f"# Company signals: {r['host']}   (collected {r['collected_utc']})")
    a("  [obs] a public source states this outright.  [inf] a reading of it.")
    if r.get("name_hints"):
        a(f"  [obs] names published on their own site: {', '.join(r['name_hints'][:4])}")

    # --- hiring -----------------------------------------------------------
    h = r["hiring"]
    a("\n## Hiring")
    if not h["boards"]:
        a("  no public job board found")
        for n in h["notes"]:
            a(f"  - {n}")
    for b in h["boards"]:
        a(f"  [{'obs' if b['provenance'] == 'observed' else 'inf'}] {b['label']} board "
          f"'{b['token']}' — {b['roles']} open roles (HTTP {b['status']})")
        a(f"        how found: {b['how']}")
        a(f"        board: {b['board_url']}")
        a(f"        api:   {b['api']}")
        if b.get("corroboration"):
            c = b["corroboration"]
            a(f"        {'confirmed' if c['ok'] else '!! NOT CONFIRMED'}: {c['how']}")
        if b.get("error"):
            a(f"        error: {b['error']}")
    for o in h["other_ats"]:
        a(f"  [obs] {o['vendor']} in use (account '{o['account']}') — {o['shows']}. "
          f"No documented public JSON board; open it in a browser.")

    if h["total_open_roles"]:
        a(f"\n  [obs] {h['total_open_roles']} open roles by function:")
        for fn, n in h["by_function"].items():
            share = 100.0 * n / h["total_open_roles"]
            a(f"        {fn:<28} {n:>4}  ({share:4.1f}%)")
        if h["by_department_as_they_name_it"]:
            a("\n  [obs] departments as their recruiting team names them "
              "(their vocabulary, not a normalisation):")
            for d, n in list(h["by_department_as_they_name_it"].items())[:18]:
                a(f"        {d[:46]:<48} {n:>4}")

        a(f"\n  [obs] locations ({h.get('remote_postings', 0)} postings mention remote):")
        for loc, n in list(h["by_location"].items())[:18]:
            a(f"        {loc[:46]:<48} {n:>4}")
        if h["possible_new_markets"]:
            a("\n  [inf] small locations whose every req was opened in the last 90 days. Read "
              "as a market being entered, or as a board that refreshes fast — their published "
              "office list settles which:")
            for m in h["possible_new_markets"][:8]:
                a(f"        {m['location'][:40]:<42} {m['roles']} roles "
                  f"({m['share_of_open_roles']}% of the board), oldest "
                  f"{m['oldest_posting_days']}d old")

        a(f"\n  [obs] {h.get('growth_stack_role_count', 0)} roles on the growth / marketing / "
          f"data surface:")
        for j in h["growth_stack_roles"][:22]:
            comp = f"  {j['comp']}" if j.get("comp") else ""
            a(f"        {(j['published'] or '----------')}  {j['title'][:62]:<64}"
              f"{(j['dept'] or '')[:22]}{comp}")
            if j.get("locations"):
                a(f"                    {', '.join(j['locations'])[:96]}")

        if h["tools_named"]:
            a("\n  [obs] tools named in the reqs. A tool in the TITLE is the system the hire "
              "will own; a tool in the body is often a nice-to-have or another team's:")
            for tool, v in list(h["tools_named"].items())[:30]:
                where = (f"TITLE x{len(v['in_title'])}" if v["in_title"] else "body only")
                n = v["postings_naming_it"]
                a(f"        {tool:<22} {n:>3} posting{'' if n == 1 else 's'}  [{where}]  "
                  f"{v['implies']}")
                for t in v["in_title"][:3]:
                    a(f"               title: {t[:84]}")
            a("  [inf] a named tool means it is in the stack or they are buying it. The reqs "
              "state the string; they do not state which.")
        for n in h["notes"]:
            a(f"  - {n}")

    # --- apps -------------------------------------------------------------
    m = r["mobile"]
    a("\n## Mobile apps")
    ids = m["identifiers"]
    a(f"  [obs] published identifiers: ios={ids['ios_bundles'] or 'none'}  "
      f"android={ids['android_packages'] or 'none'}")
    for u, s in ids["sources"].items():
        a(f"        {s:<58} {u}")
    for app in m["ios"]:
        tag = "obs" if "observed" in (app.get("bundle_source") or "") else "inf"
        a(f"\n  [{tag}] iOS: {app['name']}  ({app['bundle_id']})")
        a(f"        developer: {app['seller']}")
        a(f"        rating: {app['rating']} from {app['rating_count']:,} ratings (US store)"
          if isinstance(app["rating_count"], int) else
          f"        rating: {app['rating']} from {app['rating_count']} ratings")
        a(f"        version {app['version']}, last updated {app['last_update']} "
          f"({app['days_since_update']}d ago), first released {app['first_release']}")
        a(f"        source of the bundle id: {app['bundle_source']}")
        a(f"        listing: {app['listing']}")
    if m["portfolio"]:
        a(f"\n  [obs] every app under that developer account — {m.get('portfolio_note', '')}:")
        for app in m["portfolio"][:20]:
            a(f"        {(app['last_update'] or '----------')}  {str(app['name'])[:38]:<40}"
              f"{str(app['bundle_id'])[:32]:<34} {app['rating_count'] or 0:>7} ratings")
        a("  [inf] the portfolio is the investment read. New listings and fresh update dates "
          "show which product lines are funded; a listing with no ratings is a bet, not a "
          "business yet.")
    if m["android"]:
        a("\n  [obs] Android packages published in assetlinks.json:")
        for app in m["android"]:
            a(f"        {app['package']:<34} {app['listing']}")
        if m.get("android_internal_packages"):
            a(f"        internal build packages, not on Play: "
              f"{', '.join(m['android_internal_packages'])}")
        a(f"  {m.get('android_note', '')}")
    for n in m["notes"]:
        a(f"  - {n}")

    # --- filings ----------------------------------------------------------
    f = r["filings"]
    a("\n## Public filings")
    for n in f["resolution"].get("notes", []):
        a(f"  - {n}")
    if not f.get("entity"):
        for n in f["notes"]:
            a(f"  {n}")
    else:
        e = f["entity"]
        a(f"  [obs] {e['name']} — CIK {e['cik']}, {', '.join(e['tickers'] or []) or 'no ticker'}"
          f" on {', '.join(e['exchanges'] or []) or 'no exchange'}")
        a(f"        SIC: {e['sic']}   FY end: {e['fiscal_year_end']}   filer: {e['category']}")
        a(f"        matched: {e['matched_as']}")
        a(f"        all filings: {e['edgar']}")
        if r["filings"].get("form_counts"):
            a("  [obs] recent filing mix: "
              + ", ".join(f"{v}x {k}" for k, v in list(f["form_counts"].items())[:8]))
        a("")
        for k in f["key_filings"]:
            a(f"  [obs] {k['form']:<6} filed {k['filed']}  period {k['period'] or '—'}")
            if k["document"]:
                a(f"        {k['document']}")
            if k["filing_index"]:
                a(f"        index: {k['filing_index']}")
        for n in f["notes"]:
            a(f"  - {n}")
        # The reading guide is about an annual or quarterly report. Printing it
        # under a Form D -- the only filing a private company tends to have --
        # would promise an income statement that does not exist in the document.
        reports = [k["form"] for k in f["key_filings"]
                   if k["form"] in ("10-K", "10-K/A", "20-F", "40-F", "10-Q", "6-K")]
        if reports:
            a("\n  What to read in it (the numbers are not parsed here on purpose — a "
              "half-parsed income statement produces confident wrong figures):")
            for i, g in enumerate(f["what_to_read"], 1):
                a(f"   {i}. {g}")
        elif any(k["form"] in ("D", "D/A") for k in f["key_filings"]):
            a("\n  Form D is all there is, so there are no financials to read. What it does "
              "carry: the offering amount, the exemption claimed and the executive officers "
              "named at the time — which dates a funding round and names who was running the "
              "company when it closed.")

    # --- reviews ----------------------------------------------------------
    rv = r["reviews"]
    a("\n## Review sites")
    for name, t in rv["sites"].items():
        a(f"  {name}: {t['url']}")
        a(f"        scoped by: {t['scoped_by']}")
        a(f"        shows: {t['shows']}")
        if t.get("read"):
            a(f"        [obs] read from the page: rating {t['read']['rating']}, "
              f"count {t['read']['count']} ({t['read']['from']})")
        if t.get("note"):
            a(f"        note: {t['note']}")
    sr = rv["self_reported_on_their_own_site"]
    if sr["aggregate_ratings"] or sr["trustpilot_business_unit"]:
        a("\n  [obs] ratings they publish about themselves, from their own markup "
          "(self-reported, and chosen to flatter):")
        for x in sr["aggregate_ratings"][:6]:
            a(f"        ratingValue {x['rating']}, count {x['count']}")
        if sr["trustpilot_business_unit"]:
            a(f"        trustpilot business unit id: {sr['trustpilot_business_unit']}")
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
    board = cik = None
    if "--board" in argv:
        i = argv.index("--board")
        board = argv[i + 1] if len(argv) > i + 1 else None
    if "--cik" in argv:
        i = argv.index("--cik")
        cik = argv[i + 1] if len(argv) > i + 1 else None

    rep = collect(target, board, cik)

    if "--json" in argv:
        i = argv.index("--json")
        nxt = argv[i + 1] if len(argv) > i + 1 else None
        path = nxt if nxt and not nxt.startswith("--") else None
        if path:
            print(render(rep))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(rep, fh, indent=2, default=str)
            print(f"\n[raw facts written to {path}]")
        else:
            print(json.dumps(rep, indent=2, default=str))
    else:
        print(render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
