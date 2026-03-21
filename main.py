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
    Signal, SignalKey, PositionStore, StateDifferencer, SignalFilter, SafeExecutor, FuzzyMatcher
)
from operational_safety import OperationalSafety, log, LogLevel
from config import SIGNAL_INTERVAL, TRADE_VOLUME, MAX_SIGNAL_AGE

print(f"\n{'='*80}")
print("BLIND FOLLOWER BOT - STATE CONSISTENCY ARCHITECTURE")
print(f"Signal interval: {SIGNAL_INTERVAL}s | Volume: {TRADE_VOLUME}")
print(f"{'='*80}\n")

# Initialize MT5
init_mt5()

# Persistent position tracker
positions = PositionStore()

# Operational safety monitoring and retry control
safety = OperationalSafety(max_retries=5, unmatched_threshold=3)

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


def reconstruct_positions_from_mt5(mt5_positions_list, signals_to_process, positions_store):
    """Reconstruct position tracker from MT5 live state + fuzzy matching to signals.

    SAFETY FEATURES:
    1. Time filtering: Only match signals from same trading session (24h window)
    2. Confidence check: Best match must be 50% better than second-best
    3. Unmatched safety: Ambiguous matches sent to UNMATCHED bucket (never closed)

    Args:
        mt5_positions_list: List of MT5 position objects
        signals_to_process: List of Signal objects (already filtered, deduplicated)
        positions_store: PositionStore instance to populate

    Returns:
        (reconstructed_count, unmatched_count) - tickets loaded and fallback count
    """
    # Build dict of signals by key for fast lookup: {key: [Signal, ...]}
    signals_by_key = {}
    for sig in signals_to_process:
        key = SignalKey.build(sig.pair, sig.side, sig.tp, sig.sl)
        if key not in signals_by_key:
            signals_by_key[key] = []
        signals_by_key[key].append(sig)

    reconstructed = 0
    unmatched = 0

    for pos in mt5_positions_list:
        if pos.magic != 777:  # Not our positions
            continue

        # Extract MT5 position details
        pair = pos.symbol
        tp = pos.tp
        sl = pos.sl
        ticket = pos.ticket
        side = "BUY" if pos.type == 0 else "SELL"

        # Extract MT5 position open time (safe conversion)
        mt5_time_opened = None
        try:
            if hasattr(pos, 'time'):
                # Convert Unix timestamp to datetime
                mt5_time_opened = datetime.fromtimestamp(pos.time, tz=timezone.utc)
            elif hasattr(pos, 'time_setup'):
                mt5_time_opened = datetime.fromtimestamp(pos.time_setup, tz=timezone.utc)
        except Exception:
            pass  # If can't extract time, will still match (time_compatible returns True)

        # Find best match with SAFETY CHECKS
        best_key, best_signal, best_score, is_confident = FuzzyMatcher.find_best_match_with_confidence(
            tp, sl, mt5_time_opened, signals_by_key
        )

        threshold = FuzzyMatcher.get_threshold(pair)

        # CRITICAL: Require BOTH distance threshold AND confidence
        if best_key is not None and best_score <= threshold and is_confident:
            # MATCHED with high confidence: Reconstruct with this key
            positions_store.add_ticket(best_key, ticket)
            reconstructed += 1
            print(f"  [RECONSTRUCT] {pair} {side} ticket {ticket} -> key {best_key} (score={best_score:.6f}, confident)")
        else:
            # UNMATCHED: Either no match, threshold exceeded, or ambiguous
            fallback_key = ("_UNMATCHED_", pair, side, tp, sl)
            positions_store.add_ticket(fallback_key, ticket)
            unmatched += 1

            reason = "ambiguous" if (best_key is not None and best_score <= threshold and not is_confident) else "no_match"
            print(f"  [UNMATCHED] {pair} {side} ticket {ticket} @ TP={tp} SL={sl} ({reason}, score={best_score:.6f})")

    return reconstructed, unmatched


