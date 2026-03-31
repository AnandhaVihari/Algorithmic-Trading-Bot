"""Reverse Signal Trading Module

Executes strictly reversed trades:
- BUY signals → SELL trades
- SELL signals → BUY trades
- Original TP becomes SL
- Original SL becomes TP

No deviation, no trailing stop, only TP/SL exits.
"""

import json
import os
from typing import Dict, Tuple, Optional, List
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from operational_safety import log, LogLevel


@dataclass
class ReversedTrade:
    """Single reversed trade record."""
    trade_id: str  # Unique ID for this reversal
    timestamp: str  # When trade was opened
    original_signal_side: str  # BUY or SELL
    original_signal_tp: float
    original_signal_sl: float
    original_signal_entry: float

    reversed_side: str  # SELL if original BUY, BUY if original SELL
    reversed_tp: float  # Original SL becomes TP
    reversed_sl: float  # Original TP becomes SL

    symbol: str
    lot_size: float
    ticket: Optional[int] = None

    entry_price: Optional[float] = None
    entry_time: Optional[str] = None

    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None  # "TP", "SL", "Manual", etc.

    pnl: Optional[float] = None
    pnl_pips: Optional[float] = None

    status: str = "PENDING"  # PENDING, OPEN, CLOSED, REJECTED
    rejection_reason: Optional[str] = None


class SignalReversal:
    """Handles signal inversion and validation."""

    @staticmethod
    def invert_side(side: str) -> str:
        """Invert BUY to SELL or SELL to BUY."""
        if side == "BUY":
            return "SELL"
        elif side == "SELL":
            return "BUY"
        else:
            raise ValueError(f"Invalid side: {side}")

    @staticmethod
    def validate_and_invert(symbol: str, side: str, entry: float, tp: float, sl: float) -> Tuple[bool, str, float, float]:
        """
        Validate signal and invert TP/SL.

        Returns: (is_valid, error_message, inverted_tp, inverted_sl)
        """
        # Validate side
        if side not in ["BUY", "SELL"]:
            return False, f"Invalid side: {side}", 0, 0

        # Validate prices
        if entry <= 0 or tp <= 0 or sl <= 0:
            return False, f"Invalid prices: entry={entry}, tp={tp}, sl={sl}", 0, 0

        # Validate: TP and SL should be on opposite sides of entry
        if side == "BUY":
            if tp <= entry or sl >= entry:
                return False, f"BUY signal: TP must be > entry, SL must be < entry. Got entry={entry}, tp={tp}, sl={sl}", 0, 0
        else:  # SELL
            if tp >= entry or sl <= entry:
                return False, f"SELL signal: TP must be < entry, SL must be > entry. Got entry={entry}, tp={tp}, sl={sl}", 0, 0

        # Inversion: TP ↔ SL
        inverted_tp = sl
        inverted_sl = tp

        # Validate inverted prices for reversed side
        reversed_side = SignalReversal.invert_side(side)
        if reversed_side == "BUY":
            if inverted_tp <= entry or inverted_sl >= entry:
                return False, f"Reversed BUY: inverted TP must be > entry, inverted SL must be < entry. Got inverted_tp={inverted_tp}, inverted_sl={inverted_sl}", 0, 0
        else:  # SELL
            if inverted_tp >= entry or inverted_sl <= entry:
                return False, f"Reversed SELL: inverted TP must be < entry, inverted SL must be > entry. Got inverted_tp={inverted_tp}, inverted_sl={inverted_sl}", 0, 0

        return True, "", inverted_tp, inverted_sl


