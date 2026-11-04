---
name: growth-recon
description: >
  Research a website's growth and marketing setup from the public files it
  serves every visitor — Google Tag Manager container, GA4 and Google Ads
  conversion tracking, ad pixels, live A/B test names, per-market lead values,
  martech/ABM stack, robots.txt and sitemap — and return a detailed analyst
  report reconstructing their funnel. Use this whenever a URL or company domain
  comes up and someone wants to understand a competitor, prospect or their own
  site: "research this link", "analyse this competitor", "what's their tracking
  setup", "what tools do they use", "read their GTM container", "GTM-XXXXXXX",
  "what are they optimising for", "reverse engineer their funnel", "site
  teardown", "what's their martech stack", "are they running ads", "what are
  they A/B testing". Use it for casual asks ("look at acme.com for me") and
  narrow ones ("just find their pixel IDs") too — the workflow scales down.
  Also use it to audit your own site's public exposure.
---

# Growth recon

Every website hands its entire client-side measurement configuration to anyone
who loads a page. It has to: the browser needs the file to run the tracking. That
file names which ad platforms they buy on, which conversions they optimise
toward, what their funnel stages are called internally, which experiments are
live, and sometimes what a lead is worth to them per market.

This skill turns a URL into an analyst report built on that public evidence.

## Principles that make the difference

**Collect mechanically, interpret deliberately.** The scripts here gather raw
facts fast and without judgment. The value you add is reading them — a container
with 105 GA4 events and 2 Ads conversions tells a story about a company that
measures far more than it optimises, and no script will say that for you.

**Separate what you saw from what you infer.** A conversion label in a container
is a fact. "They pay $800 a demo" is an inference, usually a shaky one. Label
every claim in the report (see Confidence below). A report that overclaims is
worse than no report, because someone will set a bid from it.

**Configuration is not behaviour.** The container describes what *would* fire.
It does not prove anything fired, or that a tag is unpaused, or that the account
behind an ID still spends. Say so where it matters.

**Scope is a page, not a domain.** Marketing site, app, docs and checkout often
load different containers. One homepage read is a sample, not an inventory.

## Boundaries

Stay on public, unauthenticated, as-served artefacts: the HTML, response
headers, referenced scripts, the published container, and well-known paths that
exist to be fetched. That is what this skill does and all it should do.

Don't sign in, don't use anyone's credentials, don't submit forms or create test
leads in their systems, don't probe for unlisted or admin paths, don't brute
force IDs, and don't hammer a host — a handful of sequential requests, like any
visitor. Decline anything that needs access they didn't grant to the public, and
say plainly why. If the person wants the tracking *verified live* rather than
read from config, that means loading their public pages in a browser as an
ordinary visitor and watching the network tab — still fine, still public.

Reading a competitor's public pixel config is normal competitive analysis. Using
what you find to impersonate them, pollute their analytics, fire fake
conversions, or publish someone's internal naming as a gotcha is not — and
scraping personal data out of these files is out of scope regardless of who asks.

## Workflow

### 1. Collect

```bash
python3 -I scripts/recon.py <url> --json recon.json --pages /pricing,/demo
```

This fetches the page, headers, CSP, well-known paths, sitemap, DNS, every
vendor fingerprint, every GTM container it finds (parsed in full), the gtag
endpoints, and any public experimentation datafile. `--pages` is worth using:
pricing and demo pages routinely carry tags the homepage doesn't.

Then read each container closely — this is where the real material is:

```bash
python3 -I scripts/gtm_parse.py GTM-XXXXXXX
python3 -I scripts/gtm_parse.py GTM-XXXXXXX --json   # full lookup table rows
```

Both scripts are stdlib-only and fall back to `curl` when Python's CA store is
broken, which is common. If a container 404s or Google rate-limits, retry once,
then note it as a gap rather than guessing at the contents.

Two things to check in the collector's output before you read anything else,
because both can invalidate everything downstream:

- **An identity warning.** A CDN edge can answer a request for one company's
  homepage with another company's cached page, container ID and all. The
  collector compares the canonical and `og:url` hosts against what you asked
  for and prints `!! IDENTITY MISMATCH` when they disagree. Re-fetch (try the
  apex domain) before using the collection — a confident report about the wrong
  company is the worst outcome available here.
- **"Only one container found."** Sites routinely serve several containers split
  by page template or by app vs marketing site; one real target serves three,
  and only one of them is reachable from the homepage. The collector sweeps
  every page you pass and follows Zone children, so pass `--pages` generously
  and try the app or signup subdomain before calling a collection complete.

### 2. Read the container properly

`references/gtm-container.md` is the field map: what each tag type means, which
fields carry what, and the specific traps. Read it before interpreting output —
several obvious-looking readings are wrong, and four of them are common enough
that getting them right is most of this skill's edge:

- **`vtp_value` is not a conversion value.** It is usually a Meta pixel ID or a
  constant. A 15-digit "value" is a pixel, not revenue. Real per-market lead
  values show up in **lookup tables** instead.
- **Counting `AW-` strings does not count conversions.** `AW-` identifies an
  *account*. Distinct `vtp_conversionLabel` values count conversion *actions*.
