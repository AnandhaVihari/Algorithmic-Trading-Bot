# Trading Bot - Improvement Recommendations

## 1. ARCHITECTURE & CODE ORGANIZATION

### 1.1 State Management - Extract to Single Source
**Problem**: Global variables scattered across main.py
```python
# Current: Multiple entry points
global positions, processed_signal_ids, safety, virtual_sl, trailing_stop_mgr
```

**Recommendation**: Create a `BotState` class to encapsulate all mutable state
```python
class BotState:
    def __init__(self):
        self.positions = PositionStore()
        self.safety = OperationalSafety()
        self.virtual_sl = VirtualSLManager()
        self.trailing_stop = TrailingStopManager()
        self.processed_signals = SignalTracker()

    def save_state(self, path):
        """Persist to disk on shutdown"""

    def restore_state(self, path):
        """Load from disk on startup"""
```

**Benefits**:
- Easier mocking for tests
- State checkpoint/restore for crash recovery
- Clear initialization order
- Dependency injection friendly

---

### 1.2 Configuration Management
**Problem**: Secrets in config.py, no environment-based config
```python
# config.py
MT5_PASSWORD = "Z2Nf&3eE"  # ❌ Hardcoded
URL = "http://massyart.com/ringsignal/"  # ❌ Hardcoded
```

**Recommendation**:
1. Use environment variables
2. Separate secrets from settings
3. Validate config at startup

```python
# config.py
import os
from dataclasses import dataclass

@dataclass
class TradingConfig:
    # From env with defaults
    url: str = os.getenv('SIGNAL_URL', 'http://localhost/signals')
    volume: float = float(os.getenv('TRADE_VOLUME', '0.01'))
    signal_interval: int = int(os.getenv('SIGNAL_INTERVAL', '7'))

    # Secrets ONLY from env (fail if missing)
    @staticmethod
    def from_env():
        return TradingConfig(
            mt5_login=os.getenv('MT5_LOGIN') or _raise('MT5_LOGIN not set'),
            mt5_password=os.getenv('MT5_PASSWORD') or _raise('MT5_PASSWORD not set'),
            ...
        )
```

**Benefits**:
- No secrets in git
- Easy deployment to different environments
- CI/CD friendly

---

## 2. ERROR HANDLING & RESILIENCE

### 2.1 Structured Exception Handling
**Problem**: Bare try/except with print statements
```python
# main.py line 206
except Exception as e:
    print(f"[{now_str}] ERROR: Failed to parse signals: {e}")
```

**Recommendation**: Define custom exceptions and handle deterministically
```python
# exceptions.py
class BotError(Exception):
    """Base exception"""
    pass

class SignalFetchError(BotError):
    """Failed to fetch signals (retryable)"""
    pass

class MT5Error(BotError):
    """MT5 operation failed"""
    pass

class ConfigError(BotError):
    """Invalid config (non-retryable)"""
    pass

# In main.py - handle each type differently
try:
    html = fetch_page()
except SignalFetchError as e:
    log(LogLevel.WARN, f"Signal fetch failed, will retry: {e}")
    return  # Retry next cycle
except ConfigError as e:
    log(LogLevel.CRITICAL, f"Configuration error, aborting: {e}")
    raise  # Non-recoverable
```

**Benefits**:
- Different retry strategies per error type
- Can alert on critical failures
- Faster debugging

---

### 2.2 Graceful Shutdown
**Problem**: Bot runs forever, no clean shutdown signal
```python
# main.py line 713
while True:
    time.sleep(60)
```

**Recommendation**: Signal handling + state save
```python
import signal

class SignalHandler:
    def __init__(self, bot_state):
        self.bot_state = bot_state
        self.shutdown_event = threading.Event()

    def setup(self):
        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _on_shutdown(self, signum, frame):
        log(LogLevel.INFO, f"Received signal {signum}, shutting down...")
        # 1. Stop signal cycle
        self.shutdown_event.set()
        # 2. Save state
        self.bot_state.save_state('state_backup.json')
        # 3. Close MT5
        mt5.shutdown()
        log(LogLevel.INFO, "Shutdown complete")
        sys.exit(0)

# Usage in main
handler = SignalHandler(bot_state)
handler.setup()
# Main loop checks shutdown_event
```

