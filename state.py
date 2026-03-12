import json
import os
from datetime import datetime

_FILE = "processed_signals.json"


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


processed_signals = _PersistentDict()
