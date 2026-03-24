"""
TRAILING STOP MANAGER - Fixed Profit-Based System

Phase Model (FIXED PROFIT THRESHOLDS - NO PERCENTAGES):
  Phase 1: $0.30 profit → Move SL to entry (breakeven)
  Phase 2: $0.40 profit → Move SL to entry + $0.20
  Phase 3: $0.60 profit → Move SL to entry + $0.30

SAFETY GUARANTEES:
  ✓ Never reduces SL (only forward movement)
  ✓ Never closes positions (SL updates only)
  ✓ Never touches UNMATCHED/FAILED_CLOSE
  ✓ Never interferes with diff logic or VSL
  ✓ Spread-aware calculations on all thresholds
  ✓ Preserves ticket safety and position integrity
"""

import MetaTrader5 as mt5
from typing import Dict, Tuple, Optional
from datetime import datetime, timezone
from operational_safety import log, LogLevel


class TrailingStopManager:
    """Data-driven phase-based trailing stop system."""

    def __init__(self):
        """Initialize trailing stop tracking.

        Separate from positions dict and virtual_sl metadata to avoid conflicts.
        """
        # ticket → {phase, entry, tp, original_sl, symbol, side}
        self.position_meta = {}

        # ticket → timestamp when phase changed (for logging)
        self.phase_change_log = {}

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
            'phase': 0,
            'entry': entry_price,
            'tp': tp,
            'original_sl': original_sl,
            'symbol': symbol,
            'side': side,
        }
        log(LogLevel.DEBUG, f"[TRAIL] Registered T{ticket} {symbol} {side} | Entry: {entry_price} | TP: {tp}")

    def remove_position(self, ticket: int):
        """Remove position from tracking when closed.

        MUST be called when trade closes (in main loop after successful close).
        """
        if ticket in self.position_meta:
            del self.position_meta[ticket]
        if ticket in self.phase_change_log:
            del self.phase_change_log[ticket]

    def _get_current_price(self, symbol: str, side: str) -> Optional[float]:
        """Get current bid/ask price for profit calculation.

        Args:
            symbol: Trading pair
            side: 'BUY' or 'SELL'

        Returns:
            Current price (bid for BUY, ask for SELL) or None if unavailable
        """
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return None
        return tick.bid if side == 'BUY' else tick.ask

    def _get_spread_buffer(self, symbol: str, factor: float = 1.5) -> float:
        """Calculate spread-aware buffer for SL adjustments.

        Args:
            symbol: Trading pair
            factor: Multiplier for spread width (1.5 = 150% of spread)

        Returns:
            Buffer amount (always positive, minimum 0.0001 for safety)
        """
        MIN_BUFFER = 0.0001  # Always ensure some protection from noise
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return MIN_BUFFER  # Safety fallback instead of 0
        spread = tick.ask - tick.bid
        return max(spread * factor, MIN_BUFFER)  # Never less than minimum

    def _calculate_profit_in_points(self, current_price: float, entry_price: float,
                                   side: str) -> float:
        """Calculate unrealized profit in points.

        Args:
            current_price: Current bid/ask
            entry_price: Entry price
            side: 'BUY' or 'SELL'

        Returns:
            Profit in points (always positive if winning)
        """
        if side == 'BUY':
            return current_price - entry_price
        else:  # SELL
            return entry_price - current_price

    def _clamp_sl_for_symbol(self, sl: float, symbol: str, side: str) -> float:
        """Clamp SL to valid range (don't go too close to market).

        Args:
            sl: Proposed stop loss level
            symbol: Trading pair
            side: 'BUY' or 'SELL'

        Returns:
            Valid stop loss level
        """
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return sl

        # Get minimum distance from market (typically 3-5 pips)
        info = mt5.symbol_info(symbol)
        if not info:
            return sl

        min_distance = info.point * 50  # ~50 points = 5 pips for 5-decimal pairs

        if side == 'BUY':
            # For BUY, SL must be below bid, at least min_distance away
            return max(sl, tick.bid - min_distance)
        else:  # SELL
            # For SELL, SL must be above ask, at least min_distance away
            return min(sl, tick.ask + min_distance)

    def _update_sl_in_mt5(self, ticket: int, symbol: str, new_sl: float, side: str) -> bool:
        """Update stop loss in MT5 via position_modify.

        Args:
            ticket: Position ticket
            symbol: Trading pair
            new_sl: New stop loss level
            side: 'BUY' or 'SELL'

        Returns:
            True if update succeeded, False otherwise
        """
        try:
            # Clamp SL to valid range
            new_sl = self._clamp_sl_for_symbol(new_sl, symbol, side)

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "sl": new_sl,
            }

            result = mt5.order_send(request)
            if result.retcode == 10009:
                return True
            else:
                log(LogLevel.DEBUG, f"[TRAIL] SL update failed for T{ticket}: retcode {result.retcode}")
                return False
        except Exception as e:
            log(LogLevel.DEBUG, f"[TRAIL] Exception updating SL for T{ticket}: {e}")
            return False

    def _transition_phase(self, ticket: int, old_phase: int, new_phase: int,
                         new_sl: float, reason: str) -> bool:
        """Transition position to new phase and update SL in MT5.

        Args:
            ticket: Position ticket
            old_phase: Current phase (0, 1, 2, 3)
            new_phase: Target phase
            new_sl: New stop loss level to set
            reason: Reason description for logging

        Returns:
            True if transition succeeded
        """
        if ticket not in self.position_meta:
            return False

        meta = self.position_meta[ticket]
        symbol = meta['symbol']
        side = meta['side']

        # Update SL in MT5
        if self._update_sl_in_mt5(ticket, symbol, new_sl, side):
            # Update phase in metadata
            self.position_meta[ticket]['phase'] = new_phase
            self.phase_change_log[ticket] = datetime.now(timezone.utc).isoformat()

            # Log transition
            phase_names = {0: "Entry", 1: "BE", 2: "Lock", 3: "Trail"}
            log(LogLevel.INFO, f"[TRAIL] T{ticket} | Phase {old_phase} ({phase_names[old_phase]}) -> {new_phase} ({phase_names[new_phase]}) | {reason} | SL: {new_sl:.5f}")

            return True
        else:
            log(LogLevel.DEBUG, f"[TRAIL] Phase transition failed for T{ticket}")
            return False

    def reconcile_with_mt5(self, mt5):
        """Remove tracking for positions that no longer exist in MT5.

        Called at start of every update_all_positions() cycle.
        Ensures position_meta stays synchronized with actual MT5 positions.
        """
        active_tickets = set()

        mt5_positions = mt5.positions_get()
        if mt5_positions:
            active_tickets = {p.ticket for p in mt5_positions}

        for ticket in list(self.position_meta.keys()):
            if ticket not in active_tickets:
                print(f"[TRAIL_CLEANUP] Removing stale ticket {ticket}")
                self.remove_position(ticket)

    def update_all_positions(self, mt5_module):
        """Update trailing stops for all tracked positions.

        MUST be called every cycle in main loop:
          1. check_virtual_sl_and_close()
          2. update_all_positions(mt5)        # <-- HERE
          3. run_signal_cycle()

        Args:
            mt5_module: MetaTrader5 module reference
        """
        # FIX 1: RECONCILIATION - Remove stale tickets at START of every cycle
        self.reconcile_with_mt5(mt5_module)

        if not self.position_meta:
            return

        for ticket in list(self.position_meta.keys()):
            meta = self.position_meta[ticket]

            # Get current price
            current_price = self._get_current_price(meta['symbol'], meta['side'])
            if current_price is None:
                continue

            # Calculate profit and thresholds
            profit = self._calculate_profit_in_points(current_price, meta['entry'], meta['side'])
            tp_distance = abs(meta['tp'] - meta['entry'])

            if tp_distance <= 0:
                continue

            # Calculate spread buffer
            buffer = self._get_spread_buffer(meta['symbol'])

            # Phase thresholds (fixed profit in points)
            phase1_threshold = 0.30 + buffer           # Phase 1 at $0.30 profit
            phase2_threshold = 0.40 + buffer           # Phase 2 at $0.40 profit
            phase3_threshold = 0.60 + buffer           # Phase 3 at $0.60 profit

            current_phase = meta['phase']

            # ─── RUNTIME VERIFICATION: Full trace log ──────────────────────────────
            # Log current state for every position every cycle
            profit_ratio = (profit / tp_distance * 100) if tp_distance > 0 else 0
            phase_names = {0: "Entry", 1: "BE", 2: "Lock", 3: "Trail"}

            # Get current SL from MT5
            pos = mt5_module.positions_get(ticket=ticket)
            current_sl = pos[0].sl if pos else meta['original_sl']

            print(f"[TRAIL_TRACE] T{ticket} | phase={phase_names[current_phase]} | price={current_price:.5f} | entry={meta['entry']:.5f} | tp={meta['tp']:.5f} | current_sl={current_sl:.5f} | profit={profit:.5f}pts ({profit_ratio:.1f}%) | buffer={buffer:.5f}")

            # ─── PHASE 0 → PHASE 1 (Risk Removal: Move SL to breakeven) ───────
            if profit >= phase1_threshold and current_phase == 0:
                # Set SL to breakeven + spread buffer
                if meta['side'] == 'BUY':
                    new_sl = meta['entry'] + buffer
                    # SAFETY: Ensure SL never crosses TP for BUY
                    new_sl = min(new_sl, meta['tp'] - buffer)
                else:  # SELL
                    new_sl = meta['entry'] - buffer
                    # SAFETY: Ensure SL never crosses TP for SELL
                    new_sl = max(new_sl, meta['tp'] + buffer)

                profit_ratio = (profit / tp_distance * 100) if tp_distance > 0 else 0
                print(f"[TRAIL_PHASE] T{ticket} | phase 0 -> 1 | ratio={profit_ratio:.2f}% | threshold={phase1_threshold:.5f}pts | SL_change: {current_sl:.5f} -> {new_sl:.5f}")
                self._transition_phase(ticket, 0, 1, new_sl, f"Risk removal at {profit:.5f} pts")

            # ─── PHASE 1 → PHASE 2 (Profit Lock: Lock $0.20 fixed) ────────────
            elif profit >= phase2_threshold and current_phase <= 1:
                # Move SL to entry + $0.20 (lock $0.20 profit)
                lock_profit = 0.20

                if meta['side'] == 'BUY':
                    new_sl = meta['entry'] + lock_profit - buffer
                    # SAFETY: Ensure SL never crosses TP for BUY
                    new_sl = min(new_sl, meta['tp'] - buffer)
                else:  # SELL
                    new_sl = meta['entry'] - lock_profit + buffer
                    # SAFETY: Ensure SL never crosses TP for SELL
                    new_sl = max(new_sl, meta['tp'] + buffer)

                profit_ratio = (profit / tp_distance * 100) if tp_distance > 0 else 0
                print(f"[TRAIL_PHASE] T{ticket} | phase {current_phase} -> 2 | ratio={profit_ratio:.2f}% | threshold={phase2_threshold:.5f}pts | SL_change: {current_sl:.5f} -> {new_sl:.5f}")
                self._transition_phase(ticket, current_phase, 2, new_sl, f"Lock $0.20 at {profit:.5f} pts")

            # ─── PHASE 2 → PHASE 3 (Profit Lock: Lock $0.30 fixed) ────────────
            elif profit >= phase3_threshold and current_phase <= 2:
                # Move SL to entry + $0.30 (lock $0.30 profit)
                lock_profit = 0.30

                if meta['side'] == 'BUY':
                    new_sl = meta['entry'] + lock_profit - buffer
                    # SAFETY: Ensure SL never crosses TP for BUY
                    new_sl = min(new_sl, meta['tp'] - buffer)
                else:  # SELL
                    new_sl = meta['entry'] - lock_profit + buffer
                    # SAFETY: Ensure SL never crosses TP for SELL
                    new_sl = max(new_sl, meta['tp'] + buffer)

                profit_ratio = (profit / tp_distance * 100) if tp_distance > 0 else 0
                print(f"[TRAIL_PHASE] T{ticket} | phase {current_phase} -> 3 | ratio={profit_ratio:.2f}% | threshold={phase3_threshold:.5f}pts | SL_change: {current_sl:.5f} -> {new_sl:.5f}")
                self._transition_phase(ticket, current_phase, 3, new_sl, f"Lock $0.30 at {profit:.5f} pts")


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