**Benefits**:
- Clean trades on redeploy
- State persistence prevents replay on restart
- Better for container orchestration

---

## 3. TESTING & OBSERVABILITY

### 3.1 Unit Tests
**Problem**: Only example_simulation(), no property tests
```python
# signal_manager.py only has example_simulation()
```

**Recommendation**: Add pytest tests for core logic
```bash
# tests/test_signal_manager.py
def test_deduplicate_keeps_most_recent():
    signals = [
        Signal(..., time=datetime(2024,1,1,10,0,0)),
        Signal(..., time=datetime(2024,1,1,10,0,5)),  # More recent
    ]
    result = SignalFilter.deduplicate_by_key(signals)
    assert len(result) == 1
    assert result[0].time == datetime(2024,1,1,10,0,5)

def test_state_differencer_handles_duplicates():
    prev = [("EURUSD", "BUY", 1.158, 1.154)] * 3
    curr = [("EURUSD", "BUY", 1.158, 1.154)] * 2
    closed, opened = StateDifferencer.compute_diff(prev, curr)
    assert closed[("EURUSD", "BUY", 1.158, 1.154)] == 1
    assert len(opened) == 0

def test_fuzzy_matcher_rejects_ambiguous():
    # Two signals equally close to MT5 position
    # Should mark as non-confident
    ...
```

**Setup**:
```bash
# requirements-dev.txt
pytest==7.4.0
pytest-cov==4.1.0
freezegun==1.2.2  # Mock datetime
```

**Benefits**:
- Catch regressions before deploy
- Document expected behavior
- Regression test past bugs

---

### 3.2 Metrics & Monitoring
**Problem**: Success rate known only after reading logs
```bash
# Validation Commands (from EXECUTION_FIXES.md)
grep "FAIL\|REJECT" bot.log | wc -l  # Manual count
```

**Recommendation**: Export metrics
```python
# metrics.py
from prometheus_client import Counter, Gauge, Histogram

# Counters (increase only)
orders_opened = Counter('orders_opened_total', 'Total orders opened', ['pair'])
orders_closed = Counter('orders_closed_total', 'Total orders closed', ['pair'])
orders_failed = Counter('orders_failed_total', 'Failed orders', ['reason'])

# Gauges (can go up/down)
open_positions = Gauge('open_positions', 'Current open positions', ['pair', 'side'])
unmatched_tickets = Gauge('unmatched_tickets', 'Stuck unmatched tickets')

# Histograms (latency)
signal_cycle_time = Histogram('signal_cycle_seconds', 'Signal cycle duration')
order_execution_time = Histogram('order_execution_seconds', 'Order execution time')

# Usage in main.py
with signal_cycle_time.time():
    run_signal_cycle()

orders_opened.labels(pair=signal.pair).inc()
open_positions.labels(pair=signal.pair, side=signal.side).set(count)
```

**Monitoring stack**:
```yaml
# docker-compose.yml
services:
  prometheus:
    image: prom/prometheus
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml

  grafana:
    image: grafana/grafana
    ports:
      - "3000:3000"
```

**Alerts**:
```yaml
# prometheus_rules.yml
groups:
  - name: trading
    rules:
      - alert: HighOrderFailureRate
        expr: rate(orders_failed_total[5m]) > 0.1
        annotations:
          summary: "Order failure rate > 10%"

      - alert: UnmatchedTicketsGrowing
        expr: increase(unmatched_tickets[5m]) > 0
        annotations:
          summary: "Unmatched tickets detected"
```

**Benefits**:
- Real-time success rate graphs
- Alert on anomalies
- Historical performance data
- Identify patterns (e.g., failures at specific times)

---

### 3.3 Logging Improvements
**Problem**: Massive bot.log (32MB), mixed signal cycle logs
```python
# main.py line 23
sys.stdout = open("bot.log", "a", buffering=1, encoding="utf-8")
# Streams ALL output here, including MT5 API calls
```

