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
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "blind",
        "type_filling": mt5.ORDER_FILLING_IOC,
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
        print(f"  [FAILED] Order rejected: {result.retcode}")
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
                print(f"  [CLOSED] {name} | Profit: ${pos.profit:.2f}")
                closed_count += 1

    return closed_count > 0


def close_position_by_ticket(ticket, pair=None):
    """Close a specific position by ticket number."""

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
                    print(f"  [CLOSED] Ticket {ticket} @ {pos.price_open} | Profit: ${pos.profit:.2f}")
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
