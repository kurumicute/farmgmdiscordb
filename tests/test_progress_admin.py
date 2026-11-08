"""管理網站：新功能規則設定（簽到、寵物、特賣與升級、成就）與單一玩家的新功能資料調整。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import AdminAuth, hash_password
from app.db.models import AdminAction, DailyCheckin, FarmCodexEntry, FeatureSetting
from app.game import achievements, balance, checkin, feature_config, pets, special_shop
from app.game.errors import AlreadyExistsError, NotFoundError, ValidationError
from app.main import create_app
from app.repositories import feature_settings as feature_settings_repo
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer

PASSWORD = "progress-admin-pass"
ADMIN = {"X-Farmgm-Admin": "1"}


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def pid(admin, home_group):
    p = make_farmer(admin, "測試農夫", home_group)
    admin.set_farm_values("root", p, gold=500_000)
    return p


def actions(session_factory, action):
    with session_factory() as s:
        return list(s.scalars(select(AdminAction).where(AdminAction.action == action)))


# ---------- 規則設定 ----------


def test_pets_settings_apply_and_reset(admin, pid, session_factory):
    admin.update_feature_settings("root", "pets", {"buy_price": 1_000, "upgrade_prices": [2_000, 3_000],
                                                   "base_pct": 10, "pct_per_level": 20})
    assert pets.buy_price() == 1_000 and pets.max_level() == 2 and pets.bonus_pct(2) == 50
    before = admin.get_farm_status(pid).gold
    admin.buy_pet(pid, pets.CAT)
    assert admin.get_farm_status(pid).gold == before - 1_000
    admin.upgrade_pet(pid, pets.CAT)
    admin.upgrade_pet(pid, pets.CAT)
    with pytest.raises(ValidationError, match="最高等級"):
        admin.upgrade_pet(pid, pets.CAT)
    assert actions(session_factory, "update_feature_settings")[0].details["after"]["base_pct"] == 10
    with pytest.raises(ValidationError, match="不可超過 100%"):
        admin.update_feature_settings("root", "pets", {"buy_price": 1, "upgrade_prices": [1, 1, 1],
                                                       "base_pct": 50, "pct_per_level": 20})
    admin.reset_feature_settings("root", "pets")
    assert pets.buy_price() == 5_000 and pets.bonus_pct(4) == 25
    with session_factory() as s:
        assert s.get(FeatureSetting, "pets") is None


def test_settings_survive_restart_of_other_process(admin, pid):
    """另一個程序（Bot）重新讀取資料庫的設定：清掉程序內快取後，下一筆交易就套用。"""
    admin.update_feature_settings("root", "checkin", {"rewards": [{"gold": 7, "items": {}},
                                                                  {"gold": 0, "items": {balance.ITEM_FERTILIZER: 2}}]})
    feature_config.reset()
    feature_settings_repo.invalidate()
    assert checkin.cycle_days() == 7  # 還沒讀取：預設值
    admin.get_farm_status(pid)
    assert checkin.cycle_days() == 2 and checkin.reward_for(3)["gold"] == 7


def test_invalid_stored_settings_fall_back_to_defaults(admin, pid, session_factory, clock):
    with session_factory.begin() as s:
        feature_settings_repo.save(s, "pets", {"buy_price": -1}, "root", clock.now())
    feature_settings_repo.invalidate()
    admin.get_farm_status(pid)
    assert pets.buy_price() == 5_000


def test_checkin_rewards_follow_settings(admin, pid, clock):
    admin.update_feature_settings("root", "checkin", {"rewards": [
        {"gold": 11, "items": {}}, {"gold": 22, "items": {balance.ITEM_PATROL_TICKET: 1}}]})
    got = []
    for _ in range(3):
        before = admin.get_farm_status(pid).gold
        assert admin.daily_checkin(pid)["new"]
        got.append(admin.get_farm_status(pid).gold - before)
        clock.advance(86_400)
    assert got == [11, 22, 11]  # 兩天一輪


def test_special_shop_settings_and_title_clash(admin, pid):
    raw = {**special_shop.config(), "greenhouse_prices": [100, 200], "greenhouse_pct_per_level": 10,
           "titles": [{"title": "測試大亨", "price": 123}]}
    admin.update_feature_settings("root", "special_shop", raw)
    assert special_shop.greenhouse_max_level() == 2 and special_shop.title_price("測試大亨") == 123
    admin.buy_title(pid, "測試大亨")
    admin.upgrade_greenhouse(pid)
    admin.upgrade_greenhouse(pid)
    with pytest.raises(ValidationError, match="最高等級"):
        admin.upgrade_greenhouse(pid)
    with pytest.raises(ValidationError, match="不可重複"):
        admin.update_feature_settings("root", "special_shop", {**raw, "titles": [{"title": "神偷", "price": 1}]})
    with pytest.raises(ValidationError, match="不可超過 90%"):
        admin.update_feature_settings("root", "special_shop", {**raw, "greenhouse_pct_per_level": 50})


def test_achievement_overrides(admin, pid, clock):
    admin.update_feature_settings("root", "achievements", {"items": {
        "crops_100": {"target": 1, "gold": 77, "items": {}, "title": "第一顆"},
        "steal_20": achievements.editable(achievements.BASE_BY_CODE["steal_20"]),  # 和預設一樣：不存
    }})
    stored = feature_config.get("achievements")["items"]
    assert list(stored) == ["crops_100"]
    a = achievements.by_code()["crops_100"]
    assert (a.target, a.gold, a.title) == (1, 77, "第一顆")
    grow(admin, pid, 1, "wheat")
    clock.advance(600)
    admin.harvest_crop(pid, 1)
    result = admin.claim_achievements(pid)
    assert "初出茅廬" in result.message and "第一顆" in result.message
    with pytest.raises(ValidationError, match="不可重複"):
        admin.update_feature_settings("root", "achievements", {"items": {"crops_100": {"title": "神偷"}}})
    with pytest.raises(ValidationError, match="不可重複"):  # 和稱號商店重複
        admin.update_feature_settings("root", "achievements", {"items": {"crops_100": {"title": "黃金地主"}}})
    with pytest.raises(ValidationError):
        admin.update_feature_settings("root", "achievements", {"items": {"codex_all": {"target": 101}}})


def test_feature_settings_view(admin):
    view = admin.feature_settings()
    assert [k["key"] for k in view["keys"]] == ["checkin", "pets", "special_shop", "achievements"]
    assert all(k["is_default"] for k in view["keys"])
    assert len(view["achievements"]) == len(achievements.BASE_ACHIEVEMENTS)
    assert {i["code"] for i in view["items"]} == set(balance.ITEMS)


# ---------- 單一玩家 ----------


def test_player_pets_greenhouse_titles(admin, pid, session_factory):
    admin.set_greenhouse_level("root", pid, 3)
    admin.set_pet_level("root", pid, pets.DOG, 2)
    admin.set_pet_level("root", pid, pets.CAT, 0)
    view = admin.admin_progress(pid)
    assert view["greenhouse"]["level"] == 3
    assert {p["code"]: p["level"] for p in view["pets"]} == {"cat": 0, "dog": 2}
    admin.set_pet_level("root", pid, pets.CAT, None)
    assert admin.get_farm_status(pid).pets == {"dog": 2}
    with pytest.raises(ValidationError):
        admin.set_greenhouse_level("root", pid, special_shop.greenhouse_max_level() + 1)

    admin.grant_title("root", pid, "管理員之友")
    with pytest.raises(AlreadyExistsError):
        admin.grant_title("root", pid, "管理員之友")
    admin.set_active_title("root", pid, "管理員之友")
    assert admin.titles(pid)["current"] == "管理員之友"
    assert admin.titles(pid)["owned"][0]["source"] == "管理員"
    with pytest.raises(NotFoundError):
        admin.set_active_title("root", pid, "沒有的稱號")
    admin.revoke_title("root", pid, "管理員之友")
    assert admin.titles(pid) == {"current": None, "owned": []}
    assert len(actions(session_factory, "set_pet_level")) == 3


def test_player_checkin_streak_restore(admin, pid, clock, session_factory):
    admin.daily_checkin(pid)
    clock.advance(3 * 86_400)  # 斷簽兩天
    admin.set_checkin_streak("root", pid, 5)  # 補登昨天
    assert admin.daily_checkin(pid)["streak"] == 6
    admin.set_checkin_streak("root", pid, 9)  # 今天已簽到：改今天那一筆
    assert admin.checkin_status(pid)["streak"] == 9
    with session_factory() as s:
        assert s.scalar(select(DailyCheckin.gold).where(DailyCheckin.streak == 5)) == 0  # 補登的不發獎


def test_player_achievement_and_codex(admin, pid, clock, session_factory):
    admin.set_achievement_claimed("root", pid, "crops_100", True)
    rows = {r["code"]: r["state"] for r in admin.admin_progress(pid)["achievements"]}
    assert rows["crops_100"] == "claimed"
    admin.set_achievement_claimed("root", pid, "crops_100", False)
    assert {r["code"]: r["state"] for r in admin.admin_progress(pid)["achievements"]}["crops_100"] == "progress"

    admin.set_codex_entry("root", pid, "wheat", 150, 2)  # 收成 150 個小麥 → 成就「初出茅廬」可領
    view = admin.admin_progress(pid)
    crop = next(c for c in view["codex"] if c["key"] == "crop")
    wheat = next(e for e in crop["entries"] if e["code"] == "wheat")
    assert wheat["got"] and wheat["quantity"] == 150 and wheat["best_quality"] == 2
    assert {r["code"]: r["state"] for r in view["achievements"]}["crops_100"] == "ready"
    admin.set_codex_entry("root", pid, "wheat", 0, 0)
    with session_factory() as s:
        assert s.get(FarmCodexEntry, (1, "wheat")) is None
    with pytest.raises(NotFoundError):
        admin.set_codex_entry("root", pid, "fish_nope", 1, 0)


def test_player_weekly_purchases_reset(admin, pid):
    admin.update_feature_settings("root", "special_shop", {**special_shop.config(), "normal_limit": 3})
    offer = admin.special_shop(pid)["weekly"]["offers"][0]
    assert offer["limit"] == 3
    admin.buy_weekly_deal(pid, offer["crop_code"], offer["limit"])
    with pytest.raises(ValidationError, match="限購"):
        admin.buy_weekly_deal(pid, offer["crop_code"], 1)
    assert admin.admin_progress(pid)["weekly"]["purchases"][0]["quantity"] == offer["limit"]
    assert admin.reset_weekly_purchases("root", pid) == {"cleared": 1}
    admin.buy_weekly_deal(pid, offer["crop_code"], 1)


# ---------- API ----------


def test_api_requires_login_and_works(session_factory, clock, home_group):
    app = create_app(session_factory, clock=clock, refresh_seconds=0.2, allowed_hosts=["testserver"],
                     admin_auth=AdminAuth("admin", hash_password(PASSWORD)))
    game = AdminService(session_factory, clock)
    pid = make_farmer(game, "網站玩家", home_group)
    with TestClient(app) as c:
        assert c.get("/api/admin/feature-settings", headers=ADMIN).status_code == 401
        assert c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                      headers=ADMIN).status_code == 200
        view = c.get("/api/admin/feature-settings", headers=ADMIN).json()
        cfg = next(k for k in view["keys"] if k["key"] == "pets")["current"]
        r = c.put("/api/admin/feature-settings/pets", json={**cfg, "buy_price": 4_321}, headers=ADMIN)
        assert r.status_code == 200 and pets.buy_price() == 4_321
        bad = c.put("/api/admin/feature-settings/pets", json={**cfg, "base_pct": 999}, headers=ADMIN)
        assert bad.status_code == 400 and "Lv0 加成" in bad.json()["detail"]
        assert c.put("/api/admin/feature-settings/nope", json={}, headers=ADMIN).status_code == 422
        assert c.delete("/api/admin/feature-settings/pets", headers=ADMIN).status_code == 200
        assert pets.buy_price() == 5_000
        assert c.put(f"/api/admin/players/{pid}/pets/dog", json={"level": 1}, headers=ADMIN).status_code == 200
        assert c.put(f"/api/admin/players/{pid}/greenhouse", json={"level": 1}, headers=ADMIN).status_code == 200
        assert c.post(f"/api/admin/players/{pid}/titles", json={"title": "網站稱號"},
                      headers=ADMIN).status_code == 201
        assert c.put(f"/api/admin/players/{pid}/title", json={"title": "網站稱號"}, headers=ADMIN).status_code == 200
        assert c.put(f"/api/admin/players/{pid}/checkin", json={"streak": 3}, headers=ADMIN).status_code == 200
        assert c.put(f"/api/admin/players/{pid}/achievements/crops_100", json={"claimed": True},
                     headers=ADMIN).status_code == 200
        assert c.put(f"/api/admin/players/{pid}/codex/carrot", json={"quantity": 3, "best_quality": 1},
                     headers=ADMIN).status_code == 200
        progress = c.get(f"/api/admin/players/{pid}/progress", headers=ADMIN).json()
        assert progress["titles"]["current"] == "網站稱號" and progress["greenhouse"]["level"] == 1
        assert progress["checkin"]["streak"] == 3
        assert c.post(f"/api/admin/players/{pid}/titles/revoke", json={"title": "網站稱號"},
                      headers=ADMIN).status_code == 200
        assert c.delete(f"/api/admin/players/{pid}/weekly-purchases", headers=ADMIN).json() == {"cleared": 0}
