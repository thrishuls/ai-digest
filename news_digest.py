#!/usr/bin/env python3
"""AI Daily Digest — editorial-magazine static site.

Pulls RSS feeds, dedupes, scores, picks the top 12 stories (65% India / 35%
global), rewrites them with editorial copy, computes auxiliary modules
(trending, companies, stats), and writes a static HTML page to docs/ that
mirrors the high-fidelity design handoff.

Designed to run once per day on GitHub Actions. The only persistent state is
docs/state.json (today's stats, used tomorrow for the "vs yesterday" line)
and the dated HTML archives — both committed back to the repo.
"""

from __future__ import annotations

import html as html_mod
import json
import os
import pathlib
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

import feedparser
import requests
import yaml

# ---------- Tunables ----------
MAX_PER_FEED = 20
FRESHNESS_HOURS = 28
BATCH_SIZE = 20
FINAL_COUNT = 12
INDIA_SHARE = 0.65
SIMILARITY_THRESHOLD = 0.82
LLM_TIMEOUT = 45
ARCHIVE_KEEP = 60
TRENDING_COUNT = 10
COMPANIES_COUNT = 8

# ---------- Endpoints / model IDs ----------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Tried in order. First one that returns content wins. All must be free models
# that support response_format=json_object. Verify against the OpenRouter API
# at https://openrouter.ai/api/v1/models if any 404s show up in logs.
OPENROUTER_MODELS = [
    # Nemotron first — responds reliably on free tier. Qwen and Gemma free
    # tiers are heavily rate-limited (429) during peak hours, so they sit
    # behind as backups instead of burning the first call on a rate limit.
    "nvidia/nemotron-3-super-120b-a12b:free",    # Nemotron 120B MoE, 256K ctx
    "google/gemma-3-27b-it:free",                # Gemma 3 27B, 128K ctx
    "qwen/qwen3-next-80b-a3b-instruct:free",     # Qwen3-Next 80B MoE, 256K ctx
]

# ---------- Env ----------
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_REFERER = os.getenv(
    "OPENROUTER_REFERER", "https://github.com/ai-digest/ai-digest"
)
SITE_TITLE = os.getenv("SITE_TITLE", "AI Daily").strip() or "AI Daily"

IST = timezone(timedelta(hours=5, minutes=30))
DOCS_DIR = pathlib.Path("docs")
STATE_FILE = DOCS_DIR / "state.json"

# Tracks which provider served the most recent LLM call.
_last_llm_provider: str = ""


# =========================================================================
# Stage 1 — Fetch
# =========================================================================
def fetch_articles() -> list[dict]:
    with open("sources.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESHNESS_HOURS)
    articles: list[dict] = []

    for feed in cfg["feeds"]:
        name = feed["name"]
        url = feed["url"]
        region = feed["region"]
        try:
            parsed = feedparser.parse(url)
            kept_before = len(articles)
            for e in parsed.entries[:MAX_PER_FEED]:
                pub = _entry_time(e)
                if pub is None or pub < cutoff:
                    continue
                title = (getattr(e, "title", "") or "").strip()
                link = (getattr(e, "link", "") or "").strip()
                if not title or not link:
                    continue
                summary_raw = (
                    getattr(e, "summary", None)
                    or getattr(e, "description", None)
                    or _content_value(e)
                    or ""
                )
                summary = re.sub(r"<[^>]+>", " ", summary_raw)
                summary = re.sub(r"\s+", " ", summary).strip()[:600]
                articles.append(
                    {
                        "title": title,
                        "url": link,
                        "summary": summary,
                        "source": name,
                        "source_region": region,
                        "published": pub.isoformat(),
                    }
                )
            print(f"  [ok] {name}: {len(articles) - kept_before} kept")
        except Exception as exc:
            print(f"  [skip] {name}: {exc}")
            continue

    return articles


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _content_value(entry) -> str:
    c = getattr(entry, "content", None)
    if not c:
        return ""
    if isinstance(c, list) and c:
        first = c[0]
        if isinstance(first, dict):
            return first.get("value", "") or ""
        return getattr(first, "value", "") or ""
    return str(c)


# =========================================================================
# Stage 2 — Dedupe
# =========================================================================
def dedupe(articles: list[dict]) -> list[dict]:
    kept: list[dict] = []
    seen_canon: dict[str, int] = {}

    for a in articles:
        canon = _canon_url(a["url"])
        if canon in seen_canon:
            kept[seen_canon[canon]]["cross_reports"] += 1
            continue

        t_low = a["title"].lower()
        dupe_idx = None
        for i, k in enumerate(kept):
            if SequenceMatcher(None, t_low, k["title"].lower()).ratio() > SIMILARITY_THRESHOLD:
                dupe_idx = i
                break
        if dupe_idx is not None:
            kept[dupe_idx]["cross_reports"] += 1
            continue

        a["cross_reports"] = 1
        kept.append(a)
        seen_canon[canon] = len(kept) - 1

    return kept


def _canon_url(u: str) -> str:
    try:
        p = urlparse(u)
        netloc = p.netloc.lower().removeprefix("www.")
        path = p.path.rstrip("/").lower()
        return f"{netloc}{path}"
    except Exception:
        return u.strip().lower().rstrip("/")


# =========================================================================
# Stage 3 — Score
# =========================================================================
SCORING_RUBRIC = """You score AI/tech news articles for a daily digest with a bias toward India.

For EACH article, return JSON with these exact keys:
- is_ai (boolean): true only if the story is primarily about AI/ML/generative AI/LLMs. Return false for general tech, crypto, gadgets, non-AI business news.
- india_relevance (0.0 to 1.0): 1.0 = India-specific story. 0.0 = no India angle.
- impact_score (0-10): use the rubric below.
- trending_score (0-10): newsworthiness and likely discussion volume.
- category: one of ["policy", "funding", "model_release", "product", "research", "acquisition", "enterprise", "other"]

IMPACT RUBRIC (be strict):
 9-10 : RBI/MeitY/central government AI policy or directive; foundational model release from OpenAI/Anthropic/Google/Meta/Mistral/top Indian lab; acquisition above $1B; major AI regulation globally (EU AI Act, US executive orders).
 7-8  : Large funding ($50M+ global, ₹100Cr+ India); major enterprise AI deployment at Fortune 500 or top Indian conglomerate; breakthrough research with near-term real-world impact; state-level AI policy.
 5-6  : Product launches from significant players; funding $10-50M or ₹25-100Cr; notable partnerships; new benchmark results.
 3-4  : Feature updates, smaller funding, incremental research, industry reports.
 0-2  : Opinion pieces, rumors, minor UI changes, listicles.

Return ONLY a JSON array in input order. No preamble, no markdown."""


def score_articles(articles: list[dict]) -> list[dict]:
    scored: list[dict] = []
    for start in range(0, len(articles), BATCH_SIZE):
        batch = articles[start : start + BATCH_SIZE]
        results = _score_batch(batch)
        ai_count = 0
        for a, s in zip(batch, results):
            a["is_ai"] = bool(s.get("is_ai", False))
            a["india_relevance"] = _clamp_float(s.get("india_relevance", 0.0), 0.0, 1.0)
            a["impact_score"] = _clamp_float(s.get("impact_score", 0), 0.0, 10.0)
            a["trending_score"] = _clamp_float(s.get("trending_score", 0), 0.0, 10.0)
            a["category"] = s.get("category", "other")
            if a["is_ai"]:
                ai_count += 1
            scored.append(a)
        # Diagnostic: how many were tagged AI, and a peek at the first parsed
        # item so we can spot if the model is returning unexpected key names.
        sample = results[0] if results else {}
        sample_keys = list(sample.keys())[:6] if isinstance(sample, dict) else type(sample).__name__
        print(
            f"  batch {start // BATCH_SIZE + 1}: "
            f"{ai_count}/{len(batch)} is_ai=true "
            f"(sample is_ai={sample.get('is_ai') if isinstance(sample, dict) else '?'}, "
            f"impact={sample.get('impact_score') if isinstance(sample, dict) else '?'}, "
            f"keys={sample_keys})"
        )
    return scored


def _score_batch(batch: list[dict]) -> list[dict]:
    items = [
        {
            "i": i,
            "title": a["title"],
            "summary": a["summary"][:500],
            "source": a["source"],
            "region": a["source_region"],
        }
        for i, a in enumerate(batch)
    ]
    prompt = SCORING_RUBRIC + "\n\nArticles:\n" + json.dumps(items, ensure_ascii=False)
    raw = call_llm(prompt, json_mode=True)
    if not raw:
        return [_default_score() for _ in batch]
    try:
        parsed = _parse_json(raw)
        parsed = _unwrap_list(parsed)
        if not isinstance(parsed, list):
            return [_default_score() for _ in batch]
        while len(parsed) < len(batch):
            parsed.append(_default_score())
        return parsed[: len(batch)]
    except Exception as exc:
        print(f"  [warn] score parse failed: {exc}")
        return [_default_score() for _ in batch]


def _default_score() -> dict:
    return {
        "is_ai": False,
        "india_relevance": 0.0,
        "impact_score": 0,
        "trending_score": 0,
        "category": "other",
    }


def _clamp_float(v, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, f))


