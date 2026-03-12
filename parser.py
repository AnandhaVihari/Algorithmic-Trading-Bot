from bs4 import BeautifulSoup
import re
from datetime import datetime, timezone


def parse_time(text):

    match = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} [AP]M UTC", text)

    if not match:
        return None

    t = datetime.strptime(match.group(), "%Y-%m-%d %I:%M %p UTC")

    return t.replace(tzinfo=timezone.utc)


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

        status = "ACTIVE"

        if "Close" in text or "Trailing Stop" in text or "Achieved" in text:
            status = "CLOSE"

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
        })

    return signals


def parse_signals(html):

    # The page uses two tab divs: class="data_1 tabs_listner" (15/30 min, short)
    # and class="data_2 tabs_listner" (1/4 hour, long).
    divider = 'class="data_2 tabs_listner"'
    idx = html.find(divider)

    if idx == -1:
        # fallback: old text divider
        idx = html.lower().find("1/4 hours chart")

    if idx == -1:
        print("WARN: section divider not found in page, treating all signals as short")
        return _parse_rows(html, "short")

    short_html = html[:idx]
    long_html  = html[idx:]

    return _parse_rows(short_html, "short") + _parse_rows(long_html, "long")
