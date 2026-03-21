"""
BLIND FOLLOWER BOT - State Consistency Architecture

Core objective: Maintain bot state = website state

Uses Counter-based diffing instead of TP/SL matching:
  prev_counter - curr_counter = positions to close
  curr_counter - prev_counter = positions to open

Safety: Only close trades we opened.
"""

import time
import sys
import threading
import MetaTrader5 as mt5
import json
import os
from datetime import datetime, timezone, timedelta
from collections import Counter

# Log to file
sys.stdout = open("bot.log", "a", buffering=1, encoding="utf-8")
sys.stderr = sys.stdout

from scraper import fetch_page
from parser import parse_signals
from trader import open_trade, close_position_by_ticket, init_mt5, show_open_positions, account_summary
from signal_manager import (
    Signal, SignalKey, PositionStore, StateDifferencer, SignalFilter, SafeExecutor
)
from config import SIGNAL_INTERVAL, TRADE_VOLUME, MAX_SIGNAL_AGE

print(f"\n{'='*80}")
print("BLIND FOLLOWER BOT - STATE CONSISTENCY ARCHITECTURE")
print(f"Signal interval: {SIGNAL_INTERVAL}s | Volume: {TRADE_VOLUME}")
print(f"{'='*80}\n")

# Initialize MT5
init_mt5()

# Persistent position tracker
positions = PositionStore()

# Persistent signal processing tracker (prevent duplicate opens)
processed_signals_file = "processed_signals.json"


def load_processed_signals():
    """Load set of already-processed signal timestamps."""
    try:
        with open(processed_signals_file, 'r') as f:
            data = json.load(f)
        # Keep signals from last 24 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        filtered = {
            ts: v for ts, v in data.items()
            if datetime.fromisoformat(v) > cutoff
        }
        return set(filtered.keys())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_processed_signals(signal_set):
    """Save processed signal IDs."""
    data = {sig_id: datetime.now(timezone.utc).isoformat() for sig_id in signal_set}
    with open(processed_signals_file, 'w') as f:
        json.dump(data, f)


def get_signal_id(sig: Signal) -> str:
    """Create unique signal ID from signal timestamp + key."""
    key = SignalKey.build(sig.pair, sig.side, sig.tp, sig.sl)
    time_str = sig.time.isoformat()
    return f"{time_str}_{key}"


# Load processed signals at startup
processed_signal_ids = load_processed_signals()
print(f"[STARTUP] Loaded {len(processed_signal_ids)} processed signal IDs (last 24h)")

# Load positions from MT5 at startup
mt5_positions = mt5.positions_get() or []
print(f"[STARTUP] Found {len(mt5_positions)} positions in MT5")
for pos in mt5_positions:
    if pos.magic != 777:  # Not our positions
        continue
    # TODO: Reconstruct position tracker from MT5 positions
    # For now, we start fresh each cycle

print()


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL CYCLE
# ══════════════════════════════════════════════════════════════════════════════

