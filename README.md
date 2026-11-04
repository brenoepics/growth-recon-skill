<div align="center">

# growth-recon

**Audit the marketing and measurement stack a website exposes to every visitor.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](#requirements)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#requirements)
[![Agent skill](https://img.shields.io/badge/agent%20skill-skills.sh-6f42c1.svg)](https://skills.sh)
[![Stars](https://img.shields.io/github/stars/brenoepics/growth-recon-skill?style=flat&color=yellow)](https://github.com/brenoepics/growth-recon-skill/stargazers)

```bash
npx skills add brenoepics/growth-recon-skill
```

</div>

---

A website's tracking configuration is delivered to the browser as a static file, which means it can be read directly. `growth-recon` parses that file and the other public surfaces around it, then reports what a site measures, what it bids toward, which vendors it actually loads, and the conditions a tag waits for before it fires.

It works as a skill your agent invokes in conversation, or as two standalone Python scripts.

> [!NOTE]
> **Read-only and unauthenticated.** It requests the same things a browser does: the served HTML, response headers, referenced scripts, the published tag container, and standard well-known paths. It will not sign in, submit forms, create test leads, or probe unlisted paths.

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
Audit my own site's public exposure: example.com
```

### Running the scripts directly

No agent required. Both scripts are self-contained — run them from the repository, or from wherever the skill was installed.

```bash
# Collect everything a site exposes
python3 -I scripts/recon.py https://example.com --pages /pricing,/demo

# Parse one tag container
python3 -I scripts/gtm_parse.py GTM-XXXXXXX
```

| Argument | Purpose |
| :--- | :--- |
| `--pages a,b` | Also collect these paths; pricing and demo pages often carry tags the homepage does not |
| `--json <file>` | Write raw collected facts for further processing |
| `--no-containers` | Skip container parsing for a faster pass |

The container parser accepts a container ID, a full URL, or a path to a saved file.

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
| **Content posture** | Sitemap inventory, disallowed directories, targeted competitors, AI-crawler rules |
| **Consent** | Which tags declare consent requirements, which types, and the CMP in use |

> [!TIP]
> The gating conditions are usually the fastest route to something actionable. A condition applied to seven ad-platform tags is a policy decision, not a configuration detail — and the platforms *missing* from that condition are often more interesting than the ones subject to it.

<br>

## Reading the output

Generated reports mark every substantive claim so you can tell evidence from interpretation.

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
| `references/` | Field map, endpoint catalogue, vendor meanings, report template |

Ten files, no build step, no lockfile.

<br>

## Requirements

Python 3.8 or newer, standard library only — there is nothing to install with pip. Node is needed only for the install command. `curl` is used as a fallback when Python's certificate store is unconfigured, which is common on macOS.

<br>

## Contributing

Issues and pull requests are welcome. Useful contributions include new vendor fingerprints, additional public configuration endpoints, and container structures the parser mishandles — a container ID that reproduces the problem is the most helpful thing to include.

<br>

## License

[MIT](LICENSE)