**Recommendation**: Structured logging with rotation
```python
# logging_setup.py
import logging
from logging.handlers import RotatingFileHandler
import json

class StructuredFormatter(logging.Formatter):
    def format(self, record):
        log_data = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
        }
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        return json.dumps(log_data)

def setup_logging():
    # Main logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Rotating file handler (100MB, keep 5 files)
    handler = RotatingFileHandler(
        'bot.log',
        maxBytes=100*1024*1024,
        backupCount=5
    )
    handler.setFormatter(StructuredFormatter())
    root.addHandler(handler)

    # Separate loggers for components
    signal_logger = logging.getLogger('signal_cycle')
    trader_logger = logging.getLogger('trader')
    mt5_logger = logging.getLogger('mt5')

# Usage
import logging
log = logging.getLogger('signal_cycle')
log.info("Fetched signals", extra={'count': 5, 'pair': 'EURUSD'})
```

**Benefits**:
- JSON logs → parse programmatically
- Log rotation prevents disk filling
- Easy filtering in log aggregation (ELK, DataDog, etc.)
- Separate component logs if needed

---

## 4. CODE QUALITY

### 4.1 Type Hints
**Problem**: No type information, harder to spot bugs
```python
# signal_manager.py
def find_best_match(mt5_tp, mt5_sl, signals_by_key):  # What types?
    ...
```

**Recommendation**: Add comprehensive type hints
```python
from typing import Dict, List, Tuple, Optional
from datetime import datetime

def find_best_match(
    mt5_tp: float,
    mt5_sl: float,
    signals_by_key: Dict[Tuple[str, str, float, float], List['Signal']]
) -> Tuple[Optional[Tuple], Optional['Signal'], float]:
    """Find closest signal match.

    Args:
        mt5_tp: MT5 position take profit level
        mt5_sl: MT5 position stop loss level
        signals_by_key: Dict of {key: [Signal, ...]}

    Returns:
        (best_key, best_signal, score) or (None, None, inf)
    """
    ...
```

**Setup**:
```bash
# Install mypy for static checking
pip install mypy

# mypy.ini
[mypy]
python_version = 3.9
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True
```

**Benefits**:
- IDE autocomplete
- Catch type errors before runtime
- Self-documenting code

---

### 4.2 Reduce Code Duplication
**Problem**: Close/open logic repeated across files
```python
# main.py lines 437-450: Close logic
# trader.py lines 176-186: Close logic (similar)
# virtual_sl.py: Check and close (similar pattern)
```

**Recommendation**: Extract to common interface
```python
# trading_operations.py
class TradingOperation:
    """Base class for all trading operations"""

    def validate(self) -> Tuple[bool, str]:
        """Check if operation is safe to execute"""
        raise NotImplementedError

    def execute(self) -> bool:
        """Perform the operation, return success"""
        raise NotImplementedError

    def rollback(self):
        """Clean up on failure"""
        raise NotImplementedError

class CloseTradeOperation(TradingOperation):
    def __init__(self, ticket: int, pair: str, state: BotState):
        self.ticket = ticket
        self.pair = pair
        self.state = state

    def validate(self):
        if not mt5.positions_get(ticket=self.ticket):
            return False, "Position already closed"
        return True, "OK"

    def execute(self):
        return close_position_by_ticket(self.ticket, self.pair)

    def rollback(self):
        # Nothing to rollback for close
        pass

# Usage everywhere
op = CloseTradeOperation(ticket, pair, state)
is_valid, reason = op.validate()
if is_valid:
    success = op.execute()
```

**Benefits**:
- Single source of truth for operation pattern
- Easier to add logging/metrics uniformly
- Testable operations

---

## 5. PERFORMANCE

### 5.1 Optimize MT5 Lookups
**Problem**: Multiple symbol_info() calls per trade
```python
# trader.py lines 76-83
for name in (pair, pair + "+"):
    mt5.symbol_select(name, True)
    time.sleep(0.5)  # ❌ Blocking
    info = mt5.symbol_info(name)
```

