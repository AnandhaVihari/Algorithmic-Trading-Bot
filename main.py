import time
import sys
import threading
from datetime import datetime, timezone, timedelta

# log to file (required when running with pythonw)
sys.stdout = open("bot.log", "a", buffering=1, encoding="utf-8")
sys.stderr = sys.stdout

from scraper import fetch_page
from parser import parse_signals
from trader import (
    open_trade,
    close_trade,
    get_position,
    manage_positions,
    profit_guard,
    init_mt5,
    show_open_positions,
    show_recent_history,
    account_summary
)

from state import processed_signals
from config import *
from slog import slog


init_mt5()

CLEANUP_AGE   = timedelta(hours=24)
mt5_lock      = threading.Lock()
_cycle_count  = 0   # used to throttle balance.log writes

# ── website signal tracker ─────────────────────────────────────────────────────
# tracks which signals are currently on the website (key = sig_id)
# used to detect APPEARED / DISAPPEARED / REAPPEARED / STATUS CHANGE

_website_now  = {}   # sig_id → signal dict  (currently visible on website)
_website_ever = set()  # sig_ids ever seen    (so we can log REAPPEARED vs APPEARED)


def _sig_id(s):
    return f"{s['pair']}_{s['time']}_{s['side']}_{s['frame']}"


def update_signal_tracking(signals):
    current_ids = set()

    for s in signals:
        sid = _sig_id(s)
        current_ids.add(sid)

        if sid not in _website_now:
            # not currently tracked — appeared or reappeared
            event = "REAPPEARED" if sid in _website_ever else "APPEARED"
            slog(s["pair"], s["frame"], event,
                 f"{s['status']} {s['side']} open:{s['open']} tp:{s['tp']} sl:{s['sl']}")
            _website_now[sid] = s
            _website_ever.add(sid)
        else:
            prev = _website_now[sid]

            # status change (ACTIVE → CLOSE etc.)
            if s["status"] != prev["status"]:
                slog(s["pair"], s["frame"], "STATUS CHANGE",
                     f"{prev['status']} -> {s['status']}")
                _website_now[sid] = s

            # data change — provider edited TP / SL / open price
            if s["tp"] != prev["tp"] or s["sl"] != prev["sl"] or s["open"] != prev["open"]:
                slog(s["pair"], s["frame"], "MODIFIED",
                     f"open:{prev['open']}->{s['open']}  tp:{prev['tp']}->{s['tp']}  sl:{prev['sl']}->{s['sl']}")
                _website_now[sid] = s
                # remove from processed_signals so the bot retries with the new data
                if sid in processed_signals:
                    del processed_signals[sid]
                    slog(s["pair"], s["frame"], "RETRY QUEUED", "will re-attempt with updated data")

    # detect signals that were on the website but are now gone
    for sid in list(_website_now.keys()):
        if sid not in current_ids:
            s = _website_now.pop(sid)
            slog(s["pair"], s["frame"], "DISAPPEARED",
                 f"{s['status']} {s['side']} open:{s['open']} tp:{s['tp']} sl:{s['sl']}")


def prune_processed_signals():
    """Remove signal IDs older than 24 hours to keep memory bounded."""
    cutoff = datetime.now(timezone.utc) - CLEANUP_AGE
    stale  = [k for k, v in processed_signals.items() if v < cutoff]
    for k in stale:
        del processed_signals[k]
    if stale:
        print(f"CLEANUP: removed {len(stale)} old signal IDs")


# ── signal processing ─────────────────────────────────────────────────────────

