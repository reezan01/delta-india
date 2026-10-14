"""Public WebSocket feed with heartbeat, staleness detection and auto-reconnect.

Verified against wss://socket.india.delta.exchange (2026-10-14):
  -> {"type": "enable_heartbeat"}
  -> {"type": "subscribe", "payload": {"channels": [{"name": "v2/ticker", "symbols": ["BTCUSD"]}]}}
  <- {"type": "subscriptions", ...}           ack
  <- {"type": "heartbeat", "ts_origin": ...}  every ~1-2s once enabled
  <- {"type": "v2/ticker", "symbol": ..., "timestamp": <MICROseconds>, ...}
  <- {"type": "candlestick_1m", "candle_start_time": <MICROseconds>, ...}

All Delta WebSocket timestamps are microseconds, unlike REST candles (seconds).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterable

import websockets

log = logging.getLogger(__name__)

CONTROL_TYPES = {"heartbeat", "subscriptions", "unsubscribed"}


def subscribe_message(channels: Iterable[str], symbols: list[str]) -> str:
    return json.dumps({"type": "subscribe", "payload": {
        "channels": [{"name": ch, "symbols": symbols} for ch in channels]}})


async def stream(ws_url: str, channels: list[str], symbols: list[str], *,
                 stale_after: float = 30.0, max_backoff: float = 30.0,
                 include_control: bool = False) -> AsyncIterator[dict]:
    """Yield data messages forever, reconnecting on drop or silence.

    A connection that stays open but stops delivering (no message, not even a heartbeat,
    for `stale_after` seconds) is treated as dead. An open socket is not proof of a feed.
    """
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(ws_url, open_timeout=15, ping_interval=20,
                                          max_size=2**22) as ws:
                await ws.send(json.dumps({"type": "enable_heartbeat"}))
                await ws.send(subscribe_message(channels, symbols))
                log.info("connected %s channels=%s symbols=%s", ws_url, channels, symbols)
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=stale_after)
                    msg = json.loads(raw)
                    backoff = 1.0  # traffic is flowing; reset only after real data arrives
                    msg["_received_at"] = time.time()
                    if msg.get("type") == "error":
                        log.error("server error: %s", msg)
                    if include_control or msg.get("type") not in CONTROL_TYPES:
                        yield msg
        except asyncio.TimeoutError:
            log.warning("no message for %.0fs — feed stale, reconnecting", stale_after)
        except (websockets.ConnectionClosed, OSError) as e:
            log.warning("connection lost (%r), reconnecting in %.0fs", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