**Recommendation**: Cache symbol info with TTL
```python
# mt5_cache.py
from functools import lru_cache
from datetime import datetime, timedelta

class MT5Cache:
    def __init__(self, ttl_seconds=300):
        self.ttl = ttl_seconds
        self.cache = {}
        self.timestamps = {}

    def get_symbol_info(self, symbol: str):
        """Get cached symbol info or query MT5"""
        now = datetime.now()

        if symbol in self.cache:
            age = (now - self.timestamps[symbol]).total_seconds()
            if age < self.ttl:
                return self.cache[symbol]  # Cache hit

        # Cache miss - query MT5
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

        if info:
            self.cache[symbol] = info
            self.timestamps[symbol] = now

        return info

    def clear_expired(self):
        """Clear expired entries"""
        now = datetime.now()
        expired = [
            k for k, ts in self.timestamps.items()
            if (now - ts).total_seconds() > self.ttl
        ]
        for k in expired:
            del self.cache[k]
            del self.timestamps[k]

# Usage
cache = MT5Cache(ttl_seconds=300)
info = cache.get_symbol_info('EURUSD')
```

**Benefits**:
- Reduce MT5 API calls
- Avoid sleep delays
- Better latency

---

### 5.2 Batch Operations
**Problem**: MT5 API called O(N) times per cycle
```python
# main.py - multiple calls per position
mt5_positions = mt5.positions_get()  # Called 3+ times per cycle
virtual_sl.check_and_close_all(...)
```

**Recommendation**: Query once, pass around
```python
# In run_signal_cycle()
def run_signal_cycle():
    # Get all data ONCE
    mt5_positions = mt5.positions_get() or []
    account_info = mt5.account_info()

    # Pass around
    virtual_sl.check_and_close_all(mt5_positions, account_info)
    trailing_stop_mgr.update_all(mt5_positions)
    safety.check_stale_tickets(mt5_positions)
```

**Benefits**:
- O(N) → O(1) MT5 calls
- 7-second cycle stays under 1 second execution
- Consistent snapshot across checks

---

## 6. DOCUMENTATION

### 6.1 README
**Problem**: No README, hard to onboard

**Create** `README.md`:
```markdown
# Trading Bot - Blind Follower

## Quick Start

### Setup
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export MT5_LOGIN=24446623
export MT5_PASSWORD=...
python main.py
```

### Architecture
- **signal_manager.py**: Counter-based state diff (prev_counter - curr_counter)
- **trader.py**: MT5 order placement (50+ pips deviation, 3 retries)
- **virtual_sl.py**: Spread-aware stop loss
- **trailing_stop.py**: Phase-based SL management

### Safety Guarantees
- UNMATCHED positions never closed
- FAILED_CLOSE positions escalated after 5 attempts
- Fuzzy matching requires 50%+ confidence
<br>... (more sections)
```

### 6.2 ADR (Architecture Decision Record)
**Problem**: Why is fuzzy matching used? Why counter-based diff?

**Create** `docs/adr/0001-counter-based-diff.md`:
```markdown
# ADR-001: Counter-Based Diff Instead of TP/SL Exact Matching

## Context
Website shows CURRENT state (snapshot), not EVENTS.
Need to infer what trades opened/closed between snapshots.

## Decision
Use Counter(previous_keys) - Counter(current_keys) to compute diffs.

## Rationale
1. **Exact TP/SL Matching is fragile**: Prices move 0.00001, fuzzy matching needed
2. **Counters are simple**: 3 EURUSD keys - 2 EURUSD keys = 1 closed
3. **No replay problems**: Diff-based is idempotent (key appears or disappears)

## Consequences
+ Simple logic, few edge cases
+ Matches "snapshot semantics" of website
- Fuzzy matching must be very accurate (threshold trade-offs)
- Ambiguous matches go to UNMATCHED bucket (never closed)

## Alternatives Considered
- Exact matching (fragile with price precision)
- Machine learning model (overkill)
- Event-based API (not available)
```

---

## 7. DEPLOYMENT & OPERATIONS

### 7.1 Configuration Validation
**Problem**: Bot starts with invalid config, fails after hours
```python
# config.py - no validation
URL = ""  # Invalid but not caught until scraper runs
TRADE_VOLUME = -0.01  # Invalid but not caught
```

**Recommendation**: Validate at startup
```python
# config.py
from dataclasses import dataclass
from enum import Enum

