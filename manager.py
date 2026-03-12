import time
import sys
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5

sys.stdout = open("manager.log", "a", buffering=1)
sys.stderr = sys.stdout

from scraper import fetch_page
from parser  import parse_signals
from config  import (
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
    MAGIC_NUMBER, TRADE_VOLUME, POLL_INTERVAL, MAX_SIGNAL_AGE_MINUTES
)

# ── trade management constants ────────────────────────────────────────────────
BREAKEVEN_PIPS   = 10   # pips profit → move SL to entry
TRAIL_START_PIPS = 15   # pips profit → begin trailing stop
TRAIL_DISTANCE   = 5    # trailing stop distance in pips

# ── state ─────────────────────────────────────────────────────────────────────
MAX_AGE       = timedelta(minutes=MAX_SIGNAL_AGE_MINUTES)
processed_ids = set()


# ══════════════════════════════════════════════════════════════════════════════
#  MT5 HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def init_mt5():
    if not mt5.initialize():
        raise RuntimeError("MT5 init failed")
    if not mt5.login(MT5_LOGIN, MT5_PASSWORD, MT5_SERVER):
        raise RuntimeError("MT5 login failed")
    print("MT5 connected")


def resolve_symbol(pair):
    """Return the tradeable symbol name (bare or +suffix), or None if unavailable."""
    for name in (pair, pair + "+"):
        mt5.symbol_select(name, True)
        time.sleep(0.3)
        sym = mt5.symbol_info(name)
        if sym and sym.trade_mode != 0:
            return name
    return None


def get_position(pair):
    """Return the first bot-tagged open position for pair, or None."""
    for name in (pair, pair + "+"):
        positions = mt5.positions_get(symbol=name)
        if positions:
            for p in positions:
                if p.magic == MAGIC_NUMBER:
                    return p
    return None


def close_position(p):
    """Close a position by ticket. Returns True on success."""
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        print(f"  CLOSE FAILED (no tick): {p.symbol}")
        return False

    if p.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price      = tick.bid
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price      = tick.ask

    result = mt5.order_send({
        "action":       mt5.TRADE_ACTION_DEAL,
        "position":     p.ticket,
        "symbol":       p.symbol,
        "volume":       p.volume,
        "type":         order_type,
        "price":        price,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "type_filling": mt5.ORDER_FILLING_IOC,
        "type_time":    mt5.ORDER_TIME_GTC,
    })

    ok   = result.retcode == 10009
    side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
    print(f"  CLOSE {'OK' if ok else f'FAIL [{result.retcode}]'}: "
          f"{p.symbol} {side}  profit={round(p.profit, 2)}")
    return ok


def open_trade(pair, side, tp, sl):
    """Open a market order. Returns True on success."""
    sym_name = resolve_symbol(pair)
    if sym_name is None:
        print(f"  OPEN SKIP: {pair} — market closed or symbol unavailable")
        return False

    tick = mt5.symbol_info_tick(sym_name)
    if tick is None:
        print(f"  OPEN SKIP: {pair} — no tick data")
        return False

    price      = tick.ask if side == "BUY" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    result = mt5.order_send({
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       sym_name,
        "volume":       TRADE_VOLUME,
        "type":         order_type,
        "price":        price,
        "tp":           tp,
        "sl":           sl,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      "signal_bot",
        "type_filling": mt5.ORDER_FILLING_IOC,
        "type_time":    mt5.ORDER_TIME_GTC,
    })

    ok = result.retcode == 10009
    print(f"  OPEN {'OK' if ok else f'FAIL [{result.retcode}]'}: "
          f"{sym_name} {side}  price={price}  tp={tp}  sl={sl}")
    return ok


def modify_sl(p, new_sl):
    """Move stop loss on an open position. Returns True on success."""
    result = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": p.ticket,
        "symbol":   p.symbol,
        "sl":       new_sl,
        "tp":       p.tp,
    })
    ok = result.retcode == 10009
    if not ok:
        print(f"  MODIFY FAIL [{result.retcode}]: {p.symbol}")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def is_fresh(signal_time):
    if signal_time is None:
        return False
    return datetime.now(timezone.utc) - signal_time <= MAX_AGE