class ReverseTradeTracker:
    """Tracks all reversed trades."""

    def __init__(self, log_file: str = "reversed_trades.json"):
        self.log_file = log_file
        self.trades: Dict[str, ReversedTrade] = {}
        self.load_from_disk()

    def load_from_disk(self):
        """Load existing trades from disk."""
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file, 'r') as f:
                    data = json.load(f)
                    for trade_id, trade_dict in data.items():
                        self.trades[trade_id] = ReversedTrade(**trade_dict)
                log(LogLevel.DEBUG, f"Loaded {len(self.trades)} reversed trades from {self.log_file}")
            except Exception as e:
                log(LogLevel.WARN, f"Failed to load reversed trades: {e}")

    def save_to_disk(self):
        """Save trades to disk."""
        try:
            data = {tid: asdict(t) for tid, t in self.trades.items()}
            with open(self.log_file, 'w') as f:
                json.dump(data, f, indent=2)
            log(LogLevel.DEBUG, f"Saved {len(self.trades)} reversed trades to {self.log_file}")
        except Exception as e:
            log(LogLevel.ERROR, f"Failed to save reversed trades: {e}")

    def create_trade(self, symbol: str, side: str, entry: float, tp: float, sl: float, lot_size: float) -> ReversedTrade:
        """Create a reversed trade record."""
        # Validate and invert
        is_valid, error, inverted_tp, inverted_sl = SignalReversal.validate_and_invert(symbol, side, entry, tp, sl)

        reversed_side = SignalReversal.invert_side(side)
        now = datetime.now(timezone.utc).isoformat()
        trade_id = f"{symbol}_{reversed_side}_{now}"

        trade = ReversedTrade(
            trade_id=trade_id,
            timestamp=now,
            original_signal_side=side,
            original_signal_tp=tp,
            original_signal_sl=sl,
            original_signal_entry=entry,
            reversed_side=reversed_side,
            reversed_tp=inverted_tp,
            reversed_sl=inverted_sl,
            symbol=symbol,
            lot_size=lot_size,
            status="REJECTED" if not is_valid else "PENDING",
            rejection_reason=error if not is_valid else None
        )

        self.trades[trade_id] = trade
        self.save_to_disk()

        if not is_valid:
            log(LogLevel.WARN, f"Rejected reverse trade {trade_id}: {error}")
        else:
            log(LogLevel.INFO, f"Created reverse trade {trade_id}: {reversed_side} {symbol} TP={inverted_tp} SL={inverted_sl}")

        return trade

    def update_trade_execution(self, trade_id: str, ticket: int, entry_price: float):
        """Mark trade as executed with ticket and entry price."""
        if trade_id not in self.trades:
            log(LogLevel.ERROR, f"Trade {trade_id} not found")
            return

        trade = self.trades[trade_id]
        trade.ticket = ticket
        trade.entry_price = entry_price
        trade.entry_time = datetime.now(timezone.utc).isoformat()
        trade.status = "OPEN"
        self.save_to_disk()
        log(LogLevel.INFO, f"Trade {trade_id} executed: T{ticket} at {entry_price}")

    def update_trade_close(self, trade_id: str, exit_price: float, exit_reason: str):
        """Mark trade as closed with exit details."""
        if trade_id not in self.trades:
            log(LogLevel.ERROR, f"Trade {trade_id} not found")
            return

        trade = self.trades[trade_id]
        if trade.status != "OPEN":
            log(LogLevel.WARN, f"Trade {trade_id} not OPEN, current status: {trade.status}")
            return

        trade.exit_price = exit_price
        trade.exit_time = datetime.now(timezone.utc).isoformat()
        trade.exit_reason = exit_reason
        trade.status = "CLOSED"

        # Calculate PnL
        if trade.reversed_side == "BUY":
            # Long: profit = exit - entry
            trade.pnl = exit_price - trade.entry_price
            trade.pnl_pips = (exit_price - trade.entry_price) * 10000  # Simplified pips
        else:  # SELL
            # Short: profit = entry - exit
            trade.pnl = trade.entry_price - exit_price
            trade.pnl_pips = (trade.entry_price - exit_price) * 10000

        self.save_to_disk()
        log(LogLevel.INFO, f"Trade {trade_id} closed at {exit_price}, PnL={trade.pnl}, reason={exit_reason}")

    def get_active_trades(self) -> List[ReversedTrade]:
        """Get all OPEN trades."""
        return [t for t in self.trades.values() if t.status == "OPEN"]

    def get_closed_trades(self) -> List[ReversedTrade]:
        """Get all CLOSED trades."""
        return [t for t in self.trades.values() if t.status == "CLOSED"]

    def get_summary(self) -> Dict:
        """Get trading summary."""
        closed = self.get_closed_trades()
        total_pnl = sum(t.pnl for t in closed if t.pnl is not None)
        win_count = sum(1 for t in closed if t.pnl and t.pnl > 0)
        loss_count = sum(1 for t in closed if t.pnl and t.pnl < 0)

        return {
            "total_trades": len(self.trades),
            "open_trades": len(self.get_active_trades()),
            "closed_trades": len(closed),
            "rejected_trades": sum(1 for t in self.trades.values() if t.status == "REJECTED"),
            "total_pnl": total_pnl,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": f"{(win_count / len(closed) * 100):.1f}%" if closed else "N/A"
        }


def init_reverse_executor() -> ReverseTradeTracker:
    """Initialize reverse executor module."""
    return ReverseTradeTracker()
