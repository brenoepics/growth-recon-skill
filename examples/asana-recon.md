# Growth recon: Asana (https://asana.com)

Collected 2026-11-04 · 1 GTM container (+1 child via Zone) · 26 vendors · evidence: public files only

> This is a worked example produced by following SKILL.md, kept in the skill so
> the depth and the confidence-marking conventions are concrete. Everything here
> came from files asana.com serves to any visitor.

## Bottom line

Asana runs a dual-motion machine: a high-volume self-serve trial funnel and a
separate enterprise demo funnel, both instrumented in one unusually large GTM
container (364 tags, 241 variables). Measurement breadth is exceptional —
105 distinct GA4 events — but the Google Ads optimisation surface is narrow by
comparison, with a single Ads account and roughly 8 conversion actions. They
have hardcoded **per-region lead values** into the container, which is the
clearest read on their economics available publicly. The main weaknesses are
container hygiene (58 Custom HTML tags, an unowned-looking accretion) and a
consent posture where OneTrust is deployed but no tag carries a consent-aware
flag in the published container.

## So what — the three things worth acting on

1. **Their paid optimisation is narrower than their measurement.** 105 GA4
   events but ~8 Ads conversion actions `[observed]`. They measure far more than
   they bid on, which means most of that behavioural data is reporting rather
   than optimisation input `[inferred]`. A competitor feeding more mid-funnel
   signals into Smart Bidding could out-optimise them on the same spend. Caveat:
   value-based bidding on one well-chosen action often beats many weak ones, so
   this is an opportunity, not a proven flaw.
2. **Their regional value table is a bidding map.** Japan is configured at
   ~210.07 and the US at ~169.74, while CEE sits at ~33.42 and South America at
   ~50.30 `[observed]`. If these are their lead values, Japan and the Nordics
   (~174.12) are where they will tolerate the highest CPCs, and CEE/South Asia
   where they will not `[inferred]`. Markets at the bottom of that table are the
   cheapest place to compete against them.
3. **Their live experiment names tell you what they are unsure about.** The
   container lists signup-surface tests — `sidebarSignupExperiment`,
   `GoogleOneTapSignup@Visible`, `ClickGoogleSSOSignupOption@LPTest`,
   `ClickEmailSignupCTAOption@LPTest` `[observed]`. Their active uncertainty is
   concentrated on reducing signup friction via SSO, not on pricing or
   positioning `[inferred]`.

## Their funnel, in their own words

Read from GA4 event names in `GTM-5KGDZGG`. Two parallel paths are plainly
visible, which is the central structural finding.

| Stage | Event name | Optimised? | Notes |
|---|---|---|---|
| Arrival | `page_view`, `pv_counter`, `newVisitorFirstHit` | watched | visitor-sequence counters suggest multi-touch logic |
| Engagement | `scroll_50_percent`, `cta_engagement`, `product_ui_click` | watched | `pageview_count_two`, `pageview_count_six` — return-visit thresholds |
| **Self-serve path** | `get_started_click` → `signup_create` → `trial_signup` | partly | `signup_error` is also tracked — they watch failure |
| SSO variants | `signup_google_auth`, `signup_microsoft_auth`, `day_zero_google_sso` | watched | matches the live SSO experiments |
| Quality tier | `high_value_signup`, `smb_hvs`, `enterprise_hvs`, `upmarket_hvs` | **optimised** | segmented "high value signup" — the real conversion |
| **Enterprise path** | `demo_form_submit`, `sales_form_submit`, `team_request_demo_button_click` | **optimised** | separate from self-serve throughout |
| MAP handoff | `enterprise_marketo_form_success`, `upmarket_marketo_form_success` | watched | distinct enterprise vs upmarket Marketo instances |
| Monetisation | `add_to_cart`, `purchase`, `view_item`, `use_template` | **optimised** | full ecommerce schema on a B2B SaaS site |
| Upgrade intent | `upgrade_pricing_page_clicks`, `upgrade_enterprise_page_clicks`, `upgrade_starter_page_clicks` | watched | per-tier upgrade intent, separately tracked |

**The gap between measured and optimised is the story.** They have built a
segmented quality model — `smb_hvs` / `enterprise_hvs` / `upmarket_hvs` are
distinct "high value signup" events — which means they already know a signup's
value varies by segment `[observed]`. But the Ads conversion surface is small
enough that most of that segmentation likely informs reporting and sales
routing, not bidding `[inferred, moderate confidence]`. Also note `purchase` and
`add_to_cart` on a B2B site: the self-serve motion is treated as genuine
ecommerce.

