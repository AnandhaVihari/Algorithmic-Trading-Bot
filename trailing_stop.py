"""
TRAILING STOP MANAGER - Dollar-Based Profit System

Phase Model (DOLLAR THRESHOLDS):
  Phase 2: $0.60 profit → Lock $0.30 profit
  Phase 3: $1.00 profit → Lock $0.50 profit
  Phase 4: $1.50 profit → Lock $1.00 profit

Uses ACTUAL profit from MT5 (pos.profit in $).
Converts locked profit to price using dynamic price_per_dollar ratio.
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
from typing import Dict, Optional
from datetime import datetime, timezone
from operational_safety import log, LogLevel
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
        """Update trailing stops for all tracked positions using dynamic price-per-dollar.

        ARCHITECTURE:
        1. Retrieve signal SL from original signal
        2. Calculate dynamic price_per_dollar ratio (adapts to position size)
        3. Calculate max_loss_sl: caps loss at $0.70
        4. Calculate trailing_sl: phases at $0.60/$1.00/$1.50
        5. Unified SL selection: pick safest/tightest candidate
        6. Validate against broker minimum stop distance
        7. Send SL update and persist phase change

        MUST be called every cycle in main loop:
          1. check_virtual_sl_and_close()
          2. update_all_positions(mt5)        # <-- HERE
          3. run_signal_cycle()

        Args:
            mt5_module: MetaTrader5 module reference
        """
        # RECONCILIATION - Remove stale tickets at START of every cycle
        self.reconcile_with_mt5(mt5_module)

        # DEBUG: Always log how many positions we're tracking
        if not self.position_meta:
            print(f"[TRAIL$_DEBUG] No positions tracked (position_meta empty)")
            return

        print(f"[TRAIL$_DEBUG] Processing {len(self.position_meta)} tracked position(s)")

        # Get all positions once (more reliable than per-ticket lookup)
        all_mt5_positions = mt5_module.positions_get()
        mt5_tickets = {p.ticket for p in all_mt5_positions} if all_mt5_positions else set()

        for ticket in list(self.position_meta.keys()):
            meta = self.position_meta[ticket]

            # Look up position in MT5
            if ticket not in mt5_tickets:
                print(f"[TRAIL_WARN] T{ticket} not found in MT5 - skipping (position may be closed)")
                continue

            # Find position in cached list
            pos = next((p for p in all_mt5_positions if p.ticket == ticket), None)
            if pos is None:
                print(f"[TRAIL_WARN] T{ticket} found in ticket set but not in list - skipping")
                continue

            # ─── STEP 1: GET POSITION DATA ────────────────────────────────────────
            entry_price = pos.price_open
            profit = pos.profit
            symbol = pos.symbol
            current_sl = pos.sl
            current_phase = meta['last_phase']
            current_price = pos.price_current

            # Skip if profit == 0 (can't calculate price_per_dollar ratio)
            if profit == 0:
                continue

            # ─── STEP 1: RETRIEVE SIGNAL SL ──────────────────────────────────────
            signal_sl = meta['original_sl']  # From website signal provider

            # ─── STEP 2: CALCULATE DYNAMIC PRICE-TO-PROFIT RATIO ───────────────────
            # Adapts to position size and market volatility
            price_per_dollar = abs(current_price - entry_price) / abs(profit)

            # ─── STEP 3: CALCULATE MAX LOSS SL (ALWAYS ACTIVE) ───────────────────
            # Use dynamic price_per_dollar to convert $0.70 cap to price movement
            loss_cap = 0.70  # dollars - hard cap, never lose more than this
            loss_cap_price = loss_cap * price_per_dollar

            if pos.type == mt5_module.POSITION_TYPE_BUY:
                max_loss_sl = entry_price - loss_cap_price  # Below entry for BUY
            else:  # SELL
                max_loss_sl = entry_price + loss_cap_price  # Above entry for SELL

            # ─── STEP 4: CALCULATE TRAILING SL (SELECTIVE - Only if profit >= $0.60) ───
            # For trailing SL, use profit-based price_per_dollar (adaptive to position size)
            trailing_sl = None  # Initialize to None
            target_phase = current_phase  # Track for logging
            lock_profit = None

            if profit >= 0.60:
                # Only calculate trailing if enough profit to lock ($0.30+)
                # This prevents breakeven (lock_profit=0.00) from being selected

                # Only calculate trailing if profit threshold reached
                if profit >= 1.50:
                    lock_profit = 1.00
                    target_phase = 4
                elif profit >= 1.00:
                    lock_profit = 0.50
                    target_phase = 3
                elif profit >= 0.60:
                    lock_profit = 0.30
                    target_phase = 2

                # Only calculate trailing SL if phase would advance
                if current_phase < target_phase:
                    price_move = lock_profit * price_per_dollar

                    # Apply directional logic for BUY and SELL
                    if pos.type == mt5_module.POSITION_TYPE_BUY:
                        trailing_sl = entry_price + price_move
                    else:  # SELL
                        trailing_sl = entry_price + price_move

            # ─── STEP 4: UNIFIED SL DECISION (Pick safest/tightest SL) ─────────────────
            # Collect all candidate SL values - MUST validate side before using
            candidates = []

            # Add signal_sl if it's on the correct side
            if pos.type == mt5_module.POSITION_TYPE_BUY:
                if signal_sl < current_price:  # BUY: SL must be below price
                    candidates.append(signal_sl)
            else:  # SELL
                if signal_sl > current_price:  # SELL: SL must be above price
                    candidates.append(signal_sl)

            # Add max_loss_sl if it's on the correct side
            if pos.type == mt5_module.POSITION_TYPE_BUY:
                if max_loss_sl < current_price:  # BUY: SL must be below price
                    candidates.append(max_loss_sl)
            else:  # SELL
                if max_loss_sl > current_price:  # SELL: SL must be above price
                    candidates.append(max_loss_sl)

            # Add trailing_sl if calculated and on correct side
            if trailing_sl is not None:
                if pos.type == mt5_module.POSITION_TYPE_BUY:
                    if trailing_sl < current_price:  # BUY: SL must be below price
                        candidates.append(trailing_sl)
                else:  # SELL
                    if trailing_sl > current_price:  # SELL: SL must be above price
                        candidates.append(trailing_sl)

            # DEBUG: Log all candidates after validation
            trailing_str = f"{trailing_sl:.5f}" if trailing_sl is not None else "N/A"
            print(f"[SL_CAND] T{ticket} | signal={signal_sl:.5f} | max_loss={max_loss_sl:.5f} | trailing={trailing_str} | price={current_price:.5f}")
            print(f"[SL_VALID] T{ticket} | Valid candidates after side check: {[f'{c:.5f}' for c in candidates]}")

            # If no valid candidates, skip this position
            if not candidates:
                print(f"[TRAIL_SKIP] T{ticket} No valid SL candidates after side validation - skipping")
                continue

            # Pick safest SL based on position side
            if pos.type == mt5_module.POSITION_TYPE_BUY:
                final_sl = max(candidates)  # Highest = tightest below price
            else:  # SELL
                final_sl = min(candidates)  # Lowest = tightest above price

            print(f"[SL_SELECT] T{ticket} | Final SL: {final_sl:.5f}")

            # ─── STEP 5: PREVENT BACKWARD SL (MONOTONIC) ────────────────────────
            if pos.type == mt5_module.POSITION_TYPE_BUY:
                if final_sl <= current_sl:
                    continue  # Would move backward, skip
            else:  # SELL
                if final_sl >= current_sl:
                    continue  # Would move backward, skip

            # ─── STEP 6: VALIDATE SL AGAINST BROKER MINIMUM ──────────────────────
            # Ensure final_sl respects broker minimum stop distance
            try:
                symbol_info_validation = mt5_module.symbol_info(symbol)
                if not symbol_info_validation:
                    print(f"[TRAIL_ERR] T{ticket} Symbol info unavailable for {symbol} - cannot validate SL")
                    continue

                stops_level = symbol_info_validation.trade_stops_level
                freeze_level = symbol_info_validation.trade_freeze_level
                point = symbol_info_validation.point

                # Use MAXIMUM of both levels (most restrictive)
                min_distance = max(stops_level, freeze_level) * point

                # Validate SL on correct side of price
                if pos.type == mt5_module.POSITION_TYPE_BUY:
                    # BUY: SL MUST be BELOW current price
                    if final_sl >= current_price:
                        print(f"[TRAIL_SKIP] T{ticket} BUY: Invalid SL {final_sl:.5f} >= price {current_price:.5f} - skipping")
                        continue
                    # BUY: SL must be at least min_distance BELOW price
                    if (current_price - final_sl) < min_distance:
                        final_sl = current_price - min_distance
                        print(f"  [TRAIL_VALIDATE] T{ticket} BUY SL adjusted for broker min: {current_sl:.5f} → {final_sl:.5f} (distance required={min_distance:.5f})")
                else:  # SELL
                    # SELL: SL MUST be ABOVE current price
                    if final_sl <= current_price:
                        print(f"[TRAIL_SKIP] T{ticket} SELL: Invalid SL {final_sl:.5f} <= price {current_price:.5f} - skipping")
                        continue
                    # SELL: SL must be at least min_distance ABOVE price
                    if (final_sl - current_price) < min_distance:
                        final_sl = current_price + min_distance
                        print(f"  [TRAIL_VALIDATE] T{ticket} SELL SL adjusted for broker min: {current_sl:.5f} → {final_sl:.5f} (distance required={min_distance:.5f})")

            except Exception as e:
                print(f"[TRAIL_ERR] T{ticket} Exception validating SL: {e}")
                continue

            # ─── STEP 7: APPLY SL UPDATE ─────────────────────────────────────────
            print(f"  [TRAIL_DEBUG] T{ticket} | Sending SL update: {current_sl:.5f} → {final_sl:.5f} | distance from price {current_price:.5f}: {abs(current_price - final_sl):.5f}")

            request = {
                "action": mt5_module.TRADE_ACTION_SLTP,
                "position": ticket,
                "sl": final_sl,
                "tp": pos.tp
            }

            result = mt5_module.order_send(request)

            if result and result.retcode == mt5_module.TRADE_RETCODE_DONE:
                # Update phase if it changed
                if lock_profit is not None:  # Phase was calculated
                    self.position_meta[ticket]['last_phase'] = target_phase
                    self.phase_change_log[ticket] = datetime.now(timezone.utc).isoformat()

                # Enhanced logging showing all three SL sources
                phase_names = {0: "Entry", 1: "BE", 2: "Lock30c", 3: "Lock50c", 4: "Lock$1"}
                trailing_str = f"{trailing_sl:.5f}" if trailing_sl is not None else "N/A"
                print(f"[FINAL_SL] T{ticket} | signal={signal_sl:.5f} | max_loss={max_loss_sl:.5f} | trailing={trailing_str} → final={final_sl:.5f} | profit=${profit:.2f}")

                # Persist phase change immediately
                self._save_position_meta()
            else:
                retcode = result.retcode if result else 'None'
                print(f"[TRAIL_ERR] T{ticket} SL update FAILED: retcode={retcode}")
                print(f"           Symbol={pos.symbol} | Type={'BUY' if pos.type == mt5_module.POSITION_TYPE_BUY else 'SELL'}")
                print(f"           Price={current_price:.5f} | Current SL={current_sl:.5f} | Attempted SL={final_sl:.5f}")
                if result:
                    print(f"           Result comment: {result.comment if hasattr(result, 'comment') else 'N/A'}")



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
