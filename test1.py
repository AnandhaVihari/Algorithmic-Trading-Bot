import time
import MetaTrader5 as mt5
from trader import init_mt5
from config import TRADE_VOLUME, MAGIC_NUMBER

# Missed signals from bot.log - executing at current market price
MISSED = [
    {"pair": "EURAUD",  "side": "SELL", "tp": 0.0, "sl": 0.0},
]

init_mt5()

for s in MISSED:
    pair = s["pair"]
    side = s["side"]

    mt5.symbol_select(pair, True)
    time.sleep(0.5)

    sym = mt5.symbol_info(pair)
    if sym is None or sym.trade_mode == 0:
        pair = pair + "+"
        mt5.symbol_select(pair, True)
        time.sleep(0.5)
        sym = mt5.symbol_info(pair)

    if sym is None or sym.trade_mode == 0:
        print(f"SKIP {pair} - unavailable")
        continue

    tick = mt5.symbol_info_tick(pair)
    if tick is None:
        print(f"SKIP {pair} - no tick data")
        continue

    price = tick.ask if side == "BUY" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pair,
        "volume": TRADE_VOLUME,
        "type": order_type,
        "price": price,
        "tp": s["tp"],
        "sl": s["sl"],
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "missed_signal",
        "type_filling": mt5.ORDER_FILLING_IOC,
        "type_time": mt5.ORDER_TIME_GTC
    }

    result = mt5.order_send(request)
    print(f"{side} {pair} retcode:{result.retcode} ticket:{result.order}")

mt5.shutdown()
