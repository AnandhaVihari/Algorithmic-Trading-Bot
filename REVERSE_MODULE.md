# Reverse Signal Trading Module

Complete execution module for testing signal reversals with strict mapping.

## Architecture

### 1. **reverse_executor.py** - Core Module

#### SignalReversal
- **invert_side()**: BUY ↔ SELL
- **validate_and_invert()**:
  - Validates signal TP/SL correctness for original side
  - Inverts TP ↔ SL
  - Validates inverted TP/SL for reversed side
  - Returns: (is_valid, error_message, inverted_tp, inverted_sl)

#### ReversedTrade Dataclass
Tracks each trade:
```
Original Signal → BUY TP=1.67574 SL=1.67244
                     ↓
         Reversed Trade → SELL TP=1.67244 SL=1.67574
                              ↓
                    Entry Price (market)
                    Exit Price (TP or SL)
                    PnL Calculation
```

#### ReverseTradeTracker
- **create_trade()**: Create reversed trade record, validate, auto-save
- **update_trade_execution()**: Mark OPEN with ticket + entry price
- **update_trade_close()**: Mark CLOSED with exit price + reason
- **get_summary()**: Win rate, PnL, trade count statistics
- **save_to_disk()**: Persist to `reversed_trades.json`

### 2. **reverse_bot.py** - Standalone Bot

Executes independently:
```
Fetch Signals (ACTIVE only)
    ↓
Filter by Session (London-NY overlap)
    ↓
Create Reversed Trade Record
    ↓
Validate Inversion
    ↓
Execute at Market (No deviation)
    ↓
Track in reversed_trades.json
```

## Usage

### Start Reverse Trading
```bash
python reverse_bot.py
```

### Output Files
- **reversed_trades.json**: All trades with full history
- **reverse_bot.log**: Execution logs with timestamps

### Example Trade Record
```json
{
  "2026-03-31T08:00:00+00:00_GBPJPY_SELL": {
    "trade_id": "2026-03-31T08:00:00+00:00_GBPJPY_SELL",
    "timestamp": "2026-03-31T08:00:00+00:00",
    "original_signal_side": "BUY",
    "original_signal_tp": 211.300,
    "original_signal_sl": 210.970,
    "original_signal_entry": 211.168,

    "reversed_side": "SELL",
    "reversed_tp": 210.970,
    "reversed_sl": 211.300,

    "symbol": "GBPJPY",
    "lot_size": 0.01,
    "ticket": 1090937481,

    "entry_price": 211.168,
    "entry_time": "2026-03-31T08:01:15+00:00",

    "exit_price": 210.970,
    "exit_time": "2026-03-31T08:45:30+00:00",
    "exit_reason": "TP",

    "pnl": 0.198,
    "pnl_pips": 1980.0,

    "status": "CLOSED"
  }
}
```

## Core Logic

### Signal Inversion
```
Original Signal                 Reversed Trade
├─ BUY                         → SELL
├─ SELL                        → BUY
├─ Entry: 1.67574              Entry: 1.67574 (market, no change)
├─ TP: 1.67844                 TP: 1.67244 (original SL)
└─ SL: 1.67244                 SL: 1.67844 (original TP)
```

### Validation Rules

**For BUY signals:**
- TP must be > Entry
- SL must be < Entry

**For SELL signals:**
- TP must be < Entry
- SL must be > Entry

**After reversal:**
- Same validation applied to reversed side with new TP/SL
- Rejects if inverted positions are invalid

### Session Filter
- Only execute during London-NY overlap (13:00-17:00 UTC)
- Skip all other times
- Check signal.time (creation time), not current time

### Risk Management
- Fixed 0.01 lot size (no scaling)
- Market entry only (no limit orders)
- No manual intervention
- No early closing
- Only exit via TP or SL

## Testing Expectations

### Hypotheses to Test
1. **Alpha reversal**: Do reversed signals have positive expectancy?
2. **Edge disappearance**: Are original signals just noise reversal?
3. **Market regime**: Does reversal work in all sessions?
4. **Trade quality**: Are TP/SL levels valid after reversal?

### Key Metrics
- Win rate: (wins / total closed trades) %
- Total P&L: Sum of all closed trade profits
- Average trade: Total P&L / trade count
- Max loss: Largest single loss
- Max win: Largest single win

## Files

- **reverse_executor.py** (312 lines): Core module
- **reverse_bot.py** (156 lines): Standalone bot
- **reversed_trades.json**: Persistent trade log
- **reverse_bot.log**: Execution logs

## Configuration

Inherits from **config.py**:
- SIGNAL_INTERVAL = 10 seconds (cycle rate)
- TRADE_VOLUME = 0.01 lot size
- MAX_SIGNAL_AGE = 1800s (30 min, for original signals)

Session filter from **session_filter.py**:
- London-NY overlap: 13:00-17:00 UTC

MT5 connection from **trader.py**:
- Uses existing account setup
- Same error handling
- Adaptive deviation on order placement

## Safety Features

✅ Strict validation before trade execution
✅ Signal inversion verification
✅ TP/SL never swapped incorrectly
✅ Session filter prevents off-hours trading
✅ Duplicate protection per signal
✅ Persistent logging for audit trail
✅ No silent failures (all errors logged)
✅ Invalid signals rejected with reason
✅ PnL calculation with entry/exit prices

## Limitations

- Does NOT track partial fills
- Does NOT handle slippage explicitly (market order)
- Does NOT support manual trade closure (only TP/SL)
- Does NOT apply trailing stop (strict exit only)
- Does NOT support dynamic lot sizing
- Assumes MT5 connection is available
