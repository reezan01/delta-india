# Delta Exchange India — read-only client

REST client, WebSocket feed and incremental candle store for **Delta Exchange India**.
It is read-only by design: there is no code path that places, edits or cancels an order.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env     # keys optional; public data needs none
```

## Commands

```bash
.venv/bin/delta env                                  # resolved settings, secrets masked
.venv/bin/delta products --type perpetual_futures    # contract list
.venv/bin/delta ticker BTCUSD                        # snapshot: mark, spot, bid/ask, OI, funding
.venv/bin/delta candles BTCUSD 1h --days 365         # first run: fetch a year
.venv/bin/delta candles BTCUSD 1h                    # later runs: append only new closed bars
.venv/bin/delta candles MARK:BTCUSD 5m --days 30     # also FUNDING:, OI: series
.venv/bin/delta stream BTCUSD,ETHUSD --channels v2/ticker,candlestick_1m --out data/stream
.venv/bin/delta balances                             # needs API key
.venv/bin/delta positions                            # needs API key
```

Candles go to `data/candles/{SYMBOL}/{resolution}.csv`: ascending, de-duplicated, closed bars only.

## Options chain

```bash
.venv/bin/delta chain BTC                       # expiry table + nearest expiry, 8 strikes each side of ATM
.venv/bin/delta chain ETH --expiry 4 --wide     # pick by table index (or YYYY-MM-DD); add gamma/vega/theta
.venv/bin/delta chain BTC --strikes 0           # every strike
.venv/bin/delta chain BTC --save --quiet        # all contracts -> data/options/BTC/{date}/{HHMMSS}.csv
```

The expiry table shows ATM IV, the 25-delta risk reversal (call IV minus put IV at the
listed strikes nearest ±0.25 delta, not interpolated), put/call OI ratio, max pain, and OI by side.

**Units.** These were verified by reproducing Delta's greeks with Black-Scholes (r=0, expiry 12:00 UTC):

| Field | Unit |
|---|---|
| mark / bid / ask | USD per **1 BTC** of underlying, not per contract (1 contract = 0.001 BTC) |
| IV | fraction from the API, shown as % |
| delta / gamma | per 1 underlying / per $1 move |
| vega | USD per 1 vol **point** |
| theta | USD per **calendar day** |
| OI | underlying units (BTC / ETH) |

Deep in-the-money options carry placeholder IVs from Delta (bid/ask 0.000005, mark at the
venue floor). They are shown as `-` rather than as a number.

## Scheduled updates

`delta update` brings every series in [watchlist.toml](watchlist.toml) up to date. A new
series is backfilled on its first run; after that only new closed bars are appended.
It exits non-zero if any series errors **or is stale** (no closed bar within 4 bar-lengths
of now), so a feed that silently stops never reads as "ok". Overlapping runs are skipped.

```bash
scripts/install-schedule.sh            # LaunchAgent, every 15 min + at login
scripts/install-schedule.sh 300        # custom interval (seconds)
scripts/install-schedule.sh --remove   # uninstall
tail -f data/logs/update.log           # one summary line per run, plus any ERROR/STALE rows
launchctl print gui/$(id -u)/local.delta-india.candles | grep -E "runs|last exit"
```

It runs only while the Mac is awake. After sleep, launchd fires once on wake and the
incremental fetch fills the gap. The 1m series can recover up to 2000 bars (~33h) per request
and pages beyond that, so a long sleep is caught up rather than left as a gap.

To add symbols, edit `watchlist.toml`. Nothing needs reinstalling.

The `[options]` block in the watchlist also saves the **full options chain** for each
underlying on every run, to `data/options/{UND}/{date}/{HHMMSS}.csv.gz`. That's every contract
with greeks and IVs, in the same columns as `delta chain --save`. Read a snapshot with
`pandas.read_csv(path)` or `gzip -dc path`. A snapshot is flagged STALE when its median
ticker is more than 5 min old, so a frozen options feed is reported instead of being saved silently.
Size is about 40 KB (BTC) plus 22 KB (ETH) per run, roughly 6 MB/day.

**Retention:** day folders older than `retention_days` (default 90, set in `[options]`) are
deleted at the end of each run, which caps the store at about 550 MB. Pruning only removes
`YYYY-MM-DD` folders containing files named `HHMMSS.csv[.gz]`. It never follows symlinks,
leaves any other file alone, and keeps a folder that still has other files in it.
A deletion shows up in the log as `prune -N`.

## Facts verified against the live API (2026-10-14)

| | |
|---|---|
| Prod REST / WS | `https://api.india.delta.exchange` / `wss://socket.india.delta.exchange` |
| Testnet REST / WS | `https://cdn-ind.testnet.deltaex.org` / `wss://socket-ind.testnet.deltaex.org` |
| Keys | A prod key comes from delta.exchange and a testnet key from demo.delta.exchange; each works only on its own environment. `api.delta.exchange` is Delta **Global** and rejects India keys. |
| Signing | `HMAC_SHA256(secret, METHOD + ts + /v2/path + ?query + body)` hex digest; `ts` in Unix **seconds**; the signature is valid for ~5s |
| Candles | max 2000 per request, returned **newest first**, `time` in **seconds**. BTCUSD history starts 2023-12-29. |
| WebSocket | all timestamps in **microseconds**; send `enable_heartbeat` and it beats every ~1-2s |
| Rate limit | 20,000 weight per 5-min window; reads cost 3; on 429, wait `X-RATE-LIMIT-RESET` ms |
| Products | 1,205 live: 237 perps, 487 calls, 467 puts, 10 move options, 4 spot |

## Tests

```bash
.venv/bin/pytest -q
```

The suite was mutation-tested: 6 deliberate bugs were introduced and the suite caught all of them. They were an unsigned query, overlapping chunks, a 2001-bar chunk, a kept forming bar, duplicate appends and an ignored rate-limit reset.
