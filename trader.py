"""
SIMPLE TRADER - Blind follower

Just opens and closes trades as website signals say.
No fancy position management.
"""

import time
import subprocess
import MetaTrader5 as mt5
from datetime import datetime, timezone
from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_EXE, TRADE_VOLUME

MAGIC_NUMBER = 777  # All trades use same magic (no frame distinction in blind mode)


def init_mt5():
    """Initialize MetaTrader5."""
    if not mt5.initialize():
        print("MT5 not running — launching terminal...")
        subprocess.Popen(MT5_EXE, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(10)
        if not mt5.initialize():
            raise RuntimeError("MT5 init failed after launch")

    if not mt5.login(MT5_LOGIN, MT5_PASSWORD, MT5_SERVER):
        raise RuntimeError("MT5 login failed")

    print("MT5 connected")


def open_trade(signal):
    """
    Open a trade exactly as signal says.

    Returns: (success: bool, ticket: int or None)

    signal = {
        'pair': 'EURUSD',
        'side': 'BUY',
        'open': 1.08500,
        'tp': 1.09000,
        'sl': 1.08000,
        'frame': 'short',
        ...
    }
    """

    pair = signal["pair"]
    side = signal["side"]
    tp = signal["tp"]
    sl = signal["sl"]

    # ─── Get symbol ──────────────────────────────────────────────────────
    sym = None
    for name in (pair, pair + "+"):
        mt5.symbol_select(name, True)
        time.sleep(0.5)
        info = mt5.symbol_info(name)
        if info is not None and info.trade_mode != 0:
            pair = name
            sym = info
            break

    if sym is None:
        print(f"  [SKIP] Symbol {pair} not available")
        return False, None

    # ─── Get current price ───────────────────────────────────────────────
    tick = mt5.symbol_info_tick(pair)
    if tick is None:
        print(f"  [SKIP] No tick data for {pair}")
        return False, None

    price = tick.ask if side == "BUY" else tick.bid

    # ─── Send order ──────────────────────────────────────────────────────
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pair,
        "volume": TRADE_VOLUME,
        "type": order_type,
        "price": price,
        "tp": tp,
        "sl": sl,
        "deviation": 100,  # Increased from 20 (too small)
        "magic": MAGIC_NUMBER,
        "comment": "blind",
        "type_filling": mt5.ORDER_FILLING_FOK,  # Changed from IOC (not always supported)
        "type_time": mt5.ORDER_TIME_GTC
    }

    result = mt5.order_send(request)

    if result.retcode == 10009:
        actual_price = result.price
        signal_price = signal["open"]
        price_diff = abs(actual_price - signal_price)

        if price_diff > 0.0001:
            print(f"  [OPENED] {side} {pair} → Actual: {actual_price} (Signal: {signal_price}) | SL: {sl} | TP: {tp} | Ticket: {result.order}")
        else:
            print(f"  [OPENED] {side} {pair} @ {result.price} | SL: {sl} | TP: {tp} | Ticket: {result.order}")
        return True, result.order
    elif result.retcode == 10016:
        print(f"  [SKIP] Price moved, invalid stops")
        return True, None  # Mark as processed
    else:
        # Log detailed error info
        print(f"  [FAILED] Order rejected: {result.retcode} | {result.comment}")
        if result.retcode == 10019:
            print(f"    [DEBUG] Invalid request: check volume ({TRADE_VOLUME}), SL ({sl}), TP ({tp}), price ({price})")
        return False, None


def close_trade(pair):
    """Close all positions for this pair."""

    closed_count = 0
    for name in (pair, pair + "+"):
        positions = mt5.positions_get(symbol=name)
        if not positions:
            continue

        for pos in positions:
            if pos.magic != MAGIC_NUMBER:
                continue

            entry_price = pos.price_open
            ticket = pos.ticket

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": name,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "deviation": 20,
                "magic": MAGIC_NUMBER,
                "comment": "close",
                "type_filling": mt5.ORDER_FILLING_IOC,
                "type_time": mt5.ORDER_TIME_GTC
            }

            result = mt5.order_send(request)
            if result.retcode == 10009:
                # Fetch real close price from deal history
                close_deal = None
                try:
                    deals = mt5.history_deals_get(position=ticket, group="*")
                    if deals:
                        for deal in reversed(deals):
                            if deal.entry == 1:  # Closing deal
                                close_deal = deal
                                break
                except Exception:
                    pass

                if close_deal:
                    close_price = close_deal.price
                    close_profit = close_deal.profit
                    price_diff = close_price - entry_price
                    print(f"  [CLOSED] {name} T{ticket} Entry: {entry_price} -> Close: {close_price} | Diff: {price_diff:.6f} | Profit: ${close_profit:.2f}")
                else:
                    print(f"  [CLOSED] {name} | Profit: ${pos.profit:.2f}")
                closed_count += 1

    return closed_count > 0


