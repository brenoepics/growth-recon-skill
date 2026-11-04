# growth-recon

An AI agent skill that reads a website's **public** marketing setup and turns it into an analyst report.

Every site hands its entire client-side tracking configuration to anyone who loads a page — it has to, because the browser needs that file to run the tracking. This skill reads it properly: which ad platforms they buy on, which conversions they optimise toward, what their funnel stages are called internally, which A/B tests are live, and how they define a lead worth counting.

No login, no scraping behind auth, no unlisted paths. Just the files any visitor's browser already downloads.

---

## What it finds

| | |
|---|---|
| **The full tag container** | Every tag, trigger and variable in their Google Tag Manager setup |
| **What they optimise for** | Google Ads accounts vs. actual conversion *actions*, GA4 events, Floodlight, enhanced conversions |
| **Their funnel, in their words** | Event names typed by their own marketers — `trial_signup`, `qualified_contact_sales`, `demo_form_submit` |
| **Live A/B tests** | Campaign names from public experiment endpoints, plus test names left in lookup tables |
| **Their lead definition** | The conditions a tag waits for — often a hardcoded firmographic rule |
| **The real stack** | 100+ vendor fingerprints, including the ones only visible in the CSP header |
| **Content strategy** | Sitemap inventory, paid landing-page directories, which rivals they target, AI-crawler stance |

### A real example

Pointed at `monday.com`, it reports:

```
13 tags gated on:  NOT ({{lead_data.company_size}} contains '1-19')
   Google Ads Conversion Tracking x7, Microsoft Advertising UET x2, GA4 Event x1
```

Their Google and Microsoft bidding is trained **only** on leads from companies with 20+ employees. Their LinkedIn tags carry no such rule — so LinkedIn sees a population Google never does. That asymmetry is invisible from tag counts alone, and it's the kind of thing you can act on.

See [`examples/asana-recon.md`](examples/asana-recon.md) for a complete report.

---

## Install

### With npx (one command)

```bash
npx github:brenoepics/growth-recon-skill
```

Installs to `~/.claude/skills/growth-recon`. Add `--project` to install into `./.claude/skills` instead, or `--force` to overwrite an existing copy.

### By asking your agent

Paste this to Claude Code, or any agent that can run shell commands:

```
Install the growth-recon skill from https://github.com/brenoepics/growth-recon-skill
by running: npx github:brenoepics/growth-recon-skill
Then read ~/.claude/skills/growth-recon/SKILL.md and tell me what it does.
```

### Manually

```bash
git clone https://github.com/brenoepics/growth-recon-skill ~/.claude/skills/growth-recon
```

**Requirements:** Python 3.8+ (standard library only — nothing to `pip install`). `curl` is used as a fallback when Python's certificate store isn't set up, which is common on macOS.

---

## How to run it

Once installed, just ask in plain language. The skill triggers on its own:

```
Do a full competitive teardown of https://monday.com
```

```
What's asana.com's tracking setup and what are they optimising for?
```

```
What is linear.app A/B testing right now?
```

```
Audit my own site's public exposure: example.com
```

It scales to the question — a one-line answer for "what's their container ID", a full report for a teardown.

### Running the scripts directly

You don't need an agent. Both scripts work standalone:

```bash
python3 -I scripts/recon.py https://example.com --pages /pricing,/demo
```

```bash
python3 -I scripts/gtm_parse.py GTM-XXXXXXX
```

Add `--json` to either for machine-readable output.

---

## Why not just grep the file yourself

There's a popular version of this trick going around that tells you to search the container for `value` and read the number next to it as what a lead is worth. **That number is almost always a Meta pixel ID.** Report `1641293582765384` as a dollar figure and you'll set a bid off a tracking ID.

Four traps this skill handles, each verified against live containers:

- **`value` fields are not conversion values.** Real per-market lead values live in *lookup tables* — one live container maps 20 regions to numbers, Japan highest, CEE lowest.
- **Counting `AW-` strings counts accounts, not conversions.** One site has 5 ad accounts carrying 27 distinct conversion actions.
- **Classic conversion tags undercount badly.** Modern setups route conversions through GA4 or server-side, so the tag count can be 2 while the real surface is 8.
- **`__tg` is a Trigger Group, not an ad vendor.** Several tag types look like mystery platforms if you don't know them.

---

## What it can't tell you

Being straight about this matters, because these reports get turned into bids.

- **Tag names aren't published.** Purpose is inferred from type plus trigger.
- **Configuration isn't behaviour.** The file shows what *would* fire — not that it fired, or that the ad account still spends.
- **Paused tags and drafts never ship.** Only the live version is served.
- **Server-side tagging hides things.** If they moved collection server-side, part of the picture is deliberately invisible.
- **One page is a sample.** Sites often serve several containers split across marketing, app and checkout.

On the question everyone asks — *can I outbid them?* The container tells you what they optimise toward and how sophisticated their measurement is, which genuinely helps planning. It does not tell you their bid. A value fed to Smart Bidding is what the algorithm bids *toward*, not a ceiling. Bid-level answers live in auction insights and ad libraries.

Every claim in a generated report is marked `[observed]`, `[inferred]` or `[speculative]`, so you can see which sentences will survive contact with reality.

---

## Scope

Public, unauthenticated, as-served artefacts only: HTML, response headers, referenced scripts, the published container, and well-known paths that exist to be fetched (`robots.txt`, `sitemap.xml`, `ads.txt`, `security.txt`).

The skill will decline to sign in, use anyone's credentials, submit forms, create test leads, probe unlisted paths, or brute-force IDs. Reading a competitor's public pixel config is ordinary competitive analysis; interacting with their systems is not, and it pollutes the data you're trying to read.

---

## What's in here

```
SKILL.md              the workflow an agent follows
scripts/recon.py      collects page, headers, CSP, robots, sitemap, DNS, vendors, containers
scripts/gtm_parse.py  parses a container: tags, triggers, lookup tables, conversions
references/           the field map, endpoint catalogue, vendor meanings, report template
examples/             a complete worked report
evals/                test cases (runnable with `claude plugin eval .`)
```

---

## License

MIT — see [LICENSE](LICENSE).
