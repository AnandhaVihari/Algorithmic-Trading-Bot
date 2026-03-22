from bs4 import BeautifulSoup
import re
from datetime import datetime, timezone, timedelta


def parse_time(text):
    """Parse time from signal row.

    CRITICAL: Use absolute UTC time only. Relative times ("24 mins ago") are
    UNRELIABLE across bot cycles because they produce different timestamps
    each time the bot runs.

    Handles:
    1. Absolute format: "2024-01-15 10:30 AM UTC" (RELIABLE)
    2. Falls back to relative if no absolute found (UNRELIABLE but better than current time)
    """

    # Try absolute timestamp FIRST - this is the ONLY reliable method
    match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [AP]M UTC", text)
    if match:
        t = datetime.strptime(match.group(), "%Y-%m-%d %I:%M %p UTC")
        return t.replace(tzinfo=timezone.utc)

    # FALLBACK: Handle relative times (not ideal but better than current time)
    # WARNING: This will produce different timestamps each bot cycle!
    # The website SHOULD provide absolute UTC time.
    relative_match = re.search(r"(\d+)\s+(min|hour|day)s?\s+ago", text, re.IGNORECASE)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        now = datetime.now(timezone.utc)

        if unit == "min":
            return now - timedelta(minutes=amount)
        elif unit == "hour":
            return now - timedelta(hours=amount)
        elif unit == "day":
            return now - timedelta(days=amount)

    # FALLBACK: use current time if we can't parse
    # THIS IS A SIGN THE WEBSITE FORMAT HAS CHANGED!
    import sys
    print("[WARNING] Could not parse timestamp from signal. Using current time as fallback.", file=sys.stderr)
    return datetime.now(timezone.utc)



def _parse_rows(html, frame):
    """Parse signal rows from an HTML chunk and tag them with the given frame."""

    soup = BeautifulSoup(html, "html.parser")
    signals = []

    for row in soup.find_all("tr"):

        text = row.get_text(" ", strip=True)

        if "Open:" not in text:
            continue

        pair = text.split()[0].replace("/", "")

        open_price = re.search(r"Open:\s*([\d\.]+)", text)
        tp = re.search(r"TP:\s*([\d\.]+)", text)
        sl = re.search(r"SL\s*:\s*([\d\.]+)", text)

        signal_time = parse_time(text)

        if not open_price or not tp or not sl:
            continue

        # Check if this trade has a close price (is closed)
        close_price_match = re.search(r"Close:\s*([\d\.]+)", text)

        if close_price_match:
            # This is a CLOSE signal - extract it
            status = "CLOSE"
            close_price = float(close_price_match.group(1))

            # Detect close reason
            if "Achieved" in text:
                close_reason = "Achieved"
            elif "Trailing Stop" in text or "trailing" in text.lower():
                close_reason = "Trailing Stop"
            else:
                close_reason = "Manual"
        else:
            # This is an ACTIVE signal with no close yet
            status = "ACTIVE"
            close_price = None
            close_reason = None

        side = "BUY" if "Buy" in text else "SELL"

        open_val = float(open_price.group(1))
        tp_val = float(tp.group(1))
        sl_val = float(sl.group(1))

        # VALIDATION: Check SL makes sense
        if sl_val == open_val:
            import sys
            print(f"[WARN] {pair} {side}: SL ({sl_val}) = Entry ({open_val}) - INVALID! Skipping.", file=sys.stderr)
            continue

        if side == "BUY" and sl_val > open_val:
            # BUY: SL should be BELOW entry
            import sys
            print(f"[WARN] {pair} BUY: SL ({sl_val}) > Entry ({open_val}) - Swapping with TP", file=sys.stderr)
            sl_val, tp_val = tp_val, sl_val

        if side == "SELL" and sl_val < open_val:
            # SELL: SL should be ABOVE entry
            import sys
            print(f"[WARN] {pair} SELL: SL ({sl_val}) < Entry ({open_val}) - Swapping with TP", file=sys.stderr)
            sl_val, tp_val = tp_val, sl_val

        signals.append({
            "pair":   pair,
            "side":   side,
            "open":   open_val,
            "tp":     tp_val,
            "sl":     sl_val,
            "time":   signal_time,
            "status": status,
            "frame":  frame,
            "close":  close_price,
            "close_reason": close_reason,
        })

    return signals


def parse_signals(html):
    """Parse all signals and tag them by timeframe.

    Divider: "Given Signals are from 15 minute and 30 minute time frame charts"
    - Before divider: 15/30 min signals (short frame)
    - After divider: 1/4 hour signals (long frame)
    """

    # The exact divider text from the website
    divider = "Given Signals are from 15 minute and 30 minute time frame charts"
    idx = html.find(divider)

    if idx == -1:
        # Fallback options if exact text not found
        idx = html.lower().find("1/4 hours chart")
        if idx == -1:
            idx = html.lower().find("1/4 hour")
            if idx == -1:
                print("WARN: section divider not found in page, treating all signals as short")
                return _parse_rows(html, "short")

    short_html = html[:idx]
    long_html  = html[idx:]

    return _parse_rows(short_html, "short") + _parse_rows(long_html, "long")
