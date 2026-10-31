import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from app.auth import AdminAuth, hash_password
from app.db.models import Farm, GameTransaction
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.repositories import inventory as inventory_repo

# 測試專用的管理員帳密（執行時產生雜湊，與正式 .env 無關）
TEST_ADMIN_USER = "admin"
TEST_ADMIN_PASSWORD = "test-password-123"
TEST_ADMIN_HASH = hash_password(TEST_ADMIN_PASSWORD)


@pytest.fixture
def client(session_factory, clock):
    app = create_app(
        session_factory,
        clock=clock,
        refresh_seconds=0.2,
        allowed_hosts=["testserver"],
        admin_auth=AdminAuth(TEST_ADMIN_USER, TEST_ADMIN_HASH),
    )
    with TestClient(app) as c:
        yield c


def _setup(service, clock, player_id):
    service.buy_seeds(player_id, "wheat", 3)  # 470
    service.plant_crop(player_id, 1, "wheat")
    service.plant_crop(player_id, 2, "wheat")
    service.water_crop(player_id, 1)
    service.water_crop(player_id, 2)
    clock.advance(60)
    service.buy_seeds(player_id, "carrot", 1)  # 450
    service.plant_crop(player_id, 3, "carrot")
    service.water_crop(player_id, 3)  # 生長中


def test_summary_matches_database(client, service, clock, player_id, session_factory, home_group):
    other = service.create_player("小華")
    service.create_farm(other.id, home_group_id=home_group)
    _setup(service, clock, player_id)
    data = client.get("/api/monitor/summary").json()
    with session_factory() as s:
        assert data["total_gold"] == s.scalar(select(func.sum(Farm.gold))) == 450 + 500
        assert data["transactions_today"] == s.scalar(select(func.count()).select_from(GameTransaction))
    assert data["players"] == 2 and data["farms"] == 2
    assert data["plots"] == 8
    assert data["growing"] == 1 and data["ready"] == 2
    assert data["inventory_items"] == 1  # 剩 1 顆小麥種子


def test_farm_list_and_detail(client, service, clock, player_id, home_group, session_factory):
    _setup(service, clock, player_id)
    service.create_farm(service.create_player("Bob").id, "藍莓園", home_group_id=home_group)
    farms = client.get("/api/monitor/farms").json()
    assert farms["total"] == 2
    row = next(f for f in farms["items"] if f["player_name"] == "阿明")
    assert (row["plots"], row["empty"], row["growing"], row["ready"]) == (4, 1, 1, 2)
    assert row["gold"] == 450 and row["warehouse_used"] == 1

    # 賭場獎品佔倉庫空間、素材背包不佔：監控網站與群組成員列表都和遊戲內的倉庫用量一致
    farm_id = service.get_farm_status(player_id).farm_id
    with session_factory.begin() as s:
        inventory_repo.add_prize(s, farm_id, "strawberry", "rare", "none", 2, 3)
        inventory_repo.add(s, farm_id, "wood", 7)
        inventory_repo.add(s, farm_id, "seed_amber_melon", 2)
    game_used = service.get_farm_status(player_id).warehouse_used
    assert game_used == 1 + 3
    row = next(f for f in client.get("/api/monitor/farms").json()["items"] if f["player_name"] == "阿明")
    assert row["warehouse_used"] == game_used
    assert client.get(f"/api/monitor/farms/{farm_id}").json()["warehouse_used"] == game_used
    member = next(m for m in service.group_members(home_group) if m["player_id"] == player_id)
    assert member["farm"]["warehouse_used"] == game_used

    found = client.get("/api/monitor/farms", params={"q": "藍莓"}).json()
    assert [f["farm_name"] for f in found["items"]] == ["藍莓園"]
    assert client.get("/api/monitor/farms", params={"q": "%"}).json()["total"] == 0  # LIKE 萬用字元被跳脫

    detail = client.get(f"/api/monitor/farms/{row['farm_id']}").json()
    plots = {p["plot_number"]: p for p in detail["plots"]}
    assert plots[1]["state"] == "ready" and plots[1]["state_label"] == "可收成"
    assert plots[3]["state"] == "growing" and plots[3]["remaining_seconds"] == 180
    assert plots[4]["state"] == "empty" and plots[4]["crop_name"] is None
    assert detail["transactions"][0]["event_type"] == "water"


def test_input_validation(client):
    assert client.get("/api/monitor/farms/0").status_code == 422
    assert client.get("/api/monitor/farms/abc").status_code == 422
    assert client.get("/api/monitor/farms/999").status_code == 404
    assert client.get("/api/monitor/farms", params={"limit": 1000}).status_code == 422
    assert client.get("/api/monitor/farms", params={"q": "x" * 41}).status_code == 422
    assert client.get("/api/monitor/transactions", params={"limit": 0}).status_code == 422


