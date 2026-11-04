# Report template

Use this structure. Adapt the depth to the request — a "what's their pixel ID"
question gets a few lines, not this whole thing.

Two rules that do most of the work:

**Lead with the read, not the inventory.** The reader can scroll to the ID
tables. What they can't produce themselves is the judgment. If someone reads
only the first twenty lines they should still get the three things worth knowing.

**Mark every substantive claim.** Use `[observed]`, `[inferred]` or
`[speculative]` inline, or a per-section convention. This is not pedantry: these
reports get turned into bids and roadmap decisions, and a reader has to know
which sentences will survive contact with reality. Where a claim is inferred,
name the facts it rests on. Where it's speculative, say what would confirm it.

---

```markdown
# Growth recon: <company> (<url>)
Collected <date> · <n> GTM containers · <n> vendors · evidence: public files only

## Bottom line
Three to five sentences. What kind of go-to-market machine is this, how mature
is their measurement, and where are they most exposed. No ID lists here.

## So what — the three things worth acting on
1. **<Action>** — why, from which evidence, and the caveat that limits it.
2. ...
3. ...
Keep these specific enough to argue with. "Improve tracking" is not an action.

## Their funnel, in their own words
The GA4 event names and conversion actions, ordered into the funnel their team
built. Mark which stages carry a Google Ads conversion action (**optimised**)
versus measured only (**watched**).

| Stage | Event name | Optimised? | Notes |
|---|---|---|---|

Then: what the gap between measured and optimised says about them. This is
usually the most valuable paragraph in the report.

## What they optimise toward
- Ads accounts (`AW-`): <ids> — accounts, not conversion counts
- Distinct conversion actions: <n> in `__awct` tags, <n> label-shaped strings
  in the container — explain the difference if the numbers diverge, since a
  divergence means GA4-routed or server-side conversions
- Floodlight / DV360: present or not, and what that implies about media budget
- Enhanced conversions / user-data parameters: present or not

## Economics evidence
Only what the files actually support.
- Hardcoded values found: the lookup table rows, verbatim, with their keys
- What each number most likely is: a configured conversion value or bid target,
  which is not their CAC, their bid, or their margin
- If no literals: say that values are passed at runtime, that the container
  shows the schema rather than the numbers, and what that still tells you

## Experiments and personalisation
Platform, and live test names where the container or endpoints expose them.
Note testing velocity if the evidence supports a read on it. If names aren't
exposed, say "platform present, names not exposed" rather than implying
nothing is running.

## Stack and what it implies
Group by purpose, not vendor name. For each group, one line on what buying that
tool says about their motion. Mark CSP-only detections — still live, injected
at runtime.

| Purpose | Vendors | What it implies |
|---|---|---|

## Measurement maturity
Server-side tagging, consent posture, container hygiene (dead tags, legacy
`UA-` IDs, Custom HTML sprawl), dataLayer schema quality. Where they are strong
and where they are carrying debt.

## Content and SEO posture
Sitemap path inventory: programmatic SEO scale, locale investment weighted by
page count, and `/compare` or `/alternative` pages — name which competitors they
target, since that is a direct statement of who they think they displace.
robots.txt disallow directories worth noting (paid landing pages, gated funnel,
test environments) and their AI-crawler stance.

## Gaps and limits of this read
Be concrete and specific to this target:
- Tag and trigger names are not published; purpose is inferred from type + trigger
- Paused tags, drafts and server-container config never ship
- Configuration ≠ behaviour: nothing here proves a tag fired or an account spends
- Only <n> pages read; app, docs and checkout may load different containers
- Anything that failed to collect, and why
- Where server-side tagging or a CDP hides part of the picture

## Appendix: raw IDs and evidence
Container IDs, measurement IDs, conversion labels, pixel IDs, full lookup table
rows, CSP allowlist, third-party hosts. Cite the file each came from so a reader
can re-run it.
```

---

## Notes on specific sections

**Bottom line** is the hardest part and worth the effort. Write it last. If you
cannot say what kind of machine this is in three sentences, you have collected
without reading.

**The funnel table** is what people actually come for, and it is the section
most likely to be wrong in an interesting way: event names drift from reality,
and dead events linger. If two events look like duplicates from different eras,
say so — it is evidence about them.

**Economics evidence** is where overclaiming is most tempting and most costly.
Someone may set a bid from this section. A region-to-value lookup table is real
and strong; a 15-digit number in `vtp_value` is a Meta pixel ID. See the traps
in `gtm-container.md`.

**Gaps and limits** should be specific to this target, not boilerplate. A reader
who knows exactly what you could not see trusts the rest of it more — and a
generic limitations list reads as a disclaimer rather than analysis.
