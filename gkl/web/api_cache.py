"""Shared HTTP response cache backed by SQLite.

In web mode, multiple user subprocesses share this cache to avoid redundant
API calls. The cache is keyed by (api, url, params_hash) with configurable TTL.

SQLite with WAL mode supports concurrent readers and serialized writers,
which is sufficient for our workload.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from time import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key TEXT PRIMARY KEY,
    api_name TEXT NOT NULL,
    url TEXT NOT NULL,
    response_body TEXT NOT NULL,
    cached_at REAL NOT NULL,
    ttl REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_api ON response_cache(api_name);
"""

# Default TTLs in seconds
TTL_YAHOO_LEAGUE = 300       # 5 min — standings, settings, scoreboard
TTL_YAHOO_PLAYERS = 120      # 2 min — roster, free agents
TTL_MLB_SCORES = 30          # 30 sec — live scoreboard
TTL_MLB_STATS = 3600         # 1 hour — player stats
TTL_STATCAST = 6 * 3600      # 6 hours — updates overnight


def _make_key(url: str, params: dict | None = None) -> str:
    raw = url
    if params:
        raw += "|" + json.dumps(params, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class ResponseCache:
    """SQLite-backed shared HTTP response cache."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = os.environ.get("GKL_CACHE_DB", "/data/cache.db")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=10
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def get(self, url: str, params: dict | None = None) -> str | None:
        """Get cached response body if it exists and is fresh."""
        key = _make_key(url, params)
        row = self._conn.execute(
            "SELECT response_body, cached_at, ttl FROM response_cache "
            "WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        body, cached_at, ttl = row
        if time() - cached_at > ttl:
            return None
        return body

    def put(
        self,
        url: str,
        params: dict | None,
        response_body: str,
        api_name: str = "",
        ttl: float = 300,
    ) -> None:
        """Cache an HTTP response body."""
        key = _make_key(url, params)
        self._conn.execute(
            "INSERT OR REPLACE INTO response_cache "
            "(cache_key, api_name, url, response_body, cached_at, ttl) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, api_name, url, response_body, time(), ttl),
        )
        self._conn.commit()

    def cleanup(self) -> int:
        """Remove expired entries. Returns count of deleted rows."""
        now = time()
        cursor = self._conn.execute(
            "DELETE FROM response_cache WHERE (? - cached_at) > ttl",
            (now,),
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()


# Module-level singleton — lazy-initialized, only in web mode
_instance: ResponseCache | None = None


def get_cache() -> ResponseCache | None:
    """Get the shared response cache. Returns None in local mode."""
    global _instance
    if os.environ.get("GKL_MODE", "local").lower() != "web":
        return None
    if _instance is None:
        _instance = ResponseCache()
    return _instance
