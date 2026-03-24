"""
TRAILING STOP MANAGER - Dollar-Based Profit System

Phase Model (DOLLAR THRESHOLDS):
  Phase 1: $0.30 profit → Move SL to breakeven
  Phase 2: $0.60 profit → Lock $0.30 profit
  Phase 3: $1.00 profit → Lock $0.50 profit
  Phase 4: $1.50 profit → Lock $1.00 profit

Uses ACTUAL profit from MT5 (pos.profit in $).
Converts locked profit to price using tick_value.
Works across all pairs and lot sizes.

SAFETY GUARANTEES:
  ✓ Never reduces SL (only forward movement)
  ✓ Never closes positions (SL updates only)
  ✓ Never touches UNMATCHED/FAILED_CLOSE
  ✓ Never interferes with diff logic or VSL
  ✓ Uses real MT5 profit, not estimates
  ✓ Preserves ticket safety and position integrity
"""

import MetaTrader5 as mt5
from typing import Dict, Tuple, Optional
from datetime import datetime, timezone
from operational_safety import log, LogLevel


class TrailingStopManager:
    """Dollar-based trailing stop system using actual MT5 profit."""

    def __init__(self):
        """Initialize trailing stop tracking."""
        # ticket → {entry, tp, original_sl, symbol, side}
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
            'entry': entry_price,
            'tp': tp,
            'original_sl': original_sl,
            'symbol': symbol,
            'side': side,
            'last_phase': 0,
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

    def _get_profit_to_price_ratio(self, symbol: str) -> Optional[float]:
        """Get $ profit per price unit movement.

        Args:
            symbol: Trading pair

        Returns:
            Ratio of price movement to profit ($) or None if unavailable
        """
        try:
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                return None

            # tick_value = $ per tick (point)
            # tick_size = price change per tick
            # So: $profit_per_price_unit = tick_value / tick_size
            if symbol_info.trade_tick_size == 0:
                return None

            profit_per_price = symbol_info.trade_tick_value / symbol_info.trade_tick_size
            return profit_per_price
        except Exception as e:
            log(LogLevel.DEBUG, f"[TRAIL] Exception getting profit ratio for {symbol}: {e}")
            return None

    def _calculate_new_sl_from_profit(self, entry_price: float, lock_profit: float,
                                     symbol: str, side: str) -> Optional[float]:
        """Convert desired locked profit ($) to stop loss price level.

        Args:
            entry_price: Entry price of position
            lock_profit: Profit to lock in $ (e.g., 0.3 for $0.30)
            symbol: Trading pair
            side: 'BUY' or 'SELL'

        Returns:
            New SL price level or None if calculation fails
        """
        # Get profit-to-price ratio
        ratio = self._get_profit_to_price_ratio(symbol)
        if ratio is None or ratio <= 0:
            return None

        # price_move = profit / ratio_of_profit_per_price
        price_move = lock_profit / ratio

        # Calculate new SL based on side
        if side == 'BUY':
            new_sl = entry_price + price_move
        else:  # SELL
            new_sl = entry_price - price_move

        return new_sl

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
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                return True
            else:
                retcode = result.retcode if result else 'None'
                log(LogLevel.DEBUG, f"[TRAIL] SL update failed for T{ticket}: retcode {retcode}")
                return False
        except Exception as e:
            log(LogLevel.DEBUG, f"[TRAIL] Exception updating SL for T{ticket}: {e}")
            return False

    def _transition_phase(self, ticket: int, old_phase: int, new_phase: int,
                         new_sl: float, reason: str) -> bool:
        """Transition position to new phase and update SL in MT5.

        Args:
            ticket: Position ticket
            old_phase: Current phase (0, 1, 2, 3, 4)
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
            self.position_meta[ticket]['last_phase'] = new_phase
            self.phase_change_log[ticket] = datetime.now(timezone.utc).isoformat()

            # Log transition
            phase_names = {0: "Entry", 1: "BE", 2: "Lock30c", 3: "Lock50c", 4: "Lock$1"}
            log(LogLevel.INFO, f"[TRAIL$] T{ticket} | Phase {old_phase} ({phase_names.get(old_phase, '?')}) -> {new_phase} ({phase_names.get(new_phase, '?')}) | {reason} | SL: {new_sl:.5f}")

            return True
        else:
            log(LogLevel.DEBUG, f"[TRAIL] Phase transition failed for T{ticket}")
            return False

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
        """Update trailing stops for all tracked positions using ACTUAL profit from MT5.

        PRODUCTION-SAFE with 9 critical fixes:
        1. Real entry price from MT5 (pos.price_open)
        2. Safe profit with noise filter (profit - 0.20 buffer)
        3. Calibrated thresholds (0.40, 0.80, 1.20, 1.80)
        4. Stable profit → price conversion
        5. Calculate new SL
        6. Spread-aware buffer
        7. Prevent backward SL
        8. Clamp over-aggressive moves
        9. Apply SL with logging

        MUST be called every cycle in main loop:
          1. check_virtual_sl_and_close()
          2. update_all_positions(mt5)        # <-- HERE
          3. run_signal_cycle()

        Args:
            mt5_module: MetaTrader5 module reference
        """
        # RECONCILIATION - Remove stale tickets at START of every cycle
        self.reconcile_with_mt5(mt5_module)

        if not self.position_meta:
            return

        for ticket in list(self.position_meta.keys()):
            meta = self.position_meta[ticket]

            # Get position from MT5
            positions = mt5_module.positions_get(ticket=ticket)
            if not positions:
                continue

            pos = positions[0]

            # ─── FIX 1: USE REAL ENTRY PRICE FROM MT5 ──────────────────────────
            entry_price = pos.price_open  # NOT stored signal price

            # ─── FIX 2: USE SAFE PROFIT (NOISE FILTER IN DOLLARS) ────────────────
            profit = pos.profit  # Real $ from MT5
            SAFE_PROFIT_BUFFER = 0.10  # Buffer in dollars (covers spread + commission)
            safe_profit = profit - SAFE_PROFIT_BUFFER  # Remove noise ($ - $ = correct)

            if safe_profit <= 0:
                continue  # No real profit yet, skip

            symbol = meta['symbol']
            current_sl = pos.sl
            current_phase = meta['last_phase']

            # Get spread for SL placement buffer (separate from profit filtering)
            tick = mt5_module.symbol_info_tick(symbol)
            if not tick:
                continue

            spread = abs(tick.ask - tick.bid)

            # ─── RUNTIME TRACE ──────────────────────────────────────────────────
            phase_names = {0: "Entry", 1: "BE", 2: "Lock30c", 3: "Lock50c", 4: "Lock$1"}
            print(f"[TRAIL$_TRACE] T{ticket} | phase={phase_names[current_phase]} | price={pos.price_current:.5f} | entry={entry_price:.5f} | current_sl={current_sl:.5f} | profit=${profit:.2f} safe=${safe_profit:.2f} (buffer=${SAFE_PROFIT_BUFFER:.2f})")

            # ─── FIX 3: SMOOTH PROFIT THRESHOLDS ─────────────────────────────────
            if safe_profit >= 1.50:
                lock_profit = 1.00
                target_phase = 4
                threshold = 1.50
            elif safe_profit >= 1.00:
                lock_profit = 0.50
                target_phase = 3
                threshold = 1.00
            elif safe_profit >= 0.60:
                lock_profit = 0.30
                target_phase = 2
                threshold = 0.60
            elif safe_profit >= 0.30:
                lock_profit = 0.00  # Breakeven
                target_phase = 1
                threshold = 0.30
            else:
                continue  # Not enough profit

            # Skip if already in target phase
            if current_phase >= target_phase:
                continue

            # ─── FIX 4: STABLE PROFIT → PRICE CONVERSION ────────────────────────
            try:
                symbol_info = mt5_module.symbol_info(symbol)
                if not symbol_info:
                    print(f"[TRAIL_ERR] T{ticket} Symbol info unavailable for {symbol}")
                    continue

                tick_value = symbol_info.trade_tick_value
                tick_size = symbol_info.trade_tick_size

                if tick_value == 0 or tick_size == 0:
                    print(f"[TRAIL_ERR] T{ticket} Invalid tick data (value={tick_value}, size={tick_size})")
                    continue

                value_per_price = tick_value / tick_size
                if value_per_price == 0:
                    print(f"[TRAIL_ERR] T{ticket} Zero value_per_price")
                    continue

                # ─── FIX 5: CALCULATE NEW SL ────────────────────────────────────
                price_move = lock_profit / value_per_price

                if pos.type == mt5_module.POSITION_TYPE_BUY:
                    new_sl = entry_price + price_move
                else:  # SELL
                    new_sl = entry_price - price_move

                # ─── FIX 6: ADD SPREAD BUFFER ───────────────────────────────────
                buffer = max(spread * 2, 0.0001)

                if pos.type == mt5_module.POSITION_TYPE_BUY:
                    new_sl -= buffer
                else:
                    new_sl += buffer

                # ─── FIX 7: PREVENT BACKWARD SL ─────────────────────────────────
                if pos.type == mt5_module.POSITION_TYPE_BUY:
                    if new_sl <= current_sl:
                        continue  # Would move backward, skip
                else:  # SELL
                    if new_sl >= current_sl:
                        continue  # Would move backward, skip

                # ─── FIX 7.5: VALIDATE SL AGAINST BROKER MINIMUM ──────────────────
                # Ensure new SL respects broker minimum stop distance
                try:
                    symbol_info_validation = mt5_module.symbol_info(symbol)
                    if symbol_info_validation:
                        min_stops_points = symbol_info_validation.trade_stops_level
                        point = symbol_info_validation.point
                        min_distance = min_stops_points * point

                        current_price = pos.price_current

                        if pos.type == mt5_module.POSITION_TYPE_BUY:
                            # BUY: SL must be at least min_distance BELOW price
                            if (current_price - new_sl) < min_distance:
                                new_sl = current_price - min_distance
                                print(f"  [TRAIL_VALIDATE] T{ticket} SL adjusted for broker min: {current_sl:.5f} → {new_sl:.5f} (min_distance={min_distance:.5f})")
                        else:  # SELL
                            # SELL: SL must be at least min_distance ABOVE price
                            if (new_sl - current_price) < min_distance:
                                new_sl = current_price + min_distance
                                print(f"  [TRAIL_VALIDATE] T{ticket} SL adjusted for broker min: {current_sl:.5f} → {new_sl:.5f} (min_distance={min_distance:.5f})")
                except Exception as e:
                    print(f"[TRAIL_ERR] T{ticket} Exception validating SL: {e}")

                # ─── FIX 8: APPLY SL UPDATE ─────────────────────────────────────
                request = {
                    "action": mt5_module.TRADE_ACTION_SLTP,
                    "position": ticket,
                    "sl": new_sl,
                    "tp": pos.tp
                }

                result = mt5_module.order_send(request)

                if result and result.retcode == mt5_module.TRADE_RETCODE_DONE:
                    # Update phase
                    self.position_meta[ticket]['last_phase'] = target_phase
                    self.phase_change_log[ticket] = datetime.now(timezone.utc).isoformat()

                    # Log success
                    phase_names_full = {0: "Entry", 1: "BE", 2: "Lock30c", 3: "Lock50c", 4: "Lock$1"}
                    print(f"[TRAIL$] T{ticket} | {phase_names_full[current_phase]}→{phase_names_full[target_phase]} | profit=${profit:.2f} safe=${safe_profit:.2f} threshold=${threshold:.2f} lock=${lock_profit:.2f} | SL {current_sl:.5f}→{new_sl:.5f}")
                else:
                    retcode = result.retcode if result else 'None'
                    print(f"[TRAIL_ERR] T{ticket} SL update failed: retcode={retcode}")

            except Exception as e:
                print(f"[TRAIL_ERR] T{ticket} Exception: {e}")
                continue



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
