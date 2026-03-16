"""
news_watcher.py — Forex news RSS reader + per-currency sentiment scorer

Pulls headlines from multiple free RSS feeds every NEWS_REFRESH_SECS seconds.
Scores each headline with a simple keyword model and accumulates a sentiment
score per currency over a rolling 2-hour window.

Score range: -1.0 (strongly bearish) … +1.0 (strongly bullish) per currency.

Feeds used (all public, no auth):
  - ForexLive       https://www.forexlive.com/feed/news
  - FXStreet        https://www.fxstreet.com/rss/news
  - DailyFX         https://www.dailyfx.com/feeds/all
  - Investing.com   https://www.investing.com/rss/news_25.rss
"""

import re
import time
import threading
import requests
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET

# ── config ─────────────────────────────────────────────────────────────────────
NEWS_REFRESH_SECS = 300   # fetch new headlines every 5 minutes
NEWS_WINDOW_HOURS = 2     # only score headlines from the last 2 hours
NEWS_MIN_HEADLINE = 6     # minimum keyword hits needed to register a signal

_FEEDS = [
    "https://www.forexlive.com/feed/news",
    "https://www.fxstreet.com/rss/news",
    "https://www.dailyfx.com/feeds/all",
    "https://www.investing.com/rss/news_25.rss",
]

# ── keyword tables ──────────────────────────────────────────────────────────────
_BULLISH_WORDS = {
    "strong", "surge", "beat", "beats", "better", "above", "hawkish",
    "hike", "hikes", "raise", "raised", "growth", "robust", "outperform",
    "rally", "rallies", "gain", "gains", "jumped", "solid", "upbeat",
    "optimistic", "recovery", "resilient", "accelerat", "positive",
    "exceed", "exceeds", "unexpected rise", "higher than", "tightening",
}

_BEARISH_WORDS = {
    "weak", "drop", "miss", "misses", "below", "dovish", "cut", "cuts",
    "reduce", "reduced", "slow", "slowdown", "decline", "slump", "fell",
    "fall", "recession", "contraction", "disappoint", "negative",
    "worsen", "risk", "concern", "unexpected fall", "lower than",
    "easing", "stimulus", "soften",
}

# keywords that link a headline to a currency
_CURRENCY_KEYWORDS = {
    "USD": ["usd", "dollar", "federal reserve", "fed ", "fomc", "powell",
            "us inflation", "us gdp", "nonfarm", "nfp", "us jobs",
            "us economy", "united states", "america"],
    "EUR": ["eur", "euro", "ecb", "lagarde", "eurozone", "european central",
            "eu inflation", "germany", "france", "euro area"],
    "GBP": ["gbp", "pound", "sterling", "boe", "bank of england", "bailey",
            "uk inflation", "uk gdp", "britain", "united kingdom"],
    "JPY": ["jpy", "yen", "boj", "bank of japan", "ueda", "kuroda",
            "japan inflation", "japan gdp", "japanese"],
    "CAD": ["cad", "canadian dollar", "boc", "bank of canada", "macklem",
            "canada inflation", "canada gdp", "oil price", "crude"],
    "AUD": ["aud", "aussie", "rba", "reserve bank of australia", "lowe",
            "australia inflation", "australia gdp", "iron ore"],
    "NZD": ["nzd", "kiwi", "rbnz", "reserve bank of new zealand",
            "new zealand inflation", "new zealand gdp"],
    "CHF": ["chf", "franc", "snb", "swiss national bank", "jordan",
            "switzerland inflation", "swiss economy"],
}

# ── internal state ──────────────────────────────────────────────────────────────
_headlines    = []           # list of (utc_time, title, score_dict)
_last_refresh = None
_lock         = threading.Lock()


# ── helpers ─────────────────────────────────────────────────────────────────────