- **`__awct` tags undercount badly** in modern setups, where conversions route
  through GA4 events or server-side tagging with the IDs passed in the
  dataLayer. Trust the label-shaped-string count over the tag count.
- **`__tg` is a Trigger Group, not an ad vendor.** Likewise `__cl`, `__lcl`,
  `__sdl` are built-in listeners — they tell you what the site measures, not who
  it sends to.
- **A label-shaped string is not automatically an Ads label.** The parser
  separates labels tied to an Ads tag or parameter from `UNATTRIBUTED` ones; on
  one live container the extra "label" was an OpenAI pixel ID. Don't fold
  unattributed strings into the conversion-action count.

**Read the gating groups first.** `gtm_parse.py` resolves every tag's firing
conditions and reports the conditions that gate several tags at once. That
section is usually the fastest route to the real finding, because a condition
shared across many destination tags is a policy decision — the company's working
definition of an event worth counting. On one live container it surfaces
`13 tags gated on: NOT ({{lead_data.company_size}} contains '1-19')`, covering 7
Google Ads conversions, 2 Microsoft UET tags and a GA4 event: a hardcoded
firmographic definition of a paid lead. Then check which platforms are *missing*
from a gate — on that same target the LinkedIn tags carry no such condition, so
their LinkedIn bidding sees a population their Google bidding never does.

Lookup tables deserve a dedicated pass. A marketer had to hand-type both sides
of every row, so they carry names and numbers that exist nowhere else public:
per-region lead values, plan and SKU identifiers, firmographic segment bands,
live A/B test names, gated asset titles. `gtm_parse.py` classifies and sorts
them so the interesting ones come first. Read the rows, not just the labels.

### 3. Widen past the container

`references/public-endpoints.md` lists the other public surfaces and what each
one is good for — robots.txt disallow lists (which leak paid landing-page and
test directories), sitemap path inventories (comparison and alternative pages
map their competitive SEO targeting), ads.txt, security.txt, `llms.txt`, DNS TXT
vendor hints, ATS job boards, and the experimentation endpoints.

`references/vendor-fingerprints.md` covers what a detection *implies* about the
go-to-market motion. This is the interpretive layer: RB2B or Clearbit means
person-level de-anonymisation feeding outbound. Marketo or Eloqua means an
enterprise MQL motion with a long cycle. Chili Piper on a demo page means
speed-to-lead routing. Mutiny or Intellimize means ABM personalisation. Read the
stack as a statement about how they sell.

One collection note worth internalising: most vendors are injected by GTM at
runtime and never appear in the served HTML. The **CSP header** is the site's
own allowlist of every third party it talks to, so it often reveals two to four
times more of the stack than the HTML does. `recon.py` fingerprints it and
marks those hits `[seen in csp]` — they are live, just not inline.

### 4. Reconstruct the funnel

Order the GA4 event names and conversion actions into the funnel their team
actually built, because event names are written by the people who own the
funnel: `page_view → signup_create → trial_signup → high_value_signup →
demo_form_submit → purchase` is a strategy, legible in sequence. Note which
stages carry a conversion action (what they optimise) versus which are only
measured (what they watch). The gap between the two is usually the most
interesting finding in the whole report.

### 5. Write the report

Follow `references/report-template.md`. Lead with what someone can act on,
not with an inventory of IDs. A reader should be able to skip to "So what" and
get the three things worth doing.

`examples/asana-recon.md` is a complete worked report from a real target. It is
the fastest way to calibrate depth, tone and how the confidence markers read in
practice — skim it before writing your first report.

Mark every substantive claim:

- **Observed** — it is in the file; quote or cite the field.
- **Inferred** — a reasonable read of observed facts; say which facts.
- **Speculative** — plausible, unverified; include only when it is decision-
  relevant, and say what would confirm it.

### 6. On the economics question

People come to this hoping to read a competitor's unit economics and outbid
them. Be straight about what the evidence supports.

Hardcoded conversion values do exist — a region-to-value lookup table is real,
and when you find one it is strong material. But values are usually pushed from
the dataLayer at runtime, so the container shows only the *schema*: that a value
and currency travel with the event, not the numbers. Where you find numbers,
also say what they probably are: a configured bid target or a modelled lead
value, which is not the same as their CAC, their actual bid, or their margin.
And a value is an input to their bidding, not a cap on it.

If someone asks "can I outbid them", the honest answer is that the container
tells you what they optimise toward and how sophisticated their measurement is,
which is genuinely useful for planning, and that bid-level conclusions need
auction data — their actual auction behaviour is visible in an auction insights
report or an ad library, not in a tag container.

## Scaling down

Not every request needs the full report. Match the output to the ask:

- "What's their GTM ID / pixel IDs" → run the collector, answer in a few lines.
- "What are they A/B testing" → container lookup tables plus the experimentation
  endpoints, with the caveat that test names are not always exposed.
- "Full teardown" → the whole workflow and the full template.

Still run the collector even for small asks: it is one command, and it stops you
guessing at something the file states outright.