def test_transactions_and_update_after_game_action(client, service, clock, player_id):
    before = client.get("/api/monitor/summary").json()
    service.buy_seeds(player_id, "wheat", 2)  # 相當於在 CMD 執行 buy wheat 2
    after = client.get("/api/monitor/summary").json()
    assert after["total_gold"] == before["total_gold"] - 20
    assert after["inventory_items"] == before["inventory_items"] + 2
    txs = client.get("/api/monitor/transactions").json()
    assert txs[0]["event_type"] == "buy_seed" and txs[0]["gold_delta"] == -20
    assert txs[0]["player_name"] == "阿明"
    assert "購買 2 顆小麥種子" in txs[0]["description"]


def test_charts_from_real_data(client, service, clock, player_id):
    charts = client.get("/api/monitor/charts").json()
    assert charts["has_history"] is True
    assert charts["activity"][-1]["total_gold"] == 500
    _setup(service, clock, player_id)
    charts = client.get("/api/monitor/charts").json()
    wheat = next(c for c in charts["crops"] if c["code"] == "wheat")
    assert (wheat["growing"], wheat["ready"]) == (0, 2)
    assert charts["activity"][-1]["total_gold"] == 450
    assert sum(a["transactions"] for a in charts["activity"]) == 9


def test_websocket_pushes_fresh_snapshots(client, service, player_id):
    with client.websocket_connect("/ws/monitor") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["summary"]["total_gold"] == 500
        service.buy_seeds(player_id, "wheat", 1)
        second = ws.receive_json()  # 下一次定時快照
        assert second["data"]["summary"]["total_gold"] == 490
        assert second["data"]["transactions"][0]["event_type"] == "buy_seed"