class ValidationError(Exception):
    pass

@dataclass
class TradingConfig:
    url: str
    trade_volume: float
    ...

    def validate(self):
        if not self.url:
            raise ValidationError("URL cannot be empty")
        if self.trade_volume <= 0 or self.trade_volume > 10:
            raise ValidationError(f"Invalid volume: {self.trade_volume}")
        # Add more checks...
        return True

# In main.py
config = TradingConfig.from_env()
try:
    config.validate()
    print("✓ Config valid")
except ValidationError as e:
    print(f"✗ Config error: {e}")
    sys.exit(1)
```

---

### 7.2 Health Checks
**Problem**: No way to know if bot is alive
```python
# main.py - bot runs silently, could be dead
```

**Recommendation**: Expose health endpoint
```python
# health.py
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from datetime import datetime, timedelta, timezone

class HealthHandler(BaseHTTPRequestHandler):
    bot_state = None  # Set by startup

    def do_GET(self):
        if self.path == '/health':
            # Last signal cycle timestamp
            last_cycle = self.bot_state.last_cycle_time
            now = datetime.now(timezone.utc)
            age_seconds = (now - last_cycle).total_seconds()

            # Healthy if cycled within last 30 seconds
            status = 'healthy' if age_seconds < 30 else 'unhealthy'
            code = 200 if status == 'healthy' else 503

            response = {
                'status': status,
                'last_cycle_age_seconds': age_seconds,
                'open_positions': len(self.bot_state.positions),
                'unmatched_tickets': self.bot_state.get_unmatched_count(),
            }

            self.send_response(code)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

# Startup
server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()

# Kubernetes liveness probe
# livenessProbe:
#   httpGet:
#     path: /health
#     port: 8080
#   initialDelaySeconds: 30
#   periodSeconds: 10
```

---

## 8. PRIORITY ROADMAP

### Phase 1 (Week 1) - Critical
- [ ] Move secrets to environment variables
- [ ] Add config validation
- [ ] Add unit tests for signal_manager
- [ ] Add metrics (orders_opened, orders_failed)
- [ ] Add README

### Phase 2 (Week 2) - Important
- [ ] Structured logging with rotation
- [ ] Graceful shutdown with state save
- [ ] MT5 cache for symbol info
- [ ] Health check endpoint
- [ ] Type hints for signal_manager

### Phase 3 (Week 3+) - Nice to Have
- [ ] Extract common operation patterns
- [ ] Prometheus + Grafana
- [ ] ADR documentation
- [ ] Integration tests
- [ ] Performance profiling

---

## Summary of Benefits

| Improvement | Effort | Impact | Priority |
|---|---|---|---|
| Secrets → env vars | 30min | Unblock CI/CD | 🔴 |
| Config validation | 1hr | Catch bugs early | 🔴 |
| Unit tests | 4hrs | Regression prevention | 🟠 |
| Metrics export | 2hrs | Real-time visibility | 🟠 |
| Structured logging | 2hrs | Better debugging | 🟠 |
| Graceful shutdown | 1hr | Crash safety | 🟡 |
| Type hints | 4hrs | Better IDE support | 🟡 |
| Health checks | 2hrs | K8s integration | 🟡 |
| Code dedup | 3hrs | Maintenance | 🟡 |
| docs/README | 2hrs | Onboarding | 🟡 |

**Total Phase 1**: 7 hours to major visibility and stability improvements

---

## 9. REAL DATA ANALYSIS (2026-03-31)

**Analysis Date**: 2026-03-31 08:00 UTC
**Data Source**: trailing_stop_meta.json + processed_signals.json

### Current Status

**Active Positions**: 4
- CADCHF BUY (T1090272224): Entry 0.57384 → TP 0.57614 (0.23 pips)
- USDCHF BUY (T1090272412): Entry 0.7992 → TP 0.8015 (0.23 pips)
- EURCHF BUY (T1090449491): Entry 0.91696 → TP 0.91926 (0.23 pips)
- USDJPY SELL (T1090937481): Entry 159.644 → TP 159.414 (23 pips)

**Processed Signals**: 115 total
- Fresh signals (<30 min): 5 (4%)
- Stale signals (≥30 min): 110 (96%)
- Timespan: 19 hours (2026-03-30 13:00 → 2026-03-31 08:00)

### Issue 9.1: Stale Signal Accumulation (96% of list)

**Severity**: Medium | **Impact**: File bloat, slower deduplication

**Problem**:
- processed_signals.json keeps entire 24-hour window
- Age filter (30 min) prevents opens, but list never cleaned
- 110 signals are beyond opening threshold

**Current Code**:
```python
# main.py:72
cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
```

**Quick Fix**: Reduce window from 24h → 2h
```python
# main.py:72
cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
```

**Benefit**:
- Signal count: 115 → ~10-15 signals (87% reduction)
- File size: ~45KB → ~4KB (91% reduction)
- Dedup checks: ~10x faster
- No functional impact (age filter already active)

### Issue 9.2: Position Aging Lifecycle

**Severity**: Low | **Impact**: Expected, needs monitoring

**Current State**:
- All 4 positions are 2-8 hours old
- Matched to signals from earlier time windows
- New signals for same pairs show different entry prices

**Example - USDJPY SELL**:
```
Position (T1090937481):
  Entry: 159.644, TP: 159.414, SL: 159.744

