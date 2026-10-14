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
