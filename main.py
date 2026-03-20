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
    """Remove position tracking for tickets that no longer exist in MT5.

    Also unlocks frames for pairs where all positions are gone.
    """
    global active_frame

    all_mt5_tickets = set()
    for pos in (mt5.positions_get() or []):
        all_mt5_tickets.add(pos.ticket)

    stale_count = 0
    removed_pairs = set()

    for signal_id, metadata in list(position_tracker.all_positions()):
        if metadata["ticket"] not in all_mt5_tickets:
            pair = metadata["pair"]
            print(f"  [STALE] {signal_id} (ticket {metadata['ticket']} closed via TP/SL/market)")
            position_tracker.remove(signal_id)
            stale_count += 1
            removed_pairs.add(pair)

    # Unlock frames for pairs where all positions are now gone
    for pair in removed_pairs:
        remaining = len([m for _, m in position_tracker.all_positions() if m["pair"] == pair])
        if remaining == 0 and pair in active_frame:
            print(f"  [UNLOCK] Frame unlocked for {pair} (all positions closed)")
            del active_frame[pair]

    if stale_count > 0:
        print(f"[CLEANUP] Removed {stale_count} stale position(s)")

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
    1. Clean up stale positions (closed by MT5 via TP/SL)
    2. Fetch HTML via proxy
    3. Parse all signals
    4. Sort by timestamp DESC, deduplicate per pair+frame (keep most recent only)
    5. Process ACTIVE signals (with frame lock + MT5 duplicate check)
    6. Process CLOSE signals (frame-matched, closes most recent position on pair+frame)
    7. Log status + sleep
    """

    global active_frame
    now = datetime.now(timezone.utc)

    # ──── CLEAN UP STALE POSITIONS (closed by MT5 via TP/SL) ──────────────────

    cleanup_stale_positions()

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
        signal_time_str = s['time'].strftime('%H:%M:%S') if hasattr(s['time'], 'strftime') else str(s['time'])
        print(f"[{now.strftime('%H:%M:%S')}] SIGNAL: {pair} {s['side']} @ {s['open']} [Signal time: {signal_time_str}, Frame: {frame}]")

        success, ticket = open_trade(s)
        if success:
            opened += 1
            active_frame[pair] = frame  # Lock this frame for this pair
            print(f"  → OPENED ✓ Ticket: {ticket} (Signal ID: {signal_id})")
            # Track position if ticket was obtained
            if ticket:
                position_tracker.add(signal_id, ticket, pair, frame, s['open'], s['side'], s['time'], tp=s['tp'], sl=s['sl'])

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

        # Find the matching open position by pair+frame
        # Key: match by TP and SL values (they uniquely identify each trade!)
        matching_signal_id, metadata = position_tracker.find_matching_position(pair, frame, tp=s.get('tp'), sl=s.get('sl'))

        if matching_signal_id and metadata:
            print(f"[{now.strftime('%H:%M:%S')}] CLOSE: {pair} @ {s['open']} [Frame: {frame}] TP:{s.get('tp')} SL:{s.get('sl')}")
            print(f"  ✓ Matched to signal ID: {matching_signal_id}")
            print(f"    Signal time: {metadata.get('signal_time', 'unknown')} | Original price: {metadata['open_price']} | Side: {metadata['side']}")
            print(f"    TP match: {metadata.get('tp')} | SL match: {metadata.get('sl')}")
            print(f"    Ticket: {metadata['ticket']}")

            if close_position_by_ticket(metadata["ticket"], pair):
                closed += 1
                position_tracker.remove(matching_signal_id)
                # Only unlock if no more positions for this pair exist
                remaining = len([m for sid, m in position_tracker.all_positions() if m["pair"] == pair])
                if remaining == 0 and pair in active_frame:
                    del active_frame[pair]  # Unlock pair only if no more positions
                print(f"  ✓ CLOSED")

        else:
            # No matching position found
            print(f"[{now.strftime('%H:%M:%S')}] CLOSE: {pair} @ {s['open']} [Frame: {frame}]")
            print(f"  ✗ No matching position found!")
            print(f"  Available tracked positions for {pair}:")
            found_any = False
            for sid, meta in position_tracker.all_positions():
                if meta["pair"] == pair:
                    found_any = True
                    print(f"    - Signal: {sid}")
                    print(f"      Time: {meta.get('signal_time', 'unknown')} | Price: {meta['open_price']} | Frame: {meta['frame']} | Ticket: {meta['ticket']}")
            if not found_any:
                print(f"    (None)")
            # Don't auto-close unmatched - website should provide correct price

        # Mark close signal as processed to prevent duplicates
        processed_signals[close_signal_id] = now

    # ──── STATUS ──────────────────────────────────────────────────────────────

    print(f"[{now.strftime('%H:%M:%S')}] Status: {opened} opened, {closed} closed")
    show_open_positions()
    account_summary()


def signal_thread():
    """Main loop: fetch signals every N seconds."""
    import MetaTrader5 as mt5

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
                    time.sleep(5)  # Wait before retrying
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