def process_signal(signal):

    pair      = signal["pair"]
    side      = signal["side"]    # BUY | SELL
    status    = signal["status"]  # ACTIVE | CLOSE
    sig_time  = signal["time"]
    signal_id = f"{pair}_{sig_time}_{side}_{status}"

    # skip already-processed or stale signals
    if signal_id in processed_ids:
        return
    if not is_fresh(sig_time):
        return

    existing = get_position(pair)

    # ── CLOSE signal ──────────────────────────────────────────────────────────
    if status == "CLOSE":
        if existing:
            print(f"\n[CLOSE SIGNAL] {pair}")
            close_position(existing)
            processed_ids.add(signal_id)
        return

    # ── ACTIVE signal ─────────────────────────────────────────────────────────
    if existing is not None:
        pos_side = "BUY" if existing.type == mt5.POSITION_TYPE_BUY else "SELL"

        # Rule 6: same direction → skip
        if pos_side == side:
            processed_ids.add(signal_id)
            return

        # Rules 3 & 4: opposite direction → close existing, then open new
        print(f"\n[REVERSE] {pair}: {pos_side} → {side}")
        if not close_position(existing):
            return
        time.sleep(0.5)

    else:
        print(f"\n[NEW SIGNAL] {pair} {side}")

    if open_trade(pair, side, signal["tp"], signal["sl"]):
        processed_ids.add(signal_id)


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION MANAGEMENT  (breakeven + trailing stop)
# ══════════════════════════════════════════════════════════════════════════════

def manage_positions():

    positions = mt5.positions_get()
    if not positions:
        return

    now = datetime.now(timezone.utc)

    for p in positions:

        if p.magic != MAGIC_NUMBER:
            continue

        sym  = mt5.symbol_info(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        if sym is None or tick is None:
            continue

        pip = sym.point * 10

        # M1 bars since entry → find the true peak (highest high / lowest low)
        open_time = datetime.fromtimestamp(p.time, tz=timezone.utc)
        bars = mt5.copy_rates_range(p.symbol, mt5.TIMEFRAME_M1, open_time, now)

        # ── BUY position ──────────────────────────────────────────────────────
        if p.type == mt5.POSITION_TYPE_BUY:

            current_price = tick.bid
            profit_pips   = (current_price - p.price_open) / pip
            peak_price    = (float(bars['high'].max())
                             if bars is not None and len(bars) > 0
                             else current_price)

            if profit_pips >= TRAIL_START_PIPS:
                # Stage 2 — trailing: SL = highest HIGH − TRAIL_DISTANCE
                new_sl = round(peak_price - TRAIL_DISTANCE * pip, sym.digits)
                if new_sl > p.sl:
                    if modify_sl(p, new_sl):
                        print(f"  TRAIL SL  {p.symbol}: {p.sl} → {new_sl}"
                              f"  (peak={peak_price}  +{profit_pips:.1f}p)")

            elif profit_pips >= BREAKEVEN_PIPS:
                # Stage 1 — breakeven: move SL to entry price
                new_sl = round(p.price_open, sym.digits)
                if new_sl > p.sl:
                    if modify_sl(p, new_sl):
                        print(f"  BREAKEVEN {p.symbol}: SL → {new_sl}"
                              f"  (+{profit_pips:.1f}p)")

        # ── SELL position ─────────────────────────────────────────────────────
        else:

            current_price = tick.ask
            profit_pips   = (p.price_open - current_price) / pip
            peak_price    = (float(bars['low'].min())
                             if bars is not None and len(bars) > 0
                             else current_price)

            if profit_pips >= TRAIL_START_PIPS:
                # Stage 2 — trailing: SL = lowest LOW + TRAIL_DISTANCE
                new_sl = round(peak_price + TRAIL_DISTANCE * pip, sym.digits)
                if p.sl == 0 or new_sl < p.sl:
                    if modify_sl(p, new_sl):
                        print(f"  TRAIL SL  {p.symbol}: {p.sl} → {new_sl}"
                              f"  (peak={peak_price}  +{profit_pips:.1f}p)")

            elif profit_pips >= BREAKEVEN_PIPS:
                # Stage 1 — breakeven: move SL to entry price
                new_sl = round(p.price_open, sym.digits)
                if p.sl == 0 or new_sl < p.sl:
                    if modify_sl(p, new_sl):
                        print(f"  BREAKEVEN {p.symbol}: SL → {new_sl}"
                              f"  (+{profit_pips:.1f}p)")


# ══════════════════════════════════════════════════════════════════════════════
#  MONITORING
# ══════════════════════════════════════════════════════════════════════════════

def show_positions():
    positions = mt5.positions_get()
    if not positions:
        return
    print("\nOPEN POSITIONS")
    for p in positions:
        if p.magic != MAGIC_NUMBER:
            continue
        side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        print(f"  {p.symbol:<12} {side}  entry={p.price_open}  "
              f"sl={p.sl}  tp={p.tp}  profit={round(p.profit, 2)}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

init_mt5()

while True:

    try:

        html    = fetch_page()
        signals = parse_signals(html)

        print(f"\n{'='*55}")
        print(f"BOT ALIVE: {datetime.now().strftime('%H:%M:%S')}  "
              f"signals received: {len(signals)}")

        for s in signals:
            process_signal(s)

        manage_positions()
        show_positions()

        time.sleep(POLL_INTERVAL)

    except Exception as e:
        print(f"ERROR: {e}")
        time.sleep(60)
