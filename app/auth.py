"""管理員登入：密碼雜湊（scrypt）、伺服器端 session 與登入失敗鎖定。

- 密碼只以雜湊形式存放於 .env 的 ADMIN_PASSWORD_HASH，原始碼與設定中不保存明文。
- 雜湊格式：scrypt:N:r:p:salt:hash（base64url；刻意不用 $，避免 Docker Compose 讀 .env 時被展開）。
- Session 存在記憶體，服務重啟後需重新登入。
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = encoded.split(":")
        if scheme != "scrypt":
            return False
        expected_bytes = _unb64(expected)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected_bytes)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected_bytes)


# 帳號不存在時仍計算一次雜湊，避免以回應時間推測帳號是否正確
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class LoginLockedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


@dataclass
class _Session:
    username: str
    expires_at: float


class AdminAuth:
    def __init__(
        self,
        username: str | None,
        password_hash: str | None,
        *,
        session_seconds: float = 8 * 3600,
        max_failures: int = 5,
        lock_seconds: float = 300,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.username = username or None
        self.password_hash = password_hash or None
        self.session_seconds = session_seconds
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._now = now
        self._sessions: dict[str, _Session] = {}
        self._failures: dict[str, tuple[int, float]] = {}  # client -> (失敗次數, 鎖定到期時間)
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password_hash)

    def login(self, username: str, password: str, client: str) -> str | None:
        """成功回傳 session token；失敗回傳 None；被鎖定時拋出 LoginLockedError。"""
        now = self._now()
        with self._lock:
            count, locked_until = self._failures.get(client, (0, 0.0))
            if locked_until > now:
                raise LoginLockedError(int(locked_until - now) + 1)

        user_ok = self.enabled and hmac.compare_digest(username.encode(), (self.username or "").encode())
        pw_ok = verify_password(password, self.password_hash if user_ok else _DUMMY_HASH)

        with self._lock:
            if not (user_ok and pw_ok):
                count += 1
                locked_until = now + self.lock_seconds if count >= self.max_failures else 0.0
                self._failures[client] = (0 if locked_until else count, locked_until)
                return None
            self._failures.pop(client, None)
            self._purge(now)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = _Session(self.username, now + self.session_seconds)
            return token

    def validate(self, token: str | None) -> str | None:
        if not token:
            return None
        now = self._now()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if session.expires_at <= now:
                del self._sessions[token]
                return None
            return session.username

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _purge(self, now: float) -> None:
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            del self._sessions[token]
