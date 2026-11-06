<div align="center">

# growth-recon

**Audit the marketing and measurement stack a website exposes to every visitor.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](#requirements)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)
[![Agent skill](https://img.shields.io/badge/agent%20skill-skills.sh-6f42c1.svg)](https://skills.sh)
[![Stars](https://img.shields.io/github/stars/brenoepics/growth-recon-skill?style=flat&color=yellow)](https://github.com/brenoepics/growth-recon-skill/stargazers)

```bash
npx skills add brenoepics/growth-recon-skill
```

</div>

---

Point it at a company's website. It reads what that site already hands to every
visitor, then writes you a report on how the company actually grows.

**What you get back**

- **Their funnel, in their own words.** The event names their team typed, ordered into the funnel they describe.
- **What they pay to get.** Which conversions they feed to Google Ads, separated from how many ad accounts they run.
- **What a customer is worth to them**, where they leave it readable. One target sets a lead's value by region. Another fires its conversions at a value of zero.
- **The rule that defines a lead.** One company only counts a demo if the form says 20 or more staff. Their LinkedIn tags carry no such rule, so two ad platforms learn from different people.
- **What they are testing right now**, by campaign name.
- **The ads they are running today**, with the creative and the headline.
- **What they charge**, and the exact tier where self-serve stops and a salesperson starts.
- **Who they are hiring**, which names the tools they use and the teams they are building.
- **What changed since last year**, by diffing their setup against its own archived history.

**How it works.** A website cannot hide its tracking setup. The browser needs that file to run the tracking, so the file is public, and every detail above sits in it or in a public record next to it: an ad library, a job board, an archive, a pricing page.

**How you use it.** Ask your agent in plain language. It picks the right tools and writes the report. The eight scripts also run on their own if you prefer a terminal.

> [!NOTE]
> **Read-only, and never signed in.** It asks for the same things a browser asks for: the page, its headers, the scripts it loads, the published tag container, and the standard public files like `robots.txt`. It will not log in, fill a form, start a trial, or go looking for hidden pages.

<br>

## Install

Installs with [`npx skills`](https://github.com/vercel-labs/skills), the open agent-skills CLI.

```bash
npx skills add brenoepics/growth-recon-skill
```

That adds it to the current project. Add `-g` to install it once for every project:

```bash
npx skills add brenoepics/growth-recon-skill -g
```

<details>
<summary><b>Useful flags</b></summary>

<br>

| Flag | Effect |
| :--- | :--- |
| `-g` | Install globally, at user level, instead of per project |
| `-a claude-code` | Target a specific agent; `-a '*'` installs to all detected agents |
| `-l` | List what the repository provides without installing |
| `-y` | Skip confirmation prompts |
| `--copy` | Copy files instead of symlinking into the agent directory |

Other agents are supported too — Cursor and Codex among them. Run `npx skills add --help` for the current list.

</details>

<details>
<summary><b>Have your agent install it</b></summary>

<br>

Paste this into Claude Code, or any agent that can run shell commands:

```text
Install the growth-recon skill by running:
npx skills add brenoepics/growth-recon-skill -g -y

Then read the installed SKILL.md and summarise what it does.
```

</details>

<details>
<summary><b>Install manually with git</b></summary>

<br>

```bash
git clone https://github.com/brenoepics/growth-recon-skill ~/.claude/skills/growth-recon
```

The skill files sit at the repository root, so a plain clone into your skills directory works with no build step.

</details>

> [!TIP]
> In Claude Code, run `/reload-skills` after installing to pick it up without restarting the session.

<br>

## Usage

Once installed, ask in plain language. The skill triggers on its own and scales to the question — a one-line answer for an ID lookup, a full report for a teardown.

```text
Do a full competitive teardown of https://monday.com
```
```text
What is asana.com optimising for in Google Ads?
```
```text
What is linear.app A/B testing right now?
```
```text
What does zendesk.com charge, and where does self-serve end?
```
```text
What has changed in asana.com's tracking since 2022?
```
```text
Compare asana.com, monday.com and clickup.com on how they measure
```
```text
Audit my own site's public exposure: example.com
```

### Running the scripts directly

No agent required. Both scripts are self-contained — run them from the repository, or from wherever the skill was installed.

```bash
# Collect everything a site exposes
python3 -I scripts/recon.py https://example.com --pages /pricing,/demo

# Parse one tag container
python3 -I scripts/gtm_parse.py GTM-XXXXXXX

# Decode what actually fired, from a browser capture
python3 -I scripts/runtime_parse.py captured.json

# Pricing tiers, seat minimums, and the signup form's structure
python3 -I scripts/pricing.py example.com

# Hiring, mobile apps, public filings
python3 -I scripts/company.py example.com

# Diff a container against its own archived history
python3 -I scripts/history.py GTM-XXXXXXX --diff

# Benchmark several companies, or sweep one company's variants
python3 -I scripts/compare.py a.com b.com c.com
python3 -I scripts/compare.py --regional example.com

# Render a finished report as a designed PDF
python3 -I scripts/report_pdf.py acme-recon.md
```

| Argument | Purpose |
| :--- | :--- |
| `--pages a,b` | Also collect these paths; pricing and demo pages often carry tags the homepage does not |
| `--json <file>` | Write raw collected facts for further processing |
| `--no-containers` | Skip container parsing for a faster pass |

The container parser accepts a container ID, a full URL, or a path to a saved file.

<br>

## Live ads

Tag containers show the machinery. Ad libraries show the campaign — the creative, the headline, the call to action and the date it started.

The collector works out who the site is *as an advertiser* and prints ready-to-open links, each labelled with what it costs you.

| Library | Scoped by | Signed-out |
| :--- | :--- | :--- |
| Google Ads Transparency Center | Domain — exact, nothing to disambiguate | Yes |
| Meta Ad Library | Numeric Page ID, read from the site's `fb:pages` tag | Yes |
| Meta EU disclosure | Same, with an EU country — adds reach and targeting | Yes |
| TikTok commercial content | Advertiser name, where the region is covered | Yes |
| LinkedIn sponsored posts | Company slug | No — auth wall |

Google names every verified advertiser account running ads to the domain, which you can reconcile against the account IDs found in the container. More accounts there than in the container usually means agency-run or regional campaigns the main site never loads.

> [!WARNING]
> Meta only scopes correctly by **numeric Page ID**. A keyword search on a brand name also returns other advertisers who merely mention it — on a live run, searching one B2B brand returned a grocery delivery service and a health newsletter as the top two results. The collector builds the exact URL when the site publishes an `fb:pages` tag, and tells you to narrow it by hand when it does not.

Ad libraries do not publish spend, bids, targeting outside the EU disclosure, or performance. An ad that has run for months is a reasonable signal that it works, but that is inference, not a reported number.

<br>

## Beyond the tag container

Five further modules answer what a container cannot. Each runs standalone, and none is needed for a quick lookup.

| Module | Answers | Notable |
| :--- | :--- | :--- |
| **Runtime** | What actually fired, and with what values | The only route to real conversion values — one capture showed `value=110.00 USD`, another showed conversions firing at `value=0`, which cannot drive value-based bidding |
| **Pricing** | What they charge, where self-serve ends | Seat minimums, real annual discounts, tier IDs leaking through CTA links, and the signup form's required fields — read, never filled |
| **Company** | Who they hire, what they ship, what they file | Job boards name the stack in their requirements and expose internal product lines before the marketing site does |
| **History** | What changed, and when | Containers are archived by ID, so a diff dates a strategy change — one target kept 15 conversion labels across 8 years while replacing 8 of them |
| **Compare** | Whether a number is normal | "27 conversion actions" means nothing alone; against four competitors it is the widest optimisation surface in the set |

> [!NOTE]
> The modules corroborate each other, which is the point. A container gating a paid-lead conversion on company size not being "1-19" is a guess until the signup form turns out to carry exactly that firmographic field. Two independent sources agreeing is worth more than either alone.

<br>

## What it reports

| Area | Detail |
| :--- | :--- |
| **Tag container** | Every tag, trigger and variable in the published configuration, with each tag's firing conditions resolved |
| **Paid measurement** | Google Ads accounts separated from distinct conversion *actions*, plus GA4 events, Floodlight, and enhanced-conversion plumbing |
| **Funnel vocabulary** | Event names as the site's own team wrote them, ordered into the funnel they describe |
| **Gating conditions** | Conditions shared across many tags, which tend to encode a working definition of a countable event |
| **Experiments** | Campaign names from public experiment endpoints, plus test names left behind in lookup tables |
| **Vendor stack** | Over 100 fingerprints, split into those with load evidence and those that are only a name in the page |
| **Live ads** | Advertiser identity resolved, with scoped links to each public ad library |
| **Runtime** | Events, values and consent state decoded from a real page load |
| **Pricing** | Tiers, billing periods, seat minimums, and the self-serve/sales boundary |
| **Company** | Open roles by function, app portfolio and cadence, filings |
| **History** | What a container gained and lost between two dates, and publish velocity |
| **Content posture** | Sitemap inventory, disallowed directories, targeted competitors, AI-crawler rules |
| **Consent** | Which tags declare consent requirements, which types, and the CMP in use |

> [!TIP]
> The gating conditions are usually the fastest route to something actionable. A condition applied to seven ad-platform tags is a policy decision, not a configuration detail — and the platforms *missing* from that condition are often more interesting than the ones subject to it.

<br>

## The deliverable

Reports are written as markdown and rendered to a PDF with a cover page, page numbers and embedded images — a single self-contained file, not a folder of loose assets.

```bash
python3 -I scripts/report_pdf.py acme-recon.md
```

The document opens with a cover, then a contents page, then the plain-English summary set in a panel so it reads first. Confidence markers become coloured chips, so a reader can see at a glance which sentences are evidence and which are interpretation.

The renderer also checks the writing, because these reports are read by people who do not work in analytics. It counts em dashes, flags sentences over 34 words, and suggests shorter words. On one real report that came back as 24 em dashes and a 67-word sentence. Those counts are the difference between a report someone reads and one they skim. Rendering uses a headless Chrome, Chromium, Edge or Brave, found automatically; set `CHROME_BIN` to point at a specific one. With no browser available it writes the styled HTML instead and says so, which is still something you can open and print.

<br>

## Reading the output

Reports open with a plain-English section — four to six sentences with no tool names, IDs or acronyms, covering what the company sells, how it gets customers, what it measures most carefully, and what a competitor should do about it. The technical detail follows underneath for anyone who wants it.

After that, every substantive claim is marked so you can tell evidence from interpretation.

| Marker | Meaning |
| :--- | :--- |
| **observed** | Present in the file; the field is cited |
| **inferred** | A reasonable read of observed facts, with those facts named |
| **speculative** | Plausible but unverified, with the test that would settle it |

<br>

## Common misreadings

Container data invites a few confident errors. The parser is built to avoid them.

| Signal | Mistaken for | What it usually is |
| :--- | :--- | :--- |
| A `value` field | A conversion value | A Meta pixel ID. Real per-market values live in lookup tables |
| Count of `AW-` strings | Number of conversions | Number of ad *accounts*. Conversion actions are counted by label |
| Classic conversion tags | The full optimisation surface | An undercount, when conversions route through GA4 or server-side |
| A `__tg` entry | An unknown ad vendor | A trigger group. Several built-in types read like vendors |
| A vendor name in the page | A vendor in the stack | Often copy in the site's own integrations directory |
| A brand's country domain | Their regional site | Frequently a different company — one target's `.com.br` serves an unrelated business |
| A job board matching the domain | Their hiring | A guessed ATS token can resolve to someone else's live board |
| An empty browser network log | No third-party tracking | The reader returns same-origin requests only; use performance entries |
| A value on a product view | Revenue, or what a conversion is worth | A catalogue price |
| An archive gap | A quiet period | A gap in the archive |

<br>

## Limits

> [!IMPORTANT]
> A container describes what *would* happen. It does not prove a tag fired, or that the account behind an ID still spends.

<details>
<summary><b>What this method cannot see</b></summary>

<br>

**Names are not published.** Tag, trigger and variable labels exist only inside the owner's account. Purpose is inferred from type plus firing conditions.

**Paused tags and drafts never ship.** Only the live, published version is served.

**Server-side tagging hides things.** Where collection has moved server-side, that portion is deliberately invisible from the client.

**One page is a sample, not an inventory.** Sites commonly serve several containers split across marketing, application and checkout.

**A permissive CSP tells you nothing.** An empty result there means no information, not a small vendor footprint.

</details>

<details>
<summary><b>On bidding against a competitor</b></summary>

<br>

This is the usual reason people start looking, so it is worth being precise.

The container shows what a company optimises toward and how mature its measurement is, which genuinely helps planning. It does not show their bid. A value passed to Smart Bidding is a target the algorithm bids *toward*, not a ceiling it bids *at*, and a configured value is not a cost, a margin, or a customer's worth.

Bid-level questions are answered by auction insights and public ad libraries, not by a tag container.

</details>

<br>

## Repository layout

| Path | Contents |
| :--- | :--- |
| `SKILL.md` | The workflow an agent follows |
| `scripts/recon.py` | Collects page, headers, CSP, robots, sitemap, DNS, vendors, containers |
| `scripts/gtm_parse.py` | Parses a container: tags, triggers, lookup tables, conversions, consent |
| `scripts/runtime_parse.py` | Decodes captured requests and dataLayer pushes into what fired |
| `scripts/pricing.py` | Pricing tiers, seat minimums, signup form structure |
| `scripts/company.py` | Hiring, mobile apps, public filings |
| `scripts/history.py` | Archived container snapshots and diffs |
| `scripts/compare.py` | Multi-target benchmarking and variant sweeps |
| `scripts/report_pdf.py` | Renders a finished report to a designed PDF |
| `references/` | Field map, endpoint catalogue, vendor meanings, runtime method, report template |

No build step, no lockfile, no dependencies.

<br>

## Requirements

**Python 3.9 or newer**, standard library only. Nothing to install with pip.

Tested on 3.9.6 and 3.14. It very likely runs on 3.8, but that is untested, so the badge claims what has actually been verified.

| Platform | Python present? |
| :--- | :--- |
| macOS | Yes, `/usr/bin/python3` (3.9.6 on current releases) |
| Linux | Yes, on effectively every distribution |
| Windows | **No** — install it from python.org or the Microsoft Store |

Node is needed only for the install command, and a Chromium-family browser only if you want PDF output. `curl` is used as a fallback when Python's certificate store is unconfigured, which is common on macOS.

The scripts speed the work up; they are not the skill. An agent that finds no Python can still follow `SKILL.md` and do the same reading with its own tools, just more slowly.

<br>

## Testing

There is no automated test suite. Every module was built and checked by running
it against live websites and reading the output, which found real bugs but is
not the same thing as tests.

Measured line coverage from 15 representative runs:

| Module | Coverage |
| :--- | ---: |
| `gtm_parse.py` | 76% |
| `runtime_parse.py` | 68% |
| `report_pdf.py` | 64% |
| `pricing.py` | 63% |
| `company.py` | 62% |
| `recon.py` | 48% |
| `compare.py` | 30% |
| `history.py` | 17% |
| **Overall** | **54%** |

The uncovered half is mostly error handling, fallback paths, and vendor decoders
that did not fire on the sites tested. Some of those are known untested: the
Meta, LinkedIn, Snapchat and Reddit decoders in `runtime_parse.py` were written
against documented parameter shapes, because no live hit for them was captured
during development. Treat their output as unverified until you see one.

Contributions that add tests are more useful than contributions that add
features.

<br>

## Contributing

Issues and pull requests are welcome. Useful contributions include new vendor fingerprints, additional public configuration endpoints, and container structures the parser mishandles — a container ID that reproduces the problem is the most helpful thing to include.

<br>

## License

[MIT](LICENSE)
