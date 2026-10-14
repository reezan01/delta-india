import csv
from datetime import date, datetime, timezone

import pytest

from delta.options import (OptionQuote, build_chain, max_pain, parse_symbol, summarize,
                           write_snapshot_csv)


def tick(symbol, oi="1", delta="0.5", mark_iv="0.33", bid_iv="0.32", ask_iv="0.34", spot="100"):
    """Shape copied from a live /v2/tickers option row (2026-10-14)."""
    return {"symbol": symbol, "mark_price": "5", "spot_price": spot, "oi": oi, "volume": 1.0,
            "timestamp": 1791993118692378,
            "greeks": {"delta": delta, "gamma": "0.01", "vega": "1.0", "theta": "-2.0", "rho": "0.1",
                       "spot": spot},
            "quotes": {"best_bid": "4", "best_ask": "6", "bid_size": "10", "ask_size": "12",
                       "mark_iv": mark_iv, "bid_iv": bid_iv, "ask_iv": ask_iv}}


def q(symbol, **kw):
    return OptionQuote.from_ticker(tick(symbol, **kw))


def test_parse_symbol():
    assert parse_symbol("C-BTC-81800-171026") == ("C", "BTC", 81800.0, date(2026, 10, 17))
    with pytest.raises(ValueError):
        parse_symbol("BTCUSD-PERP-1-171026")


def test_placeholder_ivs_become_none_not_numbers():
    """Live deep-ITM put: bid/ask IV 0.000005, delta -1, mark IV at the venue floor."""
    deep = q("P-BTC-96000-301026", delta="-1.00000000", mark_iv="0.09583892",
             bid_iv="0.000005", ask_iv="0.000005")
    assert deep.bid_iv is None and deep.ask_iv is None and deep.mark_iv is None
    atm = q("P-BTC-81800-171026", delta="-0.5058", mark_iv="0.324")
    assert atm.mark_iv == pytest.approx(0.324) and atm.bid_iv == pytest.approx(0.32)


def test_chain_pairs_calls_and_puts_by_expiry_and_strike():
    quotes = [q("C-BTC-110-171026"), q("P-BTC-100-171026"), q("C-BTC-100-171026"),
              q("P-BTC-100-231026")]
    chain = build_chain(quotes)
    assert list(chain) == [date(2026, 10, 17), date(2026, 10, 23)]
    rows = chain[date(2026, 10, 17)]
    assert [r.strike for r in rows] == [100, 110]
    assert rows[0].call.symbol == "C-BTC-100-171026" and rows[0].put.symbol == "P-BTC-100-171026"
    assert rows[1].put is None


def test_max_pain_hand_computed():
    # Payout to holders at each settle price:
    #   s=90 : puts 5*(100-90)+1*(110-90)=70           calls 0                -> 70
    #   s=100: puts 1*(110-100)=10                     calls 2*(100-90)=20    -> 30
    #   s=110: puts 0                                  calls 2*20 + 4*10=80   -> 80
    quotes = [q("C-BTC-90-171026", oi="2"), q("C-BTC-100-171026", oi="4"), q("C-BTC-110-171026", oi="0"),
              q("P-BTC-90-171026", oi="0"), q("P-BTC-100-171026", oi="5"), q("P-BTC-110-171026", oi="1")]
    assert max_pain(build_chain(quotes)[date(2026, 10, 17)]) == 100


def test_summary_atm_pcr_and_risk_reversal():
    quotes = [
        q("C-BTC-95-171026", delta="0.70", mark_iv="0.30", oi="1"),
        q("C-BTC-100-171026", delta="0.50", mark_iv="0.32", oi="2"),
        q("C-BTC-110-171026", delta="0.26", mark_iv="0.35", oi="1"),
        q("P-BTC-80-171026", delta="-0.05", mark_iv="0.55", oi="0"),  # closest to +0.25 if sign is wrong
        q("P-BTC-90-171026", delta="-0.24", mark_iv="0.40", oi="3"),
        q("P-BTC-100-171026", delta="-0.50", mark_iv="0.34", oi="3"),
    ]
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    s = summarize(date(2026, 10, 17), build_chain(quotes)[date(2026, 10, 17)], now)
    assert s.atm_strike == 100 and s.atm_iv == pytest.approx(0.33)
    assert s.put_call_oi == pytest.approx(6 / 4)
    assert s.rr25 == pytest.approx(0.35 - 0.40)  # nearest-delta strikes: 110C and 90P
    assert s.days == pytest.approx(1.0)


def test_snapshot_csv_roundtrip(tmp_path):
    p = tmp_path / "BTC" / "snap.csv"
    write_snapshot_csv(p, [q("P-BTC-100-171026"), q("C-BTC-100-171026")],
                       datetime(2026, 10, 14, tzinfo=timezone.utc))
    rows = list(csv.DictReader(open(p)))
    assert [r["kind"] for r in rows] == ["C", "P"] and rows[0]["delta"] == "0.5"
    assert not list(tmp_path.rglob("*.tmp"))
