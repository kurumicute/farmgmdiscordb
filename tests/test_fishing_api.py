"""監控網站的釣魚 API：唯讀統計、玩家紀錄搜尋與分頁、魚種／釣具／機率設定的管理（需登入與 CSRF 標頭），
以及遊戲操作後網站資料同步。"""

import random

import pytest
from fastapi.testclient import TestClient

from app.auth import AdminAuth, hash_password
from app.game import fishing
from app.main import create_app
from app.services.admin_service import AdminService
from tests.conftest import make_farmer

PASSWORD = "fishing-admin-pass"
ADMIN = {"X-Farmgm-Admin": "1"}


@pytest.fixture
def client(session_factory, clock):
    app = create_app(session_factory, clock=clock, refresh_seconds=0.2, allowed_hosts=["testserver"],
                     admin_auth=AdminAuth("admin", hash_password(PASSWORD)))
    with TestClient(app) as c:
        yield c


def login(c):
    assert c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=ADMIN).status_code == 200


@pytest.fixture
def game(session_factory, clock):
    return AdminService(session_factory, clock, fishing_rng=random.Random(9).random)


def catch_one(game, clock, pid):
    view = game.start_fishing(pid)
    clock._now = view["bite_at"]
    view = game.hook_fish(pid, view["id"])
    while view["phase"] == "fighting":
        state = {k: view[k] for k in ("intent", "tension", "distance", "stamina", "max_stamina", "max_tension")}
        view = game.fishing_action(pid, view["id"], fishing.smart_action(state, None, {"reel_power": 10}),
                                   view["turn"])
    return view


def test_monitor_endpoints_reflect_game_actions(client, game, clock, home_group):
    game.update_fishing_settings("test", {**fishing.DEFAULT_SETTINGS, "base_success_pct": 100})
    pid = make_farmer(game, "釣魚高手", home_group)
    game.set_farm_values("test", pid, gold=5000)
    for code, q in (("bamboo_rod", 1), ("basic_line", 1), ("worm_bait", 5)):
        game.buy_fishing_gear(pid, code, q)
    landed = catch_one(game, clock, pid)
    assert landed["status"] == "landed"
    game.in_group_context(home_group).share_fish(pid, landed["fish"]["id"])
    game.sell_fish(pid, None)
    s = client.get("/api/monitor/fishing/summary").json()
    assert s["sessions"] == 1 and s["landed"] == 1 and s["failed"] == 0 and s["players"] == 1
    assert s["sell_gold"] == landed["fish"]["price"] and s["shares"] == 1 and s["base_success_pct"] == 100
    players = client.get("/api/monitor/fishing/players", params={"q": "高手"}).json()
    assert players["total"] == 1
    row = players["items"][0]
    assert row["landed"] == 1 and row["sold"] == 1 and row["shares"] == 1
    assert row["equipped"] == {"rod": "竹製魚竿", "line": "普通魚線", "bait": "普通蚯蚓×4"}
    assert client.get("/api/monitor/fishing/players", params={"q": "沒有這個人"}).json()["total"] == 0
    detail = client.get(f"/api/monitor/fishing/players/{pid}").json()
    assert detail["catches"][0]["sold_price"] == landed["fish"]["price"]
    assert detail["shares"][0]["group_name"] == "主群組"
    assert {t["event_type"] for t in detail["transactions"]} == {"fish_sell", "fishing_buy"}
    assert client.get("/api/monitor/fishing/players/999999").status_code == 404
    cat = client.get("/api/monitor/fishing/catalog").json()
    assert len(cat["fish"]) == len(fishing.INITIAL_FISH) and cat["settings_version"] == 1
    assert next(g for g in cat["gear"] if g["code"] == "bamboo_rod")["owners"] == 1


