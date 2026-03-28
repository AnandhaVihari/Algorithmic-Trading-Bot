"""
TRAILING STOP MANAGER - Simple SL + Portfolio Close System

System:
  1. SL Breakeven Protection: Move SL to breakeven when profit >= $0.40
  2. Portfolio-Level Close: Close ALL positions when:
     - Number of open positions >= 3 AND
     - Total P&L >= (num_positions × $1.00)

Example:
  3 open positions with +$3.10 total profit → Close all
  10 open positions with +$8.50 total profit → Keep open (need $10.00)
  10 open positions with +$10.10 total profit → Close all

SAFETY GUARANTEES:
  ✓ Never reduces SL (only forward movement to breakeven)
  ✓ Only closes positions at portfolio level, never individual
  ✓ Never touches UNMATCHED/FAILED_CLOSE
  ✓ Never interferes with diff logic or VSL
  ✓ Uses real MT5 profit, not estimates
  ✓ Preserves ticket safety and position integrity
"""

import MetaTrader5 as mt5
from typing import Dict, Optional
from datetime import datetime, timezone
from operational_safety import log, LogLevel
from trader import close_position_by_ticket
import json
import os


class TrailingStopManager:
    """Dollar-based trailing stop system using actual MT5 profit."""

    def __init__(self):
        """Initialize trailing stop tracking."""
        # ticket → {entry, tp, original_sl, symbol, side}
        self.position_meta = {}

        # ticket → timestamp when phase changed (for logging)
        self.phase_change_log = {}

        # Load persisted position metadata from previous sessions
        self._load_position_meta()

    def register_position(self, ticket: int, symbol: str, side: str,
                         entry_price: float, tp: float, original_sl: float):
        """Register a newly opened position for trailing stop management.

        MUST be called when trade opens (in open_trade after success).

        Args:
            ticket: Position ticket
            symbol: Trading pair (e.g., 'EURUSD', 'EURUSD+')
            side: 'BUY' or 'SELL'
            entry_price: Entry price of position
            tp: Take profit level
            original_sl: Original stop loss level
        """
        self.position_meta[ticket] = {
            'entry': entry_price,
            'tp': tp,
            'original_sl': original_sl,
            'symbol': symbol,
            'side': side,
            'last_phase': 0,
        }
        print(f"[TRAIL$_REGISTER] T{ticket} {symbol} {side} | Entry: {entry_price:.5f} | TP: {tp:.5f} | SL: {original_sl:.5f}")
        log(LogLevel.DEBUG, f"[TRAIL] Registered T{ticket} {symbol} {side} | Entry: {entry_price} | TP: {tp}")

        # Persist changes immediately
        self._save_position_meta()

    def remove_position(self, ticket: int):
        """Remove position from tracking when closed.

        MUST be called when trade closes (in main loop after successful close).
        """
        if ticket in self.position_meta:
            del self.position_meta[ticket]
        if ticket in self.phase_change_log:
            del self.phase_change_log[ticket]

        # Persist changes immediately
        self._save_position_meta()

    def _save_position_meta(self):
        """Save position metadata to disk for persistence across restarts."""
        try:
            # Convert to JSON-serializable format (tickets are ints, need to stringify)
            data = {
                str(ticket): {
                    'entry': meta['entry'],
                    'tp': meta['tp'],
                    'original_sl': meta['original_sl'],
                    'symbol': meta['symbol'],
                    'side': meta['side'],
                    'last_phase': meta['last_phase']
                }
                for ticket, meta in self.position_meta.items()
            }
            with open('trailing_stop_meta.json', 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"[TRAIL_WARN] Failed to save position_meta: {e}")

    def _load_position_meta(self):
        """Load position metadata from disk after restart."""
        try:
            if not os.path.exists('trailing_stop_meta.json'):
                return

            with open('trailing_stop_meta.json', 'r') as f:
                data = json.load(f)

            # Convert back from string keys to int keys
            for ticket_str, meta in data.items():
                ticket = int(ticket_str)
                self.position_meta[ticket] = meta

            if self.position_meta:
                print(f"[TRAIL_RESTORE] Loaded {len(self.position_meta)} persisted position(s) from disk")
        except Exception as e:
            print(f"[TRAIL_WARN] Failed to load position_meta: {e}")

    def reconcile_with_mt5(self, mt5_module):
        """Remove tracking for positions that no longer exist in MT5.

        Called at start of every update_all_positions() cycle.
        Ensures position_meta stays synchronized with actual MT5 positions.
        """
        active_tickets = set()

        mt5_positions = mt5_module.positions_get()
        if mt5_positions:
            active_tickets = {p.ticket for p in mt5_positions}

        for ticket in list(self.position_meta.keys()):
            if ticket not in active_tickets:
                print(f"[TRAIL_CLEANUP] Removing stale ticket {ticket}")
                self.remove_position(ticket)

    def update_all_positions(self, mt5_module):
        """
        Simple trailing stop system:
        1. Move SL to breakeven when profit >= $0.40
        2. Close ALL positions when:
           - num_positions >= 3 AND
           - total_pnl >= (num_positions × $1.00)

        MUST be called every cycle in main loop:
          1. check_virtual_sl_and_close()
          2. update_all_positions(mt5)        # <-- HERE
          3. run_signal_cycle()

        Args:
            mt5_module: MetaTrader5 module reference
        """
        self.reconcile_with_mt5(mt5_module)

        all_mt5_positions = mt5_module.positions_get()
        if not all_mt5_positions:
            return

        num_positions = len(all_mt5_positions)
        total_pnl = 0
        sl_updates = 0  # Track how many SL updates happened

        # ─── STEP 1: MOVE SL TO BREAKEVEN AT $0.40 PROFIT (ALWAYS ACTIVE) ───
        for pos in all_mt5_positions:
            total_pnl += pos.profit

            if pos.profit >= 0.40:
                # Calculate breakeven SL + 2 pips buffer
                sl_buffer = 0.0002  # 2 pips

                if pos.type == mt5_module.POSITION_TYPE_BUY:
                    # BUY: SL goes 2 pips ABOVE entry (higher = protection from below)
                    new_sl = pos.price_open + sl_buffer
                else:  # SELL
                    # SELL: SL goes 2 pips BELOW entry (lower = protection from above)
                    new_sl = pos.price_open - sl_buffer

                # Only update if moving forward (protective)
                if pos.type == mt5_module.POSITION_TYPE_BUY and new_sl > pos.sl:
                    print(f"[TRAIL_SL] T{pos.ticket} {pos.symbol} BUY profit=${pos.profit:.2f} → Move SL to BE: {pos.sl:.5f} → {new_sl:.5f}")
                    request = {
                        "action": mt5_module.TRADE_ACTION_SLTP,
                        "position": pos.ticket,
                        "sl": new_sl,
                        "tp": pos.tp
                    }
                    result = mt5_module.order_send(request)
                    if result and result.retcode == mt5_module.TRADE_RETCODE_DONE:
                        print(f"  [TRAIL_OK] SL updated")
                        sl_updates += 1
                    else:
                        print(f"  [TRAIL_ERR] SL update failed: {result.retcode if result else 'None'}")

                elif pos.type == mt5_module.POSITION_TYPE_SELL and new_sl < pos.sl:
                    print(f"[TRAIL_SL] T{pos.ticket} {pos.symbol} SELL profit=${pos.profit:.2f} → Move SL to BE: {pos.sl:.5f} → {new_sl:.5f}")
                    request = {
                        "action": mt5_module.TRADE_ACTION_SLTP,
                        "position": pos.ticket,
                        "sl": new_sl,
                        "tp": pos.tp
                    }
                    result = mt5_module.order_send(request)
                    if result and result.retcode == mt5_module.TRADE_RETCODE_DONE:
                        print(f"  [TRAIL_OK] SL updated")
                        sl_updates += 1
                    else:
                        print(f"  [TRAIL_ERR] SL update failed: {result.retcode if result else 'None'}")

        # ─── STEP 2: PORTFOLIO-LEVEL CLOSE (ONLY IF >= 3 POSITIONS) ───────
        if num_positions >= 3:
            close_target = num_positions * 1.00  # $1.00 per position

            if total_pnl >= close_target:
                print(f"[CLOSE_ALL] TRIGGERING PORTFOLIO CLOSE!")
                print(f"             {num_positions} positions | P&L: ${total_pnl:.2f} >= Target: ${close_target:.2f}")

                closed_count = 0
                for pos in all_mt5_positions:
                    try:
                        close_position_by_ticket(pos.ticket)
                        closed_count += 1
                    except Exception as e:
                        print(f"  [ERROR] Failed to close T{pos.ticket}: {e}")

                print(f"[CLOSE_ALL] Closed {closed_count}/{num_positions} positions")



def init_trailing_stop():
    """Initialize trailing stop manager.

    Call in main.py during setup:

        trailing_stop_mgr = init_trailing_stop()
    """
    return TrailingStopManager()


def get_trailing_stop_manager():
    """Get global trailing stop manager (after init_trailing_stop called)."""
    global _trailing_stop_instance
    if '_trailing_stop_instance' not in globals():
        _trailing_stop_instance = TrailingStopManager()
    return _trailing_stop_instance
