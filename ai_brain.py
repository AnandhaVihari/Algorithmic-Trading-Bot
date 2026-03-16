"""
ai_brain.py — Claude-powered autonomous trade decision layer (Phase 3)

Calls the Anthropic API every AI_BRAIN_INTERVAL seconds with:
  • Pending signals (pair + reversal direction)
  • News sentiment per currency
  • Upcoming economic calendar events
  • Recent account P&L and open position summary

Claude returns structured JSON deciding which signals to TAKE, SKIP, or WAIT.
The response is cached for AI_BRAIN_INTERVAL seconds so API costs stay low.

Set ANTHROPIC_API_KEY in config.py to enable.
"""

import json
import time
import threading
from datetime import datetime, timezone

# ── lazy imports so the module loads even without anthropic installed ──────────
_anthropic = None

def _get_client(api_key):
    global _anthropic
    if _anthropic is None:
        try:
            import anthropic
            _anthropic = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            print("AI BRAIN: 'anthropic' package not installed — run: pip install anthropic")
            return None
    return _anthropic


# ── cached decision state ─────────────────────────────────────────────────────
_decision_cache  = {}    # pair → {action, confidence, reason}
_cache_timestamp = None
_lock            = threading.Lock()

# ── prompt template ───────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are a disciplined forex trading analyst for a contrarian signal-fading bot.
The bot reverses signals from a consistently wrong signal source (BUY→SELL, SELL→BUY).
Account is small ($20–$50 demo), trading 0.01 lots. Max loss per trade: $0.60.

Your job: for each pending signal, decide TAKE, SKIP, or WAIT based on:
- News sentiment (should align with our reversal direction)
- Calendar risk (avoid high-impact events)
- Market context and account state

Respond ONLY with valid JSON in this exact format:
{
  "decisions": {
    "EURUSD": {"action": "TAKE", "confidence": 0.8, "reason": "..."},
    "GBPJPY": {"action": "SKIP", "confidence": 0.9, "reason": "..."}
  },
  "market_mode": "normal",
  "notes": "brief overall market observation"
}

