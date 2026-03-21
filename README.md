# Algorithmic Trading Bot — Blind Follower with Frame Lock

A fully autonomous forex trading bot that **blindly follows website signals** with **frame lock logic** and **proxy rotation** to avoid rate limiting. Opens trades exactly as the website signals, closes when the website says close. Built for MetaTrader 5 on Windows.

---

## 📖 Table of Contents

1. [Overview](#overview)
2. [Core Behaviors](#core-behaviors)
3. [How It Works](#how-it-works)
4. [Installation & Setup](#installation--setup)
5. [Configuration](#configuration)
6. [Running the Bot](#running-the-bot)
7. [Signal Processing](#signal-processing)
8. [Proxy Rotation](#proxy-rotation)
9. [Logs & Monitoring](#logs--monitoring)
10. [Troubleshooting](#troubleshooting)
11. [Advanced Topics](#advanced-topics)
12. [FAQ](#faq)
13. [Technical Documentation](#technical-documentation)

---

## Overview

### What Is This?

A trading bot that:
1. Fetches signals from a website every 7 seconds (via 10 rotating proxies)
2. Parses signals to extract: pair, direction, entry, stop loss, take profit, frame (short/long)
3. Opens trades exactly as the website signals (no filtering, no intelligence)
4. Closes trades only when the website signals CLOSE (and frame matches)
5. Logs all events for monitoring

### Why "Blind Follower"?

The bot does **zero filtering, zero analysis, zero second-guessing**:

| What It Does NOT Do |
|---|
| ✗ Check if signal is profitable |
| ✗ Analyze market conditions or charts |
| ✗ Filter pairs by any criteria |
| ✗ Adjust stops or targets |
| ✗ Use trailing stops |
| ✗ Extend take profits |
| ✗ Use profit guards |
| ✗ Check R:R ratio |
| ✗ Honor market hours |

**The website is your entire decision system.** Bot executes it, period.

---

## Core Behaviors

### 1. Frame Lock (First-Come, First-Served)

**Problem**: Website can signal BUY (short) AND SELL (long) for the same pair. Bot shouldn't open both simultaneously.

**Solution**: Frame lock prevents conflicting trades.

```
EURUSD BUY (short)  → Frame lock: EURUSD = "short"
EURUSD SELL (long)  → Skipped (different frame locked)
   ↓
CLOSE EURUSD short  → Closes position, unlocks pair
EURUSD SELL (long)  → Now allowed (pair unlocked)
```

**How it works**:
- Track `active_frame = {"EURUSD": "short", "GBPUSD": "long"}`
- On ACTIVE signal: skip if different frame already locked
- On CLOSE signal: only close if frame matches
- Release lock when CLOSE executes

**Why this matters**: Prevents opening conflicting positions that fight each other.

---

### 2. Most Recent Signal Only (Per Pair+Frame)

**Problem**: Website updates signal timestamps. Same signal appears twice, bot opens it twice.

**Solution**: Deduplicate by pair+frame, keep only newest.

```
EURUSD BUY short (timestamp 14:32:00)
EURUSD BUY short (timestamp 14:32:15)  ← Same signal, newer timestamp

→ Sort by timestamp DESC
→ Dedup by {pair}_{frame}
→ Keep only newest:  EURUSD BUY short (14:32:15)
→ Open once
```

**How it works**:
```python
signals.sort(key=lambda x: x['time'], reverse=True)
seen = set()
for s in signals:
    key = f"{s['pair']}_{s['frame']}"
    if key not in seen:
        seen.add(key)
        # process this signal
```

**Why this matters**: Website refreshes data = timestamps change. Without dedup, opens same trade multiple times.

---

### 3. MT5 Duplicate Prevention (Before Opening)

**Problem**: processed_signals.json can get out of sync with MT5 reality. Bot thinks it opened a trade, but it didn't (or still is).

**Solution**: Check MT5 directly before opening.

```python
# In open_trade(signal):
existing = mt5.positions_get(symbol=pair)
if existing:
    return False  # Already open, skip
```

**Why this matters**: Two-layer verification. processed_signals is for efficiency, MT5 is the source of truth.

---

### 4. State Pruning at Startup (24-Hour Window)

**Problem**: processed_signals.json accumulates forever. File grows unbounded.

**Solution**: Delete signals older than 24 hours at startup.

```python
def prune_signals(filepath, hours=24):
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    pruned = {k: v for k, v in data.items()
              if datetime.fromisoformat(v) > cutoff}
    # Save pruned data
```

**Why this matters**: Keeps state file compact. File stays ~10KB forever, not 100MB after months.

---

### 5. Frame-Matched Close Only

**Problem**: Website signals "CLOSE EURUSD short" but bot has "EURUSD long" open. Bot shouldn't close the wrong frame.

**Solution**: Match by pair AND frame.

```python
# On CLOSE signal:
if pair in active_frame and active_frame[pair] != frame:
    continue  # Wrong frame, skip close
close_trade(pair)
```

**Why this matters**: Prevents closing the wrong position by accident.

---

### 6. Proper Signal Processing Order

**Problem**: Fetch and process signals in wrong order = stale signals processed, duplicates opened, state mismatches.

**Solution**: Consistent, deterministic order:

```
1. Fetch HTML via proxy rotation
2. Parse all signals from HTML
3. Sort by timestamp DESC (newest first)
4. Deduplicate per pair+frame (keep one)
5. Process ACTIVE signals (frame lock + MT5 check)
6. Process CLOSE signals (frame-matched only)
7. Log status + sleep 7 seconds
8. Repeat
```

**Why this matters**: Single-threaded, predictable, debuggable. No race conditions.

---

## How It Works

### Trading Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│           WEBSITE SIGNAL PROVIDER                       │
│   (Shows: BUY EURUSD short, TP 1.09, SL 1.08)          │
└────────────────────┬────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────┐
│       BOT (7-second loop, single-threaded)              │
│                                                         │
│  STEP 1: Fetch HTML                                     │
│  ├─ Pick proxy from 10 rotating IPs                    │
│  ├─ Retry on failure                                   │
│  └─ Return HTML or None                                │
│                                                         │
│  STEP 2: Parse HTML                                    │
│  ├─ Extract: pair, side, open, tp, sl, frame, status  │
│  └─ Return list of signals                             │
│                                                         │
│  STEP 3: Sort & Deduplicate                            │
│  ├─ Sort by timestamp DESC                             │
│  ├─ Keep one per pair+frame (newest)                   │
│  └─ Discard older duplicates                           │
│                                                         │
│  STEP 4: Process ACTIVE Signals                        │
│  ├─ Check if already processed (skip)                  │
│  ├─ Check frame lock (skip if wrong frame)             │
│  ├─ Check position cap (skip if at MAX)                │
│  ├─ Check MT5 for existing position (skip if exists)   │
│  └─ Open trade with signal's exact SL/TP              │
│                                                         │
│  STEP 5: Process CLOSE Signals                         │
│  ├─ Check frame matches active_frame[pair]            │
│  └─ Close position                                     │
│                                                         │
│  STEP 6: Log Status                                    │
│  └─ Print: opened, closed, positions, profit           │
│                                                         │
│  STEP 7: Sleep 7 seconds, repeat                       │
└─────────────────────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────┐
│         METATRADER 5 ACCOUNT                            │
│   (Positions executed, SL/TP triggered automatically)   │
└─────────────────────────────────────────────────────────┘
```

### Example: Opening a Trade

**Website signals:** `BUY EURUSD short @ 1.08500, TP 1.09000, SL 1.08000`

```
CYCLE 1 (14:32:00):
  ├─ Fetch → HTML with signal (timestamp 14:32:00)
  ├─ Parse → {pair:"EURUSD", side:"BUY", frame:"short", status:"ACTIVE", ...}
  ├─ Dedup → Keep this (newest so far)
  ├─ Frame lock → active_frame["EURUSD"] not set yet
  ├─ Position cap → 5 open, max 10 → OK
  ├─ MT5 check → No EURUSD position → OK
  ├─ Open trade → Market order, ticket #123456
  ├─ Mark processed → processed_signals[signal_id] = now
  └─ Frame lock → active_frame["EURUSD"] = "short"

CYCLE 2 (14:32:07):
  ├─ Fetch → HTML (same signal, timestamp updated to 14:32:07)
  ├─ Parse → {pair:"EURUSD", side:"BUY", frame:"short", ...}
  ├─ Dedup → Keep this (newer timestamp)
  ├─ Frame lock → active_frame["EURUSD"] = "short" → MATCH
  ├─ Already processed? → Check processed_signals → YES → SKIP
  └─ (Don't open again)

CYCLE 3 (14:32:15):
  ├─ Website signals: CLOSE EURUSD short
  ├─ Fetch, Parse → {pair:"EURUSD", frame:"short", status:"CLOSE"}
  ├─ Frame match? → active_frame["EURUSD"] = "short" → MATCH
  ├─ Close trade → Sell position #123456
  ├─ Unlock frame → del active_frame["EURUSD"]
  └─ Log → Profit: +$45.67
```

---

## Installation & Setup

### Requirements

- **Windows** (MetaTrader 5 is Windows-only)
- **Python 3.8+**
- **MetaTrader 5** installed and configured
- **Demo or Live account** with a broker

### Step 1: Install Python Packages

```bash
pip install requests beautifulsoup4 MetaTrader5
```

### Step 2: Configure the Bot

Edit `config.py`:

```python
# Website
URL = "http://massyart.com/ringsignal/"

# Proxies (10 rotating free proxies)
PROXY_API_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text"
PROXY_CACHE_SECONDS = 300        # Refresh every 5 minutes
PROXY_ROTATION_STRATEGY = "round_robin"  # or "random"

# Timing
SIGNAL_INTERVAL = 7              # Check website every 7 seconds

# Trading
TRADE_VOLUME = 0.01              # Lot size (1 micro-lot ≈ $1/pip)
MAX_POSITIONS = 10               # Max concurrent open positions

# MT5 (your account credentials)
MT5_LOGIN = 24343206
MT5_PASSWORD = "your_password"
MT5_SERVER = "VantageInternational-Demo"
MT5_EXE = r"C:\Users\YourName\AppData\Roaming\MetaTrader 5\terminal64.exe"
```

### Step 3: Test MT5 Connection

```bash
python -c "
import MetaTrader5 as mt5
if mt5.initialize(login=24343206, password='pwd', server='VantageInternational-Demo'):
    print('✓ MT5 connected')
    print('Balance:', mt5.account_info().balance)
else:
    print('✗ MT5 failed')
"
```

### Step 4: Run the Bot

```bash
python main.py
```

Monitor logs:
```bash
tail -f bot.log
```

---

## Configuration

### Essential Parameters

```python
SIGNAL_INTERVAL = 7
```
- Check website every N seconds
- 7 = fast (catch signals quickly)
- 10-15 = if getting 403 rate-limit errors
- 30+ = very conservative

```python
TRADE_VOLUME = 0.01
```
- How much to trade per signal (in lots)
- 0.01 = 1 micro-lot ≈ $1/pip per signal
- Start with 0.01, increase only after 2+ weeks profitable

```python
MAX_POSITIONS = 10
```
- Maximum concurrent open positions
- If hit, new signals are skipped until positions close
- Start with 5, increase slowly

```python
PROXY_ROTATION_STRATEGY = "round_robin"
```
- "round_robin": cycle 1→2→3→...→10→1
- "random": pick random each time

---

## Running the Bot

### Start

```bash
python main.py
```

Output:
```
================================================================================
BLIND FOLLOWER BOT - STARTED
Signal interval: 7s | Max positions: 10 | Volume: 0.01
================================================================================

[STARTUP] Pruned 23 old signals from state (>24h)
MT5 connected
PROXY: fetched 12 proxies from API
```

### Monitor

Watch signals in real-time:
```bash
tail -f bot.log
```

Example:
```
[14:32:00] SIGNAL: EURUSD BUY @ 1.08500 SL:1.08000 TP:1.09000
  → OPENED ✓
[14:32:07] Status: 1 opened, 0 closed
  Open positions: 1
    [EURUSD] BUY @ 1.08523 | Profit: +$0.23
  Account: Balance $10000.00 | Equity $10000.23
[14:32:15] CLOSE: EURUSD (website signal)
  → CLOSED ✓
[14:32:22] Status: 0 opened, 1 closed
  No open positions
  Account: Balance $10000.23 | Equity $10000.23
```

### Stop

```
Ctrl+C
```

Exits cleanly. State persists.

---

## Signal Processing

### Signal Structure

```python
{
    'pair': 'EURUSD',        # Currency pair
    'side': 'BUY',           # Direction (BUY or SELL)
    'open': 1.08500,         # Entry reference (not used)
    'tp': 1.09000,           # Take profit (used exactly)
    'sl': 1.08000,           # Stop loss (used exactly)
    'frame': 'short',        # Frame: 'short' (15/30min) or 'long' (1/4hr)
    'status': 'ACTIVE',      # 'ACTIVE' (open) or 'CLOSE' (close)
    'time': datetime(...)    # Signal timestamp
}
```

### Signal States

**ACTIVE**: Website says open a trade
- Bot deduplicates by pair+frame
- Checks frame lock
- Checks position cap
- Checks MT5
- Opens once per frame

**CLOSE**: Website says close a trade
- Bot checks frame matches
- Closes only if frame matches
- Unlocks frame

### Processing Guarantees

| Guarantee | Implementation |
|-----------|---|
| **No double-open** | Dedup + processed_signals + MT5 check |
| **No wrong-frame close** | Frame match check |
| **No conflicting trades** | Frame lock (one frame per pair) |
| **No stale signals** | Sort DESC, keep newest only |
| **No state bloat** | 24-hour pruning at startup |
| **Single execution order** | Fetch → Parse → Sort → Dedup → Process |

---

## Proxy Rotation

### Why Proxies?

- Website allows ~1 request per 7 seconds
- One IP = ~515 requests/hour = instant ban
- 10 proxies = ~52 requests/hour each = acceptable

### How It Works

1. **Fetch** (every 5 minutes)
   - ProxyScrape API returns 10+ free proxies
   - Cache for 5 minutes
   - Auto-refresh

2. **Rotate** (every request)
   - Round-robin or random strategy
   - Failed proxy tried 3 times
   - After 3 failures: blacklist for 60 seconds
   - Auto-retry with next proxy

3. **Fallback**
   - All proxies fail? Skip this cycle
   - Retry next cycle (7 seconds later)

### HTTP vs HTTPS

Free proxies don't support HTTPS CONNECT tunneling. So:
- Website: `http://massyart.com/ringsignal/` ✓ (works with proxy)
- Secure enough for signals (no passwords transmitted)

---

## Logs & Monitoring

### bot.log

Every event logged:

```
[14:32:00] SIGNAL: EURUSD BUY @ 1.08500 SL:1.08000 TP:1.09000
[14:32:01]   [SKIP] Position already exists for EURUSD
[14:32:05] CLOSE: EURUSD (website signal)
[14:32:06]   → CLOSED ✓
[14:32:07] Status: 0 opened, 1 closed
  Open positions: 0
  Account: Balance $10000.23 | Equity $10000.23
```

### Console Output (Every Cycle)

```
[14:32:07] Status: X opened, Y closed
  Open positions: N
    [PAIR1] BUY @ price | Profit: $X.XX
    [PAIR2] SELL @ price | Profit: -$X.XX
  Account: Balance $XXXXX.XX | Equity $XXXXX.XX
```

### Analysis

Count trades opened today:
```bash
grep "OPENED ✓" bot.log | wc -l
```

Find skipped signals:
```bash
grep "\[SKIP\]" bot.log
```

Total profit last 24h:
```bash
grep "Profit: \$" bot.log | tail -20
```

---

## Troubleshooting

### No Trades Opening

**Check bot.log for:**
- `[SKIP] Symbol not available` → Pair doesn't exist in MT5
- `[SKIP] No tick data` → MT5 not connected
- `[SKIP] Position already exists` → Double-open protection triggered
- Position cap reached → At MAX_POSITIONS

**Fix:**
1. Verify pair is tradable in MT5
2. Check MT5 connection: `ping` broker server
3. Check processed_signals.json for accumulation (should delete old signals)
4. Reduce MAX_POSITIONS if too restrictive

### Proxy Failures

**Log message:** `WARNING: Could not fetch signals (proxy failed)`

**Causes:**
- Internet down
- ProxyScrape API unreachable
- All 10 proxies dead

**Fix:**
1. Check internet connection
2. Increase SIGNAL_INTERVAL (give proxies time to recover)
3. Test ProxyScrape API:
   ```bash
   curl "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text"
   ```
4. Switch to "random" PROXY_ROTATION_STRATEGY

### Getting Rate Limited (403 Errors)

**Fix:**
1. Increase SIGNAL_INTERVAL from 7 to 15-30
2. Try "random" rotation strategy
3. Run bot only during specific market hours

### Losing Money

**Investigate:**
1. Is the website signal provider profitable? (Compare her P&L to bot P&L)
2. Are SL/TP set correctly? (Log them, verify in MT5)
3. How many trades hit SL vs TP? (Should be ~50/50 if signals are good)
4. Check slippage impact (order fill prices vs signal prices)

---

## Advanced Topics

### Frame Lock Anti-Patterns

**DON'T expect this behavior:**
```
Website:
  EURUSD BUY short (status: ACTIVE)
  EURUSD SELL long (status: ACTIVE)

Bot:
  Opens EURUSD BUY (frames lock: short)
  Tries to open EURUSD SELL → SKIPPED (different frame locked)
```

**This is correct behavior.** Frame lock prevents conflicting trades. If you want both, close short first.

### Testing On Demo Before Live

**Mandatory steps:**
1. Run on demo account for 2+ weeks
2. Verify signal provider is profitable (check her published results)
3. Monitor bot.log daily (confirm signals open/close correctly)
4. Check: wins > losses? Average win ≈ average loss?
5. Only then go live (and start with 0.01 lots, increase after 1 week)

### Changing Configuration on Live Bot

**Safe:**
- SIGNAL_INTERVAL (just change, restart bot)
- MAX_POSITIONS (reduces new opens, doesn't affect existing)
- PROXY_ROTATION_STRATEGY (experimental only)

**Requires restart:**
- TRADE_VOLUME (affects new signals only)
- URL or MT5 credentials

---

## FAQ

### Q: Can the bot handle multiple signals at once?
**A:** Yes. bot opens multiple positions up to MAX_POSITIONS (default 10).

### Q: What if the website goes down?
**A:** Bot can't fetch signals, so it holds existing positions. Positions exit via MT5's SL/TP. No new positions open until website is back.

### Q: Can I modify the SL/TP from the bot?
**A:** Not without editing code. Philosophy: **trust the website completely**. If you want to adjust, adjust on the website.

### Q: What's the frame lock for?
**A:** Prevents opening BUY and SELL on the same pair simultaneously. Website can signal both, but bot only opens one at a time.

### Q: Can I run multiple bots?
**A:** Yes, if each trades different pairs or different MT5 accounts. Don't trade the same pair with two bots.

### Q: Is this legal?
**A:** Algorithmic trading is legal. Check your broker's terms—some restrict it. Never use to exploit glitches or market manipulation.

### Q: How much can I make?
**A:** Entirely depends on signal provider quality. Bot is just an execution engine. If signals suck, results suck.

### Q: Why does bot skip signals sometimes?
**A:** Possible reasons:
- Already opened (dedup or MT5 check)
- Wrong frame locked (frame lock)
- Position cap hit (MAX_POSITIONS)
- Symbol unavailable
- No tick data
- Proxy failed

### Q: Can I backtest this bot?
**A:** Not directly. But you can:
1. Save signals to file
2. Simulate opening/closing offline
3. Compare simulated P&L to actual bot P&L

---

## Technical Documentation

This project includes comprehensive technical documentation:

### [IMPLEMENTATION_GUIDE.md](IMPLEMENTATION_GUIDE.md)
Complete technical guide covering:
- Trade identification system (composite signal IDs with TP/SL)
- Open trade logic (deduplication, age filtering, duplicate prevention)
- Close trade logic (TP/SL matching with tolerance, ticket-based closes)
- MT5 position handling (magic number 777, filtered by magic)
- Signal parsing (complete field extraction and close reason detection)
- State management (persistent JSON storage for signals and positions)
- Order placement structure (magic number, comment, type filling)
- Multiple trades per pair (TP/SL-based differentiation)
- Restart behavior (loading state from disk, stale position cleanup)
- Edge case walkthrough (full execution trace for complex scenarios)

**Use this for:** Understanding how the bot identifies, opens, and closes trades.

### [CRITICAL_ANALYSIS.md](CRITICAL_ANALYSIS.md)
Rigorous analysis of potential failure points and edge cases:
1. Identity Consistency Test - Does website provide trade identifiers?
2. Snapshot vs Event Model - Is data event-based or snapshot-based?
3. Duplicate Trade Scenario - Can identical TP/SL cause wrong closes?
4. Missing TP/SL Fallback - What if close signal lacks TP/SL?
5. Tolerance Collision - Can two trades fall within 0.001 range?
6. Time Instability - Do relative timestamps create unstable IDs? (CRITICAL BUG)
7. Broker Adjustment Reality - What if MT5 adjusts TP/SL beyond tolerance?
8. Worst-Case Scenario - Full walkthrough of ambiguous close scenario
9. Determinism Check - Is matching deterministic or probabilistic?
10. Alternative Model Challenge - Comparison with staleness tracking approach

Summary: System works for ~90% of normal cases but has critical failure points in edge cases.

**Use this for:** Understanding limitations, risks, and recommended improvements.

### [OBJECTIVE_REFRAMING.md](OBJECTIVE_REFRAMING.md)
Fundamental reframing of the bot's actual objective:
- Exposes that the bot has been solving the WRONG problem
- Current approach: Try to match exact MT5 tickets (IMPOSSIBLE without unique IDs)
- Correct approach: Maintain state consistency with website (ACHIEVABLE)
- Compares TP/SL matching vs Counter + List approaches
- Concludes: Counter + List is more correct for the actual objective

Key insight: Website provides state snapshots, not transaction logs. Bot should sync state, not guess trades.

**Use this for:** Understanding the fundamental flaw in approach and why Counter + List may be better.

### [COUNTER_LIST_ANALYSIS.md](COUNTER_LIST_ANALYSIS.md)
Detailed analysis of alternative Counter + List approach:
- Proposes: positions[key] = [ticket1, ticket2, ...] with counter for each key
- Tests: Does it guarantee correct trade closes?
- Findings: Counts correctly but may close wrong tickets
- Edge cases: Partial closes, out-of-order closes, stale positions, tolerance collisions

7 concrete failure scenarios showing how Counter + List approach breaks in practice.

Comparison: Counter + List (~60% accuracy) vs TP/SL matching (~90%) vs Staleness detection (~95%)

Recommendation: Current TP/SL matching is better than proposed Counter + List, but staleness detection is best.

**Use this for:** Understanding why alternative approaches fail and validating current design.

---

## Summary

**What the bot does:**
- Fetches signals every 7 seconds via 10 rotating proxies
- Opens trades exactly as website signals
- Closes trades only when website signals CLOSE (frame-matched)
- Prevents stale signals, conflicting trades, and state bloat
- Logs everything for monitoring

**Core safety features:**
- Frame lock (one frame per pair)
- Most recent only (dedup per pair+frame)
- MT5 duplicate check (before opening)
- State pruning (24-hour window)
- Frame-matched close (only close correct frame)
- Deterministic processing (single-threaded)

**How to start:**
1. Install Python packages: `pip install requests beautifulsoup4 MetaTrader5`
2. Edit config.py with your credentials
3. Run: `python main.py`
4. Monitor: `tail -f bot.log`
5. Test on demo 2+ weeks before going live

**Remember:**
- Bot is only as good as website signals
- Start with 0.01 lots and demo account
- Monitor daily
- Don't trust it completely—watch the logs

---

*Last updated: March 18, 2026*
*Version: 3.0 (Frame Lock + 6 Core Behaviors)*