# =========================================================================
# Stage 4 — LLM wrapper (OpenRouter only, with multi-model fallback)
# =========================================================================
def call_llm(prompt: str, json_mode: bool = True) -> str:
    global _last_llm_provider

    if not OPENROUTER_API_KEY:
        print("  [warn] OPENROUTER_API_KEY not set")
        return ""

    for model_id in OPENROUTER_MODELS:
        try:
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": OPENROUTER_REFERER,
                "X-Title": "AI Daily Digest",
                "Content-Type": "application/json",
            }
            body: dict = {
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            r = requests.post(OPENROUTER_URL, headers=headers, json=body, timeout=LLM_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if content:
                _last_llm_provider = f"openrouter:{model_id}"
                print(f"  [llm] openrouter ({model_id})")
                return content
            raise ValueError("empty content")
        except Exception as exc:
            print(f"  [warn] openrouter {model_id} failed: {exc}")
    print("  [warn] all OpenRouter models failed")
    return ""


def _strip_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*\n?", "", s)
    s = re.sub(r"\n?\s*```$", "", s)
    return s.strip()


def _parse_json(text: str):
    return json.loads(_strip_fences(text))


def _unwrap_list(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("items", "articles", "scores", "results", "data"):
            if key in parsed and isinstance(parsed[key], list):
                return parsed[key]
        if parsed and all(str(k).isdigit() for k in parsed.keys()):
            return [parsed[k] for k in sorted(parsed.keys(), key=lambda x: int(x))]
    return parsed


# =========================================================================
# Stage 5 — Rank + split
# =========================================================================
def pick_final(scored: list[dict]) -> list[dict]:
    for a in scored:
        if not a.get("is_ai"):
            a["final_score"] = -1.0
            continue
        cr = min(a.get("cross_reports", 1) * 2, 10)
        a["final_score"] = (
            0.45 * a["impact_score"]
            + 0.30 * a["trending_score"]
            + 0.15 * cr
            + 0.10 * 5
        )

    ai = [a for a in scored if a.get("is_ai")]
    ai.sort(key=lambda x: x["final_score"], reverse=True)

    india_target = round(FINAL_COUNT * INDIA_SHARE)
    global_target = FINAL_COUNT - india_target

    india_pool = [a for a in ai if _is_india(a)]
    global_pool = [a for a in ai if not _is_india(a)]

    india_picks = india_pool[:india_target]
    global_picks = global_pool[:global_target]
    picks = india_picks + global_picks

    if len(picks) < FINAL_COUNT:
        leftover = india_pool[len(india_picks):] + global_pool[len(global_picks):]
        leftover.sort(key=lambda x: x["final_score"], reverse=True)
        picks.extend(leftover[: FINAL_COUNT - len(picks)])

    picks.sort(key=lambda x: x["final_score"], reverse=True)
    return picks


def _is_india(a: dict) -> bool:
    return a.get("source_region") == "india" or a.get("india_relevance", 0) >= 0.5


# =========================================================================
# Stage 6 — Rewrite (now produces "why it matters" too)
# =========================================================================
REWRITE_PROMPT = """Rewrite these AI news items for a daily editorial digest.

For each, return JSON with these exact keys:
- headline (string): crisp, specific, factual. MAX 12 words. No clickbait, no "How X is transforming Y", no questions. Lead with the concrete fact (who did what, or what launched).
- why (string): a single editorial pull-quote, ONE short sentence (8-12 words), explaining why this matters in plain language. Think italic magazine pull-quote, not a tagline. No hype words like "groundbreaking" or "game-changing".
- summary (string): exactly 2 sentences, ~40 words total. Sentence 1 = core news. Sentence 2 = one specific detail or consequence. No filler.
- tags (array of 1-3 short lowercase strings): the 1-3 most useful categorical tags, e.g. ["policy", "EU"] or ["funding", "india"]. Avoid generic tags like "ai" or "news".

Return ONLY a JSON array in input order."""


def rewrite(picks: list[dict]) -> list[dict]:
    if not picks:
        return picks

    items = [
        {
            "i": i,
            "title": a["title"],
            "summary": a["summary"][:500],
            "source": a["source"],
            "category": a.get("category", "other"),
        }
        for i, a in enumerate(picks)
    ]
    prompt = REWRITE_PROMPT + "\n\nItems:\n" + json.dumps(items, ensure_ascii=False)
    raw = call_llm(prompt, json_mode=True)

    parsed_list: list | None = None
    if raw:
        try:
            p = _unwrap_list(_parse_json(raw))
            if isinstance(p, list):
                parsed_list = p
        except Exception as exc:
            print(f"  [warn] rewrite parse failed: {exc}")

    for idx, a in enumerate(picks):
        r = parsed_list[idx] if parsed_list and idx < len(parsed_list) else None
        if isinstance(r, dict) and r.get("headline"):
            a["headline"] = str(r["headline"]).strip()[:160]
            a["why"] = str(r.get("why") or _fallback_why(a)).strip()[:200]
            a["short_summary"] = str(r.get("summary") or _fallback_summary(a["summary"])).strip()[:400]
            tags = r.get("tags") or []
            if isinstance(tags, list):
                a["tags"] = [str(t).strip().lower()[:24] for t in tags if t][:3]
            else:
                a["tags"] = [a.get("category", "other")]
        else:
            a["headline"] = a["title"][:160]
            a["why"] = _fallback_why(a)
            a["short_summary"] = _fallback_summary(a["summary"])
            a["tags"] = [a.get("category", "other")] if a.get("category") else []

    return picks


def _fallback_summary(raw: str) -> str:
    if not raw:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", raw.strip())
    return " ".join(sentences[:2])[:360]


def _fallback_why(article: dict) -> str:
    cat = article.get("category", "other")
    canned = {
        "policy": "New rules for everyone shipping AI in this market.",
        "funding": "Capital flowing here signals where the market expects scale.",
        "model_release": "A new option in the model menu — expect benchmarks soon.",
        "product": "A real product, not a demo, lands in users' hands.",
        "research": "An incremental result with real-world implications.",
        "acquisition": "Consolidation reshapes who builds what next.",
        "enterprise": "Enterprises moving from pilot to production.",
        "other": "A signal worth tracking even if the headline is quiet.",
    }
    return canned.get(cat, canned["other"])


# =========================================================================
# Stage 7 — Enrichment for the editorial layout
# =========================================================================
KIND_MAP = {
    "policy": "policy",
    "funding": "funding",
    "model_release": "model",
    "product": "product",
    "research": "research",
    "acquisition": "deal",
    "enterprise": "deployment",
    "other": "news",
}


def enrich_for_render(picks: list[dict]) -> list[dict]:
    """Add display fields the design needs: scope, kind, time, region."""
    for a in picks:
        a["scope"] = "national" if _is_india(a) else "international"
        a["kind"] = KIND_MAP.get(a.get("category", "other"), "news")
        a["time"] = _format_time(a.get("published", ""))
        a["region"] = a.get("source", "")
    return picks


def _format_time(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_ist = dt.astimezone(IST)
    today_ist = datetime.now(IST).date()
    delta_days = (today_ist - dt_ist.date()).days
    if delta_days <= 0:
        return dt_ist.strftime("%H:%M")
    if delta_days == 1:
        return "Yesterday " + dt_ist.strftime("%H:%M")
    return dt_ist.strftime("%b %d")


# =========================================================================
# Stage 8 — Auxiliary modules: trending, companies, stats
# =========================================================================
COMPANIES_KNOWN: list[tuple[str, list[str]]] = [
    # Display-name, list of regex-safe match patterns (case-insensitive, word-bounded).
    ("OpenAI",          [r"\bopenai\b"]),
    ("Anthropic",       [r"\banthropic\b", r"\bclaude\b"]),
    ("Google DeepMind", [r"\bdeepmind\b", r"\bgoogle deepmind\b"]),
    ("Google",          [r"\bgoogle\b", r"\bgemini\b"]),
    ("Meta",            [r"\bmeta\b", r"\bllama\b"]),
    ("Microsoft",       [r"\bmicrosoft\b", r"\bcopilot\b"]),
    ("Mistral",         [r"\bmistral\b"]),
    ("Cohere",          [r"\bcohere\b"]),
    ("Perplexity",      [r"\bperplexity\b"]),
    ("xAI",             [r"\bxai\b", r"\bgrok\b"]),
    ("Nvidia",          [r"\bnvidia\b"]),
    ("AMD",             [r"\bamd\b"]),
    ("Apple",           [r"\bapple\b"]),
    ("Amazon",          [r"\bamazon\b", r"\baws\b"]),
    ("IBM",             [r"\bibm\b", r"\bwatson\b"]),
    ("Hugging Face",    [r"\bhugging\s*face\b"]),
    ("Sarvam AI",       [r"\bsarvam\b"]),
    ("Krutrim",         [r"\bkrutrim\b"]),
    ("Reliance",        [r"\breliance jio\b", r"\bjio\b"]),
    ("TCS",             [r"\btcs\b", r"\btata consultancy\b"]),
    ("Infosys",         [r"\binfosys\b"]),
    ("Wipro",           [r"\bwipro\b"]),
    ("Yellow.ai",       [r"\byellow\.ai\b", r"\byellow ai\b"]),
    ("Fractal",         [r"\bfractal analytics\b", r"\bfractal\b"]),
    ("Ola Krutrim",     [r"\bola krutrim\b"]),
]


def compute_companies(articles: list[dict]) -> list[dict]:
    """Count company mentions across all in-pool articles."""
    counts: Counter[str] = Counter()
    for a in articles:
        haystack = (a.get("title", "") + " " + a.get("summary", "")).lower()
        for name, patterns in COMPANIES_KNOWN:
            if any(re.search(p, haystack) for p in patterns):
                counts[name] += 1
    return [
        {"name": name, "mentions": n, "change": None}
        for name, n in counts.most_common(COMPANIES_COUNT)
        if n > 0
    ]


_STOP = set("""
the a an and or but of for to from in on at by with as is are was were be been being
this that these those it its their they we you i he she there here will would can could
new news update updates report says say said this how why what who when where which
ai artificial intelligence ml generative model models tech technology india indian global
""".split())


def compute_trending(articles: list[dict]) -> list[str]:
    """Pull notable Title-Cased phrases from titles, ranked by frequency."""
    phrases: Counter[str] = Counter()
    for a in articles:
        title = a.get("title", "")
        # Match sequences of 1-3 capitalised words (basic NER).
        for m in re.finditer(
            r"\b([A-Z][a-zA-Z0-9]+(?:[\-\s]+[A-Z][a-zA-Z0-9]+){0,2})\b",
            title,
        ):
            phrase = m.group(1).strip()
            low = phrase.lower()
            if low in _STOP or len(low) < 3:
                continue
            phrases[phrase] += 1
    out: list[str] = []
    for phrase, n in phrases.most_common(TRENDING_COUNT * 3):
        if n < 1:
            continue
        out.append(phrase.lower() if phrase.isupper() else phrase)
        if len(out) >= TRENDING_COUNT:
            break
    return out


def compute_stats(picks: list[dict], all_scored: list[dict], yesterday: dict | None) -> dict:
    ai_count = sum(1 for a in all_scored if a.get("is_ai"))
    avg_impact = (
        round(sum(a["impact_score"] for a in picks) / len(picks), 1)
        if picks
        else 0.0
    )
    cats = Counter(a.get("category", "other") for a in picks if a.get("is_ai"))
    top_cat = cats.most_common(1)[0][0] if cats else "—"
    companies = compute_companies(all_scored)
    top_mover = companies[0]["name"] if companies else "—"

    yest_ai = yesterday.get("ai_count") if yesterday else None
    yest_avg = yesterday.get("avg_impact") if yesterday else None
    yest_top = yesterday.get("top_mover") if yesterday else None

    return {
        "ai_count": ai_count,
        "ai_count_yesterday": yest_ai,
        "avg_impact": avg_impact,
        "avg_impact_yesterday": yest_avg,
        "top_cat": top_cat,
        "top_mover": top_mover,
        "top_mover_yesterday": yest_top,
    }


def load_yesterday_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_today_state(stats: dict) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "ai_count": stats["ai_count"],
        "avg_impact": stats["avg_impact"],
        "top_mover": stats["top_mover"],
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# =========================================================================
# Stage 9 — Render & write static site (editorial design port)
# =========================================================================
PAGE_CSS = r"""
:root{
  --paper:oklch(0.962 0.018 82);
  --paper-2:oklch(0.94 0.022 82);
  --paper-3:oklch(0.91 0.025 82);
  --ink:oklch(0.18 0.012 60);
  --ink-2:oklch(0.32 0.012 60);
  --ink-3:oklch(0.5 0.012 60);
  --ink-4:oklch(0.7 0.012 60);
  --rule:oklch(0.82 0.02 70);
  --accent:oklch(0.66 0.21 45);
  --serif:"Fraunces","Newsreader",Georgia,serif;
  --serif-display:"Fraunces","DM Serif Display",Georgia,serif;
  --sans:"Inter Tight",ui-sans-serif,system-ui,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,monospace;
}
@media (prefers-color-scheme: dark){
  :root.auto-dark{
    --paper:oklch(0.155 0.012 60);
    --paper-2:oklch(0.19 0.012 60);
    --paper-3:oklch(0.23 0.014 60);
    --ink:oklch(0.95 0.018 82);
    --ink-2:oklch(0.85 0.018 82);
    --ink-3:oklch(0.65 0.018 82);
    --ink-4:oklch(0.45 0.012 60);
    --rule:oklch(0.32 0.012 60);
  }
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{
  background:var(--paper);
  color:var(--ink);
  font-family:var(--sans);
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
  background-image:radial-gradient(circle at 1px 1px,oklch(0.78 0.02 70 / 0.08) 0.7px,transparent 0.7px);
  background-size:3px 3px;
  min-height:100vh;
}
.digest-root{
  max-width:520px;
  margin:0 auto;
  background:var(--paper);
  background-image:radial-gradient(circle at 1px 1px,oklch(0.78 0.02 70 / 0.08) 0.7px,transparent 0.7px);
  background-size:3px 3px;
  min-height:100vh;
  border-left:1px solid var(--rule);
  border-right:1px solid var(--rule);
}
@media (max-width:540px){.digest-root{border-left:0;border-right:0;}}

/* masthead */
.masthead{padding:18px 18px 14px;border-bottom:2px solid var(--ink);}
.mast-row{display:flex;align-items:center;justify-content:space-between;gap:8px;}
.mast-meta{font-family:var(--mono);font-size:9.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--ink-3);}
.mast-title{margin:6px 0 8px;font-family:var(--serif-display);font-weight:900;font-size:64px;line-height:0.85;letter-spacing:-0.035em;color:var(--ink);font-variation-settings:"opsz" 144;text-align:center;}
.mast-the{display:block;font-family:var(--serif);font-weight:500;font-style:italic;font-size:22px;color:var(--ink-2);margin-bottom:-4px;}
.mast-main{display:block;}
.mast-bot{gap:10px;padding-top:4px;}
.mast-rule{flex:1;height:1px;background:var(--ink);}
.mast-date{font-family:var(--serif);font-style:italic;font-size:13px;color:var(--ink-2);white-space:nowrap;}

/* greeting */
.greeting{padding:22px 20px 18px;border-bottom:1px solid var(--rule);}
.greeting .hello{margin:0 0 8px;font-family:var(--serif);font-style:italic;font-weight:500;font-size:30px;line-height:1;color:var(--accent);letter-spacing:-0.01em;}
.greeting .intro{margin:0;font-family:var(--serif);font-size:16px;line-height:1.4;color:var(--ink-2);text-wrap:pretty;}

/* section labels */
.section-label{display:flex;align-items:center;gap:10px;padding:0 20px;margin:22px 0 12px;font-family:var(--mono);font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:var(--ink-3);}
.section-label .dotline{flex:1;height:1px;background-image:linear-gradient(90deg,var(--ink-4) 50%,transparent 0);background-size:4px 1px;}

/* today vs yesterday */
.tvy{padding-bottom:18px;border-bottom:1px solid var(--rule);}
.tvy-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin:0 20px;background:var(--rule);border:1px solid var(--rule);}
.tvy-cell{background:var(--paper);padding:12px 10px 10px;display:flex;flex-direction:column;gap:4px;}
.tvy-label{font-family:var(--mono);font-size:8.5px;letter-spacing:0.16em;text-transform:uppercase;color:var(--ink-3);}
.tvy-today{font-family:var(--serif-display);font-weight:600;font-size:32px;line-height:1;letter-spacing:-0.025em;color:var(--ink);font-variant-numeric:tabular-nums;}
.tvy-today.txt{font-size:18px;font-weight:600;font-style:italic;}
.tvy-yest{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);}
.tvy-yest .tvy-arrow{color:var(--ink-4);margin-right:2px;}

/* trending ticker */
.ticker{display:flex;align-items:stretch;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);margin-top:22px;background:var(--paper-2);overflow:hidden;}
.ticker-tag{flex-shrink:0;background:var(--ink);color:var(--paper);font-family:var(--mono);font-size:10px;letter-spacing:0.22em;text-transform:uppercase;padding:10px 14px;display:flex;align-items:center;}
.ticker-track-wrap{flex:1;overflow:hidden;position:relative;-webkit-mask-image:linear-gradient(90deg,transparent,#000 8%,#000 92%,transparent);mask-image:linear-gradient(90deg,transparent,#000 8%,#000 92%,transparent);}
.ticker-track{display:inline-flex;gap:28px;padding:10px 14px;white-space:nowrap;animation:marquee 38s linear infinite;}
.ticker-item{font-family:var(--serif);font-style:italic;font-size:15px;color:var(--ink-2);}
.ticker-hash{font-family:var(--mono);font-style:normal;color:var(--accent);margin-right:4px;font-weight:500;}
@keyframes marquee{0%{transform:translateX(0);}100%{transform:translateX(-50%);}}

/* scope tabs */
.scope-tabs{position:relative;margin:18px 18px 6px;display:grid;grid-template-columns:repeat(3,1fr);background:var(--paper-2);border:1px solid var(--rule);border-radius:999px;padding:4px;height:44px;}
.scope-thumb{position:absolute;top:4px;bottom:4px;background:var(--ink);border-radius:999px;transition:left 0.32s cubic-bezier(0.4,0.8,0.2,1),width 0.32s cubic-bezier(0.4,0.8,0.2,1);box-shadow:0 1px 4px rgba(0,0,0,0.18);}
.scope-tab{position:relative;z-index:1;appearance:none;border:0;background:transparent;color:var(--ink-2);font-family:var(--mono);font-size:10px;letter-spacing:0.18em;text-transform:uppercase;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:6px;transition:color 0.25s ease;padding:0;}
.scope-tab.on{color:var(--paper);}
.scope-tab .scope-count{font-family:var(--serif);font-style:italic;font-size:13px;text-transform:none;letter-spacing:0;opacity:0.7;line-height:1;}
.scope-tab.on .scope-count{color:var(--accent);opacity:1;font-weight:500;}

/* stories label */
.stories-label .stories-count{font-family:var(--serif-display);font-style:italic;font-size:16px;color:var(--accent);letter-spacing:0;text-transform:none;margin-left:4px;}

/* story card */
.story{position:relative;border-bottom:1px solid var(--rule);transition:background 0.3s ease;}
.story:first-of-type{border-top:1px solid var(--rule);}
.story.expanded{background:var(--paper-2);}
.story.hidden{display:none;}
.story-main{width:100%;background:transparent;border:0;padding:16px 18px;text-align:left;cursor:pointer;color:inherit;font:inherit;display:grid;grid-template-columns:70px 1fr;gap:10px;align-items:start;-webkit-tap-highlight-color:transparent;}
.story-num{font-family:var(--serif-display);font-weight:700;font-size:68px;line-height:0.82;letter-spacing:-0.05em;color:var(--ink);font-variant-numeric:tabular-nums;position:relative;margin-top:-2px;}
.story.expanded .story-num{color:var(--accent);}
.story-num::after{content:"";position:absolute;left:0;right:14px;bottom:-6px;height:1px;background:currentColor;opacity:0.3;}
.story-body{min-width:0;}
.story-meta{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:9.5px;letter-spacing:0.16em;text-transform:uppercase;color:var(--ink-3);margin-bottom:6px;flex-wrap:wrap;}
.story-time{background:var(--ink);color:var(--paper);padding:2px 5px;font-weight:500;}
.story-dot{color:var(--ink-4);}
.story-scope{font-weight:600;letter-spacing:0.16em;}
.story-scope.is-international{color:var(--accent);}
.story-scope.is-national{color:var(--ink-2);}
.story-region{font-family:var(--serif);font-style:italic;font-size:11px;text-transform:none;letter-spacing:0;color:var(--ink-3);}
.story-headline{margin:0 0 8px;font-family:var(--serif-display);font-weight:500;font-size:24px;line-height:1.08;letter-spacing:-0.018em;color:var(--ink);text-wrap:pretty;}
.story-read{display:inline-block;margin-top:10px;font-family:var(--mono);font-size:9.5px;letter-spacing:0.18em;text-transform:uppercase;color:var(--accent);text-decoration:none;border-bottom:1px solid transparent;padding-bottom:1px;}
.story-read:hover{border-bottom-color:var(--accent);}
.story-read .arrow{margin-left:6px;display:inline-block;}
.story-why{margin:0 0 8px;font-family:var(--serif);font-style:italic;font-size:15px;line-height:1.3;color:var(--accent);text-wrap:pretty;}
.why-label{font-family:var(--mono);font-style:normal;font-size:9.5px;letter-spacing:0.16em;text-transform:uppercase;color:var(--ink-3);margin-right:4px;}
.story-summary-wrap{display:grid;grid-template-rows:0fr;transition:grid-template-rows 0.4s cubic-bezier(0.2,0.8,0.2,1);overflow:hidden;margin-top:0;}
.story.expanded .story-summary-wrap{grid-template-rows:1fr;margin-top:6px;}
.story-summary-wrap > div{min-height:0;overflow:hidden;opacity:0;transition:opacity 0.35s ease 0.05s;}
.story.expanded .story-summary-wrap > div{opacity:1;}
.story-summary{margin:0;font-family:var(--serif);font-size:15px;line-height:1.5;color:var(--ink-2);border-left:2px solid var(--accent);padding-left:12px;text-wrap:pretty;}
.story-tags{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:10px;}
.tag{font-family:var(--mono);font-size:9px;letter-spacing:0.14em;text-transform:uppercase;padding:3px 6px;border:1px solid var(--rule);color:var(--ink-3);}
.story-expand-hint{margin-left:auto;font-family:var(--mono);font-size:9.5px;letter-spacing:0.14em;text-transform:uppercase;color:var(--ink-3);display:inline-flex;align-items:center;gap:4px;}
.hint-ar{display:inline-block;transition:transform 0.3s ease;}
.story.expanded .hint-ar{transform:rotate(180deg);}

/* save / bookmark */
.save{position:absolute;top:14px;right:14px;width:28px;height:28px;display:grid;place-items:center;background:transparent;border:1px solid var(--rule);color:var(--ink-3);cursor:pointer;transition:color 0.2s ease,border-color 0.2s ease,background 0.2s ease;}
.save:hover{color:var(--ink);border-color:var(--ink-3);}
.save.on{color:var(--accent);border-color:var(--accent);background:oklch(from var(--accent) l c h / 0.08);}
.save svg{display:block;}

/* companies */
.companies{background:var(--paper-2);border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);padding:4px 0 18px;margin:0;position:relative;}
.companies::before{content:"INTERMISSION";position:absolute;top:-7px;left:50%;transform:translateX(-50%);background:var(--paper);padding:0 10px;font-family:var(--mono);font-size:9px;letter-spacing:0.3em;color:var(--accent);}
.companies .section-label{margin-top:18px;}
.comp-list{list-style:none;margin:0;padding:0 20px;}
.comp-row{display:grid;grid-template-columns:22px 1fr 60px 30px;gap:8px;align-items:center;padding:6px 0;border-bottom:1px dotted var(--rule);}
.comp-row:last-child{border-bottom:0;}
.comp-rank{font-family:var(--mono);font-size:10px;color:var(--ink-4);font-weight:500;}
.comp-name{font-family:var(--serif);font-size:14px;font-weight:500;color:var(--ink);}
.comp-bar{height:6px;background:var(--paper-3);position:relative;}
.comp-fill{position:absolute;inset:0 auto 0 0;background:var(--accent);}
.comp-num{font-family:var(--mono);font-size:11px;color:var(--ink-2);text-align:right;font-variant-numeric:tabular-nums;}

/* hover/expand on mouse */
@media (hover: hover){
  .story:not(.expanded):hover{background:var(--paper-2);}
  .story:not(.expanded):hover .story-summary-wrap{grid-template-rows:1fr;margin-top:6px;}
  .story:not(.expanded):hover .story-summary{opacity:1;}
}

/* archive footer */
.archive{margin:36px 20px 0;padding-top:16px;border-top:1px solid var(--rule);font-size:13px;color:var(--ink-3);font-family:var(--sans);}
.archive summary{cursor:pointer;font-family:var(--mono);font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:var(--ink-3);padding:4px 0;}
.archive-list{margin-top:10px;}
.archive-list a{display:inline-block;margin:6px 14px 0 0;color:var(--ink-2);text-decoration:none;font-family:var(--serif);font-style:italic;font-size:14px;}
.archive-list a:hover{color:var(--accent);}

/* digest footer */
.digest-foot{padding:28px 20px 60px;text-align:center;}
.foot-rule{width:60%;height:1px;background:var(--ink);margin:0 auto 14px;}
.foot-text{margin:0 0 10px;font-family:var(--serif);font-size:14px;color:var(--ink-2);}
.foot-mark{font-family:var(--serif);font-size:14px;color:var(--accent);letter-spacing:0.4em;}
.back-link{display:inline-block;margin:0 0 16px;padding:18px 20px 0;color:var(--accent);text-decoration:none;font-family:var(--mono);font-size:11px;letter-spacing:0.18em;text-transform:uppercase;}
.back-link:hover{text-decoration:underline;}
.empty-state{padding:48px 20px;color:var(--ink-3);font-family:var(--serif);font-style:italic;font-size:16px;text-align:center;}
"""

PAGE_JS = r"""
(function(){
  var SAVE_KEY = 'ai-daily-saved';
  function getSaved(){
    try{ return new Set(JSON.parse(localStorage.getItem(SAVE_KEY)||'[]')); }
    catch(e){ return new Set(); }
  }
  function setSaved(s){
    try{ localStorage.setItem(SAVE_KEY, JSON.stringify(Array.from(s))); }catch(e){}
  }
  var saved = getSaved();
  document.querySelectorAll('.story').forEach(function(el){
    var idx = el.getAttribute('data-idx');
    var btn = el.querySelector('.save');
    if (!btn) return;
    if (saved.has(idx)) btn.classList.add('on');
    btn.addEventListener('click', function(ev){
      ev.stopPropagation(); ev.preventDefault();
      if (saved.has(idx)) { saved.delete(idx); btn.classList.remove('on'); }
      else                 { saved.add(idx);    btn.classList.add('on');    }
      setSaved(saved);
    });
  });
  // expand toggle
  document.querySelectorAll('.story-main').forEach(function(btn){
    btn.addEventListener('click', function(ev){
      if (ev.target.closest('a')) return;  // headline link wins
      var article = btn.closest('.story');
      var wasOpen = article.classList.contains('expanded');
      document.querySelectorAll('.story.expanded').forEach(function(o){
        if (o !== article) o.classList.remove('expanded');
      });
      article.classList.toggle('expanded', !wasOpen);
    });
  });
  // scope tabs
  var tabs = document.querySelectorAll('.scope-tab');
  var thumb = document.querySelector('.scope-thumb');
  function setActive(scope){
    var idx = 0; tabs.forEach(function(t,i){ if (t.dataset.scope===scope) idx=i; t.classList.toggle('on', t.dataset.scope===scope); });
    if (thumb && tabs.length){
      thumb.style.left = 'calc(((100% - 8px) / ' + tabs.length + ') * ' + idx + ' + 4px)';
      thumb.style.width = 'calc((100% - 8px) / ' + tabs.length + ')';
    }
    document.querySelectorAll('.story').forEach(function(s){
      var sScope = s.dataset.scope || '';
      var visible = (scope === 'all') || (sScope === scope);
      s.classList.toggle('hidden', !visible);
    });
    // Companies leaderboard only shown when scope is 'all'.
    var comp = document.querySelector('.companies');
    if (comp) comp.classList.toggle('hidden', scope !== 'all');
    // Update count label.
    var countEl = document.querySelector('.stories-count');
    if (countEl){
      var visible = document.querySelectorAll('.story:not(.hidden)').length;
      countEl.textContent = visible;
    }
    // Update label text.
    var labelEl = document.querySelector('.stories-label-text');
    if (labelEl){
      labelEl.textContent = scope==='national' ? 'India — by recency'
                          : scope==='international' ? 'Global — by recency'
                          : 'The Top — by recency';
    }
  }
  tabs.forEach(function(t){
    t.addEventListener('click', function(){ setActive(t.dataset.scope); });
  });
  // Initial thumb positioning.
  var initial = document.querySelector('.scope-tab.on');
  if (initial) setActive(initial.dataset.scope);
})();
"""

GOOGLE_FONTS_LINK = (
    'https://fonts.googleapis.com/css2?'
    'family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;0,9..144,700;0,9..144,900;1,9..144,500'
    '&family=JetBrains+Mono:wght@400;500'
    '&family=Inter+Tight:wght@400;500;600'
    '&family=Newsreader:ital,wght@0,400;0,500;1,400'
    '&display=swap'
)


def render_page(
    picks: list[dict],
    *,
    date_human: str,
    date_iso: str,
    edition: str,
    intro: str,
    trending: list[str],
    companies: list[dict],
    stats: dict,
    past_editions: list[dict] | None,
    is_archive: bool,
) -> str:
    counts = {
        "all": len(picks),
        "national": sum(1 for p in picks if p.get("scope") == "national"),
        "international": sum(1 for p in picks if p.get("scope") == "international"),
    }

    title = html_mod.escape(SITE_TITLE)
    date_safe = html_mod.escape(date_human)
    edition_safe = html_mod.escape(edition)
    intro_safe = html_mod.escape(intro)

    masthead = (
        '<header class="masthead">'
        '<div class="mast-row mast-top">'
        f'<span class="mast-meta">{edition_safe}</span>'
        '<span class="mast-meta">est. 2026</span>'
        '</div>'
        '<h1 class="mast-title">'
        '<span class="mast-the">The</span>'
        f'<span class="mast-main">{title}</span>'
        '</h1>'
        '<div class="mast-row mast-bot">'
        '<span class="mast-rule"></span>'
        f'<span class="mast-date">{date_safe}</span>'
        '<span class="mast-rule"></span>'
        '</div>'
        '</header>'
    )

    greeting_word = _greeting_for_hour(datetime.now(IST).hour)
    greeting = (
        '<section class="greeting">'
        f'<p class="hello">{html_mod.escape(greeting_word)}.</p>'
        f'<p class="intro">{intro_safe}</p>'
        '</section>'
    )

    tvy = _render_tvy(stats)
    ticker = _render_ticker(trending)
    scope_tabs = _render_scope_tabs(counts)
    stories_section = _render_stories(picks, companies)

    if is_archive:
        archive_block = '<a class="back-link" href="./">&larr; Latest edition</a>'
    elif past_editions:
        items = "".join(
            f'<a href="{html_mod.escape(e["href"], quote=True)}">{html_mod.escape(e["label"])}</a>'
            for e in past_editions
        )
        archive_block = (
            f'<details class="archive">'
            f'<summary>Past editions ({len(past_editions)})</summary>'
            f'<div class="archive-list">{items}</div>'
            f'</details>'
        )
    else:
        archive_block = ""

    foot = (
        '<footer class="digest-foot">'
        '<div class="foot-rule"></div>'
        '<p class="foot-text">End of edition. <em>Tomorrow at 08:00 IST.</em></p>'
        '<div class="foot-mark">&mdash; &#10022; &mdash;</div>'
        '</footer>'
    )

    head = (
        '<!DOCTYPE html>'
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="theme-color" content="#FF6B00">'
        '<link rel="manifest" href="manifest.json">'
        '<link rel="apple-touch-icon" href="icon.svg">'
        '<link rel="icon" type="image/svg+xml" href="icon.svg">'
        f'<title>The {title} &mdash; {date_safe}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{GOOGLE_FONTS_LINK}">'
        f'<style>{PAGE_CSS}</style>'
        '</head><body>'
    )

    if not picks:
        body_inner = (
            masthead + greeting +
            '<div class="empty-state">No edition for this date. Refresh tomorrow at 8 AM IST.</div>' +
            foot
        )
    else:
        body_inner = (
            masthead + greeting + tvy + ticker + scope_tabs + stories_section +
            archive_block + foot
        )

    return (
        head +
        '<div class="digest-root">' + body_inner + '</div>' +
        f'<script>{PAGE_JS}</script>' +
        '<script>'
        "if('serviceWorker' in navigator){"
        "window.addEventListener('load',function(){"
        "navigator.serviceWorker.register('service-worker.js')"
        ".then(function(reg){if(reg)reg.update();})"
        ".catch(function(){});"
        "});"
        "}"
        '</script>' +
        '</body></html>'
    )


def _render_tvy(stats: dict) -> str:
    def cell(label: str, today_val: str, yest_val, *, txt: bool = False) -> str:
        today_html = html_mod.escape(str(today_val))
        yest_part = (
            f'<span class="tvy-arrow">vs</span> {html_mod.escape(str(yest_val))}'
            if yest_val not in (None, "")
            else '<span class="tvy-arrow">vs</span> &mdash;'
        )
        cls = "tvy-today txt" if txt else "tvy-today"
        return (
            '<div class="tvy-cell">'
            f'<div class="tvy-label">{html_mod.escape(label)}</div>'
            f'<div class="{cls}">{today_html}</div>'
            f'<div class="tvy-yest">{yest_part}</div>'
            '</div>'
        )

    cells_html = (
        cell("Stories filed", stats["ai_count"], stats.get("ai_count_yesterday")) +
        cell("Avg. impact", stats["avg_impact"], stats.get("avg_impact_yesterday")) +
        cell("Top mover", stats["top_mover"], stats.get("top_mover_yesterday"), txt=True)
    )
    return (
        '<section class="tvy">'
        '<div class="section-label"><span>Today vs. Yesterday</span><span class="dotline"></span></div>'
        f'<div class="tvy-grid">{cells_html}</div>'
        '</section>'
    )


def _render_ticker(trending: list[str]) -> str:
    if not trending:
        return ""
    doubled = trending + trending
    items = "".join(
        f'<span class="ticker-item"><span class="ticker-hash">#</span>{html_mod.escape(t)}</span>'
        for t in doubled
    )
    return (
        '<section class="ticker">'
        '<div class="ticker-tag">Trending</div>'
        '<div class="ticker-track-wrap">'
        f'<div class="ticker-track">{items}</div>'
        '</div>'
        '</section>'
    )


def _render_scope_tabs(counts: dict) -> str:
    tabs = [
        ("all",           "All",   counts["all"]),
        ("international", "Global", counts["international"]),
        ("national",      "India",  counts["national"]),
    ]
    tab_buttons = "".join(
        f'<button class="scope-tab{" on" if i == 0 else ""}" data-scope="{tid}" type="button">'
        f'<span class="scope-label">{html_mod.escape(label)}</span>'
        f'<span class="scope-count">{n}</span>'
        f'</button>'
        for i, (tid, label, n) in enumerate(tabs)
    )
    initial_idx = 0
    return (
        '<div class="scope-tabs" role="tablist">'
        f'<span class="scope-thumb" style="left:calc(((100% - 8px) / 3) * {initial_idx} + 4px);width:calc((100% - 8px) / 3);"></span>'
        f'{tab_buttons}'
        '</div>'
    )


def _render_stories(picks: list[dict], companies: list[dict]) -> str:
    if not picks:
        return '<div class="empty-state">No stories today.</div>'
    cards: list[str] = []
    for i, a in enumerate(picks):
        cards.append(_render_story(a, i))
        if i == 4 and companies:
            cards.append(_render_companies(companies))
    return (
        '<section class="stories">'
        '<div class="section-label stories-label">'
        '<span class="stories-label-text">The Top &mdash; by recency</span>'
        '<span class="dotline"></span>'
        f'<span class="stories-count">{len(picks)}</span>'
        '</div>'
        + "".join(cards) +
        '</section>'
    )


def _render_story(a: dict, idx: int) -> str:
    n_str = str(idx + 1).zfill(2)
    headline = html_mod.escape(a.get("headline") or a.get("title") or "")
    why = html_mod.escape(a.get("why") or "")
    summary = html_mod.escape(a.get("short_summary") or "")
    url = html_mod.escape(a.get("url") or "#", quote=True)
    source_label = html_mod.escape(a.get("source") or "source")
    region = html_mod.escape(a.get("region") or "")
    time_str = html_mod.escape(a.get("time") or "")
    scope = a.get("scope") or "international"
    scope_label = "Natl" if scope == "national" else "Intl"
    kind = html_mod.escape(a.get("kind") or "news")
    tags = a.get("tags") or []
    tags_html = "".join(
        f'<span class="tag">{html_mod.escape(str(t))}</span>'
        for t in tags
    )

    return (
        f'<article class="story" data-idx="{idx}" data-scope="{scope}" data-kind="{kind}">'
        '<button type="button" class="story-main" aria-expanded="false">'
        f'<div class="story-num" aria-hidden="true">{n_str}</div>'
        '<div class="story-body">'
        '<div class="story-meta">'
        + (f'<span class="story-time">{time_str}</span>' if time_str else "")
        + '<span class="story-dot">&middot;</span>'
        f'<span class="story-scope is-{scope}">{scope_label}</span>'
        + (f'<span class="story-dot">&middot;</span><span class="story-region">{region}</span>' if region else "")
        + '<span class="story-dot">&middot;</span>'
        f'<span class="story-kind">{kind}</span>'
        '</div>'
        f'<h3 class="story-headline">{headline}</h3>'
        + (f'<p class="story-why"><span class="why-label">Why it matters &mdash;</span> {why}</p>' if why else "")
        + '<div class="story-summary-wrap"><div>'
        f'<p class="story-summary">{summary}</p>'
        f'<a class="story-read" href="{url}" target="_blank" rel="noopener">'
        f'Read at {source_label} <span class="arrow">&rarr;</span></a>'
        '</div></div>'
        '<div class="story-tags">'
        f'{tags_html}'
        '<span class="story-expand-hint">More <span class="hint-ar">&darr;</span></span>'
        '</div>'
        '</div>'
        '</button>'
        '<button type="button" class="save" aria-label="Bookmark">'
        '<svg width="14" height="18" viewBox="0 0 14 18" fill="none">'
        '<path d="M1.5 1.5h11v15l-5.5-4-5.5 4z" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linejoin="round"></path>'
        '</svg>'
        '</button>'
        '</article>'
    )


def _render_companies(companies: list[dict]) -> str:
    max_n = max((c["mentions"] for c in companies), default=1) or 1
    rows = []
    for i, c in enumerate(companies):
        pct = (c["mentions"] / max_n) * 100
        rows.append(
            '<li class="comp-row">'
            f'<span class="comp-rank">{str(i + 1).zfill(2)}</span>'
            f'<span class="comp-name">{html_mod.escape(c["name"])}</span>'
            '<span class="comp-bar">'
            f'<span class="comp-fill" style="width:{pct:.1f}%"></span>'
            '</span>'
            f'<span class="comp-num">{c["mentions"]}</span>'
            '</li>'
        )
    return (
        '<section class="companies">'
        '<div class="section-label"><span>Companies in the news</span><span class="dotline"></span></div>'
        f'<ol class="comp-list">{"".join(rows)}</ol>'
        '</section>'
    )


# ---------- Site writer ----------
def write_site(
    picks: list[dict],
    *,
    trending: list[str],
    companies: list[dict],
    stats: dict,
) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    nojekyll = DOCS_DIR / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("")

    today = datetime.now(IST)
    iso = today.strftime("%Y-%m-%d")
    human_long = today.strftime("%A, %B %d, %Y")
    human_short = today.strftime("%b %d, %Y")
    edition = _edition_label(today)
    intro = _intro_text(picks)

    past = _list_past_editions(DOCS_DIR, exclude_iso=iso)

    index_html = render_page(
        picks,
        date_human=human_long,
        date_iso=iso,
        edition=edition,
        intro=intro,
        trending=trending,
        companies=companies,
        stats=stats,
        past_editions=past,
        is_archive=False,
    )
    (DOCS_DIR / "index.html").write_text(index_html, encoding="utf-8")
    print(f"  wrote docs/index.html ({len(picks)} stories)")

    archive_html = render_page(
        picks,
        date_human=human_short,
        date_iso=iso,
        edition=edition,
        intro=intro,
        trending=trending,
        companies=companies,
        stats=stats,
        past_editions=None,
        is_archive=True,
    )
    archive_path = DOCS_DIR / f"{iso}.html"
    archive_path.write_text(archive_html, encoding="utf-8")
    print(f"  wrote {archive_path}")


def _edition_label(today: datetime) -> str:
    vol = today.year - 2025
    no = today.timetuple().tm_yday
    return f"Vol. {vol:02d} · No. {no:03d}"


def _intro_text(picks: list[dict]) -> str:
    if not picks:
        return "No edition today."
    india_n = sum(1 for p in picks if p.get("scope") == "national")
    global_n = len(picks) - india_n
    return (
        f"Today's pick: {len(picks)} stories from the last 24 hours, "
        f"{india_n} from India, {global_n} from elsewhere, ordered by editorial impact."
    )


def _greeting_for_hour(h: int) -> str:
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    if 17 <= h < 22:
        return "Good evening"
    return "Good night"


def _list_past_editions(docs_dir: pathlib.Path, exclude_iso: str) -> list[dict]:
    out: list[dict] = []
    for p in sorted(
        docs_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].html"),
        reverse=True,
    ):
        iso = p.stem
        if iso == exclude_iso:
            continue
        try:
            d = datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            continue
        out.append({"href": p.name, "iso": iso, "label": d.strftime("%b %d")})
        if len(out) >= ARCHIVE_KEEP:
            break
    return out


# =========================================================================
# Main
# =========================================================================
def main() -> None:
    print("Fetching…")
    articles = fetch_articles()
    print(f"  {len(articles)} fetched")

    print("Deduping…")
    uniques = dedupe(articles)
    print(f"  {len(uniques)} unique")

    if not uniques:
        print("No articles after dedupe. Leaving site untouched.")
        return

    print("Scoring…")
    scored = score_articles(uniques)
    print(f"  {len(scored)} scored (via {_last_llm_provider or 'none'})")

    print("Picking final…")
    picks = pick_final(scored)
    india_n = sum(1 for p in picks if _is_india(p))
    print(f"  {len(picks)} picked ({india_n} india, {len(picks) - india_n} global)")

    if not picks:
        print("No AI stories found. Leaving site untouched.")
        return

    print("Rewriting…")
    picks = rewrite(picks)

    print("Enriching…")
    picks = enrich_for_render(picks)

    print("Computing modules…")
    yesterday = load_yesterday_state()
    trending = compute_trending(scored)
    companies = compute_companies(scored)
    stats = compute_stats(picks, scored, yesterday)
    print(f"  {len(trending)} trending, {len(companies)} companies, top mover: {stats['top_mover']}")

    print("Writing site…")
    write_site(picks, trending=trending, companies=companies, stats=stats)
    save_today_state(stats)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        sys.exit(1)
