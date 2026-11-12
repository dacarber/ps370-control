"""Always-on CSV logging of PS370 readings and commands, on one shared clock.

Two files per day in the log folder (default: logs/ next to these scripts):

    readings_YYYY-MM-DD.csv   one row per sample (V, I, status bits)
    commands_YYYY-MM-DD.csv   one row per deliberate action (set commands, typed
                              queries with their reply, refusals, errors, events)

Both carry `timestamp` (local time with UTC offset, ms) and `unix_time` (seconds since
1970, UTC), taken from the same clock, so they line up exactly; e.g. in pandas:
    pd.merge_asof(commands, readings, on="unix_time", direction="backward")
"""

from __future__ import annotations

import csv
import os
import threading
import time
from datetime import datetime

READING_FIELDS = ["timestamp", "unix_time", "vout_V", "iout_A", "hv_on", "stable",
                  "vtrip", "itrip", "ilim", "stb_raw"]
COMMAND_FIELDS = ["timestamp", "unix_time", "source", "command", "status", "response"]

# status values in the commands file
OK, REPLY, ERROR, REFUSED, EVENT = "OK", "REPLY", "ERROR", "REFUSED", "EVENT"

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def stamp(t: float | None = None) -> tuple[str, float]:
    """(local ISO-8601 time with UTC offset and milliseconds, unix seconds)."""
    if t is None:
        t = time.time()
    iso = datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")
    return iso, round(t, 3)


def default_log_dir() -> str:
    return os.environ.get("PS370_LOG_DIR") or DEFAULT_DIR


class DataLogger:
    """Thread-safe writer for the readings and commands CSV files.

    ``enabled=False`` pauses writing (rows are dropped) without closing anything.
    ``clock`` is injectable for tests (defaults to time.time).
    """

    def __init__(self, log_dir: str | None = None, source: str = "script",
                 clock=time.time, enabled: bool = True):
        self.log_dir = os.path.abspath(log_dir or default_log_dir())
        self.source = source
        self.clock = clock
        self.enabled = enabled
        self._lock = threading.Lock()
        self._files: dict[str, tuple[str, object, csv.DictWriter]] = {}  # kind -> (day, fh, w)

    # ---- file handling -------------------------------------------------------------
    def path(self, kind: str, day: str) -> str:
        return os.path.join(self.log_dir, f"{kind}_{day}.csv")

    def _writer(self, kind: str, day: str, fields: list[str]) -> csv.DictWriter:
        cur = self._files.get(kind)
        if cur and cur[0] == day:
            return cur[2]
        if cur:  # date changed: midnight rollover
            cur[1].close()
        os.makedirs(self.log_dir, exist_ok=True)
        p = self.path(kind, day)
        new = not os.path.exists(p) or os.path.getsize(p) == 0
        fh = open(p, "a", newline="")
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        self._files[kind] = (day, fh, w)
        return w

    def _write(self, kind: str, fields: list[str], row: dict) -> None:
        if not self.enabled:
            return
        day = datetime.fromtimestamp(float(row["unix_time"])).strftime("%Y-%m-%d")
        with self._lock:
            w = self._writer(kind, day, fields)
            w.writerow(row)
            self._files[kind][1].flush()  # survive power loss / crashes

    def close(self) -> None:
        with self._lock:
            for _, fh, _ in self._files.values():
                fh.close()
            self._files.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- public API ------------------------------------------------------------------
    def now(self) -> tuple[str, float]:
        return stamp(self.clock())

    def reading(self, sample: dict) -> None:
        """Log one sample (as produced by monitor.take_sample)."""
        row = dict(sample)
        if "unix_time" not in row:
            row["timestamp"], row["unix_time"] = self.now()
        self._write("readings", READING_FIELDS, row)

    def command(self, command: str, status: str, response: str = "",
                source: str | None = None) -> None:
        ts, ut = self.now()
        self._write("commands", COMMAND_FIELDS, {
            "timestamp": ts, "unix_time": ut, "source": source or self.source,
            "command": command, "status": status, "response": response,
        })

    def event(self, text: str, source: str | None = None, detail: str = "") -> None:
        self.command(text, EVENT, detail, source)


def from_args(args, source: str) -> DataLogger | None:
    """Build a logger from --log-dir / --no-log command-line options."""
    if getattr(args, "no_log", False):
        return None
    return DataLogger(getattr(args, "log_dir", None), source=source)