action must be one of: TAKE, SKIP, WAIT
market_mode must be one of: normal, cautious, aggressive, pause
confidence: 0.0 to 1.0
"""


def _build_prompt(pending_signals, news_sentiment, calendar_events,
                  open_positions, recent_pnl):
    """Build the user message sent to Claude."""
    lines = []

    lines.append("=== PENDING SIGNALS (reversal direction) ===")
    if pending_signals:
        for s in pending_signals:
            lines.append(f"  {s['pair']:<12} -> {s['direction']}")
    else:
        lines.append("  (none)")

    lines.append("\n=== NEWS SENTIMENT (last 2h) ===")
    for ccy, score in news_sentiment.items():
        dirn = "BULLISH" if score > 0.15 else ("BEARISH" if score < -0.15 else "neutral")
        lines.append(f"  {ccy}: {score:+.2f} ({dirn})")

    lines.append("\n=== UPCOMING HIGH-IMPACT EVENTS (next 45min) ===")
    if calendar_events:
        for ev in calendar_events:
            mins = int((ev["utc_time"] - datetime.now(timezone.utc))
                       .total_seconds() / 60)
            lines.append(f"  {ev['country']:<4} {ev['title']:<35} in {mins}min")
    else:
        lines.append("  (none in next 45 min)")

    lines.append("\n=== OPEN POSITIONS ===")
    if open_positions:
        for p in open_positions:
            lines.append(f"  {p['pair']:<12} {p['side']:<5} pnl=${p['pnl']:+.2f}")
    else:
        lines.append("  (none)")

    lines.append(f"\n=== ACCOUNT STATS ===")
    lines.append(f"  Recent P&L (last 10 trades): ${recent_pnl.get('total', 0):+.2f}")
    lines.append(f"  Win rate (last 10):          {recent_pnl.get('win_rate', 0):.0f}%")
    lines.append(f"  Open count:                  {recent_pnl.get('open_count', 0)}")

    return "\n".join(lines)


# ── public API ────────────────────────────────────────────────────────────────

def get_decision(pair):
    """
    Return cached decision dict for *pair*, or default TAKE with low confidence.
    Keys: action ('TAKE'/'SKIP'/'WAIT'), confidence (float), reason (str).
    """
    with _lock:
        return _decision_cache.get(pair.upper().replace("+", ""),
                                   {"action": "TAKE", "confidence": 0.5,
                                    "reason": "no AI decision cached"})


def get_market_mode():
    """Return overall market mode string ('normal'/'cautious'/'aggressive'/'pause')."""
    with _lock:
        return _decision_cache.get("__market_mode__", "normal")


def is_cache_fresh(interval_secs):
    """True if cached decision is still within *interval_secs*."""
    if _cache_timestamp is None:
        return False
    return (time.time() - _cache_timestamp) < interval_secs


def refresh_decisions(api_key, pending_signals, open_positions, recent_pnl):
    """
    Call Claude API, update the decision cache.
    Should be called from a background thread every AI_BRAIN_INTERVAL seconds.

    pending_signals: list of {'pair': str, 'direction': 'BUY'|'SELL'}
    open_positions:  list of {'pair': str, 'side': str, 'pnl': float}
    recent_pnl:      dict  {'total': float, 'win_rate': float, 'open_count': int}
    """
    global _cache_timestamp

    client = _get_client(api_key)
    if client is None:
        return

    # collect news + calendar context
    try:
        from news_watcher import get_sentiment
        from calendar_watcher import _fetch_calendar, upcoming_events, pair_currencies

        majors = ["USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF"]
        news_sentiment = {ccy: get_sentiment(ccy) for ccy in majors}

        cal_events = []
        seen_pairs = set()
        for s in pending_signals:
            for ccy in pair_currencies(s["pair"]):
                if ccy not in seen_pairs:
                    seen_pairs.add(ccy)
                    cal_events.extend(upcoming_events(ccy, 45))
    except Exception as e:
        print(f"AI BRAIN context error: {e}")
        news_sentiment = {}
        cal_events     = []

    prompt = _build_prompt(pending_signals, news_sentiment, cal_events,
                           open_positions, recent_pnl)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()

        # extract JSON even if Claude added extra text
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        data  = json.loads(raw[start:end])

        with _lock:
            _decision_cache.clear()
            for pair, dec in data.get("decisions", {}).items():
                _decision_cache[pair.upper()] = dec
            _decision_cache["__market_mode__"] = data.get("market_mode", "normal")
            _decision_cache["__notes__"]        = data.get("notes", "")
        _cache_timestamp = time.time()

        mode  = data.get("market_mode", "normal")
        notes = data.get("notes", "")
        print(f"AI BRAIN: mode={mode}  {notes}")
        for pair, dec in data.get("decisions", {}).items():
            print(f"  {pair:<12} {dec['action']:<5} ({dec['confidence']:.0%}) — {dec['reason']}")

    except json.JSONDecodeError as e:
        print(f"AI BRAIN JSON parse error: {e}\nRaw: {raw[:200]}")
    except Exception as e:
        print(f"AI BRAIN API error: {e}")


def ai_decision_summary():
    """Print current cached decisions."""
    with _lock:
        mode  = _decision_cache.get("__market_mode__", "unknown")
        notes = _decision_cache.get("__notes__", "")
        age   = int(time.time() - _cache_timestamp) if _cache_timestamp else -1
        print(f"\n── AI BRAIN: mode={mode}  age={age}s  {notes}")
        for pair, dec in _decision_cache.items():
            if pair.startswith("__"):
                continue
            print(f"  {pair:<12} {dec['action']:<5} ({dec.get('confidence',0):.0%})"
                  f"  {dec.get('reason','')}")
        print()
