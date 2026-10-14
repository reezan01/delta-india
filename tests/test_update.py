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
