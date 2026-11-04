# Public endpoints and what each one tells you

Everything here is served to any visitor on request. `recon.py` collects most of
it; this file explains what to do with it and which readings are reliable.

Contents: [Google tag endpoints](#google-tag-endpoints) ·
[CSP](#the-csp-header) · [robots.txt](#robotstxt) · [Sitemaps](#sitemaps) ·
[Headers and cookies](#response-headers-and-cookies) ·
[Experimentation](#experimentation-platforms) · [Other files](#other-well-known-files) ·
[DNS](#dns) · [Hiring](#hiring-signals) · [Live verification](#live-verification)

## Google tag endpoints

| URL | Returns |
|---|---|
| `googletagmanager.com/gtm.js?id=GTM-XXXXXXX` | the full published container — see `gtm-container.md` |
| `googletagmanager.com/gtag/js?id=G-XXXXXXXX` | the GA4 loader |
| `googletagmanager.com/gtag/js?id=GT-XXXXXXX` | the Google Tag wrapper |
| `googletagmanager.com/gtag/js?id=AW-XXXXXXX` | the Ads loader |

A caveat worth knowing, because it sounds like it should work and doesn't:
fetching `gtag/js` for a GA4 measurement ID does **not** reliably enumerate
linked Ads or Floodlight destinations. Tested against live properties, the
response contains only the ID requested plus its `GT-` wrapper. Don't claim a
linked-account map from this endpoint. The container is the better source for
which destinations are actually configured.

## The CSP header

The most underrated signal available. A site's `Content-Security-Policy` is its
own allowlist of every domain it permits the browser to contact — so it names
vendors that are loaded by GTM at runtime, loaded only on other pages, or loaded
conditionally, none of which appear in the served HTML.

On a live example, the HTML yielded 6 vendor fingerprints and the CSP yielded 26:
Meta, LinkedIn, TikTok, Pinterest, Quora, Microsoft Ads, Amazon DSP, Xing, Yahoo
Japan, Capterra, G2, ZoomInfo, Contentsquare, Datadog, Recurly, Wistia,
TVSquared and more. A vendor seen only in the CSP is still live — it is injected
at runtime rather than written into the markup. `recon.py` marks these
`[seen in csp]`.

A CSP also reveals internal and staging hosts, regional app shards, and
occasionally an acquisition's domain. Read the full allowlist, not just the hits.

**A permissive or absent CSP is no information, not a small stack.** Plenty of
large sites serve `default-src *` or no CSP at all; one real target with a very
broad martech stack serves no CSP. The collector says so explicitly rather than
returning an empty list, because reading "no CSP vendors" as "few vendors" would
invert the finding. Fall back to the container's own hosts and a `--pages` sweep.

## robots.txt

The `Disallow` list is written to keep crawlers out, which means it is a
hand-curated list of paths the company considers not-for-public-search. Directory
names leak intent reliably:

- `/lp/`, `/p/`, `/go/`, `/campaign/` — paid landing pages, often the entire
  paid-acquisition surface
- `/test/`, `/staging/`, `/preview/` — test environments
- `/ebook/`, `/resources/thank-you`, `/gated/` — the gated-content funnel and
  its thank-you pages (thank-you pages are where conversion tags fire)
- a customer or partner name as a directory — a co-marketing or enterprise deal
- `/internal/`, `/admin/` — noted only; do not fetch

The `User-agent` blocks are a second signal, but they have to be *compared*
rather than listed. Naming the AI crawlers that appear in robots.txt implies
they are restricted; on one real target every AI agent's rules were identical to
the default `*` group and the file ended `Content-Signal: ai-train=yes,
search=yes` — nothing was blocked at all. The collector reports a verdict per
agent (`BLOCKED`, `same-as-default (NOT blocked)`, `more restrictive`, `more
permissive`) and surfaces any `Content-Signal` line.

`Sitemap:` lines point at the real sitemap, which is often not `/sitemap.xml`.

## Sitemaps

The path-prefix histogram is the fastest read on where a company invests its
content and SEO effort. Prefixes that matter disproportionately:

- `/compare/`, `/alternative/`, `/vs/` — their competitive SEO targeting, and
  specifically **which competitors they name**. A list of `/alternative/<rival>`
  pages is a direct statement of who they think they displace.
- `/templates/`, `/tools/`, `/integrations/` — programmatic SEO at scale; the
  count tells you how big the bet is
- `/case-study/`, `/customers/` — named logos and the segments they prove
- locale prefixes (`/de/`, `/ja/`, `/zh-tw/`) — which markets get real
  investment, weighted by page count per locale rather than mere presence
- `/jobs/`, `/careers/` — see hiring below

`lastmod` dates show publishing cadence and which sections are abandoned.

## Response headers and cookies

- `Server`, `X-Powered-By`, `Via`, `X-Vercel-Id`, `CF-Ray`, `X-Served-By`,
  `X-Amz-Cf-Id` — hosting and CDN, and whether the marketing site is deployed
  separately from the app (it usually is, and by a different team)
- `X-Generator`, `<meta name="generator">` — CMS
- `Strict-Transport-Security`, `Permissions-Policy`, `Report-To` — security
  maturity
- `Set-Cookie` **on first load, before any consent interaction** — this is the
  compliance tell. Analytics or ad cookies set pre-consent in an EU-serving site
  is a real finding; `recon.py` lists the cookie names it sees on the first
  response.

## Experimentation platforms

Worth probing, with honest expectations — exposure varies by platform and config.

| Platform | Endpoint | What you get |
|---|---|---|
| Optimizely Feature Experimentation | `cdn.optimizely.com/datafiles/<sdkKey>.json` | experiment keys, feature flags, events, audiences — when the SDK key is public and the file isn't restricted |
| Optimizely Web Experimentation | `cdn.optimizely.com/js/<projectId>.js` | the live snippet; readable experiment names sometimes survive, often not |
| VWO | `dev.visualwebsiteoptimizer.com/j.php?a=<accountId>` | **live campaign names in plain text** — the best A/B answer available anywhere |
| GrowthBook | the `apiHost` + `clientKey` in the snippet | feature definitions |
| Statsig / LaunchDarkly / Split | client SDK key in the page | the key identifies the account; flag payloads are generally **not** readable without initialising a client, which you should not do |

Verified behaviour: `cdn.optimizely.com/datafiles/<projectId>.json` returns 403
for Web Experimentation project IDs — a project ID is not an SDK key. The `/js/`
path returns the snippet. Minification often strips readable names, so report
"platform present, names not exposed" rather than implying you found nothing
because nothing is running.

`recon.py` fetches the VWO endpoint automatically when VWO is detected. The
account ID sits in the SmartCode inside a Custom HTML tag, as a minified
assignment (`window._vwo_code||function(){var h=765415,...`), so the variable
name varies — the parser matches the first numeric assignment after the
`_vwo_code` marker and exposes it under `vendor_account_ids`. On a live target
this returns all 14 running `SPLIT_URL` campaigns by name, which told you not
just that they test but exactly what they are testing and across which landing
pages.

**The other reliable source of experiment names is the GTM container's lookup
tables**, which frequently list live test names verbatim so that analytics can
segment on them. Check there first.

## Other well-known files

| Path | Signal |
|---|---|
| `/ads.txt`, `/app-ads.txt` | authorised sellers — they sell inventory; names the SSPs |
| `/.well-known/security.txt` | security contact, bug bounty programme, sometimes a hiring link |
| `/llms.txt` | their own positioning summary, written for machines — unusually candid and free of marketing hedging |
| `/.well-known/assetlinks.json`, `/apple-app-site-association` | mobile app bundle IDs; confirms a real mobile motion |
| `/humans.txt` | team and tool credits |
| `/feed`, `/rss.xml`, `/changelog` | release cadence and what they ship |
| `__NEXT_DATA__` in a Next.js page | server-rendered props; sometimes CMS structure, feature flags or pricing data |

## DNS

- `MX` — their email provider
- `NS` — DNS host, which often implies the cloud they live in
- `TXT` — the richest one. Domain-verification records name every SaaS vendor the
  company has verified a domain with: Atlassian, Workday, ServiceNow, DocuSign,
  Zoom, Slack, Figma, HubSpot, Marketo, SendGrid, Klaviyo, OneTrust and so on.
  This reaches well beyond martech into their operating stack, and new records
  can indicate a migration in progress.

## Hiring signals

The ATS detected in the CSP or page (Greenhouse, Lever, Ashby) has a public job
board. Open roles are the most forward-looking public signal there is: a
"Lifecycle Marketing Manager (Marketo)" posting confirms the stack *and* tells
you the motion they are building next. Count by function to see where headcount
is going, and read requisition text for named tools.

## Live verification

When configuration isn't enough and the question is what actually fires, load
their public pages in a browser as an ordinary visitor and watch the network
requests: `/collect` and `/g/collect` calls for GA4, `googleads.g.doubleclick.net`
for Ads conversions, `/tr/` for Meta. The dataLayer is readable in the console on
pages you have loaded normally.

That is still just being a visitor. What crosses the line is interacting with
their systems in ways that create records — submitting forms, starting trials,
firing fake conversions, or signing in. Don't; it pollutes their data, and a
report built on contaminated evidence isn't worth having.
