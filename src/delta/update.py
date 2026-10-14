"""Keep every series in the watchlist current. Run by `delta update` (and the LaunchAgent).

Each series is independent: one failing symbol never stops the rest. The run exits
non-zero if any series errored OR ended stale, because a series that silently stops
advancing looks exactly like a quiet market.
"""

from __future__ import annotations

import fcntl
import logging
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from delta.rest import RESOLUTION_SECONDS, DeltaRest
from delta.store import append_candles, candle_path, last_time

log = logging.getLogger(__name__)

# History pulled the first time a series is seen (days). Delta India starts 2023-12-29.
DEFAULT_INITIAL_DAYS = {"1m": 30, "3m": 60, "5m": 180, "15m": 365, "30m": 365,
                        "1h": 730, "2h": 730, "4h": 1100, "6h": 1100, "1d": 1100, "1w": 1100}

# A closed bar should exist within this many bar-lengths of now; beyond it the series is stale.
STALE_BARS = 3


@dataclass
class SeriesResult:
    symbol: str
    resolution: str
    appended: int = 0
    last_bar: int | None = None
    stale: bool = False
    error: str | None = None


def sync_series(rest: DeltaRest, data_dir: Path, symbol: str, resolution: str,
                initial_days: int | None = None, now: float | None = None) -> SeriesResult:
    res = SeriesResult(symbol, resolution)
    step = RESOLUTION_SECONDS[resolution]
    now_s = int(now if now is not None else time.time())
    path = candle_path(data_dir, symbol, resolution)
    try:
        stored = last_time(path)
        start = stored + step if stored is not None else \
            now_s - (initial_days or DEFAULT_INITIAL_DAYS[resolution]) * 86400
        if start <= now_s:
            res.appended = append_candles(path, rest.candles(symbol, resolution, start, now_s, now=now_s))
        res.last_bar = last_time(path)
    except Exception as e:  # isolate per series
        res.error = f"{type(e).__name__}: {e}"
        res.last_bar = last_time(path)
    # Newest closed bar opens at floor(now/step)*step - step; allow STALE_BARS of slack.
    res.stale = res.last_bar is None or (now_s - res.last_bar) > (STALE_BARS + 1) * step
    return res


def load_watchlist(path: Path) -> list[tuple[str, str, int | None]]:
    """[[series]] symbols=[...] resolutions=[...] initial_days=<optional>"""
    cfg = tomllib.loads(path.read_text())
    out = []
    for block in cfg.get("series", []):
        for sym in block["symbols"]:
            for r in block["resolutions"]:
                if r not in RESOLUTION_SECONDS:
                    raise ValueError(f"{path}: unknown resolution {r!r}")
                out.append((sym, r, block.get("initial_days")))
    if not out:
        raise ValueError(f"{path}: no [[series]] entries")
    return out


def run_update(rest: DeltaRest, data_dir: Path, watchlist: Path) -> list[SeriesResult]:
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = open(data_dir / ".update.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another `delta update` is still running") from None
    try:
        return [sync_series(rest, data_dir, s, r, d) for s, r, d in load_watchlist(watchlist)]
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
