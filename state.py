import json
import os
from datetime import datetime

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

    def add(self, signal_id, ticket, pair, frame, open_price, side, signal_time=None):
        """Store mapping: signal_id → position metadata."""
        self._data[signal_id] = {
            "ticket": ticket,
            "pair": pair,
            "frame": frame,
            "open_price": open_price,
            "side": side,
            "signal_time": signal_time.isoformat() if signal_time else None,
            "created_at": datetime.utcnow().isoformat()  # When we opened it
        }
        self._save()

    def get_by_signal(self, signal_id):
        """Get position metadata by signal_id."""
        return self._data.get(signal_id)

    def find_matching_position(self, pair, frame):
        """Find most recent position opened from same pair+frame."""
        matches = []
        for signal_id, metadata in self._data.items():
            if metadata["pair"] != pair or metadata["frame"] != frame:
                continue
            matches.append((signal_id, metadata))

        # Return the most recent (last added)
        if matches:
            return matches[-1]  # Most recent position
        return None, None

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