## What they optimise toward

- **Ads accounts**: `AW-858512220` `[observed]` — one account. Also
  `AW-11305044850` appears as the conversion ID on `__awct` tags, so two Ads
  identities are in play `[observed]`.
- **Conversion actions**: 2 in `__awct` tags, but **8 label-shaped strings** in
  the container `[observed]`. The divergence matters: GA4 `marketing_event` tags
  carry `google_ads_conversion_id` and `google_ads_conversion_label` as event
  *parameters*, so conversions are routed through GA4 rather than classic
  conversion tags `[observed]`. Counting `__awct` tags alone would have
  understated their optimisation surface fourfold.
- **Floodlight / DV360**: 2 Floodlight Counter tags present `[observed]` →
  programmatic display spend, so a media budget large enough to justify DV360
  `[inferred]`.
- **Enhanced conversions**: GA4 events carry `user_data.email_address`,
  `user_data.external_id`, `user_data.click_id` and `first_party_collection`
  `[observed]` → enhanced conversions and first-party matching are live, a
  marker of a team that has done a post-cookie measurement rebuild `[inferred]`.
- **GA4 property**: `G-J1KDXMCQTH` `[observed]`.
- **Child container**: a Zone loads `GTM-52CW97X`, nickname "Tracking Container"
  `[observed]` — a second container not referenced in the homepage HTML.

## What gates their measurement

Conditions shared across many destination tags, from trigger resolution
`[observed]`:

| Tags gated | Condition | Reading |
|---|---|---|
| 72 | `NOT {{OnetrustActiveGroups}} contains 'FIRST_LOAD'` | consent enforcement, covering most of the stack |
| 24 | `{{Constant}} contains 'org'` | account-shape split (org vs workspace) applied across ad platforms |
| 21 | `{{action}} contains 'Email Captured'` | email capture is a countable event for 6 platforms |
| 21 | `{{category}} contains 'Qualified Chat'` | chat qualification fires ads tags — chat is a conversion surface, not support |
| 17 | `{{page_count}} equals '2'` | second-visit threshold feeding Quora, UET, Pinterest and 5 GA4 events |

Two inferences worth drawing. **Qualified Chat firing 21 tags across Microsoft,
Pinterest and GA4 means their chat is a demand-capture channel measured like a
form**, not a support deflection tool `[inferred]`. And the `page_count == 2`
gate shows they treat a return visit as a qualifying signal — they are feeding
return intent into ad platforms rather than optimising on first-touch alone
`[inferred]`.

## Economics evidence

A 20-row lookup table (`macro[99]`, keyed on a Custom JavaScript variable)
mapping region to a decimal number `[observed]`:

| Region | Value | Region | Value |
|---|---|---|---|
| Japan | 210.07 | MENA | 114.63 |
| Nordics | 174.12 | Italy | 87.66 |
| United States | 169.74 | South Africa | 82.68 |
| ANZ | 159.54 | North East Asia | 81.96 |
| DACH | 144.02 | South East Asia | 75.08 |
| Canada | 132.29 | Central America | 69.56 |
| United Kingdom | 131.40 | Brazil | 69.20 |
| Benelux | 127.30 | Spain & Portugal | 64.88 |
| France | 111.07 | South Asia | 50.57 |
| | | South America | 50.30 |

**What this is**: a per-market value passed with conversions, almost certainly
for value-based bidding `[inferred, high confidence]` — the shape (regional
granularity, 8-decimal precision suggesting a currency-converted figure) fits a
modelled lead or trial value.

**What this is not**: their CAC, their actual bids, their margin, or revenue per
customer. A value fed to Smart Bidding is an input the algorithm bids *toward*,
not a ceiling it bids *at*. Treating 169.74 as "what a US lead costs them" would
be wrong.

No hardcoded `vtp_conversionValue` exists on any tag — those are runtime
dataLayer references `[observed]`. The 15–16 digit numbers in `vtp_value` fields
elsewhere in the container are Meta pixel IDs, **not** values.

## Experiments and personalisation

Platform: **Optimizely**, project IDs `4851607934861312` and `5324015129067520`
`[observed]`. The `cdn.optimizely.com/js/<id>.js` snippets load but readable
experiment names do not survive minification in these projects, so the platform
is confirmed while its test inventory is not exposed there `[observed]`.

The container's lookup tables expose the live tests instead — 36 names across
two tables `[observed]`:

