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

# ─── TRADING ────────────────────────────────────────────────────────────────
TRADE_VOLUME = 0.01    # Lot size
MAX_POSITIONS = 10     # Max concurrent positions

# ─── MT5 ────────────────────────────────────────────────────────────────────

MT5_LOGIN = 24343206
MT5_PASSWORD = "oiAZ!5s6"
MT5_SERVER = "VantageInternational-Demo"
MT5_EXE    = r"C:\Users\h\AppData\Roaming\MetaTrader 5\terminal64.exe"