# Load processed signals at startup
processed_signal_ids = load_processed_signals()
print(f"[STARTUP] Loaded {len(processed_signal_ids)} processed signal IDs (last 24h)")

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
    escalated_count = 0
    if closed:
        log(LogLevel.INFO, f"Processing {len(closed)} key(s) to close")

        # Get current MT5 positions for stale detection
        mt5_positions = mt5.positions_get() or []

        ops = SafeExecutor.prepare_close_operations(closed, positions)
        for key, ticket in ops:
            # CRITICAL SAFETY: Never close unmatched positions
            if key[0] == "_UNMATCHED_":
                log(LogLevel.INFO, f"Skipping UNMATCHED ticket {ticket} - unmatched positions never closed")
                continue

            # CRITICAL SAFETY: Never retry failed positions (already escalated)
            if key[0] == "_FAILED_CLOSE_":
                log(LogLevel.INFO, f"Skipping FAILED_CLOSE ticket {ticket} - escalated tickets never retried")
                continue

            # STALE DETECTION: Check if ticket was manually closed in MT5
            if safety.check_stale_tickets(ticket, mt5_positions):
                positions.remove_ticket(ticket)
                continue

            try:
                # Attempt close
                if close_position_by_ticket(ticket, key[0]):
                    # Success - NOW remove ticket from tracking
                    positions.remove_ticket(ticket)
                    close_count += 1
                    safety.handle_close_success(ticket)
                    log(LogLevel.INFO, f"Closed and removed ticket {ticket} for {key[0]}")

                else:
                    # Failed - ticket STAYS in positions for retry next cycle
                    # Track failure with escalation
                    action = safety.handle_close_failure(ticket, key[0], "close_position_by_ticket returned False")

                    if action == "ESCALATE":
                        # Move to failed close bucket
                        failed_key = ("_FAILED_CLOSE_", key[0], key[2], key[3])
                        positions.remove_ticket(ticket)
                        positions.add_ticket(failed_key, ticket)
                        escalated_count += 1
                        log(LogLevel.CRITICAL, f"Escalated ticket {ticket} to _FAILED_CLOSE_ bucket after max retries")

            except Exception as e:
                # Exception - ticket STAYS in positions for retry next cycle
                action = safety.handle_close_failure(ticket, key[0], str(e))

                if action == "ESCALATE":
                    # Move to failed close bucket
                    failed_key = ("_FAILED_CLOSE_", key[0], key[2], key[3])
                    positions.remove_ticket(ticket)
                    positions.add_ticket(failed_key, ticket)
                    escalated_count += 1
                    log(LogLevel.CRITICAL, f"Escalated ticket {ticket} to _FAILED_CLOSE_ bucket after max retries (exception)")

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

    # Count position types
    total_tickets = sum(len(t) for t in positions.positions.values())
    unmatched_tickets = len(positions.positions.get(("_UNMATCHED_",) + tuple([None] * 4), []))
    failed_close_tickets = len(positions.positions.get(("_FAILED_CLOSE_",) + tuple([None] * 4), []))

    # Determine actual unmatched keys
    unmatched_count = 0
    failed_close_count = 0
    for key in positions.positions.keys():
        if key[0] == "_UNMATCHED_":
            unmatched_count += len(positions.positions[key])
        elif key[0] == "_FAILED_CLOSE_":
            failed_close_count += len(positions.positions[key])

    log(LogLevel.INFO, f"Cycle complete: {open_count} opened, {close_count} closed, {escalated_count} escalated")
    log(LogLevel.INFO, f"Tracked: {total_tickets} tickets | UNMATCHED: {unmatched_count} | FAILED_CLOSE: {failed_close_count}")

    # Monitor UNMATCHED growth
    safety.check_unmatched_growth(unmatched_count)

    # Log safety status periodically
    import random
    if random.random() < 0.1:  # ~10% of cycles
        status = safety.get_status_report()
        if status["total_escalated"] > 0:
            log(LogLevel.WARN, f"Safety status - Escalated: {status['total_escalated']}, Tickets: {status['escalated_tickets']}")

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

# Initial MT5 reconstruction - must happen BEFORE signal cycle starts
# because first signal cycle will compute prev_keys = positions.get_all_keys()
print("\n[STARTUP] Initial MT5 reconstruction...")

# Fetch and parse one signal snapshot first
try:
    html = fetch_page()
    if html is not None:
        raw_signals = parse_signals(html)

        # Convert to Signal objects and filter
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
                pass  # Skip malformed

        # Filter: ACTIVE signals only, age, then deduplicate
        active_signals = [s for s in signals if s.status == "ACTIVE"]
        active_signals = SignalFilter.filter_by_age(active_signals, MAX_SIGNAL_AGE)
        signals_to_process = sorted(active_signals, key=lambda s: s.time, reverse=True)
        signals_to_process = SignalFilter.deduplicate_by_key(signals_to_process)

        if signals_to_process:
            mt5_positions = mt5.positions_get() or []
            if mt5_positions:
                reconstructed, unmatched = reconstruct_positions_from_mt5(
                    mt5_positions, signals_to_process, positions
                )
                print(f"[STARTUP] Reconstructed {reconstructed} positions, {unmatched} unmatched\n")
            else:
                print(f"[STARTUP] No existing MT5 positions to reconstruct\n")
        else:
            print(f"[STARTUP] Could not reconstruct - no valid signals available\n")
    else:
        print(f"[STARTUP] Could not reconstruct - failed to fetch signals\n")
except Exception as e:
    print(f"[STARTUP] Reconstruction error: {e}\n")
    import traceback
    traceback.print_exc()

threading.Thread(target=signal_thread, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(60)