def run_signal_cycle():
    """Fetch signals, analyse confluence across timeframes, then execute."""

    html    = fetch_page()
    signals = parse_signals(html)

    update_signal_tracking(signals)
    prune_processed_signals()

    with mt5_lock:

        now = datetime.now(timezone.utc)

        # ── STEP 1: build full website picture ────────────────────────────────
        # website_sides[pair] = set of sides currently ACTIVE on website (all frames)
        # This is used for confluence: we want to know what EVERY frame says right now,
        # including signals already processed in previous cycles.
        website_sides  = {}   # pair → set of sides shown as ACTIVE
        website_frames = {}   # pair → set of frames that have an ACTIVE signal

        for s in signals:
            if s["status"] == "ACTIVE":
                pair = s["pair"]
                website_sides.setdefault(pair, set()).add(s["side"])
                website_frames.setdefault(pair, set()).add(s["frame"])

        # ── STEP 2: collect fresh unprocessed ACTIVE signals ──────────────────
        active_by_pair = {}   # pair → [fresh unprocessed signal, ...]
        active_pairs   = set()
        close_signals  = []

        for s in signals:
            pair  = s["pair"]
            frame = s["frame"]

            if TRADE_FRAME != "both" and frame != TRADE_FRAME:
                continue

            if s["status"] == "ACTIVE":
                active_pairs.add(pair)
                signal_id = f"{pair}_{s['time']}_{s['side']}_{frame}"

                if signal_id in processed_signals:
                    continue

                if s["time"] is not None:
                    age_min = (now - s["time"]).total_seconds() / 60
                    if age_min > 15:
                        processed_signals[signal_id] = now
                        slog(pair, frame, "SKIP OLD", f"{age_min:.0f} min old")
                        continue

                active_by_pair.setdefault(pair, []).append(s)

            elif s["status"] == "CLOSE":
                close_signals.append(s)

        # ── STEP 3: analyse — confluence filter ───────────────────────────────
        to_open  = []
        to_close = []

        for pair, sigs in active_by_pair.items():

            # if website currently shows both BUY and SELL for this pair → skip
            if len(website_sides.get(pair, set())) > 1:
                for s in sigs:
                    processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                slog(pair, sigs[0]["frame"], "SKIP CONFLICT",
                     "website shows BUY+SELL across frames — not trading")
                continue

            side = list(website_sides.get(pair, {"??"}))[0]

            # confluence check: when trading both frames, require BOTH to agree
            if TRADE_FRAME == "both":
                frames_active = website_frames.get(pair, set())
                has_short = "short" in frames_active
                has_long  = "long"  in frames_active
                if not (has_short and has_long):
                    # only one timeframe visible — wait for the other to confirm
                    only = "short" if has_short else "long"
                    slog(pair, only, "WAIT CONF",
                         f"only {only} frame active — waiting for other TF to confirm")
                    continue   # do NOT mark processed — retry next cycle

            # both frames agree (or single-frame mode) — pick most recent signal
            best = max(sigs, key=lambda x: x["time"] or now)
            frames_str = "+".join(sorted(website_frames.get(pair, set())))
            slog(pair, best["frame"], "CONFLUENT", f"{side} confirmed on [{frames_str}]")
            to_open.append((best, sigs))

        for s in close_signals:
            if s["pair"] not in active_pairs:
                to_close.append(s)

        # ── STEP 4: execute closes first ──────────────────────────────────────
        closed_this_cycle = set()

        for s in to_close:
            pair = s["pair"]
            pos  = get_position(pair)
            if pos:
                slog(pair, s["frame"], "WEBSITE CLOSE", "closing per signal")
                close_trade(pair)
                closed_this_cycle.add(pair)

        # ── STEP 5: execute opens ─────────────────────────────────────────────
        just_opened = set()

        for best, all_sigs in to_open:
            pair = best["pair"]

            existing = get_position(pair)
            if existing:
                pos_side = "BUY" if existing.type == 0 else "SELL"
                if pos_side == best["side"]:
                    # already open in correct direction — mark all processed
                    for s in all_sigs:
                        processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                    continue

                # opposite direction open — only reverse if BOTH frames confirm new side
                frames_active = website_frames.get(pair, set())
                if TRADE_FRAME == "both" and len(frames_active) < 2:
                    # single-frame reversal signal — close existing but don't flip
                    print(f"CLOSE ONLY: {pair} — single frame reversal, closing {pos_side} without opening {best['side']}")
                    slog(pair, best["frame"], "CLOSE ONLY",
                         f"single TF says {best['side']} — closing {pos_side}, not reversing")
                    close_trade(pair)
                    for s in all_sigs:
                        processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                    closed_this_cycle.add(pair)
                    continue

                # both frames confirm reversal — close and flip
                print(f"REVERSE: {pair} {pos_side} -> {best['side']} (both TF confirmed)")
                slog(pair, best["frame"], "REVERSAL",
                     f"closing {pos_side} -> opening {best['side']}")
                close_trade(pair)
                time.sleep(0.5)

            print("\nNEW SIGNAL:", best)
            if open_trade(best):
                for s in all_sigs:
                    processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                just_opened.add(pair)

        # ── status ────────────────────────────────────────────────────────────
        print("\nBOT ALIVE:", datetime.now().strftime("%H:%M:%S"))
        show_open_positions()
        show_recent_history()

        global _cycle_count
        _cycle_count += 1
        trades_changed = bool(just_opened or closed_this_cycle)
        account_summary(save=trades_changed or _cycle_count % 10 == 0)


def signal_thread():
    while True:
        try:
            run_signal_cycle()
        except Exception as e:
            print("SIGNAL ERROR:", e)
        time.sleep(SIGNAL_INTERVAL)


# ── position management thread ────────────────────────────────────────────────

def position_thread():
    while True:
        try:
            with mt5_lock:
                manage_positions()
        except Exception as e:
            print("POSITION ERROR:", e)
        time.sleep(POSITION_INTERVAL)


# ── profit guard thread (every 1 second) ──────────────────────────────────────

def guard_thread():
    while True:
        try:
            with mt5_lock:
                profit_guard()
        except Exception as e:
            print("GUARD ERROR:", e)
        time.sleep(GUARD_INTERVAL)


# ── start ─────────────────────────────────────────────────────────────────────

threading.Thread(target=signal_thread,   daemon=True).start()
threading.Thread(target=position_thread, daemon=True).start()
threading.Thread(target=guard_thread,    daemon=True).start()

# keep main thread alive
while True:
    time.sleep(60)
