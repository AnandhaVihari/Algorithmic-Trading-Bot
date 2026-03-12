# Algorithmic Trading Bot — RingFlip

A Python-based contrarian signal trading bot for MetaTrader 5. It scrapes forex signals from an external source, reverses the direction (trades opposite to the signal), and manages open positions with trailing stops, breakeven logic, time-based exits, and a multi-layer profit guard.

---

## How It Works

1. **Signal Scraping** — polls a signal website every 66 seconds
2. **Confluence Filter** — only trades when both short-frame and long-frame signals agree on direction
3. **Signal Reversal** — if signal says BUY, the bot sells; if SELL, the bot buys
4. **Trade Management** — once open, positions are managed every 5 seconds:
   - Breakeven: SL moves to entry at 20% of TP distance
   - Trailing stop: activates at 50% of TP distance, tightens at 80%
   - Time exit: closes losing positions after 4h (short) or 24h (long)
5. **Profit Guard** — monitors every second, closes on rapid reversals or significant drawdown from peak

---

## Requirements

- Windows (MT5 runs on Windows only)
- Python 3.8+
- MetaTrader 5 terminal installed
- A MetaTrader 5 account (demo or live)

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/Anandha-Vihari/Algorithmic-Trading-Bot.git
cd Algorithmic-Trading-Bot
```

**2. Install dependencies**

```bash
pip install MetaTrader5 requests beautifulsoup4
```

**3. Create `config.py`**

Create a file named `config.py` in the project root (this file is excluded from the repo for security):

```python
URL = "https://massyart.com/ringsignal/"

SIGNAL_INTERVAL   = 66
POSITION_INTERVAL = 5
GUARD_INTERVAL    = 1

TRADE_VOLUME     = 0.01
MAX_POSITIONS    = 100
TRADE_FRAME      = "both"   # "short", "long", or "both"
REVERSE_SIGNALS  = True     # True = trade opposite to signal
REVERSE_RR       = 1.0      # R:R ratio for reversed trades (1.0 = 1:1)

MAGIC_SHORT = 777
MAGIC_LONG  = 778

BREAKEVEN_PCT      = 0.20
TRAIL_START_PCT    = 0.50
TRAIL_DISTANCE_PCT = 0.25
TRAIL_TIGHT_PCT    = 0.80
TRAIL_TIGHT_DIST   = 0.10

MIN_RR_RATIO    = 1.0
SHORT_MAX_HOURS = 4
LONG_MAX_HOURS  = 24

PROFIT_GUARD_MIN       = 0.20
PROFIT_GUARD_RETAIN    = 0.40
PROFIT_GUARD_FLOOR     = 0.04
PROFIT_GUARD_DROP_USD  = 0.08
PROFIT_GUARD_DROP_SECS = 8

MT5_LOGIN    = YOUR_ACCOUNT_NUMBER
MT5_PASSWORD = "YOUR_PASSWORD"
MT5_SERVER   = "YOUR_BROKER_SERVER"
MT5_EXE      = r"C:\path\to\terminal64.exe"
```

**4. Make sure MT5 terminal is installed and you can log in manually first**

---

## Running the Bot

```bash
python main.py
```

The bot starts three background threads automatically:
- **Signal thread** — scrapes and processes signals every 66s
- **Position thread** — manages open trades every 5s
- **Guard thread** — monitors profit/loss every 1s

Logs are written to:
- `bot.log` — full stdout output
- `signals.log` — structured per-signal events
- `balance.log` — periodic account snapshots

---

## Configuration Reference

| Parameter | Default | Description |
|---|---|---|
| `TRADE_VOLUME` | `0.01` | Lot size per trade |
| `MAX_POSITIONS` | `100` | Max concurrent open positions |
| `TRADE_FRAME` | `"both"` | Which timeframe signals to use |
| `REVERSE_SIGNALS` | `True` | Trade opposite to signal direction |
| `REVERSE_RR` | `1.0` | R:R for reversed trades (1.0 = 1:1) |
| `MIN_RR_RATIO` | `1.0` | Skip trade if R:R below this |
| `BREAKEVEN_PCT` | `0.20` | Move SL to entry at 20% of TP distance |
| `TRAIL_START_PCT` | `0.50` | Start trailing at 50% of TP distance |
| `TRAIL_DISTANCE_PCT` | `0.25` | Trail SL 25% of TP distance behind peak |
| `TRAIL_TIGHT_PCT` | `0.80` | Switch to tight trail above 80% of TP |
| `TRAIL_TIGHT_DIST` | `0.10` | Tight trail distance (10% of TP) |
| `SHORT_MAX_HOURS` | `4` | Force-close losing short-frame trades after 4h |
| `LONG_MAX_HOURS` | `24` | Force-close losing long-frame trades after 24h |
| `PROFIT_GUARD_MIN` | `0.20` | Guard activates once profit reaches $0.20 |
| `PROFIT_GUARD_RETAIN` | `0.40` | Close if profit drops below 40% of peak |
| `PROFIT_GUARD_FLOOR` | `0.04` | Close if profit falls to $0.04 absolute |
| `PROFIT_GUARD_DROP_USD` | `0.08` | Close if profit drops $0.08 in DROP_SECS |
| `PROFIT_GUARD_DROP_SECS` | `8` | Time window for rapid-drop detection |

> **Note:** If you set `REVERSE_RR` below `1.0`, also lower `MIN_RR_RATIO` to match, otherwise all reversed trades will be skipped.

---

## Project Structure

```
├── main.py        # Orchestrator — three threads, signal pipeline
├── trader.py      # MT5 trade execution and position management
├── parser.py      # HTML signal parser
├── scraper.py     # HTTP page fetcher
├── state.py       # Persistent signal deduplication store
├── slog.py        # Structured signal event logger
├── manager.py     # Older single-threaded version (reference only)
├── test1.py       # Manual trade injector for missed signals
├── config.py      # (not in repo) credentials and parameters
└── .gitignore
```

---

## Disclaimer

This software is for educational purposes. Trading forex involves substantial risk. Always test on a demo account before using real funds.
