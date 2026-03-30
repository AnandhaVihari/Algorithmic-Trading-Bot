"""
SESSION FILTER - Multiple Trading Session Support

Supports trading configuration for different forex session windows:
  'all' - trade 24/7
  'london' - 08:00-17:00 UTC
  'ny' - 13:00-22:00 UTC
  'overlap' - 13:00-17:00 UTC (London-NY overlap)
  'asia' - 22:00-08:00 UTC (Tokyo, Sydney, Singapore)

Configuration: Set trading_sessions in bot_control.json
"""

from datetime import datetime, timezone
def is_trading_session_allowed(trading_sessions_mode):
    """
    Check if current time is within allowed trading session.

    Args:
        trading_sessions_mode: str - which sessions to trade
            'all' - trade 24/7
            'london' - 08:00-17:00 UTC only
            'ny' - 13:00-22:00 UTC only
            'overlap' - 13:00-17:00 UTC only (London-NY overlap)
            'asia' - 22:00-08:00 UTC (Tokyo, Sydney, Singapore)

    Returns:
        bool: True if current time is within allowed session
    """
    if trading_sessions_mode == 'all':
        return True

    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    weekday = now_utc.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday

    # All modes except 'all' respect weekends
    if weekday >= 5 and trading_sessions_mode != 'all':
        return False

    if trading_sessions_mode == 'london':
        # London: 08:00-17:00 UTC
        return 8 <= hour_utc < 17

    elif trading_sessions_mode == 'ny':
        # NY: 13:00-22:00 UTC
        return 13 <= hour_utc < 22

    elif trading_sessions_mode == 'overlap':
        # Overlap: 13:00-17:00 UTC
        return 13 <= hour_utc < 17

    elif trading_sessions_mode == 'asia':
        # Asia: 22:00-08:00 UTC (wraps around midnight)
        return hour_utc >= 22 or hour_utc < 8

    # Default to 'all' mode if unknown mode
    return True


def get_session_status_for_mode(trading_sessions_mode):
    """
    Get detailed status for a trading mode.

    Returns:
        dict with current session info
    """
    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    weekday = now_utc.weekday()
    weekday_name = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][weekday]

    in_london = 8 <= hour_utc < 17
    in_ny = 13 <= hour_utc < 22
    in_overlap = 13 <= hour_utc < 17
    in_asia = hour_utc >= 22 or hour_utc < 8
    is_weekend = weekday >= 5

    allowed = is_trading_session_allowed(trading_sessions_mode)

    return {
        'now_utc': now_utc.strftime('%Y-%m-%d %H:%M:%S UTC'),
        'weekday': weekday_name,
        'hour_utc': hour_utc,
        'in_london': in_london,
        'in_ny': in_ny,
        'in_overlap': in_overlap,
        'in_asia': in_asia,
        'is_weekend': is_weekend,
        'mode': trading_sessions_mode,
        'allowed': allowed
    }

