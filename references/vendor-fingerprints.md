# What a vendor detection implies

`recon.py` reports which vendors are present. This file is the interpretive
layer: what each category says about how a company goes to market. The stack is
a statement of strategy, because every tool was bought by someone to solve a
problem they had.

Contents: [How to read a stack](#how-to-read-a-stack) ·
[De-anonymisation](#visitor-de-anonymisation--abm) · [MAP/CRM](#map--crm) ·
[Analytics](#product-analytics--cdp) · [Experimentation](#experimentation) ·
[Paid media](#paid-media) · [Speed-to-lead](#chat-and-speed-to-lead) ·
[Consent](#consent) · [Platform](#platform-and-billing) ·
[Combinations](#combinations-that-tell-a-story) · [Cautions](#cautions)

## How to read a stack

Three questions get you most of the way:

1. **Self-serve or sales-led?** Stripe or Paddle with product analytics and no
   MAP says self-serve. Marketo or Eloqua with ZoomInfo and Chili Piper says
   sales-led. Both together says a company running two motions, usually with the
   self-serve one feeding the enterprise one.
2. **How much do they spend on acquisition?** Count distinct ad platforms and
   look for Floodlight (`DC-`), which means programmatic display and a media
   budget large enough to justify DV360.
3. **How mature is their measurement?** Server-side tagging, enhanced
   conversions, consent-aware tags and a CDP are the markers of a team that has
   been through a measurement rebuild. A pile of Custom HTML tags and a legacy
   `UA-` ID is the opposite.

## Visitor de-anonymisation / ABM

The most diagnostic category, because these tools are bought specifically to
feed outbound.

| Vendor | Implies |
|---|---|
| RB2B | **person-level** US visitor identification; aggressive outbound, likely SDR-driven |
| Clearbit / Breeze Intelligence | firmographic enrichment; also used to shorten forms, so expect short forms with hidden enrichment |
| ZoomInfo (`js.zi-scripts.com`) | enterprise data contract; sales-led with real budget |
| 6sense | intent data and account scoring; enterprise ABM with a committee-based cycle |
| Demandbase | same tier as 6sense, often with ad targeting attached |
| Koala | product + web intent scoring; PLG-to-sales handoff motion |
| Warmly, Vector, Factors.ai | newer, cheaper intent layer; mid-market, experimenting |
| Leadfeeder/Dealfront, Albacross | company-level only; a lighter or earlier-stage motion |
| Influ2 | person-based advertising; small, high-value target list |
| Apollo tracker | outbound prospecting, usually earlier-stage |

Person-level tools (RB2B) versus company-level (Leadfeeder) is a real
distinction in both ambition and privacy posture — worth naming in a report.

## MAP / CRM

| Vendor | Implies |
|---|---|
| Marketo | enterprise MQL motion, scoring, long cycle, a marketing ops team |
| Eloqua | same, usually larger and older |
| Pardot / Account Engagement | Salesforce-centric, mid-market |
| HubSpot | integrated motion; if HubSpot CMS too, marketing owns the site end to end |
| Salesforce Web-to-Lead | direct-to-CRM capture, little nurture in between |
| Braze, Customer.io, Iterable | lifecycle messaging at scale; product-led retention focus |
| Klaviyo | ecommerce lifecycle |

A Marketo **sandbox** munchkin ID alongside the production one means a real
staging discipline — a mature ops team. Both IDs are usually in the page.

## Product analytics / CDP

| Vendor | Implies |
|---|---|
| Segment / RudderStack | a CDP; events fan out server-side, so the client view understates what they collect |
| Amplitude, Mixpanel | product analytics; a product team that owns metrics |
| PostHog | engineering-led, often cost-conscious or self-hosted |
| Heap | autocapture — they wanted coverage without instrumentation work |
| Pendo | in-product guides plus analytics; onboarding and adoption focus |
| Hotjar, Clarity, FullStory, Contentsquare, LogRocket | qualitative UX work; Contentsquare and Quantum Metric are the enterprise tier |

A CDP changes how you read everything else: much of the stack may be
server-side and invisible to this method. Say so.

## Experimentation

Presence matters less than **velocity**, and velocity is readable from the
container's lookup tables when they list live test names.

| Vendor | Implies |
|---|---|
| Optimizely, VWO, AB Tasty, Kameleoon, Convert | dedicated CRO function or agency |
| Statsig, Eppo, GrowthBook | modern, warehouse-native, engineering-led |
| LaunchDarkly, Split | feature flags — may be release management rather than CRO |
| Mutiny, Intellimize | landing-page personalisation, almost always paired with ABM |
| Unbounce, Instapage | standalone landing pages — a paid-traffic motion running outside the main site, often outside the main CMS and the main analytics |

## Paid media

Platform mix maps to audience strategy: LinkedIn + Bing is B2B; TikTok +
Pinterest + Reddit is consumer or prosumer; Quora and Capterra/G2 are
high-intent software buyers; Xing or Yahoo Japan means real regional investment
with local teams. Amazon DSP and TVSquared mean brand and offline spend, so a
budget large enough to need incrementality measurement.

Floodlight (`DC-`) is the clearest "large media budget" tell in the file.

## Chat and speed-to-lead

| Vendor | Implies |
|---|---|
| Qualified | Salesforce-native sales chat on high-intent pages; routing to reps in real time |
| Drift | conversational marketing, somewhat older vintage |
| Chili Piper | instant inbound meeting booking — they measure speed-to-lead |
| Calendly | self-serve booking, lighter motion |
| Intercom | often support rather than sales; check which pages it loads on |

Chat on the **pricing page** specifically means they expect sales-assisted
conversion there.

## Consent

OneTrust is the enterprise choice (legal involvement, probably EU revenue).
Cookiebot, Usercentrics and Didomi are mid-market. No CMP on an EU-serving site
is either a risk they are carrying or a market they don't serve.

Consent mode shapes their data quality, so pair the CMP with the container's
consent-aware tag count and the cookies set before any consent interaction. A
CMP present but tags not consent-gated is a common and reportable gap.

## Platform and billing

Next.js, Nuxt, Astro, Gatsby → an engineering-owned marketing site.
Webflow, WordPress, HubSpot CMS → marketing-owned, faster iteration, no deploy
queue. Contentful, Sanity, Storyblok → headless CMS, so a content ops function.

Stripe → self-serve checkout. Paddle → merchant of record, so they sell globally
without entity overhead. Recurly or Chargebee → subscription billing complexity,
usually meaning plan tiers and seat-based pricing.

## Combinations that tell a story

These read more clearly together than apart:

- **RB2B + Apollo + Calendly** — early-stage, SDR-led, doing it by hand.
- **6sense + Marketo + Qualified + Mutiny** — funded enterprise ABM; every stage
  has a dedicated tool and a team behind it.
- **Segment + Amplitude + Statsig + Stripe, no MAP** — pure PLG; the product is
  the funnel.
- **Marketo + Stripe + product analytics** — two motions at once, which usually
  means a self-serve base feeding an enterprise upmarket push. The GTM
  container's event names often confirm this: separate signup and demo paths.
- **Unbounce + a lot of ad platforms + a thin main-site container** — the real
  acquisition machine lives on landing pages this method only partly sees. Note
  the blind spot explicitly.
- **Legacy `UA-` ID still present + 50+ Custom HTML tags** — accumulated debt, no
  one owns the container. A competitive weakness worth naming.

## Cautions

- **Presence is not usage.** A tag can be installed and unused, or left over
  from a churned contract. Corroborate before calling something a core tool.
- **A name in the page is not a vendor.** `recon.py` splits detections into
  those with load evidence (a host, an ID shape, a JS global) and those that
  only matched a bare product name, reported separately as "name mentioned, no
  load evidence". This matters because a target's own integrations directory or
  comparison page mentions dozens of vendors it does not run: on one live
  target, "Clearbit" and "Jotform" came purely from i18n strings in their
  integration catalogue. Never report a weak-evidence hit as part of the stack
  without corroboration — a stack list padded from someone's integrations page
  is worse than a short accurate one.
- **`OPT-` IDs are dead.** Google sunset Optimize in September 2023, so an
  `OPT-` container in a tag manager is container debt, not a testing tool.
- **Absence is not absence.** Server-side tagging, a CDP, or an ad blocker
  reacting to your fetch all hide things. Say what the method cannot see.
- **A CSP-only hit is still live** — see `public-endpoints.md`.
- **Don't name-drop the stack as the finding.** "They use 6sense" is trivia.
  "They bought intent data and personalisation, and their event names show a
  separate enterprise demo path, so the committee-based enterprise deal is where
  they are investing" is the finding.
