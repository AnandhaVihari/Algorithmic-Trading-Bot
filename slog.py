from datetime import datetime

LOG_FILE = "signals.log"


def slog(pair, frame, event, detail=""):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {pair:<10} | {frame:<5} | {event:<16} | {detail}"
    with open(LOG_FILE, "a", buffering=1, encoding="utf-8") as f:
        f.write(line + "\n")
