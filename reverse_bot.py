"""Reverse Signal Trading Bot

Executes all signals in reverse:
- BUY → SELL
- SELL → BUY
- TP ↔ SL (strict inversion)

Runs independently as pure signal reversal test.
Session-filtered, fixed lot size, no deviation.
"""

import time
from datetime import datetime, timezone
from reverse_executor import init_reverse_executor, SignalReversal
from signal_manager import fetch_and_parse_signals
from session_filter import is_signal_in_overlap
from trader import init_mt5, open_trade
from config import SIGNAL_INTERVAL, TRADE_VOLUME
from operational_safety import log, LogLevel

# Initialize
mt5 = init_mt5()
reverse_tracker = init_reverse_executor()
processed_signal_ids = set()

LOG_FILE = "reverse_bot.log"


def log_message(level: str, msg: str):
    """Log to both console and file."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    formatted = f"[{timestamp}] {msg}"
    print(formatted)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(formatted + "\n")
    except:
        pass


def run_reverse_cycle():
    """Execute one reverse trading cycle."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*80}\n[{now_str}] REVERSE BOT CYCLE\n{'='*80}")

    # Fetch signals
    try:
        signals = fetch_and_parse_signals()
    except Exception as e:
        log_message("ERROR", f"[ERROR_FETCH] Failed to fetch signals: {e}")
        return

    if not signals:
        log_message("DEBUG", "[CYCLE] No signals available")
        return

    active_signals = [s for s in signals if s.status == "ACTIVE"]
    log_message("DEBUG", f"[CYCLE] Total signals in cycle: {len(signals)}, ACTIVE: {len(active_signals)}")

    # Process ACTIVE signals only (not CLOSE)
    for sig in active_signals:
        # Signal ID for dedup
        sig_id = f"{sig.pair}_{sig.side}_{sig.tp}_{sig.sl}"

        # Skip if already processed
        if sig_id in processed_signal_ids:
            continue

        # Check session filter
        if not is_signal_in_overlap(sig.time):
            log_message("DEBUG", f"[SESSION_SKIP] {sig.pair} {sig.side} created at {sig.time.strftime('%H:%M UTC')} (outside overlap)")
            continue

        log_message("INFO", f"[SIGNAL] Processing: {sig.pair} {sig.side} Entry={sig.open_price} TP={sig.tp} SL={sig.sl}")

        # Create reversed trade record
        trade = reverse_tracker.create_trade(
            symbol=sig.pair,
            side=sig.side,
            entry=sig.open_price,
            tp=sig.tp,
            sl=sig.sl,
            lot_size=TRADE_VOLUME
        )

        if trade.status == "REJECTED":
            log_message("WARN", f"[REJECTED] {trade.trade_id}: {trade.rejection_reason}")
            processed_signal_ids.add(sig_id)
            continue

        # Execute reversed trade
        log_message("INFO", f"[EXECUTE] Reversing: {sig.pair} {sig.side} → {trade.reversed_side} TP={trade.reversed_tp} SL={trade.reversed_sl}")

        try:
            ticket = open_trade(
                symbol=sig.pair,
                order_type="SELL" if trade.reversed_side == "SELL" else "BUY",
                volume=TRADE_VOLUME,
                entry_price=sig.open_price,
                tp=trade.reversed_tp,
                sl=trade.reversed_sl,
                comment=f"Reverse: {sig.side}→{trade.reversed_side}"
            )

            if ticket and ticket > 0:
                reverse_tracker.update_trade_execution(trade.trade_id, ticket, sig.open_price)
                log_message("SUCCESS", f"[OPENED] T{ticket} {trade.reversed_side} {sig.pair} at {sig.open_price}")
            else:
                log_message("ERROR", f"[FAILED_OPEN] {trade.trade_id}: open_trade returned {ticket}")

        except Exception as e:
            log_message("ERROR", f"[EXCEPTION_OPEN] {trade.trade_id}: {e}")

        processed_signal_ids.add(sig_id)

    # Display summary
    try:
        summary = reverse_tracker.get_summary()
        log_message("INFO", f"[SUMMARY] Total:{summary['total_trades']} Open:{summary['open_trades']} Closed:{summary['closed_trades']} PnL:{summary['total_pnl']:.2f} WinRate:{summary['win_rate']}")
    except Exception as e:
        log_message("DEBUG", f"[ERROR_SUMMARY] {e}")


def main():
    """Main loop."""
    log_message("INFO", "REVERSE BOT STARTED")
    log_message("INFO", f"Configuration: Lot Size={TRADE_VOLUME}, Session Filter=London-NY Overlap, Mode=Strict Reversal")

    try:
        while True:
            try:
                run_reverse_cycle()
            except Exception as e:
                log_message("CRITICAL", f"[ERROR_CYCLE] {e}")

            time.sleep(SIGNAL_INTERVAL)

    except KeyboardInterrupt:
        log_message("INFO", "REVERSE BOT STOPPED (user interrupt)")
        # Print final summary
        try:
            summary = reverse_tracker.get_summary()
            log_message("INFO", "=== FINAL SUMMARY ===")
            for key, value in summary.items():
                log_message("INFO", f"  {key}: {value}")
        except:
            pass


if __name__ == "__main__":
    main()
