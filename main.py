"""
BLIND FOLLOWER BOT - Ultra Simple Version

Just fetches signals and opens/closes trades exactly as website says.
No intelligence, no fancy risk management, no complexity.

CORE BEHAVIORS:
1. Most recent signal only (per pair+frame)
2. Frame lock (first-come-first-served for signal deduplication)
3. Multiple positions allowed (per pair)
4. Proper signal processing order
5. Frame-matched close only
6. State file pruning at startup
"""

import time
import sys
import threading
import MetaTrader5 as mt5
import json
import os
from datetime import datetime, timezone, timedelta

# Log to file
sys.stdout = open("bot.log", "a", buffering=1, encoding="utf-8")
sys.stderr = sys.stdout

from scraper import fetch_page
from parser import parse_signals
from trader import open_trade, close_trade, close_position_by_ticket, get_position, init_mt5, show_open_positions, account_summary
from state import processed_signals, position_tracker
from config import SIGNAL_INTERVAL, TRADE_VOLUME

print(f"\n{'='*80}")
print("BLIND FOLLOWER BOT - STARTED")
print(f"Signal interval: {SIGNAL_INTERVAL}s | Volume: {TRADE_VOLUME}")
print(f"{'='*80}\n")

# Prune old signals at startup
def prune_signals(filepath, hours=24):
    """Delete processed signals older than N hours."""
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        pruned = {
            k: v for k, v in data.items()
            if datetime.fromisoformat(v) > cutoff
        }
        with open(filepath, 'w') as f:
            json.dump(pruned, f)
        removed = len(data) - len(pruned)
        if removed > 0:
            print(f"[STARTUP] Pruned {removed} old signals from state (>24h)")
    except FileNotFoundError:
        pass

prune_signals('processed_signals.json', hours=24)

init_mt5()

# Clean up stale position tracking (positions closed via SL/TP but still in tracker)
def cleanup_stale_positions():
    """Remove position tracking for tickets that no longer exist in MT5."""
    all_mt5_tickets = set()
    for pos in (mt5.positions_get() or []):
        all_mt5_tickets.add(pos.ticket)

    stale_count = 0
    for signal_id, metadata in list(position_tracker.all_positions()):
        if metadata["ticket"] not in all_mt5_tickets:
            print(f"[STARTUP] Cleaning up stale position: {signal_id} (ticket {metadata['ticket']} closed via market)")
            position_tracker.remove(signal_id)
            stale_count += 1

    if stale_count > 0:
        print(f"[STARTUP] Cleaned {stale_count} stale positions from tracker")

cleanup_stale_positions()

# Track which frame is active per pair (frame lock)
active_frame = {}

# Log tracked positions at startup
tracked = position_tracker.all_positions()
if tracked:
    print(f"\n[STARTUP] Resuming with {len(tracked)} tracked position(s):")
    for signal_id, meta in tracked:
        signal_time_str = meta.get('signal_time', 'unknown')
        created_str = meta.get('created_at', 'unknown')
        print(f"  • {meta['pair']} {meta['side']} @ {meta['open_price']} (frame: {meta['frame']})")
        print(f"    Signal ID: {signal_id}")
        print(f"    Ticket: {meta['ticket']} | Signal time: {signal_time_str} | Opened at: {created_str}")
    print()

# ──────────────────────────────────────────────────────────────────────────────
# DEBUG HELPER FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

def detect_orphaned_positions():
    """Detect positions in MT5 we don't have in our tracker (shouldn't happen but handle gracefully)."""
    mt5_positions = mt5.positions_get() or []
    tracked_tickets = set(meta["ticket"] for _, meta in position_tracker.all_positions())

    orphaned = []
    for pos in mt5_positions:
        if pos.magic != 777:  # Not our positions
            continue
        if pos.ticket not in tracked_tickets:
            orphaned.append(pos)

    if orphaned:
        print(f"\n[WARNING] Found {len(orphaned)} orphaned position(s) in MT5 (not in tracker):")
        for pos in orphaned:
            pos_type = "BUY" if pos.type == 0 else "SELL"
            print(f"  • {pos.symbol} {pos_type} @ {pos.price_open} (ticket: {pos.ticket}) | Profit: ${pos.profit:.2f}")
        print("  These might be from before the bot started. Manual review recommended.\n")

# Run at startup
detect_orphaned_positions()

def show_tracked_positions():
    """Debug: Show all currently tracked positions with full details."""
    print("\n" + "="*80)
    print("TRACKED POSITIONS SUMMARY")
    print("="*80)

    tracked = dict(position_tracker.all_positions())
    if not tracked:
        print("No positions currently tracked")
        print("="*80 + "\n")
        return

    print(f"Total: {len(tracked)} position(s)\n")

    for signal_id, meta in tracked.items():
        print(f"Signal ID: {signal_id}")
        print(f"  Pair:      {meta['pair']}")
        print(f"  Side:      {meta['side']}")
        print(f"  Ticket:    {meta['ticket']}")
        print(f"  Price:     {meta['open_price']}")
        print(f"  Frame:     {meta['frame']}")
        print(f"  Signal Time: {meta.get('signal_time', 'unknown')}")
        print(f"  Opened At:   {meta.get('created_at', 'unknown')}")
        print()

    print("="*80 + "\n")

