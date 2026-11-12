"""Poll the PS370 and log output voltage, current and status to CSV."""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime

from ps370 import PS370

CSV_FIELDS = ["timestamp", "vout_V", "iout_A", "hv_on", "stable",
              "vtrip", "itrip", "ilim", "stb_raw"]


def take_sample(psu: PS370) -> dict:
    v = psu.read_voltage()
    i = psu.read_current()
    st = psu.status()  # clears latched trip bits, so they are captured here
    return {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "vout_V": v,
        "iout_A": i,
        "hv_on": int(st["hvon"]),
        "stable": int(st["stable"]),
        "vtrip": int(st["vtrip"]),
        "itrip": int(st["itrip"]),
        "ilim": int(st["ilim"]),
        "stb_raw": st["raw"],
    }


def format_sample(s: dict) -> str:
    flags = []
    if s["stable"]:
        flags.append("stable")
    if s["ilim"]:
        flags.append("I-LIMIT")
    if s["vtrip"]:
        flags.append("V-TRIP!")
    if s["itrip"]:
        flags.append("I-TRIP!")
    hv = "ON " if s["hv_on"] else "off"
    return (f"{s['timestamp']}  HV {hv}  V = {s['vout_V']:9.1f} V  "
            f"I = {s['iout_A'] * 1e6:7.2f} uA  {' '.join(flags)}")


def monitor(psu: PS370, interval: float = 1.0, csv_path: str | None = None,
            samples: int | None = None, off_on_exit: bool = False,
            sleep=time.sleep, out=print) -> int:
    """Sample every ``interval`` seconds until Ctrl-C (or ``samples`` taken).

    Returns the number of samples recorded.
    """
    writer = None
    fh = None
    if csv_path:
        new_file = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        fh = open(csv_path, "a", newline="")
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()

    n = 0
    failures = 0
    next_t = time.monotonic()
    try:
        while samples is None or n < samples:
            try:
                s = take_sample(psu)
                failures = 0
            except (TimeoutError, ValueError) as e:
                failures += 1
                out(f"[warn] read failed ({failures} in a row): {e}")
                if failures >= 10:
                    raise
            else:
                n += 1
                out(format_sample(s))
                if writer:
                    writer.writerow(s)
                    fh.flush()
            next_t += interval
            sleep(max(0.0, next_t - time.monotonic()))
    except KeyboardInterrupt:
        out("\nStopped by user.")
    finally:
        if off_on_exit:
            try:
                psu.hv_off()
                out("High voltage switched OFF.")
            except Exception as e:  # report but do not mask the original error
                out(f"[error] could not switch HV off: {e}")
        if fh:
            fh.close()
    return n
