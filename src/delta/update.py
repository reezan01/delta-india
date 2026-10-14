"""Keep every series in the watchlist current. Run by `delta update` (and the LaunchAgent).

Each series is independent: one failing symbol never stops the rest. The run exits
non-zero if any series errored OR ended stale, because a series that silently stops
advancing looks exactly like a quiet market.
"""

from __future__ import annotations

import fcntl
import logging
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from delta.options import OptionQuote, write_snapshot_csv
from delta.rest import RESOLUTION_SECONDS, DeltaRest
from delta.store import append_candles, candle_path, last_time

log = logging.getLogger(__name__)

# History pulled the first time a series is seen (days). Delta India starts 2023-12-29.
DEFAULT_INITIAL_DAYS = {"1m": 30, "3m": 60, "5m": 180, "15m": 365, "30m": 365,
                        "1h": 730, "2h": 730, "4h": 1100, "6h": 1100, "1d": 1100, "1w": 1100}

# An options snapshot whose median ticker is older than this is a frozen feed, not a market.
OPTIONS_STALE_SECONDS = 300

# Options snapshots older than this many days are deleted (override: [options] retention_days).
DEFAULT_OPTIONS_RETENTION_DAYS = 90
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SNAPSHOT_FILE = re.compile(r"^\d{6}\.csv(\.gz)?$")

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
    removed: int = 0   # files deleted (resolution "prune" only)


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


def snapshot_options(rest: DeltaRest, data_dir: Path, underlying: str,
                     now: datetime | None = None) -> SeriesResult:
    """Save the full chain to data/options/{UND}/{date}/{HHMMSS}.csv.gz.

    Reported as resolution "chain"; `appended` is the number of contracts written.
    """
    taken = now or datetime.now(timezone.utc)
    res = SeriesResult(underlying.upper(), "chain", last_bar=int(taken.timestamp()))
    try:
        quotes = [OptionQuote.from_ticker(t) for t in rest.option_tickers(underlying)]
        if not quotes:
            raise ValueError(f"no options returned for {underlying}")
        path = (Path(data_dir) / "options" / underlying.upper() / taken.strftime("%Y-%m-%d")
                / f"{taken.strftime('%H%M%S')}.csv.gz")
        write_snapshot_csv(path, quotes, taken)
        res.appended = len(quotes)
        ages = [taken.timestamp() - q.ticker_ts / 1e6 for q in quotes if q.ticker_ts]
        res.stale = not ages or median(ages) > OPTIONS_STALE_SECONDS
    except Exception as e:  # isolate per underlying
        res.error = f"{type(e).__name__}: {e}"
        res.stale = True
    return res


def prune_options(data_dir: Path, underlying: str, retention_days: int,
                  today: date | None = None) -> SeriesResult:
    """Delete snapshot day-folders older than `retention_days` (UTC dates).

    Only touches what this program writes: folders named YYYY-MM-DD under
    data/options/{UND}/, and inside them only files named HHMMSS.csv[.gz]. Anything
    else is left alone, and a folder is removed only once it is empty.
    """
    res = SeriesResult(underlying.upper(), "prune")
    if retention_days < 1:
        res.error = f"retention_days must be >= 1, got {retention_days}"
        return res
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=retention_days)
    root = Path(data_dir) / "options" / underlying.upper()
    if not root.is_dir():
        return res
    try:
        for day_dir in sorted(root.iterdir()):
            if day_dir.is_symlink() or not day_dir.is_dir() or not _DATE_DIR.match(day_dir.name):
                continue
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if day >= cutoff:
                continue
            for f in day_dir.iterdir():
                if f.is_file() and not f.is_symlink() and _SNAPSHOT_FILE.match(f.name):
                    f.unlink()
                    res.removed += 1
            if not any(day_dir.iterdir()):
                day_dir.rmdir()
    except OSError as e:
        res.error = f"{type(e).__name__}: {e}"
    return res


def load_options_config(path: Path) -> tuple[list[str], int]:
    """[options] underlyings=[...] retention_days=<optional, default 90>"""
    opts = tomllib.loads(path.read_text()).get("options", {})
    return list(opts.get("underlyings", [])), int(opts.get("retention_days", DEFAULT_OPTIONS_RETENTION_DAYS))


def load_watchlist(path: Path, allow_empty: bool = False) -> list[tuple[str, str, int | None]]:
    """[[series]] symbols=[...] resolutions=[...] initial_days=<optional>"""
    cfg = tomllib.loads(path.read_text())
    out = []
    for block in cfg.get("series", []):
        for sym in block["symbols"]:
            for r in block["resolutions"]:
                if r not in RESOLUTION_SECONDS:
                    raise ValueError(f"{path}: unknown resolution {r!r}")
                out.append((sym, r, block.get("initial_days")))
    if not out and not allow_empty:
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
        underlyings, retention = load_options_config(watchlist)
        series = load_watchlist(watchlist, allow_empty=bool(underlyings))
        results = [sync_series(rest, data_dir, s, r, d) for s, r, d in series]
        results += [snapshot_options(rest, data_dir, u) for u in underlyings]
        results += [prune_options(data_dir, u, retention) for u in underlyings]
        return results
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
