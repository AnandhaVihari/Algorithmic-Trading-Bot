"""Virtual SL - Spread-Aware Stop Loss Management

Prevents premature SL hits due to spread widening.
Keeps original broker SL as safety net.
Prevents reopen after bot-triggered closes (lifecycle-driven only).

Enhancement v2:
- Lifecycle-based reopen prevention (only reopen when signal disappears)
- Max spread tracking per ticket (prevents SL tightening after spike)
- Timestamp tracking for logging and debugging
- Separate position_meta dict for enhanced tracking
"""

from typing import Dict, Tuple, Optional
from datetime import datetime, timezone, timedelta
from operational_safety import log, LogLevel


class VirtualSLManager:
    """Manages spread-aware stop losses for all open positions."""

    def __init__(self, spread_factor: float = 1.5, cooldown_seconds: int = 300, reset_confirm_seconds: int = 20):
        """
        Initialize Virtual SL manager.

        Args:
            spread_factor: Multiplier for spread compensation (1.5-2.0)
                          Higher = more protection from spread spikes
            cooldown_seconds: Deprecated - kept for compatibility only.
                             Reopen is lifecycle-driven (signal disappearance), not time-based.
                             This parameter has no effect on reopen logic.
            reset_confirm_seconds: Duration (seconds) signal must stay missing before lifecycle reset.
                                  Prevents false reopen from brief flickering.
                                  Default 20 (2 cycles at 10s interval).
        """
        self.spread_factor = spread_factor
        self.cooldown_seconds = cooldown_seconds
        self.reset_confirm_seconds = reset_confirm_seconds

        # {ticket: {original_sl, tp, side, pair, entry_price, opened_at}}
        self.metadata = {}

        # {key: timestamp} - positions closed by bot with timestamp (for logging/debugging)
        self.closed_by_bot = {}

        # {ticket: max_spread_seen} - track max spread per ticket to prevent SL tightening
        self.max_spread_seen = {}

        # {key: timestamp} - tracks when each closed signal first went missing (debouncing)
        self.signal_missing_since = {}

    def add_position(self, ticket: int, pair: str, side: str,
                     original_sl: float, tp: float, entry_price: float):
        """Register position for virtual SL tracking."""
        now = datetime.now(timezone.utc).isoformat()
        self.metadata[ticket] = {
            "ticket": ticket,
            "pair": pair,
            "side": side,
            "original_sl": original_sl,
            "tp": tp,
            "entry_price": entry_price,
            "opened_at": now,  # NEW: track when position was opened
        }
        # Initialize max spread tracking
        self.max_spread_seen[ticket] = 0.0

    def remove_position(self, ticket: int):
        """Unregister position (normal close)."""
        self.metadata.pop(ticket, None)
        self.max_spread_seen.pop(ticket, None)

    def mark_closed_by_bot(self, key: Tuple):
        """Mark key as closed by bot with timestamp (for logging and debugging).

        The timestamp is kept only for visibility in logs. Reopen is NOT controlled by time.
        Reopen happens only when cleanup_closed_signals() detects signal disappeared.
        """
        self.closed_by_bot[key] = datetime.now(timezone.utc)
        log(LogLevel.DEBUG, f"Marked {key} as closed_by_bot at {self.closed_by_bot[key].isoformat()}")

    def is_closed_by_bot(self, key: Tuple) -> bool:
        """Check if position was closed by virtual SL (prevent reopen until signal disappears).

        Reopen is ONLY allowed when the signal completely disappears from the website.
        This is enforced by cleanup_closed_signals() which removes the key when it no longer
        appears in current signals.

        Returns:
            True if the signal should stay closed (key still in closed_by_bot dict)
            False if signal disappeared and reopen is allowed
        """
        return key in self.closed_by_bot

    def cleanup_closed_signals(self, curr_keys):
        """Remove from closed_by_bot if signal disappeared AND stayed missing long enough.

        Debouncing: signal must be missing for reset_confirm_seconds to prevent false reopen
        from brief website flickers.

        Workflow:
        1. Signal exists → open trade
        2. VSL closes trade → mark_closed_by_bot(key)
        3. Signal still exists → is_closed_by_bot(key) = True (blocked)
        4. Signal disappears → start timer in signal_missing_since
        5. Signal reappears within timer → clear timer, stay closed
        6. Signal missing > reset_confirm_seconds → lifecycle reset, allow reopen

        Call this after computing curr_keys in main loop.
        """
        now = datetime.now(timezone.utc)
        curr_keys_set = set(curr_keys)

        # Check each signal that was closed by bot
        for key in list(self.closed_by_bot.keys()):
            if key not in curr_keys_set:
                # Signal missing - track time
                if key not in self.signal_missing_since:
                    # First time missing - start timer
                    self.signal_missing_since[key] = now
                    log(LogLevel.DEBUG, f"Signal {key} missing - starting confirmation timer")
                else:
                    # Signal already missing - check if timer expired
                    age = (now - self.signal_missing_since[key]).total_seconds()
                    if age >= self.reset_confirm_seconds:
                        # Confirmed missing long enough - lifecycle reset
                        del self.closed_by_bot[key]
                        del self.signal_missing_since[key]
                        log(LogLevel.INFO, f"Signal {key} missing for {age:.0f}s, lifecycle reset confirmed - allowing reopen")
            else:
                # Signal reappeared - clear timer
                if key in self.signal_missing_since:
                    del self.signal_missing_since[key]
                    log(LogLevel.DEBUG, f"Signal {key} reappeared - cancelling lifecycle reset timer")

    def check_and_close_all(self, mt5, positions, close_position_cb):
        """Check virtual SL for all positions and close if triggered.

        Core logic (runs every 10s cycle):
        1. Get current spread for each pair
        2. Update max_spread_seen (prevents SL tightening after spike)
        3. Calculate trigger_sl with spread_factor compensation
        4. Close if price touches trigger_sl
        5. Mark as closed_by_bot with timestamp

        Args:
            mt5: MT5 API object
            positions: PositionStore instance
            close_position_cb: Callback function(ticket, pair) -> bool

        Returns:
            List of (ticket, key, close_reason) closed by virtual SL
        """
        closed_tickets = []

        # Iterate through all positions
        for key, tickets in list(positions.positions.items()):
            if not tickets:
                continue

            # Skip special buckets
            if key[0] in ("_UNMATCHED_", "_FAILED_CLOSE_"):
                continue

            pair = key[0]
            side = key[1]

            try:
                # Get current tick price and spread
                tick = mt5.symbol_info_tick(pair)
                if not tick:
                    log(LogLevel.DEBUG, f"No tick for {pair}, skipping virtual SL check")
                    continue

                spread = tick.ask - tick.bid
                bid = tick.bid
                ask = tick.ask

                # Check each ticket in this key
                for ticket in tickets[:]:  # Copy list to iterate safely
                    # STALE TICKET CHECK: Skip if not in MT5 anymore
                    if ticket not in self.metadata:
                        continue

                    meta = self.metadata[ticket]
                    original_sl = meta["original_sl"]

                    # UPDATE MAX SPREAD: Use highest spread seen (OPTIONAL but recommended)
                    if ticket in self.max_spread_seen:
                        self.max_spread_seen[ticket] = max(self.max_spread_seen[ticket], spread)
                        effective_spread = self.max_spread_seen[ticket]
                    else:
                        effective_spread = spread
                        self.max_spread_seen[ticket] = spread

                    # Determine if should close
                    should_close = False
                    close_reason = None

                    if side == "BUY":
                        # For BUY: SL is below entry
                        # Add spread compensation below SL
                        trigger_sl = original_sl - (effective_spread * self.spread_factor)

                        if bid <= trigger_sl:
                            should_close = True
                            close_reason = (
                                f"VSL | BID {bid:.5f} <= trigger {trigger_sl:.5f} | "
                                f"Spread {spread:.5f} | MaxSpread {effective_spread:.5f} | "
                                f"SL {original_sl:.5f}"
                            )

                    elif side == "SELL":
                        # For SELL: SL is above entry
                        # Add spread compensation above SL
                        trigger_sl = original_sl + (effective_spread * self.spread_factor)

                        if ask >= trigger_sl:
                            should_close = True
                            close_reason = (
                                f"VSL | ASK {ask:.5f} >= trigger {trigger_sl:.5f} | "
                                f"Spread {spread:.5f} | MaxSpread {effective_spread:.5f} | "
                                f"SL {original_sl:.5f}"
                            )

                    # Execute close if triggered
                    if should_close:
                        try:
                            result = close_position_cb(ticket, pair)
                            if result:
                                # Successful close
                                positions.remove_ticket(ticket)
                                self.remove_position(ticket)
                                self.mark_closed_by_bot(key)
                                closed_tickets.append((ticket, key, close_reason))

                                log(LogLevel.INFO, f"Virtual SL closed T{ticket}: {close_reason}")
                            else:
                                # Close failed - will retry next cycle
                                log(LogLevel.WARN, f"Failed to close T{ticket} on VSL: {close_reason}")
                        except Exception as e:
                            log(LogLevel.ERROR, f"Error closing T{ticket} via VSL: {e}")

            except Exception as e:
                log(LogLevel.ERROR, f"Error checking VSL for {pair}: {e}")

        return closed_tickets


# Global instance
virtual_sl_manager = None


def init_virtual_sl(spread_factor: float = 1.5, cooldown_seconds: int = 300, reset_confirm_seconds: int = 20):
    """Initialize virtual SL manager.

    Args:
        spread_factor: Multiplier for spread compensation (1.5-2.0)
        cooldown_seconds: Deprecated - kept for backward compatibility only.
                         Reopen is lifecycle-driven, not time-based.
        reset_confirm_seconds: Duration (seconds) signal must stay missing before lifecycle reset.
                              Prevents false reopen from brief flickering.
                              Default 20 (2 cycles at 10s interval).
    """
    global virtual_sl_manager
    virtual_sl_manager = VirtualSLManager(spread_factor=spread_factor, cooldown_seconds=cooldown_seconds, reset_confirm_seconds=reset_confirm_seconds)
    return virtual_sl_manager


def get_virtual_sl_manager():
    """Get singleton instance."""
    global virtual_sl_manager
    if virtual_sl_manager is None:
        virtual_sl_manager = VirtualSLManager()
    return virtual_sl_manager