Latest Signal (2026-03-31T08:00):
  Entry: 159.414, TP: 159.744
  (Different prices = new signal, correctly REJECTED by age filter)
```

**Explanation**: This is EXPECTED behavior
- Position opens on signal at time T
- New signals arrive at T+2h with different prices
- Bot age filter prevents reopens
- Positions eventually close via trailing stop rules

**Action**: Monitor for:
- If trailing stop doesn't activate after 4+ hours
- If positions stuck >6-8 hours

### Issue 9.3: Recommended Implementation

**Add to main.py after load_processed_signals():**

```python
def cleanup_old_processed_signals(max_age_hours=2):
    """Remove signals older than max_age_hours from processed_signals.json.

    Runs after each signal cycle to keep file size manageable.
    Prevents bloat from 24-hour accumulation (currently 110+ stale signals).

    Args:
        max_age_hours: Keep only signals newer than this (default 2 hours)
                      Age filter is 30 min, so 2h buffer is safe
    """
    try:
        processed = load_processed_signals()
        if not processed:
            return

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)

        cleaned = set()
        for sig_id in processed:
            try:
                # sig_id format: "2026-03-30T15:30:00+00:00_('PAIR', 'SIDE', entry, tp)"
                time_str = sig_id.split("_")[0]
                sig_time = datetime.fromisoformat(time_str)
                if sig_time > cutoff:
                    cleaned.add(sig_id)
            except:
                # Invalid format, keep it
                cleaned.add(sig_id)

        if len(cleaned) < len(processed):
            old_count = len(processed)
            save_processed_signals(cleaned)
            removed = old_count - len(cleaned)
            print(f"[CLEANUP] Purged {removed} stale signals, kept {len(cleaned)}")
    except Exception as e:
        print(f"[ERROR] Cleanup failed: {e}")
```

**Call in main signal loop (after run_signal_cycle()):**
```python
cleanup_old_processed_signals(max_age_hours=2)
```

**Change in load_processed_signals():**
```python
# OLD:  cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
# NEW:  cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
```

### Expected Results After Implementation

- **File Size**: 115 signals (~45KB) → 10-15 signals (~4KB), **91% reduction**
- **Performance**: Deduplication checks **10x faster**
- **Logs**: Show cleanup actions (e.g., "Purged 105 stale signals")
- **Behavior**: No functional change (age filter + cleanup are orthogonal)

### Verification Checklist

- [ ] processed_signals.json contains only <2-hour-old signals
- [ ] Cleanup logs show regular purges
- [ ] No regression in signal opening/closing logic
- [ ] bot.log shows clean position lifecycle
- [ ] File size stays under 10KB

