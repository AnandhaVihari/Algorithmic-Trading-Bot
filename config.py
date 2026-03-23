# ═══════════════════════════════════════════════════════════════════════════════
# BLIND FOLLOWER - MINIMAL CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

URL = "http://massyart.com/ringsignal/"

# ─── PROXIES ────────────────────────────────────────────────────────────────
PROXY_API_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text"
PROXY_CACHE_SECONDS = 300
PROXY_ROTATION_STRATEGY = "round_robin"

# ─── TIMING ────────────────────────────────────────────────────────────────
SIGNAL_INTERVAL = 7   # Check website every 7 seconds
MAX_SIGNAL_AGE = 3600  # Skip signals older than 60 minutes (3600 seconds) - allows trades to run longer

# ─── TRADING ────────────────────────────────────────────────────────────────
TRADE_VOLUME = 0.01    # Lot size

MT5_LOGIN = 24446623
MT5_PASSWORD = "Z2Nf&3eE"
MT5_SERVER = "VantageInternational-Demo"
MT5_EXE    = r"D:\MT5s\MetaTrader 5\terminal64.exe" 