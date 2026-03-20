import json
import os
from datetime import datetime, timezone

_FILE = "processed_signals.json"
_POSITIONS_FILE = "open_positions.json"


class _PersistentDict:
    """Dict-like that auto-saves to disk so restarts don't re-process old signals."""

    def __init__(self):
        self._data = {}
        if os.path.exists(_FILE):
            try:
                with open(_FILE) as f:
                    raw = json.load(f)
                self._data = {k: datetime.fromisoformat(v) for k, v in raw.items()}
                print(f"STATE: loaded {len(self._data)} processed signals from disk")
            except Exception as e:
                print(f"STATE: could not load {_FILE} ({e}), starting fresh")

    def _save(self):
        with open(_FILE, "w") as f:
            json.dump({k: v.isoformat() for k, v in self._data.items()}, f)

    def __contains__(self, key):  return key in self._data
    def __iter__(self):           return iter(self._data)
    def __setitem__(self, key, value):
        self._data[key] = value
        self._save()
    def __delitem__(self, key):
        del self._data[key]
        self._save()
    def items(self):  return self._data.items()
    def keys(self):   return self._data.keys()


class _PositionTracker:
    """Maps signal_id → position metadata (ticket, pair, frame, price).

    Used to match close signals to the positions they opened.
    """

    def __init__(self):
        self._data = {}
        if os.path.exists(_POSITIONS_FILE):
            try:
                with open(_POSITIONS_FILE) as f:
                    self._data = json.load(f)
                    print(f"STATE: loaded {len(self._data)} open position mappings from disk")
            except Exception as e:
                print(f"STATE: could not load {_POSITIONS_FILE} ({e}), starting fresh")

    def _save(self):
        with open(_POSITIONS_FILE, "w") as f:
            json.dump(self._data, f)

    def add(self, signal_id, ticket, pair, frame, open_price, side, signal_time=None, tp=None, sl=None):
        """Store mapping: signal_id → position metadata."""
        # Handle signal_time as string or datetime
        if signal_time:
            if hasattr(signal_time, 'isoformat'):
                time_str = signal_time.isoformat()
            else:
                time_str = str(signal_time)
        else:
            time_str = None

        self._data[signal_id] = {
            "ticket": ticket,
            "pair": pair,
            "frame": frame,
            "open_price": open_price,
            "side": side,
            "signal_time": time_str,
            "tp": tp,
            "sl": sl,
            "created_at": datetime.now(timezone.utc).isoformat()  # When we opened it
        }
        self._save()

    def get_by_signal(self, signal_id):
        """Get position metadata by signal_id."""
        return self._data.get(signal_id)

    def find_matching_position(self, pair, frame, tp=None, sl=None):
        """Find position by pair+frame.

        Matching strategy:
        1. If TP and SL provided, match by TP/SL values (allowing small tolerance)
           - Tolerance needed because MT5 might adjust TP/SL due to min distance rules
           - Match if: abs(stored_tp - incoming_tp) < tolerance AND abs(stored_sl - incoming_sl) < tolerance
           - If found → return it (match!)
           - If NOT found → return None (close signal is NOT for our trades)
        2. Otherwise return oldest position (FIFO - first to open, first to close)

        KEY: TP/SL matching is definitive. If no match with provided TP/SL,
        the close signal belongs to a different trade (or already closed).
        """
        TP_SL_TOLERANCE = 0.001  # MT5 might adjust by small amount

        matches = []
        for signal_id, metadata in self._data.items():
            if metadata["pair"] != pair or metadata["frame"] != frame:
                continue
            matches.append((signal_id, metadata))

        if not matches:
            return None, None

        # If single match, return it
        if len(matches) == 1:
            return matches[0]

        # If multiple matches and TP/SL provided, match by TP/SL values with tolerance
        if tp is not None and sl is not None and len(matches) > 1:
            for signal_id, metadata in matches:
                stored_tp = metadata.get('tp')
                stored_sl = metadata.get('sl')

                # Check if TP and SL match within tolerance
                if (stored_tp is not None and stored_sl is not None and
                    abs(stored_tp - tp) < TP_SL_TOLERANCE and
                    abs(stored_sl - sl) < TP_SL_TOLERANCE):
                    return signal_id, metadata
            # If no TP/SL match found, return None
            return None, None

        # Multiple matches, no TP/SL provided: return OLDEST (FIFO)
        oldest = min(matches, key=lambda x: x[1].get('created_at', ''))
        return oldest

    def remove(self, signal_id):
        """Remove position mapping when closed."""
        if signal_id in self._data:
            del self._data[signal_id]
            self._save()

    def all_positions(self):
        """Get all open position mappings."""
        return self._data.items()


processed_signals = _PersistentDict()
position_tracker = _PositionTracker()
