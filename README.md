<div align="center">

# 🔍 growth-recon

### Find out how any company really grows.

**Point it at a website. Get a report on how that company gets customers,
what they pay for them, and what they are testing right now.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Agent skill](https://img.shields.io/badge/agent%20skill-skills.sh-6f42c1.svg)](https://skills.sh)
[![No account needed](https://img.shields.io/badge/no%20account-needed-brightgreen.svg)](#is-this-allowed)
[![Stars](https://img.shields.io/github/stars/brenoepics/growth-recon-skill?style=flat&color=yellow)](https://github.com/brenoepics/growth-recon-skill/stargazers)

```bash
npx skills add brenoepics/growth-recon-skill
```

</div>

---

## Every website shows you how it makes money

A website cannot hide its tracking setup. Your browser has to download that file
to make the tracking work, so the file is public. Anyone can open it.

Inside is the shape of the business. What they count as a sale. What they pay to
get one. What they are testing this week. Sometimes what a customer is worth to
them, written down.

Almost nobody looks. This looks, and writes you a report.

<br>

## What you find out

### 💰 What a customer is worth to them

One company we tested writes down a price for a lead in every region. Japan is
their most valuable at about 210. Central and Eastern Europe is their cheapest
at about 33. Their number, in their own file.

Another fires its sales tracking with a value of zero. So their ad bidding
cannot be chasing revenue, because no revenue is being reported.

### 🎯 The exact rule that decides what counts as a lead

One company only counts a demo request if the form says 20 staff or more.
Smaller companies are never reported to Google. Their LinkedIn tracking has no
such rule.

Two ad platforms, being taught about two different kinds of customer. That is
the sort of gap a competitor can use.

### 🧪 What they are testing right now

Live A/B tests, by name. One company was running 14 at once. All of them pushed
people from a cheap plan toward the enterprise plan, across 14 different pages.

### 📣 The ads they are running today

Their live campaigns, with the picture and the headline, from the public ad
libraries. What they are actually saying to customers this week.

### 💳 What they charge, and where the salesperson appears

Every price tier, the real yearly discount, and the exact point where "buy now"
becomes "contact sales". One company advertises 20% off for paying yearly. The
real discounts are 20%, 23% and 24%, depending on the plan.

### 🗂️ Their funnel, in their own words

The names their own team gave each step, in order. Not your guess at their
funnel. Their labels for it.

### 👥 Who they are hiring

Open roles name the tools they use and the teams they are building. Job ads
often show a new product line months before the website mentions it.

### 📆 What changed since last year

Their setup is archived going back years. A comparison shows what they started
measuring, what they gave up on, and roughly when they changed their mind.

<br>

## Try it in one line

Once installed, just ask. There are no commands to learn.

```text
Do a full competitive teardown of monday.com
```
```text
What is asana.com paying for in Google Ads?
```
```text
What is linear.app A/B testing right now?
```
```text
What does zendesk.com charge, and where does self-serve end?
```
```text
Compare asana.com, monday.com and clickup.com
```

You get back a designed PDF, not a wall of code. It opens with a plain summary
anyone can read, with the detail underneath for whoever wants it.

<br>

## Install

**Step one.** Run this:

```bash
npx skills add brenoepics/growth-recon-skill
```

**Step two.** Ask your AI assistant one of the questions above.

That is the whole setup.

<details>
<summary><b>Other ways to install</b></summary>

<br>

Add `-g` to make it available in every project:

```bash
npx skills add brenoepics/growth-recon-skill -g
```

Or let your assistant do it. Paste this in:

```text
Install the growth-recon skill by running:
npx skills add brenoepics/growth-recon-skill -g -y

Then read the installed SKILL.md and tell me what it does.
```

Works with Claude Code, Cursor, Codex and other coding assistants. In Claude
Code, run `/reload-skills` afterwards so it shows up straight away.

</details>

<details>
<summary><b>What you need on your computer</b></summary>

<br>

| Thing | What for | Do you have it? |
| :--- | :--- | :--- |
| Node | The one install command | Usually yes |
| Python 3.9+ | Running the reading tools | Yes on Mac and Linux. Windows users get it from python.org |
| Chrome or Edge | Only for making the PDF | Almost certainly yes |

Nothing else. No account, no API key, no subscription.

</details>

<br>

## Is this allowed?

Yes. It reads the same files your browser downloads when you visit a page.
Nothing hidden, nothing hacked, no passwords.

It is like reading a shop's prices in the window. They are on display. Most
people just walk past.

> [!IMPORTANT]
> It will never log in, fill in a form, start a free trial, or send fake sales
> into anyone's reports. That would create records inside another company's
> systems and spoil their numbers. It only reads what is already public.

<br>

## What it cannot tell you

Worth being straight about, because people make decisions from these reports.

| It will not tell you | Why |
| :--- | :--- |
| How much they spend on ads | Nobody publishes that |
| What they actually bid | Also private. Settings show intent, not spending |
| Whether their tracking really works | It shows what is set up, not what happened |
| Their profit, or what a customer truly costs them | A number in a settings file is a target, not a result |

Every claim in the report carries a label, so you can see what is solid:

| Label | Meaning |
| :--- | :--- |
| 🟢 **Observed** | It is written in their files |
| 🟡 **Inferred** | A fair reading of those files |
| ⚪ **Speculative** | A guess worth checking before you act on it |

Most people try this hoping to outbid a competitor. The honest answer: it shows
what they chase and how well they measure it, which genuinely helps you plan. It
does not show their bid. For that you need Google's own auction reports.

<br>

## For the curious

<details>
<summary><b>What is actually inside</b></summary>

<br>

Eight small programs in plain Python, with nothing to install:

| File | What it reads |
| :--- | :--- |
| `recon.py` | The site itself: page, headers, vendors, sitemap, ad libraries |
| `gtm_parse.py` | The tracking setup, tag by tag, and what each one waits for |
| `runtime_parse.py` | What a page actually sends when someone visits |
| `pricing.py` | Price tiers and the shape of the signup form |
| `company.py` | Hiring, mobile apps, public filings |
| `history.py` | How their setup looked in past years |
| `compare.py` | Several companies side by side |
| `report_pdf.py` | Turns the finished report into a PDF |

`SKILL.md` tells your assistant how to use them. `references/` holds the
detailed notes it reads when it needs them.

</details>

<details>
<summary><b>Running it yourself, without an assistant</b></summary>

<br>

```bash
# Everything a site exposes
python3 -I scripts/recon.py https://example.com --pages /pricing,/demo

# One tracking container, in full
python3 -I scripts/gtm_parse.py GTM-XXXXXXX

# Prices and the signup form
python3 -I scripts/pricing.py example.com

# Hiring, apps, filings
python3 -I scripts/company.py example.com

# What changed over the years
python3 -I scripts/history.py GTM-XXXXXXX --diff

# Several companies at once
python3 -I scripts/compare.py a.com b.com c.com

# Make the PDF
python3 -I scripts/report_pdf.py report.md
```

Add `--help` to any of them.

</details>

<details>
<summary><b>Mistakes it avoids</b></summary>

<br>

Reading these files by hand is easy to get wrong. Some traps it handles:

| Looks like | Actually is |
| :--- | :--- |
| A number labelled "value" | Usually a Facebook tracking ID, not money |
| Counting ad account IDs | That counts accounts, not the things they track |
| A brand's `.com.br` address | Often a completely different company |
| A quiet year in the archives | Usually a gap in the archive, not a quiet year |

</details>

<br>

## Contributing

Issues and pull requests are welcome. The most useful ones add a vendor it does
not yet recognise, a public source it does not yet read, or a website where it
gets something wrong. If you report a mistake, include the address so it can be
checked.

<br>

## License

[MIT](LICENSE). Free to use, change and share.

<div align="center">
<br>
<sub>Built for people who would rather look than guess.</sub>
</div>
