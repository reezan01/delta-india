"""Options chain snapshot with greeks.

Units, verified 2026-10-14 by reproducing Delta's numbers with Black-Scholes (r=0,
spot = greeks.spot, expiry 12:00 UTC) to 4 significant figures on ATM BTC options:
  - prices (mark, bid, ask): USD per 1 unit of underlying (1 BTC), NOT per contract
  - one contract = contract_value units (BTC options: 0.001 BTC)
  - IV: fraction (0.32 = 32%)
  - delta: per 1 underlying; gamma: per $1 move
  - vega: USD per 1 vol POINT (1%)
  - theta: USD per CALENDAR day (365-day year)
  - oi: in underlying units (BTC); oi_contracts: in contracts
Symbols: {C|P}-{UNDERLYING}-{STRIKE}-{DDMMYY}, settling 12:00 UTC.

Deep in-the-money options carry junk bid/ask IV (e.g. 0.000005) and a mark IV pinned
near the venue floor; those IVs are reported as None, not as a real number.
"""

from __future__ import annotations

import csv
import gzip
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path

MIN_REAL_IV = 0.01  # below this an IV quote is a placeholder, not a market


def _f(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _iv(x) -> float | None:
    v = _f(x)
    return v if v is not None and v >= MIN_REAL_IV else None


def parse_symbol(symbol: str) -> tuple[str, str, float, date]:
    """'C-BTC-81800-171026' -> ('C', 'BTC', 81800.0, date(2026, 10, 17))"""
    kind, und, strike, exp = symbol.split("-")
    if kind not in ("C", "P"):
        raise ValueError(f"not an option symbol: {symbol}")
    return kind, und, float(strike), datetime.strptime(exp, "%d%m%y").date()


@dataclass
class OptionQuote:
    symbol: str
    kind: str          # "C" / "P"
    underlying: str
    expiry: date
    strike: float
    spot: float | None
    mark: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    mark_iv: float | None
    bid_iv: float | None
    ask_iv: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    rho: float | None
    oi: float | None           # underlying units
    volume: float | None
    ticker_ts: int | None      # microseconds

    @classmethod
    def from_ticker(cls, t: dict) -> "OptionQuote":
        kind, und, strike, exp = parse_symbol(t["symbol"])
        g = t.get("greeks") or {}
        q = t.get("quotes") or {}
        delta = _f(g.get("delta"))
        # Mark IV is only meaningful where the option still has time value.
        mark_iv = _iv(q.get("mark_iv")) if delta is None or abs(delta) < 0.98 else None
        return cls(
            symbol=t["symbol"], kind=kind, underlying=und, expiry=exp, strike=strike,
            spot=_f(g.get("spot")) or _f(t.get("spot_price")),
            mark=_f(t.get("mark_price")), bid=_f(q.get("best_bid")), ask=_f(q.get("best_ask")),
            bid_size=_f(q.get("bid_size")), ask_size=_f(q.get("ask_size")),
            mark_iv=mark_iv, bid_iv=_iv(q.get("bid_iv")), ask_iv=_iv(q.get("ask_iv")),
            delta=delta, gamma=_f(g.get("gamma")), vega=_f(g.get("vega")),
            theta=_f(g.get("theta")), rho=_f(g.get("rho")),
            oi=_f(t.get("oi")), volume=_f(t.get("volume")), ticker_ts=t.get("timestamp"),
        )


@dataclass
class StrikeRow:
    strike: float
    call: OptionQuote | None
    put: OptionQuote | None


@dataclass
class ExpirySummary:
    expiry: date
    days: float
    spot: float | None
    atm_strike: float | None
    atm_iv: float | None          # mean of ATM call/put mark IV
    rr25: float | None            # call IV at ~+25d minus put IV at ~-25d (nearest listed strikes)
    put_call_oi: float | None
    max_pain: float | None
    call_oi: float
    put_oi: float
    strikes: int


def build_chain(quotes: list[OptionQuote]) -> dict[date, list[StrikeRow]]:
    by_exp: dict[date, dict[float, StrikeRow]] = {}
    for q in quotes:
        row = by_exp.setdefault(q.expiry, {}).setdefault(q.strike, StrikeRow(q.strike, None, None))
        if q.kind == "C":
            row.call = q
        else:
            row.put = q
    return {e: [rows[k] for k in sorted(rows)] for e, rows in sorted(by_exp.items())}


def max_pain(rows: list[StrikeRow]) -> float | None:
    """Settlement price that minimises total intrinsic value paid to option holders (by OI)."""
    if not rows:
        return None
    calls = [(r.strike, r.call.oi or 0.0) for r in rows if r.call]
    puts = [(r.strike, r.put.oi or 0.0) for r in rows if r.put]
    if not any(oi for _, oi in calls + puts):
        return None

    def payout(s: float) -> float:
        return (sum(oi * max(s - k, 0.0) for k, oi in calls)
                + sum(oi * max(k - s, 0.0) for k, oi in puts))

    return min((r.strike for r in rows), key=payout)


def _nearest_delta(quotes: list[OptionQuote], target: float) -> OptionQuote | None:
    cands = [q for q in quotes if q.delta is not None and q.mark_iv is not None]
    return min(cands, key=lambda q: abs(q.delta - target)) if cands else None


def summarize(expiry: date, rows: list[StrikeRow], now: datetime | None = None) -> ExpirySummary:
    now = now or datetime.now(timezone.utc)
    settle = datetime(expiry.year, expiry.month, expiry.day, 12, tzinfo=timezone.utc)
    spots = [q.spot for r in rows for q in (r.call, r.put) if q and q.spot]
    spot = spots[0] if spots else None
    atm = min(rows, key=lambda r: abs(r.strike - spot)) if rows and spot else None
    atm_ivs = [q.mark_iv for q in ((atm.call, atm.put) if atm else ()) if q and q.mark_iv is not None]
    call_oi = sum((r.call.oi or 0.0) for r in rows if r.call)
    put_oi = sum((r.put.oi or 0.0) for r in rows if r.put)
    c25 = _nearest_delta([r.call for r in rows if r.call], 0.25)
    p25 = _nearest_delta([r.put for r in rows if r.put], -0.25)
    return ExpirySummary(
        expiry=expiry, days=(settle - now).total_seconds() / 86400, spot=spot,
        atm_strike=atm.strike if atm else None,
        atm_iv=sum(atm_ivs) / len(atm_ivs) if atm_ivs else None,
        rr25=(c25.mark_iv - p25.mark_iv) if c25 and p25 else None,
        put_call_oi=put_oi / call_oi if call_oi else None,
        max_pain=max_pain(rows), call_oi=call_oi, put_oi=put_oi, strikes=len(rows),
    )


def write_snapshot_csv(path: Path, quotes: list[OptionQuote], taken_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["taken_at"] + [f.name for f in fields(OptionQuote)]
    tmp = path.with_name(path.name + ".tmp")
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(tmp, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for q in sorted(quotes, key=lambda q: (q.expiry, q.strike, q.kind)):
            d = asdict(q)
            w.writerow([taken_at.isoformat()] + [d[c] for c in cols[1:]])
    tmp.replace(path)  # never leave a half-written snapshot