def close_position_by_ticket(ticket, pair=None):
    """Close a specific position by ticket number."""

    # ──── EXECUTION TRACE ────
    import traceback
    import inspect

    stack = traceback.extract_stack()
    caller_frame = None
    caller_function = "UNKNOWN"

    # Find caller (skip this function and decorator frames)
    for frame in reversed(stack[:-1]):
        if "close_position_by_ticket" not in frame.name:
            caller_frame = frame
            caller_function = frame.name if frame.name else "UNKNOWN"
            break

    print(f"[TRACE_CLOSE] Ticket {ticket} close initiated")
    print(f"[TRACE_CLOSE] Caller: {caller_function}() at {caller_frame.filename.split(chr(92))[-1] if caller_frame else 'unknown'}:{caller_frame.lineno if caller_frame else '?'}")
    if ticket in [1029131995, 1028771560, 1028924631]:  # Known problem tickets
        print(f"[TRACE_CLOSE] **PROBLEM TICKET DETECTED**")

    names = [(pair, pair + "+")] if pair else [(None, None)]

    for name1, name2 in names:
        for name in (name1, name2):
            if name is None:
                continue
            positions = mt5.positions_get(symbol=name)
            if not positions:
                continue

            for pos in positions:
                if pos.ticket != ticket or pos.magic != MAGIC_NUMBER:
                    continue

                # Store entry details BEFORE close
                entry_price = pos.price_open
                entry_time = datetime.now(timezone.utc)

                # Get tick info before close
                tick = mt5.symbol_info_tick(name)
                bid_before = tick.bid if tick else 0
                ask_before = tick.ask if tick else 0

                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": name,
                    "volume": pos.volume,
                    "type": mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY,
                    "position": ticket,
                    "deviation": 20,
                    "magic": MAGIC_NUMBER,
                    "comment": "close",
                    "type_filling": mt5.ORDER_FILLING_IOC,
                    "type_time": mt5.ORDER_TIME_GTC
                }

                result = mt5.order_send(request)
                if result.retcode == 10009:
                    # Close succeeded - fetch real close price from deal history
                    close_deal = None
                    try:
                        # Get deals for this position (most recent deals)
                        deals = mt5.history_deals_get(position=ticket, group="*")
                        if deals:
                            # Find the CLOSING deal (should be most recent)
                            for deal in reversed(deals):
                                # Deal type: DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1
                                # Deal entry: DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1
                                if deal.entry == 1:  # CLOSING deal
                                    close_deal = deal
                                    break
                    except Exception as e:
                        print(f"    [DEBUG] Deal history error: {e}")

                    if close_deal:
                        close_price = close_deal.price
                        close_profit = close_deal.profit
                        price_diff = close_price - entry_price

                        # Detect if close at entry or real movement
                        if abs(price_diff) < 0.00001 and close_profit != 0:
                            warning = " [WARNING: Close at entry but non-zero profit - likely friction only]"
                        else:
                            warning = ""

                        print(f"  [CLOSED] T{ticket} Entry: {entry_price} -> Close: {close_price} | Movement: {price_diff:.6f} pips | Profit: ${close_profit:.2f}{warning}")

                        # ─── CLOSE CORRELATION TRACE ─────────────────────────────────────
                        # Log close details for correlation with trailing stop moves
                        close_reason = caller_function if caller_function != "UNKNOWN" else "UNKNOWN"
                        print(f"[CLOSE_TRACE] T{ticket} | reason={close_reason} | close={close_price:.5f} | sl={pos.sl:.5f} | tp={pos.tp:.5f} | entry={entry_price:.5f} | profit=${close_profit:.2f}")
                    else:
                        # Fallback if deal history unavailable
                        print(f"  [CLOSED] T{ticket} (deal history unavailable) | Entry: {entry_price} | Profit: ${pos.profit:.2f}")

                        # ─── CLOSE CORRELATION TRACE (FALLBACK) ─────────────────────────────────
                        close_reason = caller_function if caller_function != "UNKNOWN" else "UNKNOWN"
                        print(f"[CLOSE_TRACE] T{ticket} | reason={close_reason} | close=UNKNOWN | sl={pos.sl:.5f} | tp={pos.tp:.5f} | entry={entry_price:.5f} | profit=${pos.profit:.2f}")

                    # Verify position actually closed in MT5
                    time.sleep(0.5)  # Wait for MT5 to update
                    remaining = mt5.positions_get(ticket=ticket)
                    if remaining:
                        print(f"    [ERROR] Position T{ticket} still exists in MT5 after close!")
                    else:
                        print(f"    [OK] Position T{ticket} fully closed in MT5")

                    return True

    print(f"  [WARN] Position ticket {ticket} not found")
    return False


def get_position(pair):
    """Get any open position for this pair."""

    for name in (pair, pair + "+"):
        positions = mt5.positions_get(symbol=name)
        if positions:
            for p in positions:
                if p.magic == MAGIC_NUMBER:
                    return p

    return None


def show_open_positions():
    """Display all open positions."""

    positions = mt5.positions_get()
    if not positions:
        print("  No open positions")
        return

    print(f"  Open positions: {len(positions)}")
    for p in positions[:5]:  # Show first 5
        pos_type = "BUY" if p.type == 0 else "SELL"
        print(f"    [{p.symbol}] {pos_type} @ {p.price_open} | Profit: ${p.profit:.2f}")


def account_summary():
    """Show account stats."""

    info = mt5.account_info()
    if not info:
        return

    print(f"  Account: Balance ${info.balance:.2f} | Equity ${info.equity:.2f}")
