"""Read-only REST client for Delta Exchange India.

Deliberately has NO order endpoints. Adding order placement is a separate, reviewed step.

Envelope: {"success": true, "result": ..., "meta": {...}} or
          {"success": false, "error": {"code": ..., "context": ...}}.
Rate limit: 20,000 weight per fixed 5-minute window; reads cost 3. On 429 the
X-RATE-LIMIT-RESET header gives milliseconds to wait.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from delta.auth import auth_headers, build_query
from delta.config import Settings

USER_AGENT = "delta-india-readonly/0.1"

RESOLUTION_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800,
}
MAX_CANDLES_PER_REQUEST = 2000


class DeltaAPIError(RuntimeError):
    def __init__(self, status: int, payload: Any, path: str):
        self.status = status
        self.payload = payload
        self.path = path
        super().__init__(f"{path} -> HTTP {status}: {payload}")


class DeltaRest:
    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 max_retries: int = 4, timeout: float = 15.0):
        self.settings = settings
        self.max_retries = max_retries
        self._client = client or httpx.Client(base_url=settings.rest_url, timeout=timeout,
                                              headers={"User-Agent": USER_AGENT})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DeltaRest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── transport ──────────────────────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None, auth: bool = False) -> Any:
        """GET and unwrap the envelope. Returns the full body (result + meta)."""
        query = build_query(params)
        for attempt in range(self.max_retries + 1):
            headers = {"Content-Type": "application/json"}
            if auth:
                if not self.settings.has_credentials:
                    raise DeltaAPIError(0, "DELTA_API_KEY / DELTA_API_SECRET not set", path)
                # Re-sign every attempt: signatures expire after ~5 seconds.
                headers.update(auth_headers(self.settings.api_key, self.settings.api_secret,
                                            "GET", path, query))
            try:
                resp = self._client.get(path + query, headers=headers)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
                time.sleep(min(2 ** attempt, 10))
                continue

            if resp.status_code == 429 and attempt < self.max_retries:
                reset_ms = float(resp.headers.get("X-RATE-LIMIT-RESET", "1000"))
                time.sleep(max(reset_ms / 1000.0, 0.5))
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 10))
                continue

            try:
                body = resp.json()
            except ValueError:
                raise DeltaAPIError(resp.status_code, resp.text[:300], path) from None
            if resp.status_code != 200 or not body.get("success", False):
                raise DeltaAPIError(resp.status_code, body.get("error", body), path)
            return body
        raise AssertionError("unreachable")

    # ── public market data ────────────────────────────────────────────────
    def products(self, contract_types: str | None = None, states: str = "live") -> list[dict]:
        """All products, following cursor pagination."""
        out: list[dict] = []
        after = None
        while True:
            body = self._get("/v2/products", {"contract_types": contract_types, "states": states,
                                              "page_size": 100, "after": after})
            out.extend(body["result"])
            after = (body.get("meta") or {}).get("after")
            if not after:
                return out

    def tickers(self, contract_types: str | None = None) -> list[dict]:
        return self._get("/v2/tickers", {"contract_types": contract_types})["result"]

    def option_tickers(self, underlying: str, expiry_date=None) -> list[dict]:
        """All call+put tickers for an underlying (e.g. 'BTC'), optionally one expiry (a date)."""
        return self._get("/v2/tickers", {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": underlying.upper(),
            "expiry_date": expiry_date.strftime("%d-%m-%Y") if expiry_date else None,
        })["result"]

    def ticker(self, symbol: str) -> dict:
        return self._get(f"/v2/tickers/{symbol}")["result"]

    def candles_page(self, symbol: str, resolution: str, start: int, end: int) -> list[dict]:
        """One request, at most 2000 candles. Returned sorted ascending by time (API gives newest first)."""
        if resolution not in RESOLUTION_SECONDS:
            raise ValueError(f"resolution must be one of {list(RESOLUTION_SECONDS)}")
        rows = self._get("/v2/history/candles", {"resolution": resolution, "symbol": symbol,
                                                 "start": int(start), "end": int(end)})["result"]
        return sorted(rows, key=lambda r: r["time"])

    def candles(self, symbol: str, resolution: str, start: int, end: int,
                drop_forming: bool = True, now: float | None = None) -> list[dict]:
        """Candles over [start, end], chunked into 2000-bar requests, de-duplicated, ascending.

        drop_forming removes the candle still in progress, so a stored file never holds a
        bar whose OHLC will change later.
        """
        step = RESOLUTION_SECONDS[resolution]
        span = step * MAX_CANDLES_PER_REQUEST
        by_time: dict[int, dict] = {}
        cursor = int(start)
        while cursor <= end:
            chunk_end = min(cursor + span - step, int(end))
            for row in self.candles_page(symbol, resolution, cursor, chunk_end):
                by_time[int(row["time"])] = row
            cursor = chunk_end + step
        rows = [by_time[t] for t in sorted(by_time)]
        if drop_forming:
            now_s = int(now if now is not None else time.time())
            rows = [r for r in rows if int(r["time"]) + step <= now_s]
        return rows

    # ── private, read-only ────────────────────────────────────────────────
    def wallet_balances(self) -> list[dict]:
        return self._get("/v2/wallet/balances", auth=True)["result"]

    def positions_margined(self, contract_types: str | None = None) -> list[dict]:
        return self._get("/v2/positions/margined", {"contract_types": contract_types}, auth=True)["result"]
