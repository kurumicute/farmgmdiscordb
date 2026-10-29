"""管理員登入：密碼雜湊、session、失敗鎖定與 API 行為。"""

import pytest
from fastapi.testclient import TestClient

from app.auth import AdminAuth, LoginLockedError, hash_password, verify_password
from app.main import create_app

PASSWORD = "correct-horse-123"
HASH = hash_password(PASSWORD)
ADMIN = {"X-Farmgm-Admin": "1"}


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_hash_roundtrip_and_format():
    assert HASH.startswith("scrypt:16384:8:1:") and "$" not in HASH and PASSWORD not in HASH
    assert verify_password(PASSWORD, HASH)
    assert not verify_password("wrong", HASH)
    assert hash_password(PASSWORD) != HASH  # 每次使用不同的 salt
    for bad in ("", "plain", "md5:1:2:3:a:b", "scrypt:x:8:1:a:b"):
        assert not verify_password(PASSWORD, bad)


def test_login_session_and_logout():
    clock = FakeTime()
    auth = AdminAuth("admin", HASH, session_seconds=60, now=clock)
    assert auth.login("admin", "wrong", "c") is None
    assert auth.login("other", PASSWORD, "c") is None
    token = auth.login("admin", PASSWORD, "c")
    assert token and auth.validate(token) == "admin"
    assert auth.validate("forged-token") is None
    clock.t += 61
    assert auth.validate(token) is None  # 過期
    token = auth.login("admin", PASSWORD, "c")
    auth.logout(token)
    assert auth.validate(token) is None


def test_lockout_after_repeated_failures():
    clock = FakeTime()
    auth = AdminAuth("admin", HASH, max_failures=3, lock_seconds=300, now=clock)
    for _ in range(3):
        assert auth.login("admin", "wrong", "1.2.3.4") is None
    with pytest.raises(LoginLockedError) as info:
        auth.login("admin", PASSWORD, "1.2.3.4")  # 鎖定期間正確密碼也不接受
    assert info.value.retry_after > 0
    assert auth.login("admin", PASSWORD, "5.6.7.8") is not None  # 其他來源不受影響
    clock.t += 301
    assert auth.login("admin", PASSWORD, "1.2.3.4") is not None


def test_disabled_when_not_configured():
    auth = AdminAuth(None, None)
    assert not auth.enabled
    assert auth.login("", "", "c") is None


def _client(session_factory, auth):
    app = create_app(session_factory, refresh_seconds=0.2, allowed_hosts=["testserver"], admin_auth=auth)
    return TestClient(app)


def test_login_api_flow(session_factory, service):
    with _client(session_factory, AdminAuth("admin", HASH)) as c:
        assert c.get("/api/auth/me").json() == {"enabled": True, "logged_in": False, "username": None}
        body = {"username": "admin", "password": PASSWORD}
        # 登入請求同樣需要防 CSRF 標頭，且不接受跨來源
        assert c.post("/api/auth/login", json=body).status_code == 403
        cross = {**ADMIN, "Origin": "http://evil.example"}
        assert c.post("/api/auth/login", json=body, headers=cross).status_code == 403
        res = c.post("/api/auth/login", json={"username": "admin", "password": "nope"}, headers=ADMIN)
        assert res.status_code == 401 and res.json()["detail"] == "帳號或密碼錯誤。"
        res = c.post("/api/auth/login", json={"username": "nobody", "password": PASSWORD}, headers=ADMIN)
        assert res.json()["detail"] == "帳號或密碼錯誤。"  # 不透露帳號是否存在

        res = c.post("/api/auth/login", json=body, headers=ADMIN)
        assert res.status_code == 200
        cookie = res.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api" in cookie
        assert PASSWORD not in res.text and "token" not in res.json()
        assert c.get("/api/auth/me").json()["logged_in"] is True

        on = {"active": True}
        assert c.put("/api/admin/crops/wheat/active", json=on, headers=ADMIN).status_code == 200
        assert c.post("/api/auth/logout", headers=ADMIN).status_code == 200
        assert c.get("/api/auth/me").json()["logged_in"] is False
        assert c.put("/api/admin/crops/wheat/active", json=on, headers=ADMIN).status_code == 401


def test_forged_cookie_rejected(session_factory, service):
    with _client(session_factory, AdminAuth("admin", HASH)) as c:
        c.cookies.set("farmgm_admin", "forged", path="/api")
        assert c.put("/api/admin/crops/wheat/active", json={"active": False}, headers=ADMIN).status_code == 401
        assert c.delete("/api/admin/events/1", headers=ADMIN).status_code == 401


def test_login_lockout_api(session_factory):
    with _client(session_factory, AdminAuth("admin", HASH, max_failures=3)) as c:
        wrong = {"username": "admin", "password": "x"}
        for _ in range(3):
            assert c.post("/api/auth/login", json=wrong, headers=ADMIN).status_code == 401
        res = c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=ADMIN)
        assert res.status_code == 429 and "Retry-After" in res.headers


def test_admin_disabled_without_config(session_factory):
    with _client(session_factory, AdminAuth(None, None)) as c:
        assert c.get("/api/auth/me").json()["enabled"] is False
        assert c.post("/api/auth/login", json={"username": "a", "password": "b"}, headers=ADMIN).status_code == 503
        assert c.put("/api/admin/crops/wheat/active", json={"active": False}, headers=ADMIN).status_code == 503
        # 唯讀監控不受影響
        assert c.get("/api/monitor/groups").status_code == 200
