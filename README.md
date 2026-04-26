# AI Daily Digest

A zero-cost, fully automated **editorial-style daily news app** you read every
morning at **08:00 IST**. A scheduled GitHub Action curates 10–12 AI stories,
writes a magazine-style static page to `docs/`, and pushes it back to your
repo. GitHub Pages serves the page; you bookmark the URL and refresh it
daily.

- **65% India-focused, 35% global** — scope tabs let you filter
- Each story: editorial headline + italic "why it matters" pull-quote +
  expandable 2-sentence summary + "Read at source →" link
- Bonus modules: **Today vs Yesterday** stats strip, **Trending** marquee
  ticker, **Companies in the news** leaderboard between stories #5 and #6
- Bookmarks persist in your browser via `localStorage`
- Past editions automatically archived at `docs/YYYY-MM-DD.html`
- Bold serif numerals (Fraunces), warm cream paper, single accent — see the
  design in `news_digest.py:PAGE_CSS`
- No email, no database, no server. One Python file, one HTML output.

---

## How it works

```
RSS  →  fetch  →  dedupe  →  LLM score  →  rank+split  →  LLM rewrite  →  enrich
                                                                          ↓
                                                          docs/index.html  ←  render
                                                          docs/YYYY-MM-DD.html
                                                          docs/state.json (for tomorrow's "vs")
```

Nine sequential stages inside `news_digest.py`:

1. **Fetch** — `feedparser` pulls the top 20 entries from each feed in
   `sources.yaml`, keeps items published within the last 28 hours.
2. **Dedupe** — canonicalises URLs and compares titles with
   `difflib.SequenceMatcher` (>0.82 = duplicate). Duplicate hits bump a
   `cross_reports` counter, used as a trending signal.
3. **Score** — batches of 20 articles scored by the LLM against an impact
   rubric. Each gets `is_ai`, `india_relevance`, `impact_score`,
   `trending_score`, `category`.
4. **LLM call** — Tries a list of free OpenRouter models in order
   (`OPENROUTER_MODELS` constant; defaults to Qwen3-Next 80B → Nemotron 120B
   → Gemma 3 27B). If every OpenRouter model fails or returns empty, falls
   through to Gemini 2.5 Flash via Google's direct API. All providers fail
   → defaults to zeros, pipeline degrades gracefully (yesterday's site
   stays).
5. **Rank & split** — weighted `final_score`, then split into India and
   global pools (8 + 4). Either pool back-fills the other if under-supplied.
6. **Rewrite** — one LLM call rewrites all 12 into editorial copy: crisp
   headline, italic "why it matters" pull-quote, 2-sentence summary, 1–3
   tags. Falls back to original title + canned why-line on failure.
7. **Enrich** — adds display fields the layout needs: `scope` (national /
   international), `kind` (model / policy / funding / …), `time` (HH:MM or
   "Yesterday HH:MM"), `region` (the source publication).
8. **Compute modules** — extracts the trending tags (Title-cased phrases by
   frequency), the companies leaderboard (regex matches against a curated
   list of AI labs), and the stats strip. Yesterday's stats come from
   `docs/state.json`.
9. **Write site** — renders `docs/index.html` (today + past-editions list)
   and `docs/YYYY-MM-DD.html` (immutable archive), saves `docs/state.json`
   for tomorrow's "vs yesterday" line. The Action commits and pushes;
   GitHub Pages serves it.

---

## Setup (one-time, ~10 minutes)

### 1. Get two API keys

- **OpenRouter** — https://openrouter.ai → sign in → API key. The pipeline
  tries a list of free models (`OPENROUTER_MODELS` near the top of
  `news_digest.py`); the default first choice is
  `qwen/qwen3-next-80b-a3b-instruct:free`. To verify any slug or pick
  alternatives, query the live API:
  `curl https://openrouter.ai/api/v1/models | jq '.data[] | select(.pricing.prompt=="0") | .id'`
  — only models that include `response_format` in `supported_parameters`
  will work with our JSON-mode requirement.
- **Google Gemini** — https://aistudio.google.com/app/apikey → API key.
  Free tier handles a daily run easily and acts as the OpenRouter fallback.

### 2. Push to GitHub

```bash
git init
git add .
git commit -m "Initial AI daily digest"
gh repo create ai-digest --public --source=. --push
```

> **Public vs private:** GitHub Pages on free accounts requires a public
> repo. The site has `noindex,nofollow` so search engines won't surface it,
> but anyone with the URL can read it. If you need true privacy, upgrade to
> GitHub Pro and use a private repo with Pages.

### 3. Add secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `OPENROUTER_API_KEY` | from step 1 |
| `GEMINI_API_KEY` | from step 1 |

### 4. Enable GitHub Pages

Repo → **Settings → Pages**:
- **Source:** Deploy from a branch
- **Branch:** `main` / **folder:** `/docs`
- Save.

Wait ~30 seconds. Pages tells you the URL: usually
`https://<username>.github.io/<repo>/`. Open it — you'll see the placeholder
`Setting up.` page. **Bookmark this URL.**

### 5. First build

