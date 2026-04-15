"""Server-side session management backed by SQLite.

Each session maps a browser cookie to a Yahoo user's credentials and preferences.
Anthropic API keys are encrypted at rest using Fernet symmetric encryption.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import time

from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer

SESSION_DB_PATH = Path(os.environ.get("GKL_SESSION_DB", "/data/sessions.db"))
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
COOKIE_NAME = "gkl_session"


def _get_fernet() -> Fernet:
    key = os.environ.get("GKL_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("GKL_ENCRYPTION_KEY environment variable is required")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _get_signer() -> URLSafeTimedSerializer:
    secret = os.environ.get("GKL_SESSION_SECRET")
    if not secret:
        raise RuntimeError("GKL_SESSION_SECRET environment variable is required")
    return URLSafeTimedSerializer(secret)


@dataclass
class Session:
    session_id: str
    yahoo_guid: str
    yahoo_email: str
    yahoo_name: str
    access_token: str
    refresh_token: str
    token_expires_at: float
    anthropic_key: str | None
    created_at: float
    last_active: float


class SessionStore:
    """SQLite-backed session store with encrypted credential storage."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or SESSION_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                yahoo_guid TEXT NOT NULL,
                yahoo_email TEXT NOT NULL DEFAULT '',
                yahoo_name TEXT NOT NULL DEFAULT '',
                access_token_enc TEXT NOT NULL,
                refresh_token_enc TEXT NOT NULL,
                token_expires_at REAL NOT NULL,
                anthropic_key_enc TEXT,
                created_at REAL NOT NULL,
                last_active REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_guid
                ON sessions(yahoo_guid);
        """)

    def _encrypt(self, plaintext: str) -> str:
        return _get_fernet().encrypt(plaintext.encode()).decode()

    def _decrypt(self, ciphertext: str) -> str:
        return _get_fernet().decrypt(ciphertext.encode()).decode()

    def create_session(
        self,
        yahoo_guid: str,
        yahoo_email: str,
        yahoo_name: str,
        access_token: str,
        refresh_token: str,
        token_expires_at: float,
    ) -> Session:
        session_id = secrets.token_urlsafe(32)
        now = time()

        # Carry over Anthropic key from any previous session for this user
        existing_key_enc = None
        if yahoo_guid:
            row = self._conn.execute(
                """SELECT anthropic_key_enc FROM sessions
                   WHERE yahoo_guid = ? AND anthropic_key_enc IS NOT NULL
                   ORDER BY last_active DESC LIMIT 1""",
                (yahoo_guid,),
            ).fetchone()
            if row:
                existing_key_enc = row["anthropic_key_enc"]

        self._conn.execute(
            """INSERT INTO sessions
               (session_id, yahoo_guid, yahoo_email, yahoo_name,
                access_token_enc, refresh_token_enc, token_expires_at,
                anthropic_key_enc, created_at, last_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, yahoo_guid, yahoo_email, yahoo_name,
                self._encrypt(access_token),
                self._encrypt(refresh_token),
                token_expires_at,
                existing_key_enc,
                now, now,
            ),
        )
        self._conn.commit()

        anthropic_key = None
        if existing_key_enc:
            try:
                anthropic_key = self._decrypt(existing_key_enc)
            except Exception:
                pass

        return Session(
            session_id=session_id,
            yahoo_guid=yahoo_guid,
            yahoo_email=yahoo_email,
            yahoo_name=yahoo_name,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires_at=token_expires_at,
            anthropic_key=anthropic_key,
            created_at=now,
            last_active=now,
        )

    def get_session(self, session_id: str) -> Session | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        if time() - row["created_at"] > SESSION_MAX_AGE:
            self.delete_session(session_id)
            return None
        # Update last_active
        self._conn.execute(
            "UPDATE sessions SET last_active = ? WHERE session_id = ?",
            (time(), session_id),
        )
        self._conn.commit()
        return Session(
            session_id=row["session_id"],
            yahoo_guid=row["yahoo_guid"],
            yahoo_email=row["yahoo_email"],
            yahoo_name=row["yahoo_name"],
            access_token=self._decrypt(row["access_token_enc"]),
            refresh_token=self._decrypt(row["refresh_token_enc"]),
            token_expires_at=row["token_expires_at"],
            anthropic_key=(
                self._decrypt(row["anthropic_key_enc"])
                if row["anthropic_key_enc"]
                else None
            ),
            created_at=row["created_at"],
            last_active=row["last_active"],
        )

    def update_tokens(
        self,
        session_id: str,
        access_token: str,
        refresh_token: str,
        token_expires_at: float,
    ) -> None:
        self._conn.execute(
            """UPDATE sessions
               SET access_token_enc = ?, refresh_token_enc = ?,
                   token_expires_at = ?, last_active = ?
               WHERE session_id = ?""",
            (
                self._encrypt(access_token),
                self._encrypt(refresh_token),
                token_expires_at,
                time(),
                session_id,
            ),
        )
        self._conn.commit()

    def set_anthropic_key(self, session_id: str, key: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET anthropic_key_enc = ? WHERE session_id = ?",
            (self._encrypt(key), session_id),
        )
        self._conn.commit()

    def delete_session(self, session_id: str) -> None:
        self._conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )
        self._conn.commit()

    def cleanup_expired(self) -> int:
        cutoff = time() - SESSION_MAX_AGE
        cursor = self._conn.execute(
            "DELETE FROM sessions WHERE created_at < ?", (cutoff,)
        )
        self._conn.commit()
        return cursor.rowcount

    def sign_session_id(self, session_id: str) -> str:
        return _get_signer().dumps(session_id)

    def unsign_session_id(self, signed: str) -> str | None:
        try:
            return _get_signer().loads(signed, max_age=SESSION_MAX_AGE)
        except Exception:
            return None
