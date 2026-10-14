"""Environment-driven settings.

Delta runs separate venues. This project targets Delta *India* only:
api.delta.exchange is Delta Global and rejects India keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENVIRONMENTS: dict[str, dict[str, str]] = {
    "prod": {
        "rest": "https://api.india.delta.exchange",
        "ws": "wss://socket.india.delta.exchange",
    },
    "testnet": {
        "rest": "https://cdn-ind.testnet.deltaex.org",
        "ws": "wss://socket-ind.testnet.deltaex.org",
    },
}


@dataclass(frozen=True)
class Settings:
    env: str
    rest_url: str
    ws_url: str
    api_key: str | None
    api_secret: str | None
    data_dir: Path

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file)  # no-op if the file is absent; real env vars win
    env = os.getenv("DELTA_ENV", "prod").strip().lower()
    if env not in ENVIRONMENTS:
        raise ValueError(f"DELTA_ENV must be one of {sorted(ENVIRONMENTS)}, got {env!r}")
    urls = ENVIRONMENTS[env]
    return Settings(
        env=env,
        rest_url=urls["rest"],
        ws_url=urls["ws"],
        api_key=os.getenv("DELTA_API_KEY") or None,
        api_secret=os.getenv("DELTA_API_SECRET") or None,
        data_dir=Path(os.getenv("DELTA_DATA_DIR", "./data")),
    )
