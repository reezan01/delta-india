"""Command line entry point: `delta <command>`."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from delta.config import load_settings
from delta.rest import RESOLUTION_SECONDS, DeltaAPIError, DeltaRest
from delta.store import append_candles, candle_path, count_gaps, last_time
from delta.options import OptionQuote, build_chain, summarize, write_snapshot_csv
from delta.update import run_update
from delta.ws import stream


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def cmd_products(rest: DeltaRest, args) -> None:
    rows = rest.products(contract_types=args.type)
    for p in sorted(rows, key=lambda p: (p.get("contract_type", ""), p["symbol"])):
        print(f'{p["id"]:>7}  {p["symbol"]:<28} {p.get("contract_type", ""):<20} '
              f'{(p.get("settling_asset") or {}).get("symbol", "")}')
    print(f"\n{len(rows)} products", file=sys.stderr)


def cmd_ticker(rest: DeltaRest, args) -> None:
    t = rest.ticker(args.symbol)
    q = t.get("quotes") or {}
    _print({k: t.get(k) for k in ("symbol", "contract_type", "mark_price", "spot_price", "close",
                                  "high", "low", "volume", "oi", "funding_rate")}
           | {"best_bid": q.get("best_bid"), "best_ask": q.get("best_ask")})


def cmd_candles(rest: DeltaRest, args, data_dir: Path) -> None:
    step = RESOLUTION_SECONDS[args.resolution]
    path = candle_path(data_dir, args.symbol, args.resolution)
    end = int(time.time()) if not args.end else _parse_date(args.end)
    stored_last = last_time(path)
    if stored_last is not None and not args.start:
        start = stored_last + step  # incremental update
    elif args.start:
        start = _parse_date(args.start)
    else:
        start = end - args.days * 86400

    if start > end:
        print(f"{path}: already up to date (last bar {stored_last})")
        return
    rows = rest.candles(args.symbol, args.resolution, start, end)
    written = append_candles(path, rows)

    with open(path) as f:
        times = [int(r["time"]) for r in csv.DictReader(f)]
    first = datetime.fromtimestamp(times[0], tz=timezone.utc) if times else None
    last = datetime.fromtimestamp(times[-1], tz=timezone.utc) if times else None
    print(f"{path}\n  fetched {len(rows)}, appended {written}, total {len(times)} bars"
          f"\n  range {first} -> {last}\n  gaps (missing bars) {count_gaps(times, step)}")


def cmd_update(rest: DeltaRest, args, data_dir: Path) -> int:
    started = time.time()
    try:
        results = run_update(rest, data_dir, Path(args.watchlist))
    except RuntimeError as e:  # overlapping run: not a failure, the other run is doing the work
        print(f"skip: {e}")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bad = 0
    for r in results:
        last = datetime.fromtimestamp(r.last_bar, tz=timezone.utc).strftime("%m-%d %H:%M") if r.last_bar else "-"
        status = "ERROR" if r.error else "STALE" if r.stale else "ok"
        bad += status != "ok"
        if status == "ok" and args.quiet_ok:
            continue
        print(f"{stamp}  {status:<5} {r.symbol:<14} {r.resolution:<4} +{r.appended:<6} last {last}"
              + (f"  {r.error}" if r.error else ""))
    print(f"{stamp}  done: {len(results)} series, {bad} not ok, {time.time() - started:.1f}s")
    return 1 if bad else 0


def _n(x, fmt: str, width: int) -> str:
    return (format(x, fmt) if x is not None else "-").rjust(width)


def cmd_chain(rest: DeltaRest, args, data_dir: Path) -> int:
    taken = datetime.now(timezone.utc)
    quotes = [OptionQuote.from_ticker(t) for t in rest.option_tickers(args.underlying)]
    if not quotes:
        print(f"no options listed for {args.underlying}", file=sys.stderr)
        return 1
    chain = build_chain(quotes)
    sums = {e: summarize(e, rows, taken) for e, rows in chain.items()}
    expiries = list(chain)

    stale = [q for q in quotes if q.ticker_ts and taken.timestamp() - q.ticker_ts / 1e6 > 300]
    if args.save:
        path = (data_dir / "options" / args.underlying.upper() / taken.strftime("%Y-%m-%d")
                / f"{taken.strftime('%H%M%S')}.csv")
        write_snapshot_csv(path, quotes, taken)
        print(f"saved {len(quotes)} options -> {path}")
        if args.quiet:
            return 0

    spot = next((s.spot for s in sums.values() if s.spot), None)
    print(f"\n{args.underlying.upper()} options  spot {spot:,.1f}  {taken:%Y-%m-%d %H:%M:%S} UTC  "
          f"{len(quotes)} contracts, {len(expiries)} expiries"
          + (f"  ⚠ {len(stale)} tickers >5min old" if stale else ""))
    print(f"\n  {'#':>2} {'expiry':<10} {'days':>6} {'strikes':>7} {'ATM':>8} {'ATM IV':>7} "
          f"{'25d RR':>7} {'P/C OI':>7} {'max pain':>9} {'call OI':>9} {'put OI':>9}")
    for i, (e, s) in enumerate(sums.items()):
        print(f"  {i:>2} {e.isoformat():<10} {s.days:6.2f} {s.strikes:7d} {_n(s.atm_strike, ',.0f', 8)} "
              f"{_n(s.atm_iv and s.atm_iv * 100, '.1f', 6)}% {_n(s.rr25 and s.rr25 * 100, '+.1f', 6)}% "
              f"{_n(s.put_call_oi, '.2f', 7)} {_n(s.max_pain, ',.0f', 9)} {s.call_oi:9.2f} {s.put_oi:9.2f}")

    # Pick the expiry to print: index, date, or the nearest by default.
    if args.expiry is None:
        exp = expiries[0]
    elif args.expiry.isdigit() and int(args.expiry) < len(expiries):
        exp = expiries[int(args.expiry)]
    else:
        try:
            exp = datetime.strptime(args.expiry, "%Y-%m-%d").date()
        except ValueError:
            print(f"--expiry: use an index 0-{len(expiries) - 1} or YYYY-MM-DD", file=sys.stderr)
            return 2
        if exp not in chain:
            print(f"no {args.underlying} options expire {exp}", file=sys.stderr)
            return 2

    rows, s = chain[exp], sums[exp]
    if s.atm_strike is not None and args.strikes:
        i = next(k for k, r in enumerate(rows) if r.strike == s.atm_strike)
        rows = rows[max(i - args.strikes, 0): i + args.strikes + 1]

    wide = args.wide
    side = ["OI", "IV%", "delta"] + (["gamma", "vega", "theta"] if wide else []) + ["bid", "ask", "mark"]
    w = [7, 6, 7] + ([9, 7, 8] if wide else []) + [8, 8, 8]

    def cells(q, order):
        if q is None:
            vals = [None] * len(order)
        else:
            m = {"OI": (q.oi, ".2f"), "IV%": (q.mark_iv and q.mark_iv * 100, ".1f"),
                 "delta": (q.delta, "+.3f"), "gamma": (q.gamma, ".2e"), "vega": (q.vega, ".2f"),
                 "theta": (q.theta, ".1f"), "bid": (q.bid, ",.1f"), "ask": (q.ask, ",.1f"),
                 "mark": (q.mark, ",.1f")}
            return " ".join(_n(m[c][0], m[c][1], w[side.index(c)]) for c in order)
        return " ".join("-".rjust(w[side.index(c)]) for c in order)

    # Mirror the call side around the strike, but keep bid before ask.
    put_order = ["mark", "bid", "ask"] + list(reversed(side[:-3]))
    print(f"\n{exp}  ({s.days:.2f} days, settles 12:00 UTC)  ATM {s.atm_strike:,.0f}  "
          f"prices USD per 1 {args.underlying.upper()}; vega per vol-pt; theta per day")
    half = sum(w) + len(w) - 1
    print("CALLS".center(half) + " │" + " " * 10 + "│ " + "PUTS".center(half))
    print(" ".join(c.rjust(w[side.index(c)]) for c in side) + " │" + "STRIKE".center(10) + "│ "
          + " ".join(c.rjust(w[side.index(c)]) for c in put_order))
    for r in rows:
        mark = "◀" if r.strike == s.atm_strike else " "
        print(cells(r.call, side) + " │" + f"{r.strike:>9,.0f}{mark}" + "│ " + cells(r.put, put_order))
    return 0


def cmd_stream(settings, args) -> None:
    """Print to stdout, and with --out append NDJSON per channel per UTC day."""
    out_dir = Path(args.out) if args.out else None

    async def run():
        n = 0
        async for msg in stream(settings.ws_url, args.channels.split(","), args.symbols.split(",")):
            n += 1
            if out_dir:
                day = datetime.fromtimestamp(msg["_received_at"], tz=timezone.utc).strftime("%Y-%m-%d")
                p = out_dir / msg.get("type", "unknown").replace("/", "_") / f"{day}.ndjson"
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a") as f:
                    f.write(json.dumps(msg) + "\n")
            if not args.quiet:
                print(json.dumps(msg)[:300])
            if args.max and n >= args.max:
                return

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="delta", description="Read-only Delta Exchange India tools")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("products", help="list live products")
    p.add_argument("--type", help="contract_types filter, e.g. perpetual_futures,call_options")

    p = sub.add_parser("ticker", help="one ticker snapshot")
    p.add_argument("symbol")

    p = sub.add_parser("candles", help="fetch/update stored OHLCV candles")
    p.add_argument("symbol", help="e.g. BTCUSD, or MARK:BTCUSD / FUNDING:BTCUSD / OI:BTCUSD")
    p.add_argument("resolution", choices=list(RESOLUTION_SECONDS))
    p.add_argument("--days", type=int, default=30, help="history for a new file (default 30)")
    p.add_argument("--start", help="YYYY-MM-DD (forces a range fetch)")
    p.add_argument("--end", help="YYYY-MM-DD (default now)")

    p = sub.add_parser("update", help="bring every watchlist series up to date (scheduled job)")
    p.add_argument("--watchlist", default=str(Path(__file__).resolve().parents[2] / "watchlist.toml"))
    p.add_argument("--quiet-ok", action="store_true", help="print only problem series + the summary")

    p = sub.add_parser("chain", help="options chain snapshot with greeks")
    p.add_argument("underlying", help="BTC, ETH, ...")
    p.add_argument("--expiry", help="index from the expiry table, or YYYY-MM-DD (default: nearest)")
    p.add_argument("--strikes", type=int, default=8, help="strikes each side of ATM (0 = all)")
    p.add_argument("--wide", action="store_true", help="also show gamma, vega, theta")
    p.add_argument("--save", action="store_true", help="write every contract to data/options/...csv")
    p.add_argument("--quiet", action="store_true", help="with --save: don't print the table")

    p = sub.add_parser("stream", help="live WebSocket feed")
    p.add_argument("symbols", help="comma-separated, e.g. BTCUSD,ETHUSD")
    p.add_argument("--channels", default="v2/ticker", help="e.g. v2/ticker,candlestick_1m,all_trades")
    p.add_argument("--out", help="directory to append NDJSON captures to")
    p.add_argument("--max", type=int, help="stop after N messages")
    p.add_argument("--quiet", action="store_true")

    sub.add_parser("balances", help="wallet balances (needs API key)")
    sub.add_parser("positions", help="open margined positions (needs API key)")
    sub.add_parser("env", help="show resolved settings (secrets masked)")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if args.cmd == "env":
        _print({"env": settings.env, "rest_url": settings.rest_url, "ws_url": settings.ws_url,
                "api_key": (settings.api_key[:4] + "…") if settings.api_key else None,
                "api_secret": "set" if settings.api_secret else None,
                "data_dir": str(settings.data_dir.resolve())})
        return 0
    if args.cmd == "stream":
        cmd_stream(settings, args)
        return 0

    try:
        with DeltaRest(settings) as rest:
            if args.cmd == "products":
                cmd_products(rest, args)
            elif args.cmd == "ticker":
                cmd_ticker(rest, args)
            elif args.cmd == "candles":
                cmd_candles(rest, args, settings.data_dir)
            elif args.cmd == "chain":
                return cmd_chain(rest, args, settings.data_dir)
            elif args.cmd == "update":
                return cmd_update(rest, args, settings.data_dir)
            elif args.cmd == "balances":
                _print(rest.wallet_balances())
            elif args.cmd == "positions":
                _print(rest.positions_margined())
    except DeltaAPIError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
