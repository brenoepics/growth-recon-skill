# Runtime observation: what actually fired, and for how much

Every other reference here reads configuration. Configuration has one hole it
cannot close: **conversion values**. They are pushed from the dataLayer at
runtime, so the published container shows only that a value travels with the
event, never the number. The same is true of which tags are really unpaused,
which fire for a visitor in your geo, and what consent actually gates.

A measurement hit carries all of it in the query string. So load their public
pages as an ordinary visitor, save the network requests, and read the numbers off
the wire. This file is the method.

Contents: [Boundaries](#boundaries) · [Capturing](#capturing-a-visit) ·
[Endpoints that matter](#endpoints-that-matter) ·
[Reading the output](#reading-the-output) ·
[Consent-gated difference](#the-consent-gated-difference) ·
[Resolving runtime references](#resolving-the-values-the-container-could-only-reference) ·
[What this proves](#what-this-proves) · [What it does not](#what-it-does-not-prove) ·
[Gotchas](#gotchas-found-on-live-sites)

## Boundaries

Loading a public page in a browser and looking at what it sent is being a
visitor. It is the same artefact `recon.py` fetches, just observed running. That
is fine, and it is the whole method.

What is not fine, and is never worth it:

- **Don't sign in.** Not with your credentials, not with anyone's, not with a
  Google or LinkedIn button.
- **Don't submit their forms.** No demo requests, no newsletter signups, no
  "test" leads. A submitted form creates a record in a real CRM, alerts a real
  SDR, and corrupts the very funnel you are measuring.
- **Don't create trial accounts.** Same reason, plus it is usually a terms
  violation.
- **Don't fire conversions.** Don't replay a captured hit, don't hand-edit a
  `/collect` URL and load it, don't add to cart to "see the value", don't walk a
  checkout. Any of these writes a fake conversion into someone's ad account,
  trains their bidding on it and costs them money.
- **Don't bypass anything.** No consent-wall workarounds, no spoofed geo to see
  another market's tags, no disabling CSP, no editing their page's JS. If a
  banner gates the tags, the gated state *is* the finding.

Which leaves: navigate to public URLs, scroll, read. Clicking a consent banner is
an interaction with a third party's compliance flow, so get the person's
go-ahead before clicking Accept, and otherwise keep the privacy-preserving
choice. Everything below works on a page you loaded normally.

## Capturing a visit

Open the target's public pages in the browser one at a time, giving each a few
seconds to settle — most measurement fires after load, not during it. Visit the
pages where values live: a **product page** (`view_item` carries a price), a
**pricing page**, a **demo or contact page**, and the homepage for the baseline.

Then pull two things out of the tab.

**1. The network requests.** Use the browser's network-request reader if it
returns third-party hits. It frequently does not: on every site tested for this,
the in-app reader returned *only* same-origin requests — 131 of them on one
Shopify storefront, and not one `googletagmanager.com`, `google.com/ccm/collect`
or `ct.pinterest.com` call among them. Reading that as "no third-party tracking"
would invert the finding completely.

The reliable route is Resource Timing, which the page keeps for every
subresource including cross-origin pixels, with the query string intact:

```js
JSON.stringify(performance.getEntriesByType('resource').map(e => e.name))
```

On the same storefront that returned 0 third-party requests through the network
reader, this returned 121, `value=110.00&currency_code=USD` included. Filter it
if the list is long, but filter on hosts you *expect* rather than on `collect`
alone — the interesting hits live on `googleadservices.com`, `doubleclick.net`
and `bat.bing.com` too.

Resource Timing has one real blind spot: it records the URL, not the body. A hit
POSTed with its payload in the body (TikTok's `/api/v2/pixel`, Clarity's
`/collect`, Snowplow's `/tp2`, most server-side endpoints) shows up as having
fired with nothing readable attached. `runtime_parse.py` reports those as fired
with payload unseen rather than inventing an event name for them. If the capture
tool does save bodies, pass them through — the parser reads a `body` or HAR
`postData` field when one is present.

**2. The dataLayer.** On a page you have loaded normally, it is just a global:

```js
JSON.stringify(window.dataLayer)
```

This is where the numbers are literal. It is also where the event names are
*theirs* — `dl_view_item`, `generate_lead`, `high_value_signup` — rather than
Google's.

Save both to files and run the parser:

```bash
python3 -I scripts/runtime_parse.py requests.json --datalayer datalayer.json
python3 -I scripts/runtime_parse.py capture.json --json      # machine output
```

It takes whatever shape the capture came in: an array of URL strings, an array of
request objects (`url`, or `name` as Resource Timing names it), a HAR file, a
dataLayer array, one object holding a mix, or a text file with one URL per line.
Capture formats vary by tool and re-capturing because a parser was fussy wastes a
page load you cannot take back.

## Endpoints that matter

| Endpoint | Carries |
|---|---|
| `/g/collect`, `/collect` (with `v=2`) | GA4: `en` event, `ep.*`/`epn.*` params, `cu` currency, `epn.value`, `tid` property, `uid`, `pr1..prN` items |
| `/ccm/collect` | **both** GA4 and Google Ads, depending on `tid` — see gotchas |
| `googleadservices.com/pagead/conversion/<id>/` | a Google Ads **conversion**: `label`, `value`, `currency_code`, `bttype`, `ec_mode` |
| `googleads.g.doubleclick.net/pagead/viewthroughconversion/<id>/` | remarketing with `value` and a `data=` blob of their own custom params |
| `google.com/pagead/1p-user-list/<id>/` | audience membership only — **not** a conversion |
| `ad.doubleclick.net/activity;…`, `<id>.fls.doubleclick.net/activityi;…` | Floodlight: `src` advertiser, `type`/`cat` their own activity names, `cost`, `qty` |
| `facebook.com/tr` | Meta: `id` pixel, `ev` event, `cd[value]`, `cd[currency]`, `eid` |
| `bat.bing.com/action/0`, `/actionp/0` | Microsoft UET: `ti` tag, `evt`, `gv` revenue, `gc` currency, `ec`/`ea`/`el` |
| `px.ads.linkedin.com/collect/` | LinkedIn: `pid` partner, `conversionId`, `value`, `currency` |
| `analytics.tiktok.com/api/v2/pixel` | TikTok — body only, nothing in the URL |
| `ct.pinterest.com/v3/`, `/user/` | Pinterest: `tid` pixel, `event`, value nested in the `ed` JSON blob |
| `tr.snapchat.com/p`, `alb.reddit.com/rp.gif` | Snapchat `price`, Reddit `m.value` |

Loader URLs (`gtm.js`, `gtag/js`, `fbevents.js`, `connect.facebook.net/signals/config/<pixel>`,
`i18n/pixel/events.js?sdkid=`) are not events. They prove a vendor is live and
they hand over the account ID — a Meta pixel ID is often only obtainable this
way — but counting them as "what fired" is the easiest way to overstate a runtime
read, so the parser lists them in their own section.

## Reading the output

The parser leads with **conversion values actually observed**, then **values the
plumbing carries but left empty**, then every hit grouped by vendor and event.

Three distinctions do most of the analytical work:

- **A number, versus a blank, versus absent.** `value=` arriving empty means the
  wiring exists and the number is not reaching it — a defect, and the opposite of
  "they don't track value". Absent means value was never part of this event.
- **`epn.value` versus `ep.value`.** GA4's `epn.*` namespace is numeric;
  `ep.*` is strings. A value in `ep.value` was sent as a string, which is a
  common reason a platform ignores it for bidding. Both live sites tested did
  exactly this.
- **`value=0`.** Deliberate, not broken. A conversion that reports zero revenue
  cannot be feeding value-based bidding, whatever its `bttype` says.

## The consent-gated difference

A capture is taken under one consent state, and the hits say which. Google's
`gcs` parameter is `G` + a format digit + `ad_storage` + `analytics_storage`, so
`gcs=G100` is both denied and `gcs=G111` is both granted. The parser decodes it
per hit and counts the distinct states across the capture.

That gives the comparison for free. On one real storefront capture, the same
pageview produced `gcs=G100` on the early `pagead2.googlesyndication.com` hit and
`gcs=G111` on everything after a `consent_update` event — i.e. the first hits
went out under denied consent and the rest after the CMP resolved. Where more
than one state appears, the parser warns, because the hits either side of the
update are not comparable and treating them as one capture produces nonsense.

To observe the difference deliberately: capture the page untouched first, with
the banner still up. That is the pre-consent state, and it is the compliance
finding — which vendors a site talks to before anyone agrees to anything. Then,
**with the person's go-ahead**, interact with the banner and capture again. Diff
the vendor lists. Without that go-ahead, report the pre-consent capture and say
that is what it is; it is the more interesting half anyway.

Related signals worth reading off the same hits: `npa=1` (non-personalised ads,
so consent was limited), `evt=consent&src=update` on UET, `us_privacy=1---`,
and the dataLayer's own `consent_v2` block, which on one site declared every
signal `default: true` — a site-side decision to grant by default that no
container read would have told you had actually taken effect.

## Resolving the values the container could only reference

Work from the container read, not blind. `gtm_parse.py` tells you where to look:

| Container said | Capture answers it with |
|---|---|
| `vtp_conversionValue: <Data Layer 'value'>` | the `value=` on the Ads conversion hit, plus `currency_code` |
| a conversion label with no value field | whether a value rides it at all, and whether it is 0 |
| a GA4 event with a `value` parameter | `epn.value` / `ep.value` and `cu` on the `/g/collect` hit |
| `sends_ecommerce: true` | the decoded `pr1..prN` items — id, name, brand, category, variant, price |
| a lookup table of per-market values | which row fired for *your* geo, once |
| a conversion routed server-side (no `__awct` tag) | the first-party `/g/collect` host it actually posts to |

Match them up explicitly in the report: "the container references
`{{lead_data.value}}`; on a US product-page visit the hit carried
`value=110.00&currency_code=USD`". That sentence is observed fact on both halves,
and it is the thing nobody else in the funnel-teardown genre can produce.

One honesty trap here. **A value on a `view_item` is a catalogue price, not
revenue.** On a live storefront, `value=110.00` on a product-page conversion hit
is the shelf price of the shoe being looked at, passed for remarketing and ROAS
modelling — the hit was even typed `bttype=purchase`, on a page where nothing was
purchased. Reporting it as "their average order value" or "what a conversion is
worth to them" would be wrong. Say what the number is and which event carried it.

## What this proves

- These endpoints received these events, with these values and currencies, from
  this browser on this visit. That is direct observation, not inference — the
  strongest evidence class this skill can produce.
- Which accounts are genuinely reachable: an `AW-` that appears in a live hit is
  live now, which a container entry is not.
- Which tags are paused: a tag present in the container and absent from a capture
  of the page it should fire on is a real signal, if a weak one.
- That a server-side or CAPI path exists: an `eid` on a Meta hit, an `oid` on an
  Ads conversion, or collection posting to a first-party `/g/collect` host.
- That person-level identity is being passed: `uid`, `ec_mode`, `em`/`emd`
  (hashed email for enhanced conversions). Report that these are **present**.
  Their values are a visitor's personal data, out of scope for this skill, and
  the parser never prints them.

## What it does not prove

- **One visit is one session.** Absence is not evidence. A tag that did not fire
  may need a different page, a scroll depth, a referrer, a logged-in state or a
  returning visitor. Never write "they do not track X" off one capture; write
  "X did not fire on these pages, on this visit".
- **Your geo and your consent choices shape the whole capture.** Across four
  commercial sites tested from one browser, Meta's `/tr` never fired once, while
  Google's hits went out freely — the Meta pixel library loaded and its config
  endpoint named the pixel ID, but no event followed. The capture reflected one
  CMP's decision in one region (a Pinterest payload on the same page reported
  `is_eu: true`), not the absence of Meta advertising. From another country the
  same page will fire a different set.
- **An ad account's spend stays invisible.** A hit proves a conversion action
  exists and what value it reports. It says nothing about budget, bids, CPA
  targets, auction behaviour or whether the campaign is active. For that, the ad
  libraries in `public-endpoints.md`.
- **A runtime read does not replace the container read.** One live B2B marketing
  page served *no* third-party measurement at all on a normal visit — no
  dataLayer, no analytics host — while its configuration describes a full stack.
  The two methods answer different questions and disagreeing is informative;
  pick one and you will be confidently wrong half the time.
- **Values are their inputs, not their economics.** A reported value is a number
  their tag computed for one pageview. It is not their CAC, their margin, their
  bid, or a ceiling on what they will pay.

## Gotchas found on live sites

- **Route on `tid`, not on host.** `/ccm/collect` is served from `google.com`,
  `pagead2.googlesyndication.com` and others, and carries GA4 hits (`tid=G-`)
  and Google Ads hits (`tid=AW-`) through the identical path. Labelling by host
  mislabels every Ads conversion on a modern gtag site.
- **Floodlight uses `;` as its separator, inside the path.** A standard query
  parser sees `ad.doubleclick.net/activity;src=16787178;type=pagev0;cat=homep0`
  as a path with no parameters at all.
- **`/collect` alone is not a GA endpoint.** Microsoft Clarity serves one;
  server-side GTM serves a real one on the target's own domain. Check for the GA
  payload shape (`v=2`, `en`, a Google-shaped `tid`), not the path.
- **Legacy ecommerce is still everywhere in dataLayers.** Shopify dataLayer apps
  push `ecommerce.detail.products[]` and `ecommerce.purchase.actionField.revenue`
  — the pre-GA4 Enhanced Ecommerce shapes — not `ecommerce.items[]`. A reader
  that only knows GA4 walks straight past the prices.
- **`gtag()` calls land in the dataLayer as numbered keys.** `{"0":"config",
  "1":"AW-935878905"}` is a real push naming a real Ads account, not junk.
- **Some Google hits carry a base64 JSON product catalogue.** The `sdl` parameter
  on a shopping-remarketing hit decoded to the full variant list of the page's
  product with per-size prices and availability. Undocumented, so treat it as a
  bonus rather than something to rely on.
- **Two conversion labels can share one value.** One B2B site fired two distinct
  Ads conversion actions on a single homepage view, both at `value=0` with
  `bttype=purchase` and enhanced conversions declared. Deduplicating on value
  alone would have hidden half of their optimisation surface.
- **`gcd` is not worth decoding.** The consent-default string
  (`gcd=13t3t3t3t5l1`) is undocumented and Google has changed it. Report it raw
  rather than guessing; `gcs` already answers the question that matters.
