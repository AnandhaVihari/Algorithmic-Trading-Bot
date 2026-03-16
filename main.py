import time
import sys
import threading
import MetaTrader5 as mt5
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
    active_brain,
    init_mt5,
    show_open_positions,
    show_recent_history,
    account_summary
)

from state import processed_signals
from config import *
from slog import slog

# ── autonomous agent modules ───────────────────────────────────────────────────
from calendar_watcher import should_pause, calendar_summary
from news_watcher     import news_confirms_direction, news_summary
from ai_brain         import (get_decision, get_market_mode, is_cache_fresh,
                               refresh_decisions, ai_decision_summary)

MAGIC_BY_FRAME = {"short": MAGIC_SHORT, "long": MAGIC_LONG}

init_mt5()

CLEANUP_AGE            = timedelta(hours=24)
mt5_lock               = threading.Lock()
_cycle_count           = 0   # used to throttle balance.log writes
_pending_signals_queue = []  # pending signals passed to AI brain thread each cycle

# ── website signal tracker ─────────────────────────────────────────────────────
# tracks which signals are currently on the website (key = sig_id)
# used to detect APPEARED / DISAPPEARED / REAPPEARED / STATUS CHANGE

_website_now  = {}   # sig_id → signal dict  (currently visible on website)
_website_ever      = set()  # sig_ids ever seen    (so we can log REAPPEARED vs APPEARED)
_close_ignored_ids = set()  # close signal ids already logged as IGNORED (suppress repeats)


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
                # ignore stale signals (>1 day old) so they don't cause false conflicts
                if s["time"] is not None:
                    age_min = (now - s["time"]).total_seconds() / 60
                    if age_min > 1440:
                        continue
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

        # ── STEP 3: analyse + autonomous filters ──────────────────────────────
        to_open  = []
        to_close = []

        # block new entries outside market hours (fri 21:00+ UTC, sat, sun before 21:00 UTC)
        market_open = not (
            (now.weekday() == 4 and now.hour >= 21) or
             now.weekday() == 5 or
            (now.weekday() == 6 and now.hour < 21)
        )

        # collect pending signals for AI brain context
        pending_for_ai = []

        for pair, sigs in active_by_pair.items():

            # if website currently shows both BUY and SELL for this pair → skip
            if len(website_sides.get(pair, set())) > 1:
                for s in sigs:
                    processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                slog(pair, sigs[0]["frame"], "SKIP CONFLICT",
                     "website shows BUY+SELL across frames — not trading")
                continue

            if not market_open:
                slog(pair, sigs[0]["frame"], "SKIP MARKET", "outside trading hours")
                continue

            side = list(website_sides.get(pair, {"??"}))[0]
            # reversal mode: we trade the opposite direction
            our_direction = ("SELL" if side == "BUY" else "BUY") if REVERSE_SIGNALS else side

            # ── Phase 1: Calendar filter ───────────────────────────────────────
            if CALENDAR_ENABLED:
                paused, cal_reason = should_pause(
                    pair, CALENDAR_PAUSE_MINS, CALENDAR_RESUME_MINS)
                if paused:
                    slog(pair, sigs[0]["frame"], "SKIP CALENDAR", cal_reason)
                    continue

            # ── Phase 2: News sentiment filter ────────────────────────────────
            if NEWS_ENABLED:
                news_ok, news_reason = news_confirms_direction(
                    pair, our_direction, NEWS_SENTIMENT_THRESHOLD)
                if not news_ok:
                    slog(pair, sigs[0]["frame"], "SKIP NEWS", news_reason)
                    continue

            # ── Phase 3: AI brain filter ───────────────────────────────────────
            if AI_BRAIN_ENABLED and ANTHROPIC_API_KEY:
                decision = get_decision(pair)
                action   = decision.get("action", "TAKE")
                if action == "SKIP":
                    slog(pair, sigs[0]["frame"], "SKIP AI",
                         f"AI says SKIP ({decision.get('confidence', 0):.0%}) — {decision.get('reason', '')}")
                    continue
                if action == "WAIT":
                    slog(pair, sigs[0]["frame"], "WAIT AI",
                         f"AI says WAIT ({decision.get('confidence', 0):.0%}) — {decision.get('reason', '')}")
                    continue
                # market-level pause from AI
                if get_market_mode() == "pause":
                    slog(pair, sigs[0]["frame"], "SKIP AI", "AI set market_mode=pause")
                    continue

            pending_for_ai.append({"pair": pair, "direction": our_direction})

            # all filters passed — queue for opening
            for s in sigs:
                slog(s["pair"], s["frame"], "SIGNAL", f"{side} [{s['frame']}]")
                to_open.append((s, [s]))

        for s in close_signals:
            if s["pair"] not in active_pairs:
                to_close.append(s)

        # ── STEP 4: execute closes first ──────────────────────────────────────
        closed_this_cycle = set()

        for s in to_close:
            pair = s["pair"]
            pos  = get_position(pair)
            if pos:
                if REVERSE_SIGNALS:
                    sid = _sig_id(s)
                    if sid not in _close_ignored_ids:
                        slog(pair, s["frame"], "WEBSITE CLOSE IGNORED", "reverse mode — letting trail/TP exit")
                        _close_ignored_ids.add(sid)
                else:
                    slog(pair, s["frame"], "WEBSITE CLOSE", "closing per signal")
                    close_trade(pair)
                    closed_this_cycle.add(pair)

        # ── STEP 5: execute opens ─────────────────────────────────────────────
        just_opened = set()

        for best, all_sigs in to_open:
            pair  = best["pair"]
            magic = MAGIC_BY_FRAME[best["frame"]]

            # check only this frame's existing position
            existing = None
            for name in (pair, pair + "+"):
                for p in (mt5.positions_get(symbol=name) or []):
                    if p.magic == magic:
                        existing = p
                        break
                if existing:
                    break

            # 1-per-pair: if other frame already has a position, skip this frame
            if existing is None:
                other_magic = MAGIC_LONG if magic == MAGIC_SHORT else MAGIC_SHORT
                for name in (pair, pair + "+"):
                    for p in (mt5.positions_get(symbol=name) or []):
                        if p.magic == other_magic:
                            slog(pair, best["frame"], "SKIP PAIR",
                                 "other frame already open — 1-per-pair limit")
                            for s in all_sigs:
                                processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                            existing = "skip"
                            break
                    if existing:
                        break
                if existing == "skip":
                    continue

            if existing:
                pos_side = "BUY" if existing.type == 0 else "SELL"
                # in reverse mode the bot trades opposite to the website signal
                expected_side = ("SELL" if best["side"] == "BUY" else "BUY") if REVERSE_SIGNALS else best["side"]
                if pos_side == expected_side:
                    # already open in correct direction — mark processed
                    for s in all_sigs:
                        processed_signals[f"{pair}_{s['time']}_{s['side']}_{s['frame']}"] = now
                    continue

                # opposite direction — close this frame and re-open
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

        # every 10 cycles (~11 min) print autonomous agent summaries
        if _cycle_count % 10 == 0:
            calendar_summary()
            news_summary()
            if AI_BRAIN_ENABLED and ANTHROPIC_API_KEY:
                ai_decision_summary()

        # feed pending signals to AI brain (non-blocking — brain thread picks it up)
        if pending_for_ai:
            _pending_signals_queue.clear()
            _pending_signals_queue.extend(pending_for_ai)


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
                active_brain()
        except Exception as e:
            print("GUARD ERROR:", e)
        time.sleep(GUARD_INTERVAL)


