"""Request signing for Delta Exchange.

signature = HMAC_SHA256(secret, method + timestamp + path + query + body).hexdigest()

- timestamp: Unix seconds as a string; the server rejects signatures older than ~5s.
- path: includes the /v2 prefix, e.g. "/v2/wallet/balances".
- query: "?a=1&b=2" exactly as sent on the wire, or "" when there is none.
- body: the exact JSON string sent, or "" for GET.

The query must be built once and used for both signing and the URL, otherwise any
encoding difference produces a signature mismatch.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote_plus


def build_query(params: dict | None) -> str:
    """Build the query string the way Delta's reference client does (keeps insertion order)."""
    if not params:
        return ""
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return ""
    return "?" + "&".join(f"{k}={quote_plus(str(v))}" for k, v in clean.items())


def sign(secret: str, method: str, timestamp: str, path: str, query: str = "", body: str = "") -> str:
    message = method.upper() + timestamp + path + query + body
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def auth_headers(api_key: str, secret: str, method: str, path: str, query: str = "", body: str = "",
                 now: float | None = None) -> dict[str, str]:
    timestamp = str(int(now if now is not None else time.time()))
    return {
        "api-key": api_key,
        "timestamp": timestamp,
        "signature": sign(secret, method, timestamp, path, query, body),
    }
