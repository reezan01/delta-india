import hashlib
import hmac
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from delta.auth import build_query, sign
from delta.config import Settings
from delta.rest import DeltaAPIError, DeltaRest
from delta.store import append_candles, count_gaps, last_time

BASE = "https://api.india.delta.exchange"


def settings(key="k", secret="s3cret", tmp=Path(".")):
    return Settings(env="prod", rest_url=BASE, ws_url="wss://x", api_key=key, api_secret=secret,
                    data_dir=tmp)


def rest(**kw):
    s = settings(**kw)
    return DeltaRest(s, client=httpx.Client(base_url=BASE), max_retries=2)


# ── signing ───────────────────────────────────────────────────────────────
def test_sign_matches_reference_formula():
    expected = hmac.new(b"s3cret", b"GET1700000000/v2/positions/margined?contract_types=perpetual_futures",
                        hashlib.sha256).hexdigest()
    assert sign("s3cret", "get", "1700000000", "/v2/positions/margined",
                "?contract_types=perpetual_futures") == expected


def test_build_query_drops_none_and_is_empty_without_params():
    assert build_query(None) == ""
    assert build_query({"a": None}) == ""
    assert build_query({"a": 1, "b": None, "c": "x y"}) == "?a=1&c=x+y"


@respx.mock
def test_signed_request_signs_exactly_what_is_sent():
    """The server recomputes the signature from the wire path+query, so ours must too."""
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.raw_path.decode()
        seen["headers"] = request.headers
        return httpx.Response(200, json={"success": True, "result": []})

    respx.get(url__startswith=BASE + "/v2/positions/margined").mock(side_effect=handler)
    rest().positions_margined(contract_types="perpetual_futures,call_options")

    h = seen["headers"]
    path, _, q = seen["path"].partition("?")
    wire = path + ("?" + q if q else "")
    expected = hmac.new(b"s3cret", ("GET" + h["timestamp"] + wire).encode(), hashlib.sha256).hexdigest()
    assert q, "test must exercise a query string, or it proves nothing about query signing"
    assert h["signature"] == expected
    assert h["api-key"] == "k"


def test_private_call_without_keys_fails_before_network():
    with pytest.raises(DeltaAPIError, match="not set"):
        rest(key=None, secret=None).wallet_balances()


@respx.mock
def test_429_waits_and_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr("delta.rest.time.sleep", sleeps.append)
    route = respx.get(BASE + "/v2/tickers/BTCUSD").mock(side_effect=[
        httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "1500"}, json={}),
        httpx.Response(200, json={"success": True, "result": {"symbol": "BTCUSD"}}),
    ])
    assert rest().ticker("BTCUSD")["symbol"] == "BTCUSD"
    assert route.call_count == 2 and sleeps == [1.5]


@respx.mock
def test_error_envelope_raises():
    respx.get(BASE + "/v2/tickers/NOPE").mock(return_value=httpx.Response(
        404, json={"success": False, "error": {"code": "not_found"}}))
    with pytest.raises(DeltaAPIError, match="not_found"):
        rest().ticker("NOPE")


# ── candles ───────────────────────────────────────────────────────────────
def fake_candle_server(available: list[int], requests: list):
    """Serves candles whose time is in `available`, newest first, max 2000 — like Delta."""
    def handler(request: httpx.Request):
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        s, e = int(q["start"]), int(q["end"])
        requests.append((s, e))
        rows = [{"time": t, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 5}
                for t in available if s <= t <= e]
        return httpx.Response(200, json={"success": True, "result": sorted(rows, key=lambda r: -r["time"])[:2000]})
    return handler


@respx.mock
def test_candles_chunk_without_gaps_or_overlap_and_drop_forming_bar():
    step = 60
    start = 1_700_000_000 - (1_700_000_000 % step)
    n = 4500  # forces 3 requests at 2000/request
    available = [start + i * step for i in range(n)]
    reqs: list = []
    respx.get(url__startswith=BASE + "/v2/history/candles").mock(side_effect=fake_candle_server(available, reqs))

    now = available[-1] + 30  # last bar is still forming
    rows = rest().candles("BTCUSD", "1m", start, available[-1], now=now)

    assert len(reqs) == 3
    for (s1, e1), (s2, _) in zip(reqs, reqs[1:]):
        assert s2 == e1 + step, "chunks must be contiguous"
        assert (e1 - s1) // step + 1 <= 2000
    times = [r["time"] for r in rows]
    assert times == available[:-1], "ascending, complete, forming bar dropped"


# ── store ─────────────────────────────────────────────────────────────────
def bars(ts):
    return [{"time": t, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 3} for t in ts]


def test_append_is_incremental_and_never_duplicates(tmp_path):
    p = tmp_path / "BTCUSD" / "1m.csv"
    assert append_candles(p, bars([60, 120, 180])) == 3
    assert last_time(p) == 180
    assert append_candles(p, bars([120, 180, 240])) == 1  # overlap re-fetched
    lines = p.read_text().strip().splitlines()
    assert lines[0].startswith("time") and len(lines) == 5


def test_append_rejects_unsorted(tmp_path):
    with pytest.raises(ValueError):
        append_candles(tmp_path / "x.csv", bars([120, 60]))


def test_count_gaps():
    assert count_gaps([0, 60, 240, 300], 60) == 2