- **Signup friction / SSO**: `sidebarSignupExperiment`,
  `GoogleOneTapSignup@Visible`, `GoogleOTS@ClickCTA`, `GoogleOTS@ClickDropdown`,
  `ClickGoogleSSOSignupOption@LPTest`, `ClickEmailSignupCTAOption@LPTest`,
  `desktopGoogleSSOButtonCopyEnrollmentEvent`
- **Homepage composition**: `homepageCustomerLogosEnrollment`,
  `homepageDifferentiatorVideoEnrollmentEvent`, `homepageBasicFooterEnrollmentEvent`
- **Pricing surface**: `reorderSkusEnrollmentEvent`,
  `highlightNonProfitPricingEnrollmentEvent`, `Nonprofit@GetStartedClicks`
- **Banners / CTAs**: `TopbarAnnouncement`, `WorkflowBuilderBanner`,
  `DemoBannerViewDemo`, `ScrollSocialProofLP`, `SidebarNavCta`, `FreeTrialUgt`

Read together: a meaningful CRO programme concentrated on the signup surface,
with secondary work on homepage proof and SKU ordering `[inferred]`.

Plan identifiers also leak via `macro[44]`: `workspace-true-tier-1-premium`,
`new_org-true-tier-1plus-premium`, `org-true-tier-1-premium` → `trial`
`[observed]`. So their internal SKU taxonomy distinguishes workspace / new_org /
org account shapes across tier-1 and tier-1plus premium `[inferred]`.

## Stack and what it implies

26 vendors. **20 of these are visible only in the CSP allowlist**, not the served
HTML — they are injected by GTM at runtime `[observed]`. Reading the HTML alone
would have found 6.

| Purpose | Vendors | What it implies |
|---|---|---|
| MAP | Marketo (`784-XZD-582` prod, `733-SJI-039` sandbox) | enterprise MQL motion; a sandbox ID means real staging discipline and a marketing ops team |
| Enrichment | ZoomInfo (`js.zi-scripts.com`) | enterprise data contract; form shortening and routing |
| Experimentation | Optimizely | dedicated CRO function |
| Consent | OneTrust | legal involvement, EU revenue |
| Paid social/search | Meta, LinkedIn, TikTok, Pinterest, Quora, Microsoft Ads, Amazon DSP | broad mix; Quora + Capterra/G2 = high-intent software buyers |
| Regional paid | Xing, Yahoo Japan | real DACH and Japan investment with local teams — consistent with Japan topping the value table |
| Brand/offline | TVSquared | TV attribution; brand spend large enough to need incrementality measurement |
| Review sites | G2, Capterra | paid category listings |
| Experience analytics | Contentsquare | enterprise tier, not a Hotjar-grade deployment |
| Billing | Recurly | subscription complexity: tiers and seats |
| Video | Wistia | gated video with lead capture |
| CMS / platform | Contentful + Next.js, hosted on Netlify | engineering-owned marketing site with content ops |
| Monitoring | Datadog RUM | performance is owned by someone |
| Inbound AI | Spara | an AI SDR / inbound agent — a recent bet |
| ATS | Greenhouse | job board is public; forward-looking headcount signal |
| Forms | Jotform | form tooling outside the main stack — likely a team working around marketing ops |

## Measurement maturity

**Strong**: enhanced conversions with first-party user data, 105-event
instrumentation with a consistent `gtm_tag_name` convention on nearly every
event (a deliberate governance choice), separate enterprise/upmarket Marketo
flows, a Zone-based child container, and `experimentation_event` tags carrying
`bucketing_id`, `variant_id`, `variant_label` and `Holdback` — they measure
experiment exposure properly, holdbacks included `[observed]`.

**Weak**: 58 Custom HTML tags `[observed]` — the usual sign of a container
nobody fully owns, where vendors get pasted in rather than templated.

**Consent is enforced two ways over.** 36 of 364 tags declare consent
requirements directly (9 of them requiring `ad_storage`) `[observed]`. On top of
that, trigger resolution shows **72 tags** gated on
`NOT ({{OnetrustActiveGroups}} contains 'FIRST_LOAD')` `[observed]` — spanning
29 Custom HTML tags, 11 GA4 events, 3 Microsoft UET, 3 Pinterest, 2 Quora and an
Ads conversion. They gate on OneTrust's own group state via trigger conditions
instead of consent mode, which is a legitimate and fully-enforced pattern
`[inferred, high confidence]`. The homepage still sets a `user_geo` cookie
before any consent interaction `[observed]`, which is the one genuine exposure
here — and it is a geo cookie, not an analytics or ad identifier.

