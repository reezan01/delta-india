import fcntl

import pytest

from delta.store import append_candles, candle_path, last_time
from delta.update import load_watchlist, run_update, sync_series

NOW = 1_800_000_000 - (1_800_000_000 % 3600)  # on an hour boundary


class FakeRest:
    """Serves hourly bars up to `latest`; raises for symbols in `broken`."""

    def __init__(self, latest, broken=()):
        self.latest, self.broken, self.calls = latest, set(broken), []

    def candles(self, symbol, resolution, start, end, now=None):
        self.calls.append((symbol, resolution, start, end))
        if symbol in self.broken:
            raise ConnectionError("boom")
        step = 3600
        first = start + (-start % step)
        return [{"time": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                for t in range(first, min(end, self.latest) + 1, step) if t + step <= end]


def test_first_run_backfills_then_appends_only_new(tmp_path):
    rest = FakeRest(latest=NOW - 3600)
    r1 = sync_series(rest, tmp_path, "BTCUSD", "1h", initial_days=2, now=NOW)
    assert r1.appended == 48 and not r1.stale and r1.error is None
    rest.latest = NOW  # an hour passes; one more bar closes
    r2 =sync_series(rest, tmp_path, "BTCUSD", "1h", now=NOW + 3600)
    assert r2.appended == 1 and rest.calls[-1][2] == NOW  # resumes at last+step
    assert last_time(candle_path(tmp_path, "BTCUSD", "1h")) == NOW


def test_series_that_stops_advancing_is_flagged_stale(tmp_path):
    """API answers successfully but with nothing new for hours: must not read as 'ok'."""
    rest = FakeRest(latest=NOW - 10 * 3600)
    r = sync_series(rest, tmp_path, "BTCUSD", "1h", initial_days=2, now=NOW)
    assert r.error is None and r.appended > 0
    assert r.stale


def test_one_broken_symbol_does_not_stop_the_others(tmp_path):
    wl = tmp_path / "w.toml"
    wl.write_text('[[series]]\nsymbols=["BAD","BTCUSD"]\nresolutions=["1h"]\ninitial_days=1\n')
    rest = FakeRest(latest=10**12, broken={"BAD"})
    import delta.update as u
    orig = u.time.time
    u.time.time = lambda: NOW
    try:
        res = {r.symbol: r for r in run_update(rest, tmp_path / "data", wl)}
    finally:
        u.time.time = orig
    assert res["BAD"].error and res["BAD"].stale
    assert res["BTCUSD"].error is None and res["BTCUSD"].appended == 24


def test_overlapping_run_is_refused(tmp_path):
    wl = tmp_path / "w.toml"
    wl.write_text('[[series]]\nsymbols=["X"]\nresolutions=["1h"]\n')
    data = tmp_path / "data"
    data.mkdir()
    with open(data / ".update.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="still running"):
            run_update(FakeRest(latest=NOW), data, wl)


def test_watchlist_rejects_unknown_resolution(tmp_path):
    wl = tmp_path / "w.toml"
    wl.write_text('[[series]]\nsymbols=["X"]\nresolutions=["7m"]\n')
    with pytest.raises(ValueError, match="7m"):
        load_watchlist(wl)


# ── options snapshots ─────────────────────────────────────────────────────
import csv
import gzip
from datetime import datetime, timezone

from delta.update import snapshot_options

TAKEN = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


def opt(symbol, age_s):
    return {"symbol": symbol, "mark_price": "5", "oi": "1",
            "timestamp": int((TAKEN.timestamp() - age_s) * 1e6),
            "greeks": {"delta": "0.5", "spot": "100"}, "quotes": {"mark_iv": "0.3"}}


class FakeOptRest:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail

    def option_tickers(self, underlying):
        if self.fail:
            raise ConnectionError("down")
        return self.rows


def test_snapshot_writes_readable_gzip(tmp_path):
    rest = FakeOptRest([opt("C-BTC-100-171026", 2), opt("P-BTC-100-171026", 2)])
    r = snapshot_options(rest, tmp_path, "btc", now=TAKEN)
    assert r.error is None and not r.stale and r.appended == 2 and r.resolution == "chain"
    path = tmp_path / "options" / "BTC" / "2026-10-14" / "160000.csv.gz"
    rows = list(csv.DictReader(gzip.open(path, "rt")))
    assert [x["symbol"] for x in rows] == ["C-BTC-100-171026", "P-BTC-100-171026"]


def test_snapshot_of_frozen_feed_is_stale(tmp_path):
    rest = FakeOptRest([opt("C-BTC-100-171026", 900), opt("P-BTC-100-171026", 900),
                        opt("C-BTC-110-171026", 1)])
    r = snapshot_options(rest, tmp_path, "BTC", now=TAKEN)
    assert r.error is None and r.stale  # saved, but flagged: median ticker is 15 min old


def test_snapshot_empty_or_failing_is_an_error(tmp_path):
    assert snapshot_options(FakeOptRest([]), tmp_path, "BTC", now=TAKEN).error
    assert snapshot_options(FakeOptRest(fail=True), tmp_path, "BTC", now=TAKEN).error


def test_options_only_watchlist_runs_and_includes_chain(tmp_path):
    wl = tmp_path / "w.toml"
    wl.write_text('[options]\nunderlyings=["BTC"]\n')
    rest = FakeOptRest([opt("C-BTC-100-171026", 1)])
    res = run_update(rest, tmp_path / "data", wl)
    assert [(r.symbol, r.resolution, r.appended) for r in res] == [("BTC", "chain", 1)]
