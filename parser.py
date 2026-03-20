from bs4 import BeautifulSoup
import re
from datetime import datetime, timezone, timedelta


def parse_time(text):
    """Parse time from signal row. Handles:
    1. Absolute format: "2024-01-15 10:30 AM UTC"
    2. Relative format: "31 mins ago", "9 hours ago", "1 day ago", etc.
    """

    # Try absolute timestamp first
    match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [AP]M UTC", text)
    if match:
        t = datetime.strptime(match.group(), "%Y-%m-%d %I:%M %p UTC")
        return t.replace(tzinfo=timezone.utc)

    # Handle relative times: "31 mins ago", "9 hours ago", "1 day ago", etc.
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

    # Fallback: use current time if we can't parse
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
        else:
            # This is an ACTIVE signal with no close yet
            status = "ACTIVE"
            close_price = None

        side = "BUY" if "Buy" in text else "SELL"

        signals.append({
            "pair":   pair,
            "side":   side,
            "open":   float(open_price.group(1)),
            "tp":     float(tp.group(1)),
            "sl":     float(sl.group(1)),
            "time":   signal_time,
            "status": status,
            "frame":  frame,
            "close":  close_price,
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