def test_player_list_paginates(client, game, home_group):
    for i in range(5):
        pid = make_farmer(game, f"釣客{i}", home_group)
        game.set_farm_values("test", pid, gold=500)
        game.buy_fishing_gear(pid, "bamboo_rod")
    page1 = client.get("/api/monitor/fishing/players", params={"limit": 2, "offset": 0}).json()
    page3 = client.get("/api/monitor/fishing/players", params={"limit": 2, "offset": 4}).json()
    assert page1["total"] == 5 and len(page1["items"]) == 2 and len(page3["items"]) == 1
    assert client.get("/api/monitor/fishing/players", params={"limit": 1000}).status_code == 422


def test_admin_writes_require_login_and_header(client):
    body = {"code": "test_fish", "name": "測試魚", "emoji": "🐟", "rarity": "rare", "min_price": 21, "max_price": 30,
            "min_weight_g": 100, "max_weight_g": 500, "difficulty": 3}
    assert client.post("/api/admin/fish-types", json=body, headers=ADMIN).status_code == 401
    login(client)
    assert client.post("/api/admin/fish-types", json=body).status_code == 403  # 沒有 CSRF 標頭
    res = client.post("/api/admin/fish-types", json=body, headers=ADMIN)
    assert res.status_code == 201 and res.json()["rarity_name"] == "稀有"
    bad = {**body, "code": "bad_fish", "name": "壞魚", "min_price": 31}  # 最低高於最高
    assert client.post("/api/admin/fish-types", json=bad, headers=ADMIN).status_code == 400
    free = {**body, "code": "rich_fish", "name": "富魚", "rarity": "common", "min_price": 5000, "max_price": 90000}
    assert client.post("/api/admin/fish-types", json=free, headers=ADMIN).status_code == 201  # 售價不受稀有度限制
    upd = {k: v for k, v in body.items() if k != "code"} | {"name": "改名魚", "rarity": "epic", "min_price": 51,
                                                             "max_price": 70}
    assert client.put("/api/admin/fish-types/test_fish", json=upd, headers=ADMIN).json()["rarity"] == "epic"
    off = client.put("/api/admin/fish-types/test_fish/active", json={"active": False}, headers=ADMIN)
    assert off.json()["active"] is False
    actions = client.get("/api/monitor/admin-actions").json()
    assert {"create_fish_type", "update_fish_type", "disable_fish_type"} <= {a["action"] for a in actions}


def test_admin_gear_and_settings(client):
    login(client)
    gear = {"code": "gold_rod", "kind": "rod", "name": "黃金魚竿", "emoji": "🎣", "price": 9999, "required_level": 5,
            "stock": 3, "effects": {"reel_power": 16, "control_pct": 20, "reaction_bonus_ms": 1000}}
    assert client.post("/api/admin/fishing-gear", json=gear, headers=ADMIN).status_code == 201
    bad = {**gear, "code": "bad_rod", "name": "壞魚竿", "effects": {"max_tension": 120}}
    assert client.post("/api/admin/fishing-gear", json=bad, headers=ADMIN).status_code == 400
    upd = {k: v for k, v in gear.items() if k not in ("code", "kind")} | {"price": 5000, "stock": None}
    res = client.put("/api/admin/fishing-gear/gold_rod", json=upd, headers=ADMIN).json()
    assert res["price"] == 5000 and res["stock"] is None
    assert client.put("/api/admin/fishing-gear/gold_rod/active", json={"active": False},
                      headers=ADMIN).json()["active"] is False
    settings = {**fishing.DEFAULT_SETTINGS, "rarity_bp": {"common": 7000, "rare": 2000, "epic": 800, "legendary": 200}}
    assert client.put("/api/admin/fishing-settings", json=settings, headers=ADMIN).json() == {"version": 1}
    wrong = {**settings, "rarity_bp": {"common": 7000, "rare": 2000, "epic": 800, "legendary": 100}}
    res = client.put("/api/admin/fishing-settings", json=wrong, headers=ADMIN)
    assert res.status_code == 400 and "100%" in res.json()["detail"]
    cat = client.get("/api/monitor/fishing/catalog").json()
    assert cat["settings"]["rarity_bp"]["legendary"] == 200 and cat["settings_version"] == 1
