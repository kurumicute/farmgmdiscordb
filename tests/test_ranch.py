"""動物養殖、建築與食品加工。"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.auth import AdminAuth
from app.cli.commands import CommandHandler
from app.db.models import CraftOrder, Farm, GameTransaction
from app.db.session import make_engine, make_session_factory
from app.game.errors import (
    AlreadyExistsError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.main import create_app
from app.repositories import inventory as inventory_repo
from app.repositories import ranch as ranch_repo
from app.repositories import transactions as tx_repo
from app.services.social_service import SocialService
from tests.conftest import make_farmer

DAY = 24 * 3600


def set_gold(session_factory, pid, gold):
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == pid).values(gold=gold))


def give(session_factory, pid, code, qty):
    with session_factory.begin() as s:
        farm_id = s.scalar(select(Farm.id).where(Farm.player_id == pid))
        inventory_repo.add(s, farm_id, code, qty)


def gold(service, pid):
    return service.get_farm_status(pid).gold


def qty(service, pid, code):
    return next((i.quantity for i in service.get_inventory(pid).items if i.item_code == code), 0)


def states(service, pid):
    return [a["state"] for a in service.list_animals(pid)]


def run_concurrently(n, fn):
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            r = ("ok", fn())
        except Exception as exc:  # noqa: BLE001
            r = ("err", exc)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.fixture
def farmer(service, home_group, session_factory):
    pid = make_farmer(service, "牧場主", home_group)
    set_gold(session_factory, pid, 100_000)
    return pid


# ---------- 建築 ----------


def test_build_limits(service, farmer):
    for i in (1, 2, 3):
        assert f"第 {i} 座" in service.build(farmer, "畜舍").message
    with pytest.raises(AlreadyExistsError):
        service.build(farmer, "barn")
    service.build(farmer, "磨坊")
    with pytest.raises(AlreadyExistsError):
        service.build(farmer, "mill")
    with pytest.raises(NotFoundError):
        service.build(farmer, "城堡")
    assert gold(service, farmer) == 100_000 - 3 * 1000 - 2000
    assert service.workshop_status(farmer)["capacity"] == 12


def test_build_requires_gold(service, home_group):
    poor = make_farmer(service, "窮農", home_group)
    with pytest.raises(InsufficientGoldError):
        service.build(poor, "烘焙坊")
    assert gold(service, poor) == 500 and service.workshop_status(poor)["counts"]["bakery"] == 0


# ---------- 購買與容量 ----------


def test_animals_need_barn_and_capacity(service, farmer, session_factory):
    with pytest.raises(ValidationError, match="畜舍"):
        service.buy_animals(farmer, "雞", 1)
    assert gold(service, farmer) == 100_000
    service.build(farmer, "畜舍")  # 容量 4
    with pytest.raises(ValidationError, match="容量不足"):
        service.buy_animals(farmer, "雞", 5)
    assert gold(service, farmer) == 99_000 and service.list_animals(farmer) == []  # 容量不足時不扣款
    result = service.buy_animals(farmer, "雞", 3)
    assert "編號 1–3" in result.message and gold(service, farmer) == 99_000 - 1500
    service.buy_animals(farmer, "牛", 1)
    with pytest.raises(ValidationError, match="容量不足"):
        service.buy_animals(farmer, "羊", 1)
    with pytest.raises(NotFoundError):
        service.buy_animals(farmer, "恐龍", 1)
    assert [a["animal_no"] for a in service.list_animals(farmer)] == [1, 2, 3, 4]


def test_buy_animals_insufficient_gold(service, farmer, session_factory):
    service.build(farmer, "畜舍")
    set_gold(session_factory, farmer, 1999)
    with pytest.raises(InsufficientGoldError):
        service.buy_animals(farmer, "牛", 1)
    assert gold(service, farmer) == 1999 and service.list_animals(farmer) == []


def test_concurrent_animal_purchases_respect_capacity(service, farmer):
    service.build(farmer, "畜舍")  # 容量 4
    results = run_concurrently(6, lambda: service.buy_animals(farmer, "雞", 1))
    assert sum(1 for r in results if r[0] == "ok") == 4
    assert len(service.list_animals(farmer)) == 4
    assert gold(service, farmer) == 100_000 - 1000 - 4 * 500


# ---------- 餵食、生產、收集 ----------


def test_feed_produce_collect_cycle(service, farmer, clock, session_factory):
    service.build(farmer, "畜舍")
    service.buy_animals(farmer, "雞", 1)
    before = gold(service, farmer)
    clock.advance(10 * DAY)
    assert states(service, farmer) == ["hungry"]  # 未餵食不生產，也不會死亡
    with pytest.raises(ValidationError, match="尚未餵食"):
        service.collect_animal(farmer, 1)

    service.feed_animal(farmer, 1)
    assert gold(service, farmer) == before - 20
    with pytest.raises(ValidationError, match="生產中"):
        service.feed_animal(farmer, 1)  # 已餵食：不重複扣款
    assert gold(service, farmer) == before - 20
    clock.advance(DAY - 1)
    with pytest.raises(ValidationError, match="生產中"):
        service.collect_animal(farmer, 1)
    clock.advance(1)
    assert states(service, farmer) == ["ready"]
    with pytest.raises(ValidationError, match="請先收集"):
        service.feed_animal(farmer, 1)
    assert service.collect_animal(farmer, 1).quantity == 2
    assert qty(service, farmer, "egg") == 2
    with pytest.raises(ValidationError, match="尚未餵食"):
        service.collect_animal(farmer, 1)  # 同一週期只能收集一次
    clock.advance(5 * DAY)
    assert states(service, farmer) == ["hungry"]  # 未再餵食就停止下一週期
    assert service.list_animals(farmer)[0]["cycles_completed"] == 1
    with pytest.raises(NotFoundError):
        service.feed_animal(farmer, 99)


def test_sheep_cycle_is_48_hours(service, farmer, clock):
    service.build(farmer, "畜舍")
    service.buy_animals(farmer, "羊", 1)
    service.feed_animal(farmer, 1)
    clock.advance(DAY)
    assert states(service, farmer) == ["producing"]
    clock.advance(DAY)
    assert service.collect_animal(farmer, 1).quantity == 1 and qty(service, farmer, "wool") == 1


def test_feed_all_with_insufficient_gold(service, farmer, session_factory):
    service.build(farmer, "畜舍")
    service.buy_animals(farmer, "雞", 1)  # #1 20
    service.buy_animals(farmer, "牛", 1)  # #2 50
    service.buy_animals(farmer, "雞", 1)  # #3 20
    set_gold(session_factory, farmer, 45)
    result = service.feed_all(farmer)
    # 依編號順序餵到金幣不夠為止：只餵 #1，只扣 20
    assert result.quantity == 1 and "未餵食：牛#2、雞#3" in result.message
    assert gold(service, farmer) == 25
    assert states(service, farmer) == ["producing", "hungry", "hungry"]
    with session_factory() as s:
        assert s.scalar(select(func.count()).where(GameTransaction.event_type == "feed_animal")) == 1
    set_gold(session_factory, farmer, 10)
    with pytest.raises(InsufficientGoldError):
        service.feed_all(farmer)
    assert gold(service, farmer) == 10 and states(service, farmer) == ["producing", "hungry", "hungry"]
    set_gold(session_factory, farmer, 1000)
    assert "已餵食 2 隻" in service.feed_all(farmer).message
    assert gold(service, farmer) == 930
    assert service.feed_all(farmer).message == "目前沒有需要餵食的動物。"


def test_feed_all_rolls_back_on_failure(service, farmer, monkeypatch):
    service.build(farmer, "畜舍")
    service.buy_animals(farmer, "雞", 3)
    before = gold(service, farmer)
    calls = {"n": 0}
    original = tx_repo.add

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模擬寫入失敗")
        return original(*args, **kwargs)

    monkeypatch.setattr(tx_repo, "add", flaky)
    with pytest.raises(RuntimeError):
        service.feed_all(farmer)
    monkeypatch.undo()
    # 不會出現「扣了錢但動物沒更新」或部分餵食
    assert gold(service, farmer) == before and states(service, farmer) == ["hungry"] * 3


def test_collect_with_full_backpack_keeps_product(service, farmer, clock, session_factory):
    service.build(farmer, "畜舍")
    service.buy_animals(farmer, "牛", 1)
    service.feed_animal(farmer, 1)
    clock.advance(DAY)
    give(session_factory, farmer, "wheat", 49)  # 背包 49/50，牛奶需要 2 格
    with pytest.raises(WarehouseFullError, match="保留"):
        service.collect_animal(farmer, 1)
    assert states(service, farmer) == ["ready"] and qty(service, farmer, "milk") == 0
    service.sell(farmer, "wheat", 10)
    assert service.collect_animal(farmer, 1).quantity == 2


def test_restart_and_concurrent_collect(service, farmer, clock, database_url):
    service.build(farmer, "畜舍")
    service.buy_animals(farmer, "雞", 1)
    service.feed_animal(farmer, 1)
    ready_at = service.list_animals(farmer)[0]["ready_at"]
    clock.advance(DAY // 2)
    engine = make_engine(database_url)
    try:
        restarted = SocialService(make_session_factory(engine), clock)
        a = restarted.list_animals(farmer)[0]
        assert a["state"] == "producing" and a["ready_at"] == ready_at  # 重啟不重設時間
        assert a["remaining_seconds"] == DAY // 2
        clock.advance(DAY // 2)
        results = run_concurrently(5, lambda: restarted.collect_animal(farmer, 1))
        assert sum(1 for r in results if r[0] == "ok") == 1
        assert restarted.get_backpack(farmer).goods[0].quantity == 2  # 不重複發放
    finally:
        engine.dispose()


# ---------- 加工 ----------


def test_craft_requires_building_and_ingredients(service, farmer, session_factory):
    give(session_factory, farmer, "wheat", 5)
    with pytest.raises(ValidationError, match="磨坊"):
        service.start_craft(farmer, "麵粉", 1)
    service.build(farmer, "磨坊")
    with pytest.raises(InsufficientItemsError, match="小麥 需要 6、只有 5"):
        service.start_craft(farmer, "麵粉", 2)
    assert qty(service, farmer, "wheat") == 5  # 原料不足時不扣除
    with pytest.raises(ValidationError):
        service.start_craft(farmer, "麵粉", 11)
    with pytest.raises(NotFoundError):
        service.start_craft(farmer, "披薩", 1)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(CraftOrder)) == 0


def test_craft_lifecycle(service, farmer, clock, session_factory):
    service.build(farmer, "磨坊")
    give(session_factory, farmer, "wheat", 7)
    result = service.start_craft(farmer, "flour", 2)
    assert qty(service, farmer, "wheat") == 1  # 開單時立即扣除 6 個小麥
    [order] = service.craft_orders(farmer)
    assert order["input_codes"] == {"wheat": 6} and order["output_quantity"] == 2
    assert order["state"] == "in_progress" and order["remaining_seconds"] == 60 * 60
    assert (order["ready_at"] - order["started_at"]).total_seconds() == 3600
    assert f"#{order['order_id']}" in result.message
    give(session_factory, farmer, "wheat", 2)
    with pytest.raises(ValidationError, match="一次只能加工一張"):
        service.start_craft(farmer, "flour", 1)
    with pytest.raises(ValidationError, match="加工中"):
        service.collect_craft(farmer, order["order_id"])
    clock.advance(3600)
    # 已完成但未領取的訂單不佔用設施
    service.start_craft(farmer, "flour", 1)
    assert service.collect_craft(farmer, order["order_id"]).quantity == 2
    with pytest.raises(ValidationError, match="已經領取"):
        service.collect_craft(farmer, order["order_id"])
    assert qty(service, farmer, "flour") == 2
    history = service.craft_orders(farmer, include_collected=True)
    assert [o["state"] for o in history] == ["in_progress", "collected"]


def test_bread_and_jam_recipes(service, farmer, clock, session_factory):
    service.build(farmer, "烘焙坊")
    service.build(farmer, "果醬工坊")
    give(session_factory, farmer, "flour", 4)
    give(session_factory, farmer, "strawberry", 3)
    bread = service.start_craft(farmer, "麵包", 2)
    jam = service.start_craft(farmer, "果醬", 1)  # 不同設施可同時加工
    assert "2 份麵包" in bread.message and "1 份草莓果醬" in jam.message
    clock.advance(2 * 3600)
    for o in service.craft_orders(farmer):
        service.collect_craft(farmer, o["order_id"])
    assert qty(service, farmer, "bread") == 2 and qty(service, farmer, "strawberry_jam") == 1
    assert qty(service, farmer, "flour") == 0 and qty(service, farmer, "strawberry") == 0


def test_craft_full_backpack_keeps_pending(service, farmer, clock, session_factory):
    service.build(farmer, "磨坊")
    give(session_factory, farmer, "wheat", 3)
    service.start_craft(farmer, "flour", 1)
    clock.advance(1800)
    give(session_factory, farmer, "carrot", 50)
    order_id = service.craft_orders(farmer)[0]["order_id"]
    with pytest.raises(WarehouseFullError, match="待領取"):
        service.collect_craft(farmer, order_id)
    assert service.craft_orders(farmer)[0]["state"] == "ready"
    service.sell(farmer, "carrot", 1)
    assert service.collect_craft(farmer, order_id).quantity == 1


def test_craft_concurrency_and_duplicates(service, farmer, clock, session_factory):
    service.build(farmer, "磨坊")
    give(session_factory, farmer, "wheat", 30)
    results = run_concurrently(5, lambda: service.start_craft(farmer, "flour", 2))
    assert sum(1 for r in results if r[0] == "ok") == 1  # 一座設施只會成立一張訂單
    assert qty(service, farmer, "wheat") == 24  # 原料只扣一次
    clock.advance(3600)
    order_id = service.craft_orders(farmer)[0]["order_id"]
    results = run_concurrently(5, lambda: service.collect_craft(farmer, order_id))
    assert sum(1 for r in results if r[0] == "ok") == 1
    assert qty(service, farmer, "flour") == 2  # 成品只發放一次


def test_craft_failure_rolls_back_ingredients(service, farmer, session_factory, monkeypatch):
    service.build(farmer, "磨坊")
    give(session_factory, farmer, "wheat", 3)

    def boom(*args, **kwargs):
        raise RuntimeError("模擬建立訂單失敗")

    monkeypatch.setattr(ranch_repo, "add_order", boom)
    with pytest.raises(RuntimeError):
        service.start_craft(farmer, "flour", 1)
    monkeypatch.undo()
    assert qty(service, farmer, "wheat") == 3


def test_cannot_touch_other_players_orders(service, farmer, home_group, session_factory):
    other = make_farmer(service, "別人", home_group)
    set_gold(session_factory, other, 10_000)
    service.build(other, "磨坊")
    give(session_factory, other, "wheat", 3)
    service.start_craft(other, "flour", 1)
    order_id = service.craft_orders(other)[0]["order_id"]
    with pytest.raises(NotFoundError):
        service.collect_craft(farmer, order_id)


def test_craft_progress_survives_restart(service, farmer, clock, session_factory, database_url):
    service.build(farmer, "磨坊")
    give(session_factory, farmer, "wheat", 3)
    service.start_craft(farmer, "flour", 1)
    clock.advance(1000)
    engine = make_engine(database_url)
    try:
        restarted = SocialService(make_session_factory(engine), clock)
        assert restarted.craft_orders(farmer)[0]["remaining_seconds"] == 800
        clock.advance(800)
        assert restarted.craft_orders(farmer)[0]["state"] == "ready"
    finally:
        engine.dispose()


# ---------- 販售與排行 ----------


def test_goods_sales_and_leaderboards(service, farmer, home_group, session_factory):
    rival = make_farmer(service, "對手", home_group)
    give(session_factory, farmer, "egg", 4)
    give(session_factory, farmer, "bread", 1)
    give(session_factory, rival, "wheat", 3)
    before = gold(service, farmer)
    result = service.sell(farmer, "雞蛋", 4)
    assert result.gold_delta == 100 and gold(service, farmer) == before + 100
    service.sell(farmer, "麵包", 1)
    service.sell(rival, "wheat", 3)
    with pytest.raises(InsufficientItemsError):
        service.sell(farmer, "麵包", 1)
    with session_factory() as s:
        egg_tx = s.scalars(select(GameTransaction).where(GameTransaction.item_code == "egg")).one()
        assert (egg_tx.event_type, egg_tx.quantity, egg_tx.gold_delta) == ("sell", 4, 100)
        assert "單價 25" in egg_tx.description

    # 農產品販售排行只統計作物
    sales = {r["player_name"]: (r["sold_quantity"], r["sales_income"]) for r in service.leaderboard("sales")}
    assert sales == {"牧場主": (0, 0), "對手": (3, 45)}
    # 商品總販售納入動物產品與加工品，依收入排名
    goods = service.leaderboard("goods")
    assert [(r["player_name"], r["sold_quantity"], r["sales_income"]) for r in goods] == [
        ("牧場主", 5, 260),
        ("對手", 3, 45),
    ]


def test_backpack_shows_goods(service, farmer, session_factory):
    give(session_factory, farmer, "milk", 2)
    bp = service.get_backpack(farmer)
    assert not bp.is_empty
    assert [(g.name, g.quantity, g.price) for g in bp.goods] == [("牛奶", 2, 60)]


# ---------- 網頁監控 ----------


def test_monitor_ranch_api(service, farmer, clock, session_factory):
    service.build(farmer, "畜舍")
    service.build(farmer, "磨坊")
    service.buy_animals(farmer, "雞", 2)
    service.feed_animal(farmer, 1)
    give(session_factory, farmer, "wheat", 3)
    service.start_craft(farmer, "flour", 1)
    app = create_app(session_factory, clock=clock, allowed_hosts=["testserver"], admin_auth=AdminAuth(None, None))
    with TestClient(app) as c:
        [farm] = c.get("/api/monitor/ranch").json()
        assert farm["capacity"] == 4 and farm["animal_count"] == 2
        assert farm["state_counts"] == {"hungry": 1, "producing": 1, "ready": 0}
        assert farm["feed_cost_hungry"] == 20 and farm["next_ready_at"] is not None
        assert farm["buildings"] == {"畜舍": 1, "磨坊": 1}
        assert [a["state_label"] for a in farm["animals"]] == ["生產中", "待餵食"]
        [order] = c.get("/api/monitor/craft-orders").json()
        assert order["player_name"] == "牧場主" and order["inputs"] == {"小麥": 3}
        assert order["state_label"] == "加工中" and order["remaining_seconds"] == 1800
        assert c.post("/api/monitor/ranch", json={}).status_code == 405
        assert c.post("/api/monitor/craft-orders", json={}).status_code == 405
        assert c.get("/api/monitor/leaderboard", params={"kind": "goods"}).status_code == 200


# ---------- CMD ----------


def test_cli_ranch_and_craft_flow(service, farmer, clock, session_factory):
    cli = CommandHandler(service)
    assert "請先選擇玩家" in cli.handle("animal list")
    cli.handle(f"player use {farmer}")
    assert "雞" in cli.handle("animal shop") and "畜舍" in cli.handle("animal shop")
    assert "畜舍" in cli.handle("animal buy 雞 2")  # 尚未建造畜舍
    assert "已建造畜舍" in cli.handle("workshop build 畜舍")
    assert "已購買 2 隻雞" in cli.handle("animal buy 雞 2")
    assert "待餵食" in cli.handle("animal list")
    assert "已餵食 2 隻" in cli.handle("animal feed all")
    assert "生產中 2" in cli.handle("animal status")
    clock.advance(DAY)
    assert "收集了 2 個雞蛋" in cli.handle("animal collect 1")
    assert "已建造磨坊" in cli.handle("workshop build 磨坊")
    assert "閒置" in cli.handle("workshop")
    assert "小麥 x3 → 1 份麵粉" in cli.handle("recipe list")
    give(session_factory, farmer, "wheat", 3)
    out = cli.handle("craft 麵粉 1")
    order_id = out.split("#")[1].split("：")[0]
    assert "加工中" in cli.handle("craft status") and "加工中" in cli.handle("workshop")
    clock.advance(1800)
    assert "已領取" in cli.handle(f"craft collect {order_id}")
    assert "【動物產品與加工品】" in cli.handle("inventory items")
    assert "獲得 60 金幣" in cli.handle("sell 麵粉 1")
    assert "商品總販售" in cli.handle("leaderboard global goods")
    assert "用法" in cli.handle("craft")
