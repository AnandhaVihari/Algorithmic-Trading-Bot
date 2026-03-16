"""
calendar_watcher.py — Economic calendar monitor

Scrapes the ForexFactory public JSON calendar.
Blocks new trade entries 30 min before high-impact events and resumes
15 min after, per currency.

Public endpoints (no auth required):
  https://nfs.faireconomy.media/ff_calendar_thisweek.json
  https://nfs.faireconomy.media/ff_calendar_nextweek.json
"""

import requests
from datetime import datetime, timezone, timedelta

_THIS_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_NEXT_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

_cache_events   = []
_cache_time     = None
_CACHE_TTL_SECS = 3600   # refresh once per hour

# ── helpers ────────────────────────────────────────────────────────────────────

def _fetch_calendar():
    global _cache_events, _cache_time
    now = datetime.now(timezone.utc)
    if _cache_time and (now - _cache_time).total_seconds() < _CACHE_TTL_SECS:
        return _cache_events
    events = []
    for url in (_THIS_WEEK_URL, _NEXT_WEEK_URL):
        try:
            r = requests.get(url, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                events.extend(r.json())
        except Exception as e:
            print(f"CALENDAR FETCH ERROR ({url}): {e}")
    _cache_events = events
    _cache_time   = now
    print(f"CALENDAR: loaded {len(events)} events")
    return events


def _parse_time(ev):
    """Return event UTC datetime or None."""
    raw = ev.get("date", "")
    if not raw:
        return None
    try:
        # format: "2024-03-15T08:30:00+00:00"
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except Exception:
        return None


def pair_currencies(pair):
    """'EURUSD+' → ['EUR', 'USD']"""
    p = pair.upper().replace("+", "").strip()
    if len(p) >= 6:
        return [p[:3], p[3:6]]
    return []


# ── public API ─────────────────────────────────────────────────────────────────

def upcoming_events(currency, minutes_ahead=60):
    """
    Return list of HIGH-impact events for *currency* in the next
    *minutes_ahead* minutes.  Each item is the raw calendar dict plus
    an extra 'utc_time' key.
    """
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=minutes_ahead)
    out    = []
    for ev in _fetch_calendar():
        if ev.get("impact", "").lower() != "high":
            continue
        if ev.get("country", "").upper() != currency.upper():
            continue
        t = _parse_time(ev)
        if t and now <= t <= cutoff:
            ev["utc_time"] = t
            out.append(ev)
    return out


def recent_events(currency, minutes_past=15):
    """
    Return HIGH-impact events that fired in the last *minutes_past* minutes.
    Used to decide when to resume trading after an event.
    """
    now  = datetime.now(timezone.utc)
    floor = now - timedelta(minutes=minutes_past)
    out  = []
    for ev in _fetch_calendar():
        if ev.get("impact", "").lower() != "high":
            continue
        if ev.get("country", "").upper() != currency.upper():
            continue
        t = _parse_time(ev)
        if t and floor <= t <= now:
            ev["utc_time"] = t
            out.append(ev)
    return out


def should_pause(pair, pause_mins=30, resume_mins=15):
    """
    Returns (pause: bool, reason: str).

    Pause = True when a high-impact event is coming within *pause_mins*
    minutes OR fired within the last *resume_mins* minutes for either
    currency in *pair*.
    """
    for ccy in pair_currencies(pair):
        ahead = upcoming_events(ccy, pause_mins)
        if ahead:
            names = [e.get("title", "event") for e in ahead]
            mins_to = int((ahead[0]["utc_time"] - datetime.now(timezone.utc))
                          .total_seconds() / 60)
            return True, f"{ccy} {', '.join(names)} in {mins_to}min"

        recent = recent_events(ccy, resume_mins)
        if recent:
            names = [e.get("title", "event") for e in recent]
            return True, f"{ccy} {', '.join(names)} just fired — cooldown"

    return False, ""


def calendar_summary():
    """Print next 24h high-impact events for the major currencies."""
    majors = ["USD", "EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "NZD"]
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    found  = []
    for ev in _fetch_calendar():
        if ev.get("impact", "").lower() != "high":
            continue
        if ev.get("country", "").upper() not in majors:
            continue
        t = _parse_time(ev)
        if t and now <= t <= cutoff:
            ev["utc_time"] = t
            found.append(ev)
    found.sort(key=lambda e: e["utc_time"])
    print(f"\n── CALENDAR: next 24h high-impact events ({'none' if not found else len(found)}) ──")
    for ev in found:
        local = ev["utc_time"].strftime("%m-%d %H:%M UTC")
        print(f"  {local}  {ev['country']:<4}  {ev['title']}")
    print()