# Call at startup to log everything
show_tracked_positions()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SIGNAL LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_signal_cycle():
    """
    Fetch signals and open/close trades with proper deduplication and frame locking.

    Order:
    1. Fetch HTML via proxy
    2. Parse all signals
    3. Sort by timestamp DESC, deduplicate per pair+frame (keep most recent only)
    4. Process ACTIVE signals (with frame lock + MT5 duplicate check)
    5. Process CLOSE signals (frame-matched, closes most recent position on pair+frame)
    6. Log status + sleep
    """

    global active_frame
    now = datetime.now(timezone.utc)

    # ──── FETCH & PARSE SIGNALS ──────────────────────────────────────────────

    html = fetch_page()
    if html is None:
        print(f"[{now.strftime('%H:%M:%S')}] WARNING: Could not fetch signals (proxy failed)")
        return

    signals = parse_signals(html)
    if not signals:
        return

    # ──── SORT & DEDUPLICATE: Most recent signal only per pair+frame ─────────

    signals.sort(key=lambda x: x['time'], reverse=True)
    seen = set()
    filtered_signals = []

    for s in signals:
        key = f"{s['pair']}_{s['frame']}"
        if key not in seen:
            seen.add(key)
            filtered_signals.append(s)

    signals = filtered_signals

    # ──── PROCESS ACTIVE SIGNALS (with frame lock for dedup) ────────────────

    opened = 0
    for s in signals:
        if s["status"] != "ACTIVE":
            continue

        pair = s["pair"]
        frame = s["frame"]
        signal_id = f"{pair}_{s['time']}_{s['side']}_{frame}"

        # Skip if already processed
        if signal_id in processed_signals:
            continue

        # Frame lock: if this pair has a different frame active, skip
        if pair in active_frame and active_frame[pair] != frame:
            continue

        # Open trade
        print(f"[{now.strftime('%H:%M:%S')}] SIGNAL: {pair} {s['side']} @ {s['open']} SL:{s['sl']} TP:{s['tp']}")

        success, ticket = open_trade(s)
        if success:
            opened += 1
            active_frame[pair] = frame  # Lock this frame for this pair
            print(f"  → OPENED ✓")
            # Track position if ticket was obtained
            if ticket:
                position_tracker.add(signal_id, ticket, pair, frame, s['open'], s['side'], s['time'])

        processed_signals[signal_id] = now

    # ──── PROCESS CLOSE SIGNALS (frame-matched + position-matched) ───────────

    closed = 0
    for s in signals:
        if s["status"] != "CLOSE":
            continue

        pair = s["pair"]
        frame = s["frame"]

        # Create unique close signal ID
        close_signal_id = f"{pair}_{s['time']}_CLOSE_{frame}"

        # Skip if we already processed this exact close signal
        if close_signal_id in processed_signals:
            print(f"[{now.strftime('%H:%M:%S')}] CLOSE: {pair} @ {s['open']} (already processed, skipping)")
            continue

        # Only close if frame matches
        if pair in active_frame and active_frame[pair] != frame:
            continue

        # Find the matching open position by pair+frame (use close price to match if multiple exist)
        matching_signal_id, metadata = position_tracker.find_matching_position(pair, frame, s['open'])

        if matching_signal_id and metadata:
            print(f"[{now.strftime('%H:%M:%S')}] CLOSE: {pair} @ {s['open']}")
            print(f"  Matched to Signal ID: {matching_signal_id}")
            print(f"  Original signal time: {metadata.get('signal_time', 'unknown')}")
            print(f"  Closing Ticket: {metadata['ticket']}")

            if close_position_by_ticket(metadata["ticket"], pair):
                closed += 1
                position_tracker.remove(matching_signal_id)
                # Only unlock if no more positions for this pair exist
                remaining = len([m for sid, m in position_tracker.all_positions() if m["pair"] == pair])
                if remaining == 0 and pair in active_frame:
                    del active_frame[pair]  # Unlock pair only if no more positions
                print(f"  → CLOSED ✓")

        else:
            # No matching position found
            print(f"[{now.strftime('%H:%M:%S')}] CLOSE: {pair} @ {s['open']} (no matching position found)")
            print(f"  ⚠ WARN: No tracked position for {pair} on frame {frame}")
            print(f"  Available tracked positions for {pair}:")
            for sid, meta in position_tracker.all_positions():
                if meta["pair"] == pair:
                    print(f"    - Signal {sid}: price {meta['open_price']}, frame {meta['frame']}")
            # Don't auto-close unmatched - website should provide correct price

        # Mark close signal as processed to prevent duplicates
        processed_signals[close_signal_id] = now

    # ──── STATUS ──────────────────────────────────────────────────────────────

    print(f"[{now.strftime('%H:%M:%S')}] Status: {opened} opened, {closed} closed")
    show_open_positions()
    account_summary()


def signal_thread():
    """Main loop: fetch signals every N seconds."""
    while True:
        try:
            run_signal_cycle()
        except Exception as e:
            print(f"ERROR: {e}")

        time.sleep(SIGNAL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# START
# ══════════════════════════════════════════════════════════════════════════════

threading.Thread(target=signal_thread, daemon=True).start()

# Keep main thread alive
while True:
    time.sleep(60)
