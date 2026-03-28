"""
SESSION FILTER - London & New York Overlap Detection

Forex Trading Sessions (UTC):
  London: 08:00 - 17:00 UTC
  New York: 13:00 - 22:00 UTC

Overlap Window:
  - Winter (EST): 13:00 - 17:00 UTC (8:00 AM - 12:00 PM EST)
  - Summer (EDT): 13:00 - 17:00 UTC (9:00 AM - 1:00 PM EDT)

Note: Daylight saving transition handled automatically by system timezone
"""

from datetime import datetime, timezone, timedelta

# Track last logged state to avoid repetitive logging
_last_state = None


def is_london_ny_overlap():
    """
    Check if current time is within London-New York session overlap.
    
    Returns:
        bool: True if within overlap, False otherwise
    """
    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    weekday = now_utc.weekday()  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday
    
    # Don't trade on weekends
    if weekday >= 5:
        return False
    
    # Overlap: 13:00 - 17:00 UTC
    # This covers both EST and EDT overlaps
    if 13 <= hour_utc < 17:
        return True
    
    return False


def get_session_info():
    """
    Get detailed session information for logging.
    
    Returns:
        dict: Session status and times
    """
    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    weekday = now_utc.weekday()
    weekday_name = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][weekday]
    
    # Session times (UTC)
    london_open = 8
    london_close = 17
    ny_open = 13
    ny_close = 22
    
    overlap_start = 13
    overlap_end = 17
    
    in_london = london_open <= hour_utc < london_close
    in_ny = ny_open <= hour_utc < ny_close
    in_overlap = overlap_start <= hour_utc < overlap_end
    is_weekend = weekday >= 5
    
    return {
        'now_utc': now_utc.strftime('%Y-%m-%d %H:%M:%S UTC'),
        'weekday': weekday_name,
        'hour_utc': hour_utc,
        'london_open': london_open,
        'london_close': london_close,
        'ny_open': ny_open,
        'ny_close': ny_close,
        'in_london_session': in_london,
        'in_ny_session': in_ny,
        'in_overlap': in_overlap,
        'is_weekend': is_weekend,
        'trading_allowed': in_overlap and not is_weekend
    }


def session_status_string():
    """
    Get human-readable session status. Only logs if state changed.

    Returns:
        str or None: Session status message (None if no change)
    """
    global _last_state

    info = get_session_info()
    current_state = info['trading_allowed']

    # Only return log if state changed
    if _last_state == current_state:
        return None

    _last_state = current_state

    status = f"[SESSION] {info['weekday']} {info['now_utc']} "

    if info['is_weekend']:
        status += "| WEEKEND - Trading disabled"
    elif info['in_overlap']:
        status += "| IN OVERLAP - Trading ACTIVE"
    elif info['in_london_session']:
        status += "| London only - Waiting for NY"
    elif info['in_ny_session']:
        status += "| NY only - London closed"
    else:
        status += "| Market closed - Waiting for London"

    return status


if __name__ == "__main__":
    print(session_status_string())
    print()
    info = get_session_info()
    for key, val in info.items():
        print(f"  {key}: {val}")
