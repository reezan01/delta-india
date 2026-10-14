"""On-disk candle store: data/candles/{SYMBOL}/{resolution}.csv

One CSV per symbol/resolution, ascending by time, no duplicates, closed bars only.
Updates are incremental: fetch from the last stored bar onwards and append.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = ["time", "datetime_utc", "open", "high", "low", "close", "volume"]


def candle_path(data_dir: Path, symbol: str, resolution: str) -> Path:
    safe = symbol.replace(":", "_").replace("/", "_")
    return Path(data_dir) / "candles" / safe / f"{resolution}.csv"


def last_time(path: Path) -> int | None:
    """Time of the last stored bar, read from the file tail (no full scan)."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(size - 4096, 0))
        lines = [ln for ln in f.read().decode().splitlines() if ln.strip()]
    if not lines or lines[-1].startswith("time"):
        return None
    return int(lines[-1].split(",", 1)[0])


def _row(c: dict) -> list:
    t = int(c["time"])
    iso = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [t, iso, c["open"], c["high"], c["low"], c["close"], c["volume"]]


def append_candles(path: Path, candles: list[dict]) -> int:
    """Append bars strictly newer than the last stored one. Returns rows written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    last = last_time(path)
    new = [c for c in candles if last is None or int(c["time"]) > last]
    if not new:
        return 0
    times = [int(c["time"]) for c in new]
    if times != sorted(set(times)):
        raise ValueError("candles must be ascending and unique before appending")
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(COLUMNS)
        w.writerows(_row(c) for c in new)
    return len(new)


def count_gaps(times: list[int], step: int) -> int:
    """Missing bars between consecutive stored bars. Delta skips intervals with no trades,
    so gaps are normal on illiquid contracts and do not by themselves mean data loss."""
    return sum((b - a) // step - 1 for a, b in zip(times, times[1:]) if b - a > step)
