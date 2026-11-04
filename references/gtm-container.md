# Reading a published GTM container

Contents: [Why it's public](#why-its-public) · [Structure](#structure) ·
[Tag types](#tag-types) · [Variable types](#variable-types) ·
[Where the real value is](#where-the-real-value-is) · [Traps](#traps) ·
[What is not in the file](#what-is-not-in-the-file) · [Server-side](#server-side-tagging)

## Why it's public

The GTM snippet loads `https://www.googletagmanager.com/gtm.js?id=GTM-XXXXXXX`.
Every visitor's browser downloads it, so it is public by construction — there is
no access control to bypass and nothing is being circumvented by reading it as
text. Fetching it with the ID alone returns the **published** container version.

Find the ID in the page source (`GTM-` followed by 4–10 uppercase alphanumerics),
or let `recon.py` extract it. A page can load more than one container, and
different sections of a site (marketing, app, docs, checkout) frequently use
different ones, so treat a container as belonging to a page rather than a domain.

## Structure

Inside `gtm.js` is a single `var data = {...}` object. It happens to be strict
JSON, so it parses cleanly — `gtm_parse.py` extracts it with string-aware brace
counting so that braces inside Custom HTML bodies don't end the object early.

`data.resource` holds four parallel lists, cross-referenced **by index**:

| Key | What it is |
|---|---|
| `macros` | variables — dataLayer reads, constants, lookup tables, custom JS |
| `tags` | each tag's type and field values |
| `predicates` | single conditions, e.g. `{{Event}} equals purchase` |
| `rules` | which predicates fire or block which tags |

A reference to a variable is encoded as `["macro", <index>]`. Distinguishing
that from a literal is the single most important read in the file: a literal is
a number their marketer typed, a macro reference is a runtime value you cannot
see. `gtm_parse.py` marks this with a `runtime` flag on every field it renders.

Other top-level keys worth knowing: `permissions` (per-template permission
grants — the way to fingerprint an unnamed community template), `security_groups`,
`sandboxed_scripts`, `productSettings` (e.g. `preAutoPii` per Ads account), and
`runtime` (compiled custom template code).

## Tag types

Each tag carries a `function` key naming its type.

**Google**
| `function` | Meaning |
|---|---|
| `__googtag` | Google Tag (gtag base) |
| `__gaawe` | GA4 Event — `vtp_eventName`, `vtp_eventSettingsTable` |
| `__gaawc` | GA4 Configuration |
| `__awct` | Google Ads Conversion — `vtp_conversionId`, `vtp_conversionLabel`, `vtp_conversionValue` |
| `__sp` | Google Ads Remarketing / user list |
| `__gclidw` | Conversion Linker |
| `__flc` / `__fls` | Floodlight Counter / Sales (DV360, CM360) |

**Other named vendors**: `__baut` Microsoft UET, `__bzi` LinkedIn Insight,
`__pntr` Pinterest, `__qpx` Quora, `__crto` Criteo,
`__twitter_website_tag` X.

**Generic**: `__html` Custom HTML (read `vtp_html` for the hosts it pulls in),
`__img` Custom Image pixel, `__zone` Zone (loads another container),
`__cvt_*` community template.

**Not vendor tags** — these are easy to misread as mystery platforms:
`__tg` is a **Trigger Group**. `__cl` (all-element click), `__lcl` (link click),
`__fsl` (form submit), `__sdl` (scroll depth), `__tl` (timer), `__ytl` (YouTube),
`__evl` (element visibility), `__hl` (history change), `__jel` (JS error) are
built-in **listeners**. Listeners are still informative: they tell you what
interactions the site bothers to measure.

### Identifying `__cvt_` templates

Community template names are not published. Their requested **permissions** are,
and they give the vendor away: a template asking to touch the global `fbq`, or to
send a pixel to `analytics.tiktok.com`, has identified itself. `gtm_parse.py`
extracts the globals and hosts from each template's permission grants into a
`fingerprint` block. Report an unresolved one as "unidentified template" with its
fingerprint rather than guessing a vendor.

## Variable types

| `function` | Meaning |
|---|---|
| `__v` | dataLayer / built-in variable — `vtp_name` is the key it reads |
| `__c` | Constant |
| `__jsm` | Custom JavaScript (body is readable) |
| `__smm` | **Lookup Table** |
| `__remm` | **RegEx Lookup Table** |
| `__u` | URL variable |
| `__e` | Event name |
| `__d` | DOM element |
| `__k` | 1st-party cookie — names the cookies they read |
| `__aev` | Auto-event variable |
| `__gas` | GA settings |

The set of `__v` variable names is effectively their **dataLayer schema** — the
data model their engineering and marketing teams agreed on.

## Where the real value is

**Lookup tables are the best thing in the file and almost nobody reads them.**
Someone had to hand-type both sides of every row, so the rows contain names and
numbers that appear nowhere else publicly. Real examples from live containers:

- A 20-row table mapping region → number: `United States → 169.74`,
  `Canada → 132.29`, `United Kingdom → 131.40`, `DACH → 144.02`,
  `Japan → 210.07`, `CEE → 33.42`. That is a per-market lead or conversion
  value, configured by market. This is the genuine version of the "find their
  unit economics" idea.
- Tables of live **A/B test names**: `ClickEmailSignupCTAOption@LPTest`,
  `GoogleOneTapSignup@Visible`, `sidebarSignupExperiment`, `FormShort388`.
- **Plan and SKU identifiers**: `org-true-tier-1-premium → trial`,
  `workspace-true-tier-1plus-premium → trial`.
- **Firmographic segment bands** from an employee-count regex:
  `^([1-9])$ → VVSB`, `^([1-9]\d)$ → VSB`, `^([1-9]\d\d\d|\d{3,})$ → CORP & ENT`.
- **Gated asset titles** by path: `/go/forrester-tei → Forrester TEI Report`.

`gtm_parse.py` classifies tables (`numeric-values`, `experiment / variant names`,
`plan / SKU / pricing`, `segmentation bands`, `locale / geo routing`) and sorts
the interesting ones first. The classification is a hint to steer attention —
read the rows yourself. Use `--json` for complete rows when a table is truncated.

**GA4 event names** are the funnel in their own vocabulary, written by the people
who own it. The event *parameters* matter too: `user_data.email_address` or
`user_data.external_id` means enhanced conversions / CAPI-style matching, and
`first_party_collection` means server-side.

**Google Ads conversion labels** count the conversion actions they optimise
toward. See the traps below for how to count them correctly.

## Traps

These are the readings that look obvious and are wrong. Getting them right is
most of the analytical edge available here.

**`vtp_value` is not a conversion value.** Across live containers, the numbers in
`vtp_value` are overwhelmingly Meta pixel IDs (15–16 digits) and constants. A
viral version of this technique tells you to search the container for `value` and
read the number next to it as what a demo is worth. Do that and you will report a
pixel ID of `1641293582765384` as a dollar figure. Conversion values live in
`vtp_conversionValue` on `__awct` tags — and are usually macro references — or in
a lookup table.

**`AW-` counts accounts, not conversions.** `vtp_conversionId` is the Ads
account. Distinct `vtp_conversionLabel` values are the conversion *actions*. One
real container has 5 Ads accounts across 27 conversion tags carrying 27 distinct
labels; "5 AW- strings" would have understated the optimisation surface fivefold.

**`__awct` tags undercount modern setups.** Conversions increasingly route
through GA4 events or server-side tagging, with the conversion ID and label
passed as event parameters or resolved from a lookup table. One container has
exactly 2 `__awct` tags but 8 label-shaped strings in the file, and GA4 events
carrying `google_ads_conversion_id` / `google_ads_conversion_label` parameters.
`gtm_parse.py` reports both counts; prefer the higher one and say why.

**Tag count is not sophistication.** Large containers accumulate dead weight.
`gtm_parse.py` flags tags no rule fires. A 364-tag container with 58 Custom HTML
tags is often a decade of unowned accretion, not an advanced setup — and that is
itself a finding.

**A listener is not a destination.** See the tag table above.

**A label-shaped string is not necessarily an Ads label.** `gtm_parse.py`
reports labels tied to an `__awct` tag or an Ads parameter separately from
`UNATTRIBUTED` ones, because on a live container the extra label-shaped string
was an OpenAI advertising pixel ID. Counting it would have inflated the
conversion-action count by one and invented a conversion that doesn't exist.

### Worked example: a finding that should stay speculative

monday.com's containers carry an Adxcel affiliate pixel whose payload includes,
verbatim:

    af_revenue: "[10]"    (on action=lead, in both marketing containers)
    af_revenue: "[20]"    (on signup, in the auth container)

Square brackets included. Two careful readers resolved this in opposite
directions: one called the brackets an unreplaced template placeholder, the
other reported "$10 a lead, $20 a signup" and called the 2:1 ratio a revealed
preference. Both overreached. Unreplaced tokens usually repeat rather than
differ by action, which argues the numbers are deliberate; but bracket syntax
isn't a number, which argues they're templated.

There is also a second, independent problem with reading those numbers, found
only by resolving the triggers: **the `"[10]"` pixel fires on page view**, gated
only on the CMP being ready and a visitor ID existing — not on a lead at all.
The `"[20]"` one does fire on signup. So "$10 a lead, $20 a signup" misreads
which event the number is attached to, on top of the syntax ambiguity.

The lesson generalises past this example: **before reading a value off a tag,
resolve what fires that tag.** A number attached to the wrong event is worse
than no number, because it looks like evidence.

The right move is to quote the raw string, give both readings, name the firing
condition, mark it `[speculative]`, and say what would settle it (observing the
actual pixel request fire on a public page). When evidence genuinely
underdetermines a conclusion, reporting the ambiguity *is* the finding —
resolving it to look decisive is how a report ends up wrong in a way the reader
cannot detect.

## Trigger resolution

`gtm_parse.py` walks `rules` -> `predicates` -> `macros` to recover, for each
tag, the conditions that fire and block it, rendering macro references as the
dataLayer key or cookie name they read. Two outputs come from this:

- **per-tag `fires_when`** on GA4 events and Ads conversions;
- **gating groups** — conditions shared by three or more tags.

Read the gating groups first. A condition on one tag is a configuration detail;
the same condition on thirteen tags across Google, Microsoft and GA4 is a policy
the company has committed to. Conditions that just name the event or wait for
container load are filtered out, because the qualifying conditions layered on
top are what carry intent.

Then ask which platforms are **absent** from a gate. A qualifying condition
applied to some ad platforms and not others means those platforms are being
trained on different populations, which is both a real finding about their
measurement and an exploitable asymmetry.

## What is not in the file

Be explicit about these in a report, because the gaps shape what you can claim:

- **Names, folders and notes** — tag, trigger and variable names live only in the
  GTM account. You infer purpose from type plus trigger.
- **Paused tags** — not published, and their settings are not recoverable.
- **Drafts and unpublished workspaces** — only the live version is served.
- **Server container configuration** — never reaches the browser.
- **Whether anything actually fired** — the file describes what *would* happen.
  Live verification means loading their public pages as a visitor and watching
  the network requests.
- **Whether the account still spends** — an `AW-` ID proves configuration, not
  active budget.
- **Other pages and other containers** — one read is a sample.

## Server-side tagging

Look for `transport_url` or `vtp_serverContainerUrl` (often a first-party
subdomain like `cdp-cdn.example.com` or `t-antenna.example.com`), and
`vtp_firstPartyCollectionDomain`. Server-side tagging means a meaningful part of
their measurement is deliberately invisible from the client, which is itself a
finding — it signals investment and usually a privacy or ad-blocker-resilience
programme. Note the transport host and stop there; what runs behind it is not
public.

## Consent

`gtm_parse.py` reports consent-aware tag counts, whether `ad_storage` and
`analytics_storage` are referenced, and CMP hints. Combined with the cookies set
on first load before any consent interaction (`recon.py` reports these), this
says a lot about their data quality in the EU — and about how much of their
measured funnel survives consent in practice.