def _fetch_feed(url):
    """Fetch one RSS feed, return list of (pub_date_utc, title) tuples."""
    try:
        r = requests.get(url, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = root.findall(".//item")
        out   = []
        for item in items:
            title_el = item.find("title")
            date_el  = item.find("pubDate")
            if title_el is None:
                continue
            title = (title_el.text or "").strip()
            # best-effort date parse
            pub = _parse_rss_date(date_el.text if date_el is not None else "")
            out.append((pub, title))
        return out
    except Exception as e:
        print(f"NEWS FEED ERROR ({url}): {e}")
        return []


def _parse_rss_date(s):
    """Return UTC datetime from RSS pubDate string, or 'now' on failure."""
    if not s:
        return datetime.now(timezone.utc)
    # RFC-2822: "Mon, 15 Apr 2024 10:30:00 +0000"
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            return datetime.strptime(s.strip(), fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _score_headline(title):
    """
    Return dict {currency: float} for all currencies mentioned.
    Score = (bullish_hits - bearish_hits) normalised to ±1.
    """
    low    = title.lower()
    bull   = sum(1 for w in _BULLISH_WORDS if w in low)
    bear   = sum(1 for w in _BEARISH_WORDS if w in low)
    raw    = bull - bear
    if raw == 0 or (bull + bear) < 1:
        return {}

    # which currencies does this headline mention?
    currencies_hit = []
    for ccy, kws in _CURRENCY_KEYWORDS.items():
        if any(kw in low for kw in kws):
            currencies_hit.append(ccy)

    if not currencies_hit:
        return {}

    # normalise to ±1
    magnitude = min(abs(raw) / 3, 1.0) * (1 if raw > 0 else -1)
    return {ccy: magnitude for ccy in currencies_hit}


def _refresh():
    """Re-fetch all feeds and rebuild headline list."""
    global _headlines, _last_refresh
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=NEWS_WINDOW_HOURS)
    fresh  = []
    total  = 0

    for url in _FEEDS:
        for pub, title in _fetch_feed(url):
            if pub < cutoff:
                continue
            scores = _score_headline(title)
            fresh.append((pub, title, scores))
            total += 1

    # deduplicate by title (different feeds carry same headline)
    seen  = set()
    dedup = []
    for pub, title, scores in sorted(fresh, key=lambda x: x[0], reverse=True):
        key = re.sub(r"\W+", "", title.lower())[:60]
        if key not in seen:
            seen.add(key)
            dedup.append((pub, title, scores))

    with _lock:
        _headlines    = dedup
        _last_refresh = now

    print(f"NEWS: refreshed — {len(dedup)} unique headlines ({total} raw)")


def _maybe_refresh():
    global _last_refresh
    now = datetime.now(timezone.utc)
    if (_last_refresh is None or
            (now - _last_refresh).total_seconds() >= NEWS_REFRESH_SECS):
        _refresh()


# ── public API ──────────────────────────────────────────────────────────────────

def get_sentiment(currency):
    """
    Return a sentiment score in [-1.0, +1.0] for *currency* based on
    headlines from the last NEWS_WINDOW_HOURS hours.

    > 0  = bullish bias for that currency (bad for our SELL reversal trades)
    < 0  = bearish bias (bad for our BUY reversal trades)
    ~ 0  = neutral / no clear signal
    """
    _maybe_refresh()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_WINDOW_HOURS)
    total  = 0.0
    count  = 0
    with _lock:
        for pub, _, scores in _headlines:
            if pub < cutoff:
                continue
            if currency in scores:
                total += scores[currency]
                count += 1
    if count == 0:
        return 0.0
    # average, capped at ±1
    return max(-1.0, min(1.0, total / count))


def pair_sentiment(pair):
    """
    Return (base_score, quote_score) for a pair like 'EURUSD+'.
    Base bullish + quote bearish = good for BUY trade on the pair.
    """
    from calendar_watcher import pair_currencies
    ccys = pair_currencies(pair)
    if len(ccys) < 2:
        return 0.0, 0.0
    return get_sentiment(ccys[0]), get_sentiment(ccys[1])


def news_confirms_direction(pair, direction, threshold=0.25):
    """
    Returns (confirms: bool, reason: str).
    Checks if recent news sentiment is aligned with *direction* ('BUY'/'SELL').

    BUY on EURUSD  → good if EUR bullish OR USD bearish
    SELL on EURUSD → good if EUR bearish OR USD bullish
    """
    base_s, quote_s = pair_sentiment(pair)
    if direction == "BUY":
        score = base_s - quote_s   # want base up, quote down
    else:
        score = quote_s - base_s   # want quote up, base down

    from calendar_watcher import pair_currencies
    ccys = pair_currencies(pair)
    reason = (f"news sentiment: {ccys[0] if ccys else '?'}={base_s:+.2f} "
              f"{ccys[1] if len(ccys)>1 else '?'}={quote_s:+.2f} "
              f"→ {direction} score={score:+.2f}")

    if score < -threshold:
        return False, f"news AGAINST {direction} — {reason}"
    return True, reason


def news_summary():
    """Print current sentiment for all major currencies."""
    _maybe_refresh()
    majors = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF"]
    print("\n── NEWS SENTIMENT ──────────────────────────────────────────────")
    for ccy in majors:
        score = get_sentiment(ccy)
        bar   = "█" * int(abs(score) * 10)
        dirn  = "BULL" if score > 0 else ("BEAR" if score < 0 else "NEUT")
        print(f"  {ccy}  {dirn}  {score:+.2f}  {bar}")
    print()