def test_websocket_rejects_cross_origin(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/monitor", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()


def test_monitor_is_read_only(client, service, player_id, session_factory):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/monitor/farms/1").status_code == 405
        assert getattr(client, method)("/api/monitor/summary").status_code == 405
    with session_factory() as s:
        assert s.scalar(select(Farm.gold)) == 500


def test_monitor_session_is_read_only_transaction(monitor):
    from sqlalchemy import text
    from sqlalchemy.exc import InternalError

    with pytest.raises(InternalError):
        with monitor._read_only() as s:
            s.execute(text("UPDATE farms SET gold = 0"))


def test_database_down_returns_error_not_fake_data():
    engine = make_engine("postgresql+psycopg://u:p@127.0.0.1:1/nope_test")
    app = create_app(make_session_factory(engine), refresh_seconds=0.2, check=False, allowed_hosts=["testserver"])
    try:
        with TestClient(app) as c:
            res = c.get("/api/monitor/summary")
            assert res.status_code == 503
            assert res.json() == {"detail": "資料庫暫時無法連線，無法取得監控資料。"}
            with c.websocket_connect("/ws/monitor") as ws:
                msg = ws.receive_json()
                assert msg["type"] == "error" and "data" not in msg
    finally:
        engine.dispose()


def test_index_served_with_security_headers(client):
    res = client.get("/")
    assert res.status_code == 200 and "農場即時監控" in res.text
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert client.get("/docs").status_code == 404


# ---------- 群組、偷菜、排行榜、背包、管理 API ----------

ADMIN = {"X-Farmgm-Admin": "1"}


@pytest.fixture
def social_world(service, clock):
    from tests.conftest import grow, make_farmer

    g1 = service.create_group("一號群").id
    g2 = service.create_group("二號群").id
    a = make_farmer(service, "甲", g1)
    b = make_farmer(service, "乙", g1)
    c = make_farmer(service, "丙", g2)
    service.add_member(g2, a)
    grow(service, b, 1, "strawberry")
    grow(service, c, 1, "strawberry")
    clock.advance(600)
    service.steal_crop(g1, a, b, 1, 2)
    clock.advance(86_400)  # 隔天
    return {"a": a, "b": b, "c": c, "g1": g1, "g2": g2}


def test_groups_endpoints(client, social_world):
    groups = client.get("/api/monitor/groups").json()
    assert [(g["name"], g["member_count"]) for g in groups] == [("一號群", 2), ("二號群", 2)]
    detail = client.get(f"/api/monitor/groups/{social_world['g1']}").json()
    assert [m["player_name"] for m in detail["members"]] == ["甲", "乙"]
    # 乙的草莓被偷後一直沒收成，隔天已乾枯（狀態依時間即時結算）
    assert detail["members"][1]["farm"]["withered"] == 1
    assert client.get("/api/monitor/groups/999").status_code == 404


def test_steals_endpoint_filters(client, service, social_world, clock):
    g1, g2, a, b, c = (social_world[k] for k in ("g1", "g2", "a", "b", "c"))
    from tests.conftest import grow

    grow(service, c, 2, "wheat")
    clock.advance(60)
    service.steal_crop(g2, a, c, 2, 1)

    rows = client.get("/api/monitor/steals").json()
    fields = ("group_name", "thief_name", "victim_name", "crop_name", "quantity", "plot_number")
    assert [tuple(r[f] for f in fields) for r in rows] == [
        ("二號群", "甲", "丙", "小麥", 1, 2),
        ("一號群", "甲", "乙", "草莓", 2, 1),
    ]
    assert len(client.get("/api/monitor/steals", params={"group_id": g1}).json()) == 1
    assert len(client.get("/api/monitor/steals", params={"player_id": b}).json()) == 1
    assert len(client.get("/api/monitor/steals", params={"player_id": a}).json()) == 2
    # 時鐘起點 2026-01-01 00:00 UTC = 台灣 08:00；第二筆在隔天
    def steals(**params):
        return client.get("/api/monitor/steals", params=params)

    assert len(steals(date_from="2026-01-01", date_to="2026-01-01").json()) == 1
    assert len(steals(date_from="2026-01-02").json()) == 1
    assert steals(date_from="2026-01-03", date_to="2026-01-01").status_code == 422
    assert client.get("/api/monitor/steals", params={"date_from": "not-a-date"}).status_code == 422


def test_leaderboard_endpoint(client, service, social_world):
    g1 = social_world["g1"]
    service.sell_crop(social_world["a"], "strawberry", 2)
    data = client.get("/api/monitor/leaderboard", params={"scope": "global", "kind": "sales"}).json()
    assert data["items"][0]["player_name"] == "甲" and data["items"][0]["sold_quantity"] == 2
    assert len(data["items"]) == 3
    grp = client.get("/api/monitor/leaderboard", params={"scope": "group", "group_id": g1, "kind": "money"}).json()
    assert [r["player_name"] for r in grp["items"]] == ["甲", "乙"]
    assert client.get("/api/monitor/leaderboard", params={"scope": "group"}).status_code == 422
    assert client.get("/api/monitor/leaderboard", params={"scope": "group", "group_id": 999}).status_code == 404
    assert client.get("/api/monitor/leaderboard", params={"kind": "hack"}).status_code == 422


def test_backpack_endpoint(client, service, social_world):
    a = social_world["a"]
    service.buy(a, "fertilizer", 1)
    bp = client.get(f"/api/monitor/players/{a}/backpack").json()
    assert [(x["name"], x["quantity"], x["price"]) for x in bp["produce"]] == [("草莓", 2, 25)]
    assert [x["name"] for x in bp["consumables"]] == ["化肥"]
    assert bp["tools"][0]["owned"] is False and bp["effects"][0]["status"] == "inactive"
    assert client.get("/api/monitor/players/999/backpack").status_code == 404


def test_admin_write_protection_and_group_endpoints_removed(client, service, social_world):
    c = social_world["c"]
    g1 = social_world["g1"]
    gold_before = service.get_farm_status(c).gold
    body = {"item_code": "fertilizer", "quantity": 1}
    url = f"/api/admin/players/{c}/inventory"
    # 缺少管理標頭或跨來源請求一律拒絕
    assert client.put(url, json=body).status_code == 403
    assert client.put(url, json=body, headers={**ADMIN, "Origin": "http://evil.example"}).status_code == 403
    # 未登入不能修改
    assert client.put(url, json=body, headers=ADMIN).status_code == 401

    login = {"username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASSWORD}
    assert client.post("/api/auth/login", json=login, headers=ADMIN).status_code == 200
    assert client.put(url, json=body, headers=ADMIN).status_code == 200
    # 群組建立與成員管理改由 Discord 處理，網站不再提供
    assert client.post("/api/admin/groups", json={"name": "三號群"}, headers=ADMIN).status_code in (404, 405)
    assert client.put(f"/api/admin/groups/{g1}/members/{c}", headers=ADMIN).status_code in (404, 405)
    # 調整背包不影響金幣；不存在的管理路徑與唯讀監控端點不接受寫入
    assert service.get_farm_status(c).gold == gold_before
    assert client.put(f"/api/admin/farms/{c}", json={"gold": 99999}, headers=ADMIN).status_code in (404, 405)
    assert client.post("/api/monitor/players/1/backpack", json={}).status_code == 405


def test_untrusted_host_rejected(client):
    assert client.get("/api/monitor/summary", headers={"host": "evil.example"}).status_code == 400
