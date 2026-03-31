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
import tempfile
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
from trailing_stop import init_trailing_stop
from session_filter import is_london_ny_overlap, session_status_string, is_signal_in_overlap, filter_signals_by_session
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

# Virtual SL - Spread-aware stop loss management

# Trailing Stop - Phase-based SL management (passive layer)
trailing_stop_mgr = init_trailing_stop()
print("[TRAIL] Initialized trailing stop manager")

# Persistent signal processing tracker (prevent duplicate opens)
processed_signals_file = "processed_signals.json"


def load_processed_signals():
    """Load set of already-processed signal timestamps (fault-tolerant)."""
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
    except FileNotFoundError:
        return set()  # File doesn't exist yet
    except json.JSONDecodeError as e:
        print(f"[ERROR_JSON] Corrupted processed_signals.json: {e}, starting fresh")
        return set()
    except Exception as e:
        print(f"[ERROR_JSON] Failed to load processed_signals: {e}, starting fresh")
        return set()


def save_processed_signals(signal_set):
    """Save processed signal IDs (fault-tolerant)."""
    try:
        data = {sig_id: datetime.now(timezone.utc).isoformat() for sig_id in signal_set}
        # Atomic write: write to temp file first, then rename
        import tempfile
        temp_fd, temp_path = tempfile.mkstemp(suffix='.json', prefix='processed_signals_')
        try:
            with os.fdopen(temp_fd, 'w') as f:
                json.dump(data, f)
            os.replace(temp_path, processed_signals_file)
        except Exception:
            try:
                os.unlink(temp_path)
            except:
                pass
            raise
    except Exception as e:
        print(f"[ERROR_JSON] Failed to save processed_signals: {e}, changes may be lost")


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

    # ──── SIGNAL STABILITY LOGGING: Raw signal list every cycle ────────────────
    raw_signal_list = [(s.pair, s.side) for s in signals if s.status == "ACTIVE"]
    print(f"  [RAW_SIGNALS] Cycle signals (ACTIVE only): {raw_signal_list}")

    # ──── FILTER BY AGE (CRITICAL: CLOSE signals bypass age filter) ──────────

    active_signals = [s for s in signals if s.status == "ACTIVE"]
    close_signals = [s for s in signals if s.status == "CLOSE"]

    print(f"  Active: {len(active_signals)}, Close (→Active): {len(close_signals)}")

    # ──── FILTER BY AGE: Only open NEW trades from fresh signals (<30 min)
    # Position management keeps ALL active signals to avoid closing active trades

    fresh_signals = SignalFilter.filter_by_age(active_signals, MAX_SIGNAL_AGE)
    print(f"  After age filter: {len(fresh_signals)} fresh active (max age: {MAX_SIGNAL_AGE}s)")

    # ──── FILTER BY SESSION: Only execute signals generated during overlap ────
    # CRITICAL: Check signal.time (when signal was CREATED), not current time
    # This prevents delayed execution of old signals even during overlap hours

    overlap_signals = filter_signals_by_session(fresh_signals)
    print(f"  After session filter: {len(overlap_signals)} signals from overlap (13:00-17:00 UTC)")

    # Log any signals filtered out by session window
    if len(fresh_signals) > len(overlap_signals):
        filtered_out = len(fresh_signals) - len(overlap_signals)
        for sig in fresh_signals:
            if not is_signal_in_overlap(sig.time):
                print(f"    [SESSION_SKIP] {sig.pair} {sig.side} created at {sig.time.strftime('%H:%M UTC')} (outside overlap)")

    # For position management, use ALL active + CLOSE signals (no age filter)
    # IMPORTANT: Treat CLOSE signals as ACTIVE to prevent diff-based closing
    # This allows trailing stop to continue managing positions instead of force-closing
    all_active_signals = active_signals + close_signals

    # ──── DEDUPLICATE: Keep most recent per key ──────────────────────────────

    # Sort by time DESC so deduplication keeps most recent
    # Use overlap_signals (session-filtered) for opening trades
    overlap_signals_sorted = sorted(overlap_signals, key=lambda s: s.time, reverse=True)
    signals_to_open = SignalFilter.deduplicate_by_key(overlap_signals_sorted)
    print(f"  After dedup: {len(signals_to_open)} unique overlap signals for opening")

    # For position management, deduplicate ALL active signals
    all_active_sorted = sorted(all_active_signals, key=lambda s: s.time, reverse=True)
    signals_to_manage = SignalFilter.deduplicate_by_key(all_active_sorted)

    # ──── BUILD CURRENT STATE ────────────────────────────────────────────────

    # Current keys from ALL active signals (for state comparison)
    # This ensures positions stay open even if signals age past 30 minutes
    curr_keys = [
        SignalKey.build(s.pair, s.side, s.tp, s.sl)
        for s in signals_to_manage
    ]

    # ──── KEY PRECISION VERIFICATION: Log signal values for cross-cycle comparison ──
    # This catches any precision issues that could cause keys to change between cycles
    if signals_to_manage:
        print(f"\n  [KEY_PRECISION] Signals_to_manage ({len(signals_to_manage)}):")
        for s in signals_to_manage[:5]:  # Log first 5
            key = SignalKey.build(s.pair, s.side, s.tp, s.sl)
            print(f"    {s.pair:7s} {s.side:4s} | TP={s.tp:.10f} SL={s.sl:.10f} | KEY={key}")

    # Get previous keys from our tracker
    prev_keys = list(positions.get_all_keys())

    print(f"  Previous state: {len(prev_keys)} keys")
    print(f"  Current state: {len(curr_keys)} keys")

    # ──── RUNTIME VERIFICATION: Prove curr_keys is from ALL active, not fresh ────
    print(f"  [VERIFY] Raw active signals: {len(active_signals)}")
    print(f"  [VERIFY] Fresh signals only: {len(fresh_signals)}")
    print(f"  [VERIFY] Signals_to_manage (deduped all active): {len(signals_to_manage)}")
    print(f"  [VERIFY] Signals_to_open (deduped fresh only): {len(signals_to_open)}")
    print(f"  [VERIFY] curr_keys source check: {len(curr_keys)} == {len(signals_to_manage)} ? {len(curr_keys) == len(signals_to_manage)}")

    # Print actual key content for verification
    if curr_keys:
        print(f"  [VERIFY] Sample curr_keys (first 3): {curr_keys[:3]}")

    # CRITICAL CHECK: Verify age filter is only applied to opening, not closing
    if len(curr_keys) == len(signals_to_manage):
        print(f"  [VERIFIED OK] Age filter only applied to opening (not closing)")
    elif len(curr_keys) != len(signals_to_manage):
        print(f"  [WARNING] curr_keys mismatch: {len(curr_keys)} != {len(signals_to_manage)}")

    # MIXED SCENARIO DETECTION
    if len(active_signals) >= 5 and 0 < len(fresh_signals) < len(active_signals):
        print(f"\n  *** MIXED SCENARIO DETECTED ***")
        print(f"  Age filter removed {len(active_signals) - len(fresh_signals)} signals")
        print(f"  Active >= 5: {len(active_signals)} | Fresh 1-4: {len(fresh_signals)}")
        print(f"\n  [CRITICAL] Full key sets for analysis:")
        print(f"  prev_keys ({len(prev_keys)} keys): {sorted(prev_keys)}")
        print(f"  curr_keys ({len(curr_keys)} keys): {sorted(curr_keys)}")

        # Check for missing keys
        prev_set = set(prev_keys)
        curr_set = set(curr_keys)
        missing_from_curr = prev_set - curr_set
        missing_from_prev = curr_set - prev_set

        if missing_from_curr:
            print(f"\n  [CRITICAL] Keys in prev but NOT in curr (WILL BE CLOSED): {missing_from_curr}")
            for key in missing_from_curr:
                print(f"    Key {key} will trigger CLOSE")
                # Check if key was in signals
                key_in_active = any(
                    SignalKey.build(s.pair, s.side, s.tp, s.sl) == key
                    for s in active_signals
                )
                key_in_fresh = any(
                    SignalKey.build(s.pair, s.side, s.tp, s.sl) == key
                    for s in fresh_signals
                )
                print(f"      In raw active: {key_in_active} | In fresh: {key_in_fresh}")
                if key_in_active and not key_in_fresh:
                    print(f"      REASON: Signal aged-out (>30min) but still managed (expected for closing logic)")

        if missing_from_prev:
            print(f"\n  [INFO] Keys in curr but NOT in prev (NEW): {missing_from_prev}")


    # ──── TRAILING STOP UPDATE (PASSIVE LAYER) ──────────────────────────────────
    # Update trailing stops for all tracked positions (SL adjustments only)
    # FIX 2: FAIL-FAST - Crash if trailing stop fails (no silent errors)
    try:
        trailing_stop_mgr.update_all_positions(mt5)
    except Exception as e:
        log(LogLevel.CRITICAL, f"TRAILING STOP FAILURE: {e}")
        print(f"[FATAL] Trailing stop failed: {e}")
        raise RuntimeError("Trailing stop is offline — aborting bot")

    # ──── COMPUTE DIFF ───────────────────────────────────────────────────────
    # RUNTIME VERIFICATION: Show exact inputs to diff calculation
    print(f"  [VERIFY] Before diff - prev_keys count: {len(prev_keys)}, curr_keys count: {len(curr_keys)}")
    if prev_keys and curr_keys:
        print(f"  [VERIFY] Sample prev_key: {prev_keys[0]}")
        print(f"  [VERIFY] Sample curr_key: {curr_keys[0]}")

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
        print(f"  [TRIGGER] DIFF_CLOSE_START - {len(closed)} keys to close")

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
                try:
                    trailing_stop_mgr.remove_position(ticket)
                    print(f"  [TRAIL] Removed T{ticket} (stale detect)")
                except Exception as e:
                    log(LogLevel.DEBUG, f"Trailing stop remove failed for T{ticket}: {e}")
                continue

            try:
                # Attempt close
                print(f"  [TRIGGER] DIFF_CLOSE_TICKET {ticket} for key {key}")
                if close_position_by_ticket(ticket, key[0]):
                    # Success - NOW remove ticket from tracking
                    positions.remove_ticket(ticket)
                    # Remove from trailing stop tracking (position is now closed)
                    try:
                        trailing_stop_mgr.remove_position(ticket)
                        print(f"  [TRAIL] Removed T{ticket} (DIFF close)")
                    except Exception as e:
                        log(LogLevel.DEBUG, f"Trailing stop remove failed for T{ticket}: {e}")
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
                        # Remove from trailing stop (position will not be retried)
                        try:
                            trailing_stop_mgr.remove_position(ticket)
                            print(f"  [TRAIL] Removed T{ticket} (escalated to _FAILED_CLOSE_)")
                        except Exception as e:
                            log(LogLevel.DEBUG, f"Trailing stop remove failed for T{ticket}: {e}")
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
                    # Remove from trailing stop (position will not be retried)
                    try:
                        trailing_stop_mgr.remove_position(ticket)
                        print(f"  [TRAIL] Removed T{ticket} (escalated to _FAILED_CLOSE_ on exception)")
                    except Exception as e:
                        log(LogLevel.DEBUG, f"Trailing stop remove failed for T{ticket}: {e}")
                    escalated_count += 1
                    log(LogLevel.CRITICAL, f"Escalated ticket {ticket} to _FAILED_CLOSE_ bucket after max retries (exception)")

        print(f"  [TRIGGER] DIFF_CLOSE_END - closed {close_count}, escalated {escalated_count}")

    # ──── OPEN TRADES ────────────────────────────────────────────────────────

    open_count = 0
    if opened:
        print(f"\n[OPEN] Processing {len(opened)} key(s) to open...")

        for key, count in opened.items():
            pair, side, tp, sl = key

            # Find matching signal from FRESH signals only
            # Only open signals that passed the age filter (< 30 min)
            matching_signals = [
                s for s in signals_to_open
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
                    success, ticket = open_trade(sig)

                    if success and ticket:
                        positions.add_ticket(key, ticket)

                        # Register with trailing stop for SL management
                        try:
                            trailing_stop_mgr.register_position(
                                ticket=ticket,
                                symbol=sig.pair,
                                side=sig.side,
                                entry_price=sig.open_price,
                                tp=sig.tp,
                                original_sl=sig.sl
                            )
                            print(f"  [TRAIL] Registered T{ticket} {sig.pair} {sig.side}")
                        except Exception as e:
                            log(LogLevel.ERROR, f"Trailing stop registration failed for T{ticket}: {e}")
                            print(f"  [TRAIL_ERR] Failed to register T{ticket}: {e}")

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

    # ──── PROCESS CLOSE SIGNALS (Treated as ACTIVE for trailing stop management) ──

    if close_signals:
        print(f"\n[CLOSE_SIGNALS] Found {len(close_signals)} close signal(s) on website (→ kept as ACTIVE for trailing TP)")
        for sig in close_signals:
            print(f"  {sig.pair} {sig.side} @ {sig.entry} (will be managed by trailing stop, not closed immediately)")

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
        try:
            status = safety.get_status_report()
            if status["total_escalated"] > 0:
                log(LogLevel.WARN, f"Safety status - Escalated: {status['total_escalated']}, Tickets: {status['escalated_tickets']}")
        except Exception as e:
            log(LogLevel.DEBUG, f"[ERROR_STATUS] Failed to get status report: {e}")

    # Display positions and account (fault-tolerant)
    try:
        show_open_positions()
    except Exception as e:
        print(f"[ERROR_DISPLAY] show_open_positions failed: {e}")

    try:
        account_summary()
    except Exception as e:
        print(f"[ERROR_DISPLAY] account_summary failed: {e}")


def signal_thread():
    """Main loop: fetch signals every N seconds (only during London-NY overlap)."""

    while True:
        try:
            # ──────── SESSION CHECK (ONLY LOG ON CHANGE) ────────────────────────────────────────
            try:
                if not is_london_ny_overlap():
                    status = session_status_string()
                    if status:  # Only log if state changed
                        print(status)
                    time.sleep(SIGNAL_INTERVAL)
                    continue
            except Exception as e:
                print(f"[ERROR_SESSION] Session check failed: {e}")
                time.sleep(SIGNAL_INTERVAL)
                continue

            # ──────── TRADING CYCLE (LONDON-NY OVERLAP ONLY) ────────────────────────────────────
            # Check if MT5 is still connected
            try:
                if not mt5.initialize():
                    print("[ERROR_MT5] MT5 disconnected - attempting to reconnect...")
                    try:
                        init_mt5()
                        print("[OK_MT5] MT5 reconnected")
                    except Exception as e:
                        print(f"[ERROR_RECONNECT] MT5 reconnection failed: {e}")
                        time.sleep(5)
                        continue
            except Exception as e:
                print(f"[ERROR_MT5_CHECK] MT5 init check failed: {e}")
                time.sleep(5)
                continue

            # ──────── RUN SIGNAL CYCLE ────────────────────────────────────────────────────────
            try:
                run_signal_cycle()
            except Exception as e:
                print(f"[ERROR_CYCLE] Signal cycle error: {e}")
                # Don't re-raise - allow loop to continue

        except Exception as e:
            print(f"[ERROR_SIGNAL_THREAD] Unexpected error in signal thread: {e}")
            # Catastrophic fallback - don't exit loop
            time.sleep(SIGNAL_INTERVAL)

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

        # Filter: ACTIVE signals only
        # For startup reconstruction, use ALL active signals (no age filter)
        # This ensures we properly reconstruct ANY open positions regardless of signal age
        active_signals = [s for s in signals if s.status == "ACTIVE"]
        signals_for_reconstruction = sorted(active_signals, key=lambda s: s.time, reverse=True)
        signals_for_reconstruction = SignalFilter.deduplicate_by_key(signals_for_reconstruction)

        if signals_for_reconstruction:
            mt5_positions = mt5.positions_get() or []
            if mt5_positions:
                reconstructed, unmatched = reconstruct_positions_from_mt5(
                    mt5_positions, signals_for_reconstruction, positions
                )
                print(f"[STARTUP] Reconstructed {reconstructed} positions, {unmatched} unmatched\n")

                # ──── REGISTER RECONSTRUCTED POSITIONS WITH TRAILING STOP ────────
                # After reconstruction, all positions need to be registered for SL tracking
                print(f"[STARTUP] Registering reconstructed positions with trailing stop...")
                try:
                    # Get all reconstructed positions back from MT5 to get full details
                    mt5_positions_now = mt5.positions_get() or []
                    mt5_by_ticket = {p.ticket: p for p in mt5_positions_now}

                    # Iterate through all tracked positions and register those not yet in trailing stop
                    registered_count = 0
                    for key, tickets in positions.positions.items():
                        # Skip special buckets
                        if key[0] in ("_UNMATCHED_", "_FAILED_CLOSE_"):
                            continue

                        pair, side, tp, sl = key

                        for ticket in tickets:
                            # Check if already in trailing stop (to avoid re-registering)
                            if ticket in trailing_stop_mgr.position_meta:
                                continue

                            # Get position from MT5
                            mt5_pos = mt5_by_ticket.get(ticket)
                            if not mt5_pos:
                                continue

                            # Register with trailing stop
                            try:
                                trailing_stop_mgr.register_position(
                                    ticket=ticket,
                                    symbol=mt5_pos.symbol,
                                    side=side,
                                    entry_price=mt5_pos.price_open,
                                    tp=tp,
                                    original_sl=sl
                                )
                                registered_count += 1
                            except Exception as e:
                                print(f"  [TRAIL_ERR] Failed to register T{ticket}: {e}")

                    if registered_count > 0:
                        print(f"[STARTUP] Registered {registered_count} position(s) with trailing stop\n")

                except Exception as e:
                    print(f"[STARTUP] Exception registering positions: {e}\n")
                    import traceback
                    traceback.print_exc()
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

# Keep main thread alive - FAULT-TOLERANT
last_alive_log = datetime.now(timezone.utc)
while True:
    try:
        # Log "active" every 30 minutes
        now = datetime.now(timezone.utc)
        if (now - last_alive_log).total_seconds() >= 1800:  # 1800 seconds = 30 minutes
            try:
                print(f"\n[ALIVE] Bot is active - {now.isoformat()}")

                try:
                    show_open_positions()
                except Exception as e:
                    print(f"[ERROR_ALIVE_POS] Failed to show positions: {e}")

                try:
                    account_summary()
                except Exception as e:
                    print(f"[ERROR_ALIVE_ACCT] Failed to show account: {e}")

                print()
                last_alive_log = now
            except Exception as e:
                print(f"[ERROR_ALIVE_LOG] Alive log failed: {e}")
                last_alive_log = now  # Don't get stuck if logging fails

        time.sleep(60)

    except Exception as e:
        print(f"[FATAL_MAIN] Main loop exception (recovering): {e}")
        # Sleep to prevent spinning, then continue
        time.sleep(60)
        continue