Repo → **Actions → Daily AI Digest → Run workflow**. Watch the logs. After
~1 minute the workflow:
- Builds today's `docs/index.html` and `docs/YYYY-MM-DD.html`
- Commits as `ai-digest-bot` with message `Digest YYYY-MM-DD`
- Pushes to `main`; Pages re-deploys automatically (~20s)

Refresh your bookmark. Today's digest is there.

The cron now runs unattended every day at **02:30 UTC (08:00 IST)**.

---

## Local testing

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # fill in real keys
export $(grep -v '^#' .env | xargs)  # or load however you prefer
python news_digest.py
open docs/index.html                 # macOS; or just double-click on Windows
```

You'll see:

```
Fetching…
  N fetched
Deduping…
  N unique
Scoring…
  [llm] openrouter
  ...
  N scored (via openrouter)
Picking final…
  12 picked (8 india, 4 global)
Rewriting…
  [llm] openrouter
Writing site…
  wrote docs/index.html (12 stories)
  wrote docs/2026-04-26.html
Done.
```

---

## Tuning

All knobs live near the top of `news_digest.py`:

| Constant | Default | Effect |
|---|---|---|
| `FINAL_COUNT` | `12` | Total stories per page |
| `INDIA_SHARE` | `0.65` | India vs. global split |
| `FRESHNESS_HOURS` | `28` | Drop anything older than this |
| `MAX_PER_FEED` | `20` | Upper cap per source |
| `BATCH_SIZE` | `20` | Articles per LLM scoring call |
| `SIMILARITY_THRESHOLD` | `0.82` | Title-similarity dedupe cutoff |
| `ARCHIVE_KEEP` | `60` | Past editions shown in the footer disclosure |
| `TRENDING_COUNT` | `10` | Items in the marquee ticker |
| `COMPANIES_COUNT` | `8` | Rows in the leaderboard |

**Change the impact rubric** — edit `SCORING_RUBRIC` in `news_digest.py`.
**Change rewrite voice** — edit `REWRITE_PROMPT` (controls headline + "why
it matters" + summary tone).
**Add or remove feeds** — edit `sources.yaml`. Keep `region: india` or
`region: global`; the pipeline uses it as a fallback classifier when the LLM
is unsure about India-relevance.
**Track more companies** — extend `COMPANIES_KNOWN` near the middle of
`news_digest.py`. Each entry is `(display_name, [regex_patterns])`.
**Restyle** — edit `PAGE_CSS` near the bottom of `news_digest.py`. The
design uses [`oklch()`](https://developer.mozilla.org/en-US/docs/Web/CSS/color_value/oklch)
colour and [Fraunces](https://fonts.google.com/specimen/Fraunces) for the
display serif. To swap palettes, change the `--paper`, `--ink*`, `--rule`,
and `--accent` custom properties in `:root`. The handoff includes presets
for cream, off-white, stone, sage, midnight — pick from the comments in
`PAGE_CSS` or roll your own.
**Auto-dark mode** — add `class="auto-dark"` to the `<html>` element in
`render_page` to enable the `prefers-color-scheme: dark` override. Off by
default to keep the editorial cream-paper feel consistent.
**Send time** — edit the cron in `.github/workflows/daily-digest.yml`. It's
UTC: `30 2 * * *` is 08:00 IST.

---

## Common issues

**RSS feed returns 404 / 403.** Sites rotate feed URLs or block default
User-Agents. The pipeline is fail-soft — one broken feed doesn't stop the
others. If a source goes dark for a few days, replace the URL in
`sources.yaml` or drop it.

**OpenRouter 429.** Free-tier limits. Logs show `[warn] openrouter failed:
429 …` then `[llm] gemini` on the next line. Expected, not a bug.

**`Setting up.` page never updates.** Check **Actions → latest run**:
- Did the build step complete? If logs say `No AI stories found`, scoring
  returned zero `is_ai=true` — likely both LLMs failed. Re-run the workflow.
- Did `Commit and push` say `No site changes to commit`? That happens if the
  HTML output is byte-identical to what's already in `docs/` (no
  meaningful change today). Rare in practice.
- Did Pages re-deploy? Settings → Pages shows the latest deployment time.

**Page works but looks broken on phone.** Hard-refresh (pull-down). Mobile
browsers cache aggressively. The CSS includes a `@media (max-width:480px)`
breakpoint.

**Push fails with `permission denied`.** The workflow needs
`permissions: contents: write` (already set in `daily-digest.yml`). If you
forked from another repo with restrictive defaults, also check **Settings →
Actions → General → Workflow permissions → Read and write**.

---

## Scope

**In v1:**
- 15 RSS feeds (8 India, 7 global)
- LLM scoring + rewriting with automatic fallback
- 65/35 India/global split with back-fill
- Static-site output to GitHub Pages with daily archive

**Out of scope (deliberately):**
- Per-user personalisation
- Search across archives
- Dark mode toggle (the `:root` CSS vars make this trivial to add)
- RSS output of the digest itself
- Twitter/X trending signal (paid API)
- Paywall bypass

If the basic digest proves useful, layer these on. The static-site shape
makes additions easy: drop a new file in `docs/`, link to it from
`render_page`.