This is worth dwelling on as a method point, because an earlier draft of this
very report got it wrong. It read "0 consent-aware tags" and floated a possible
compliance gap. Two separate defects produced that: the parser was looking for
consent declarations under the wrong key, and it had no way to see trigger-level
gating. Corrected, the same container reads "36 tags declare consent
requirements and 72 are gated on the CMP's group state" — properly enforced,
opposite conclusion. Check both the declarations and the gating groups before
calling consent a gap.

No server-side tagging: no `transport_url` or first-party collection domain in
the container `[observed]`. So this read is more complete than it would be for a
competitor who had moved collection server-side.

## Content and SEO posture

5,012 URLs in the sitemap index `[observed]`. Path distribution:

- **Localisation is the dominant bet**: `/zh-tw` (1,200 URLs) and `/sv` (1,009)
  together are 44% of the sitemap, and 14 hreflang locales are declared. Traditional
  Chinese and Swedish at that page count is a deliberate, heavy investment
  `[observed → inferred]`.
- `/resources` (771) and `/templates` (262) — programmatic SEO at real scale.
- `/apps` (615) — integrations surface, an SEO and ecosystem play.
- `/case-study` (182) — substantial proof library.
- `/compare` (10) — competitive SEO targeting is **small**, which is notable: a
  market leader does not need to rank for rivals' names `[inferred]`.
- `/inside-asana` (582) — employer brand and culture content at unusual volume.

robots.txt disallows `/ebook/`, `/resources/thank-you`, `/apply/`,
`/asanapedia/`, `/internal/`, and `/nike/` `[observed]`. A customer name as its
own directory suggests co-marketing or an enterprise microsite `[inferred]`. The
thank-you disallow confirms a gated-content funnel whose conversion pages are
kept out of search `[inferred]`.

They publish an `llms.txt` (5.7 KB) and a `security.txt` pointing to a Bugcrowd
programme `[observed]`. No AI-crawler blocks in robots.txt — they are not
blocking GPTBot, ClaudeBot or PerplexityBot `[observed]`, which given the
`llms.txt` looks like a deliberate choice to be AI-discoverable `[inferred]`.

## Gaps and limits of this read

- Tag, trigger and variable **names are not published**; every purpose above is
  inferred from tag type plus trigger conditions.
- **Paused tags and draft workspaces never ship** — the live container only.
- **Configuration is not behaviour.** Nothing here proves a tag fired, that
  `AW-858512220` still spends, or that the Floodlight tags are funded.
- **2 pages read** (homepage, /pricing). The child container `GTM-52CW97X` was
  identified but not read, and app.asana.com, the docs and the academy may load
  different containers entirely.
- The **Optimizely test inventory** is not exposed; the experiment names above
  come from GTM lookup tables and may lag what is currently running.
- The regional value table's **meaning is inferred**. It is 20 regions mapped to
  decimals; that it is a lead value fed to Smart Bidding is the best reading, not
  a stated fact.
- Consent handling may happen in OneTrust rather than GTM consent mode; the
  container alone cannot distinguish "not gated" from "gated elsewhere".

## Appendix: raw IDs and evidence

| Type | Value | Source |
|---|---|---|
| GTM container | `GTM-5KGDZGG` (v848, 364 tags, 862 KB) | homepage HTML |
| Child container | `GTM-52CW97X` "Tracking Container" | Zone tag in container |
| GA4 | `G-J1KDXMCQTH` | container |
| Google Ads | `AW-858512220`, conversion ID `11305044850` | container |
| Ads labels (7 attached + 1 unattributed) | `2rsECMaohNcYEPKW1Y4q`, `7rF1CO3jkNcYEPKW1Y4q`, `0MpvCL7NzfYCELidnasC`, `2Zi4CKKW5NIBELidnasC`, `8ODeCLuIqogBENy2r5kD`, `VsFUCOjmwpEBENy2r5kD`, `uQBSCKXqzpEBENy2r5kD`, `QQHFbQ6sp8WxMQ3BXmFMNE` (unattributed — not tied to any Ads tag; verify before counting) | container |
| Marketo | `784-XZD-582` (prod), `733-SJI-039` (sandbox) | homepage HTML |
| Optimizely | `4851607934861312`, `5324015129067520` | homepage HTML |
| Hosting | Netlify, Next.js, AWS DNS, Google Workspace MX | headers + DNS |
| CSP | 86 allowed domains | response header |

Reproduce with:

```bash
python3 -I scripts/recon.py https://asana.com --json recon.json --pages /pricing
python3 -I scripts/gtm_parse.py GTM-5KGDZGG --json
```