def run_signal_cycle():
    """
    Main signal processing cycle using state consistency logic.

    1. Fetch website snapshot
    2. Parse signals
    3. Build current state (list of keys)
    4. Compute diff vs previous state
    5. Close trades (safe)
    6. Open trades
    7. Sleep
    """
    global positions, processed_signal_ids

    now = datetime.now(timezone.utc)
    now_str = now.strftime('%H:%M:%S')

    # ──── FETCH & PARSE ──────────────────────────────────────────────────────

    html = fetch_page()
    if html is None:
        print(f"[{now_str}] WARNING: Could not fetch signals (proxy failed)")
        return

    try:
        raw_signals = parse_signals(html)
    except Exception as e:
        print(f"[{now_str}] ERROR: Failed to parse signals: {e}")
        import traceback
        traceback.print_exc()
        return

    if not raw_signals:
        print(f"[{now_str}] No signals found on website")
        return

    print(f"[{now_str}] Fetched {len(raw_signals)} raw signals")

    # ──── CONVERT TO SIGNAL OBJECTS ──────────────────────────────────────────

    signals = []
    for raw in raw_signals:
        try:
            sig = Signal(
                pair=raw['pair'],
                side=raw['side'],
                open_price=raw['open'],
                tp=raw['tp'],
                sl=raw['sl'],
                time=raw['time'],
                frame=raw['frame'],
                status=raw['status'],
                close_price=raw.get('close'),
                close_reason=raw.get('close_reason'),
            )
            signals.append(sig)
        except Exception as e:
            print(f"  [WARN] Skipping malformed signal: {e}")
            continue

    if not signals:
        print(f"[{now_str}] No valid signals after parsing")
        return

    # ──── FILTER BY AGE (CRITICAL: CLOSE signals bypass age filter) ──────────

    active_signals = [s for s in signals if s.status == "ACTIVE"]
    close_signals = [s for s in signals if s.status == "CLOSE"]

    print(f"  Active: {len(active_signals)}, Close: {len(close_signals)}")

    # Filter ACTIVE by age, keep ALL CLOSE signals
    active_signals = SignalFilter.filter_by_age(active_signals, MAX_SIGNAL_AGE)
    print(f"  After age filter: {len(active_signals)} active (max age: {MAX_SIGNAL_AGE}s)")

    # ──── DEDUPLICATE: Keep most recent per key ──────────────────────────────

    # Sort by time DESC so deduplication keeps most recent
    signals_to_process = sorted(active_signals, key=lambda s: s.time, reverse=True)

    signals_to_process = SignalFilter.deduplicate_by_key(signals_to_process)
    print(f"  After dedup: {len(signals_to_process)} unique active signals")

    # ──── BUILD CURRENT STATE ────────────────────────────────────────────────

    # Current keys from website (for state comparison)
    curr_keys = [
        SignalKey.build(s.pair, s.side, s.tp, s.sl)
        for s in signals_to_process
    ]

    # Get previous keys from our tracker
    prev_keys = list(positions.get_all_keys())

    print(f"  Previous state: {len(prev_keys)} keys")
    print(f"  Current state: {len(curr_keys)} keys")

    # ──── COMPUTE DIFF ───────────────────────────────────────────────────────

    closed, opened = StateDifferencer.compute_diff(prev_keys, curr_keys)

    if closed or opened:
        print(f"  Diff: {dict(closed)} closed | {dict(opened)} opened")
    else:
        print(f"  No changes")

    # ──── CLOSE TRADES (SAFE) ────────────────────────────────────────────────

    close_count = 0
    if closed:
        print(f"\n[CLOSE] Processing {len(closed)} key(s) to close...")

        ops = SafeExecutor.prepare_close_operations(closed, positions)
        for key, count, ticket in ops:
            try:
                if close_position_by_ticket(ticket, key[0]):
                    close_count += 1
                    print(f"  [OK] Closed ticket {ticket} for {key}")
                else:
                    print(f"  [ERR] Failed to close ticket {ticket}")
            except Exception as e:
                print(f"  [ERR] Exception closing {ticket}: {e}")

    # ──── OPEN TRADES ────────────────────────────────────────────────────────

    open_count = 0
    if opened:
        print(f"\n[OPEN] Processing {len(opened)} key(s) to open...")

        for key, count in opened.items():
            pair, side, tp, sl = key

            # Find matching signal
            matching_signals = [
                s for s in signals_to_process
                if s.pair == pair and s.side == side
                and round(s.tp, 3) == round(tp, 3)
                and round(s.sl, 3) == round(sl, 3)
            ]

            if not matching_signals:
                print(f"  [SKIP] No signal found for {key}")
                continue

            sig = matching_signals[0]  # Use first match

            # Check if already processed recently
            sig_id = get_signal_id(sig)
            if sig_id in processed_signal_ids:
                print(f"  [SKIP] Signal already processed: {sig_id}")
                continue

            # Open trades for this key
            for i in range(count):
                try:
                    success, ticket = open_trade({
                        'pair': sig.pair,
                        'side': sig.side,
                        'open': sig.open_price,
                        'tp': sig.tp,
                        'sl': sig.sl,
                        'time': sig.time,
                        'frame': sig.frame,
                    })

                    if success and ticket:
                        positions.add_ticket(key, ticket)
                        open_count += 1
                        print(f"  [OK] Opened ticket {ticket} for {key}")
                        processed_signal_ids.add(sig_id)
                    else:
                        print(f"  [ERR] Failed to open trade for {key}")

                except Exception as e:
                    print(f"  [ERR] Exception opening {key}: {e}")

    # ──── SAVE STATE ─────────────────────────────────────────────────────────

    if open_count > 0 or close_count > 0:
        save_processed_signals(processed_signal_ids)

    # ──── PROCESS CLOSE SIGNALS (Informational) ───────────────────────────────

    if close_signals:
        print(f"\n[CLOSE_SIGNALS] Found {len(close_signals)} close signal(s) on website")
        for sig in close_signals:
            print(f"  {sig.pair} {sig.side} @ close {sig.close_price} ({sig.close_reason})")
            # These are FYI only - the counter diff already handled closing

    # ──── STATUS ─────────────────────────────────────────────────────────────

    print(f"\n[{now_str}] Cycle complete: {open_count} opened, {close_count} closed")
    print(f"  Tracked positions: {sum(len(t) for t in positions.positions.values())} tickets")

    show_open_positions()
    account_summary()


def signal_thread():
    """Main loop: fetch signals every N seconds."""

    while True:
        try:
            # Check if MT5 is still connected
            if not mt5.initialize():
                print("[ERROR] MT5 disconnected - attempting to reconnect...")
                try:
                    init_mt5()
                    print("[OK] MT5 reconnected")
                except Exception as e:
                    print(f"[ERROR] MT5 reconnection failed: {e}")
                    time.sleep(5)
                    continue

            run_signal_cycle()

        except Exception as e:
            print(f"[ERROR] Signal cycle failed: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(SIGNAL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════════════════════

threading.Thread(target=signal_thread, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(60)