# ── AI brain thread (calls Claude API every AI_BRAIN_INTERVAL seconds) ────────

def ai_brain_thread():
    while True:
        try:
            if AI_BRAIN_ENABLED and ANTHROPIC_API_KEY and not is_cache_fresh(AI_BRAIN_INTERVAL):
                # build open-position summary
                positions = mt5.positions_get() or []
                open_pos  = [
                    {"pair": p.symbol,
                     "side": "BUY" if p.type == 0 else "SELL",
                     "pnl":  round(p.profit, 2)}
                    for p in positions
                    if p.magic in (MAGIC_SHORT, MAGIC_LONG)
                ]

                # recent P&L from last 24h of closed bot deals (last 10)
                deals = mt5.history_deals_get(
                    datetime.now(timezone.utc) - timedelta(hours=24),
                    datetime.now(timezone.utc)) or []
                our   = [d for d in deals
                         if d.magic in (MAGIC_SHORT, MAGIC_LONG)
                         and d.entry == mt5.DEAL_ENTRY_OUT][-10:]
                wins  = [d for d in our if d.profit > 0]
                recent_pnl = {
                    "total":      round(sum(d.profit for d in our), 2),
                    "win_rate":   len(wins) / len(our) * 100 if our else 0,
                    "open_count": len(open_pos),
                }

                refresh_decisions(
                    ANTHROPIC_API_KEY,
                    list(_pending_signals_queue),
                    open_pos,
                    recent_pnl
                )
        except Exception as e:
            print("AI BRAIN THREAD ERROR:", e)
        time.sleep(30)   # check every 30s, only calls API when cache is stale


# ── start ─────────────────────────────────────────────────────────────────────

threading.Thread(target=signal_thread,   daemon=True).start()
threading.Thread(target=position_thread, daemon=True).start()
threading.Thread(target=guard_thread,    daemon=True).start()
threading.Thread(target=ai_brain_thread, daemon=True).start()

# keep main thread alive
while True:
    time.sleep(60)
