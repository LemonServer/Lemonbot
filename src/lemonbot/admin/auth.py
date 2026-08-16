from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Session:
    token: str
    csrf: str
    expires_at: datetime


class LocalTokenManager:
    def __init__(self, *, session_hours: int = 8) -> None:
        self._bootstrap_digest: bytes | None = None
        self._sessions: dict[bytes, Session] = {}
        self._session_ttl = timedelta(hours=session_hours)

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def issue_bootstrap(self) -> str:
        token = secrets.token_urlsafe(32)
        self._bootstrap_digest = self._digest(token)
        return token

    def exchange(self, token: str) -> Session:
        supplied = self._digest(token)
        expected, self._bootstrap_digest = self._bootstrap_digest, None
        if expected is None or not hmac.compare_digest(expected, supplied):
            raise AuthenticationError("bootstrap token is invalid or has already been used")
        session = Session(
            token=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(24),
            expires_at=datetime.now(UTC) + self._session_ttl,
        )
        self._sessions[self._digest(session.token)] = session
        return session

    def authenticate(self, token: str | None) -> Session:
        if not token:
            raise AuthenticationError("authentication required")
        digest = self._digest(token)
        session = self._sessions.get(digest)
        if session is None or session.expires_at <= datetime.now(UTC):
            self._sessions.pop(digest, None)
            raise AuthenticationError("session is invalid or expired")
        return session

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._digest(token), None)
