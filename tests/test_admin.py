"""管理員功能：作物／動物種類、限時事件，以及調整玩家背包、土地、建築、動物與作物。"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import AdminAuth, hash_password
from app.db.models import AdminAction, GameTransaction, StealRecord
from app.game import balance, genetics
from app.game import events as event_rules
from app.game.errors import (
    AlreadyExistsError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.main import create_app
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer

ACTOR = "admin"
ADMIN = {"X-Farmgm-Admin": "1"}
PASSWORD = "test-password-123"

NEW_CROP = {
    "code": "pumpkin",
    "name": "南瓜",
    "seed_price": 30,
    "grow_seconds": 120,
    "harvest_quantity": 2,
    "sell_price": 40,
    "variant_code": "moon_pumpkin",
    "variant_name": "月光南瓜",
}
NEW_ANIMAL = {
    "code": "duck",
    "name": "鴨",
    "price": 300,
    "feed_cost": 10,
    "cycle_seconds": 3600,
    "product_code": "duck_egg",
    "product_name": "鴨蛋",
    "product_quantity": 3,
    "product_price": 15,
}


@pytest.fixture
def admin(session_factory, clock) -> AdminService:
    return AdminService(session_factory, clock)


@pytest.fixture
def farmer(admin) -> int:
    group = admin.create_group("主群組").id
    return make_farmer(admin, "阿明", group)


def once_event(clock, **effects) -> dict:
    now = clock.now()
    return {
        "name": effects.pop("name", "測試活動"),
        "schedule_type": "once",
        "starts_at": now - timedelta(hours=1),
        "ends_at": now + timedelta(hours=1),
        **effects,
    }


def txs(session_factory, event_type: str) -> list[GameTransaction]:
    with session_factory() as s:
        return list(s.scalars(select(GameTransaction).where(GameTransaction.event_type == event_type)))


def audit_actions(session_factory) -> list[str]:
    with session_factory() as s:
        return [a.action for a in s.scalars(select(AdminAction).order_by(AdminAction.id))]


# ---------- 純規則 ----------


class _Ev:
    def __init__(self, **kw):
        self.id, self.name, self.enabled, self.crop_type_id = kw.pop("id", 1), kw.pop("name", "e"), True, None
        self.starts_at = self.ends_at = self.start_minute = self.end_minute = self.weekdays = None
        self.schedule_type = "once"
        for k, (_label, _range, neutral, _cap) in event_rules.EFFECTS.items():
            setattr(self, k, neutral)
        for k, v in kw.items():
            setattr(self, k, v)


def test_event_schedule_rules():
    # 2026-01-01 是週四；UTC 02:00 = 台灣 10:00
    thu_10 = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    daily = _Ev(schedule_type="daily", start_minute=9 * 60, end_minute=12 * 60)
    assert event_rules.is_active(daily, thu_10)
    assert not event_rules.is_active(daily, thu_10 + timedelta(hours=2))  # 12:00 結束（不含）
    assert not event_rules.is_active(daily, thu_10 - timedelta(hours=1, seconds=1))
    weekly = _Ev(schedule_type="weekly", start_minute=0, end_minute=1440, weekdays=1 << 3)
    assert event_rules.is_active(weekly, thu_10)
    assert not event_rules.is_active(weekly, thu_10 + timedelta(days=1))
    # 有效期間限制與停用
    daily.ends_at = thu_10
    assert not event_rules.is_active(daily, thu_10)
    once = _Ev(starts_at=thu_10, ends_at=thu_10 + timedelta(hours=1))
    assert event_rules.is_active(once, thu_10) and not event_rules.is_active(once, thu_10 + timedelta(hours=1))
    once.enabled = False
    assert not event_rules.is_active(once, thu_10)


def test_event_bonus_stacking_caps_and_crop_filter():
    events = [_Ev(id=1, sell_price_pct=250), _Ev(id=2, sell_price_pct=200), _Ev(id=3, grow_reduction_pct=50)]
    sell = event_rules.bonus(events, "sell_price_pct", 7)
    assert sell.value == balance.EVENT_MAX_SELL_BONUS_PCT and [a["id"] for a in sell.applied] == [1, 2]
    assert event_rules.apply_sell_bonus(25, sell.value) == 75
    only_other = [_Ev(id=4, harvest_bonus_pct=50, crop_type_id=99)]
    assert event_rules.bonus(only_other, "harvest_bonus_pct", 7).value == 0
    assert event_rules.bonus(only_other, "harvest_bonus_pct", 99).value == 50
    assert event_rules.merge_applied(sell, event_rules.bonus(events, "grow_reduction_pct", 7)) == [
        {"id": 1, "name": "e"}, {"id": 2, "name": "e"}, {"id": 3, "name": "e"},
    ]
    assert event_rules.merge_applied(event_rules.Bonus()) is None


def test_event_validation():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="至少要設定一項"):
        event_rules.validate({"name": "空", "schedule_type": "once", "starts_at": now, "ends_at": now + timedelta(1)})
    with pytest.raises(ValidationError, match="結束時刻"):
        event_rules.validate({"name": "x", "schedule_type": "daily", "start_minute": 600, "end_minute": 600,
                              "sell_price_pct": 150})
    with pytest.raises(ValidationError, match="至少要選擇一天"):
        event_rules.validate({"name": "x", "schedule_type": "weekly", "start_minute": 0, "end_minute": 60,
                              "harvest_bonus_pct": 10})
    with pytest.raises(ValidationError, match="售價倍率"):
        event_rules.validate({"name": "x", "schedule_type": "once", "starts_at": now, "ends_at": now + timedelta(1),
                              "sell_price_pct": 500})


def test_genetics_event_formulas():
    settings = {**balance.DEFAULT_GENETICS}
    bp = genetics.effective_mutation_bp(settings, 800)
    assert sum(bp.values()) == sum(settings["mutation_bp"].values()) + 800
    assert genetics.effective_mutation_bp({**settings, "mutation_bp": dict.fromkeys(genetics.MUTATION_ORDER, 0)}, 800)[
        "giant"
    ] == 0
    q = genetics.effective_quality_bp(settings, 1, 3000)
    assert sum(q.values()) == 10_000 and q["normal"] == 4000
    assert genetics.effective_quality_bp(settings, 1, 99_999)["normal"] == 0  # 不會變成負數
    assert genetics.harvest_quantity(3, "giant", settings, 50) == 9
    assert genetics.grow_seconds(7200, "none", False, settings, 50) == 3600
    # 順序：事件減少 % 後再減快速生長秒數，最短 60 秒
    assert genetics.grow_seconds(7200, "fast", False, settings, 50) == balance.MIN_GROW_SECONDS
    assert genetics.grow_seconds(60, "none", True, settings, 50) == 45  # 蚯蚓後已低於下限時，事件不會拉長


# ---------- 限時事件整合 ----------


def test_sell_event_applies_and_is_recorded(admin, farmer, clock, session_factory):
    grow(admin, farmer, 1, "wheat")
    clock.advance(60)
    admin.harvest_crop(farmer, 1)
    wheat = admin.find_crop_info("wheat")
    event = admin.create_event(ACTOR, once_event(clock, name="豐收祭", sell_price_pct=150))
    admin.create_event(ACTOR, once_event(clock, name="停用活動", sell_price_pct=300, enabled=False))
    admin.create_event(ACTOR, {**once_event(clock, name="只限草莓", sell_price_pct=300), "crop_code": "strawberry"})
    result = admin.sell_crop(farmer, "wheat", 1)
    assert result.gold_delta == wheat["sell_price"] * 150 // 100
    sell = txs(session_factory, "sell")[0]
    assert sell.unit_price == 22 and sell.applied_events == [{"id": event["id"], "name": "豐收祭"}]
    assert "豐收祭" in sell.description
    # 停用後恢復原價
    admin.set_event_enabled(ACTOR, event["id"], False)
    assert admin.sell_crop(farmer, "wheat", 1).gold_delta == wheat["sell_price"]
    assert txs(session_factory, "sell")[1].applied_events is None


def test_plant_and_water_events(admin, farmer, clock, session_factory):
    admin.create_event(ACTOR, once_event(clock, name="大豐收", harvest_bonus_pct=100))
    admin.create_event(ACTOR, once_event(clock, name="生長加速", grow_reduction_pct=50))
    admin.buy_seeds(farmer, "carrot", 1)
    admin.plant_crop(farmer, 1, "carrot")
    admin.water_crop(farmer, 1)
    plot = admin.get_farm_status(farmer).plots[0]
    assert plot.remaining_quantity == 6  # 3 × (100% + 100%)
    assert plot.remaining_seconds == 90  # 180 秒 × 50%
    assert txs(session_factory, "plant")[0].applied_events == [{"id": 1, "name": "大豐收"}]
    assert txs(session_factory, "water")[0].applied_events == [{"id": 2, "name": "生長加速"}]
    # 事件結束後已決定的數量不變
    clock.advance(3 * 3600)
    assert admin.active_events() == []
    assert admin.get_farm_status(farmer).plots[0].remaining_quantity == 6


def test_event_crud_and_audit(admin, clock, session_factory):
    ev = admin.create_event(
        ACTOR,
        {"name": "週末", "schedule_type": "weekly", "start_minute": 600, "end_minute": 720, "weekdays": 0b1100000,
         "quality_bonus_bp": 500},
    )
    assert ev["schedule_text"] == "每週六、日 10:00–12:00" and ev["active_now"] is False
    changes = {"weekdays": 1 << 3, "start_minute": 480, "crop_code": "tomato"}
    updated = admin.update_event(ACTOR, ev["id"], {**ev, **changes})
    assert updated["active_now"] is True and updated["crop_type_id"] is not None  # 測試時鐘：週四 08:00（台灣）
    with pytest.raises(NotFoundError):
        admin.update_event(ACTOR, 999, ev)
    with pytest.raises(NotFoundError):
        admin.create_event(ACTOR, {**once_event(clock, harvest_bonus_pct=1), "crop_code": "nothing"})
    admin.delete_event(ACTOR, ev["id"])
    assert admin.active_events() == []
    assert audit_actions(session_factory) == ["create_event", "update_event", "delete_event"]


# ---------- 作物種類 ----------


def test_create_crop_full_lifecycle(admin, farmer, clock, session_factory):
    crop = admin.create_crop(ACTOR, NEW_CROP)
    assert crop["active"] is True
    assert "pumpkin" in [c.code for c in admin.get_market_prices()]
    admin.buy_seeds(farmer, "南瓜", 1)
    admin.plant_crop(farmer, 1, "pumpkin")
    admin.water_crop(farmer, 1)
    clock.advance(120)
    admin.harvest_crop(farmer, 1)
    assert admin.sell_crop(farmer, "南瓜", 2).gold_delta == 80
    # 珍稀變種使用資料庫中的代碼與名稱
    admin.set_inventory(ACTOR, farmer, "moon_pumpkin", "rare", "variant", 1)
    bp = admin.get_backpack(farmer)
    assert [(x.name, x.quality) for x in bp.produce] == [("月光南瓜", "rare")]
    assert admin.sell_crop(farmer, "月光南瓜", 1, "rare").gold_delta == 40 * 4 * 150 // 100


def test_crop_validation_and_duplicates(admin):
    with pytest.raises(AlreadyExistsError):
        admin.create_crop(ACTOR, {**NEW_CROP, "code": "wheat"})
    with pytest.raises(AlreadyExistsError):
        admin.create_crop(ACTOR, {**NEW_CROP, "variant_code": "egg"})  # 與動物產品重複
    with pytest.raises(AlreadyExistsError, match="小麥"):
        admin.create_crop(ACTOR, {**NEW_CROP, "name": "小麥"})
    for bad in ({"code": "Seed_x"}, {"code": "seed_pumpkin"}, {"grow_seconds": 10}, {"seed_price": 0},
                {"variant_code": "pumpkin"}):
        with pytest.raises(ValidationError):
            admin.create_crop(ACTOR, {**NEW_CROP, **bad})
    updated = admin.update_crop(ACTOR, "wheat", {**NEW_CROP, "name": "大麥", "variant_name": "金大麥"})
    assert updated["name"] == "大麥" and updated["code"] == "wheat" and updated["variant_code"] == "amber_wheat"


def test_disable_and_delete_crop(admin, farmer, clock, session_factory):
    grow(admin, farmer, 1, "tomato")
    admin.buy_seeds(farmer, "tomato", 1)
    admin.set_crop_active(ACTOR, "tomato", False)
    assert "tomato" not in [c.code for c in admin.get_market_prices()]
    with pytest.raises(ValidationError, match="停用"):
        admin.buy_seeds(farmer, "tomato", 1)
    with pytest.raises(ValidationError, match="停用"):
        admin.plant_crop(farmer, 2, "tomato")
    # 已種下與已持有的仍可收成、販售
    clock.advance(300)
    admin.harvest_crop(farmer, 1)
    assert admin.sell_crop(farmer, "tomato", 1).gold_delta == 20
    with pytest.raises(ValidationError, match="改為停用"):
        admin.delete_crop(ACTOR, "tomato")
    with pytest.raises(ValidationError, match="加工配方"):
        admin.delete_crop(ACTOR, "wheat")
    admin.create_crop(ACTOR, NEW_CROP)
    admin.delete_crop(ACTOR, "pumpkin")
    with pytest.raises(NotFoundError):
        admin.delete_crop(ACTOR, "pumpkin")
    assert audit_actions(session_factory) == ["disable_crop", "create_crop", "delete_crop"]


# ---------- 動物種類 ----------


def test_animal_type_lifecycle(admin, farmer, clock, session_factory):
    admin.create_animal_type(ACTOR, NEW_ANIMAL)
    admin.add_building(ACTOR, farmer, "barn")
    admin.buy_animals(farmer, "鴨", 1)
    admin.add_animals_for_player(ACTOR, farmer, "duck", 1)
    admin.feed_all(farmer)
    clock.advance(3600)
    admin.collect_animal(farmer, 1)
    assert admin.sell(farmer, "鴨蛋", 3).gold_delta == 45
    # 停用：不能再購買，已飼養的仍可餵食、收集
    admin.set_animal_type_active(ACTOR, "duck", False)
    with pytest.raises(NotFoundError, match="停用"):
        admin.buy_animals(farmer, "duck", 1)
    admin.feed_animal(farmer, 1)
    clock.advance(3600)
    admin.collect_animal(farmer, 2)
    with pytest.raises(ValidationError, match="改為停用"):
        admin.delete_animal_type(ACTOR, "duck")
    updated = admin.update_animal_type(ACTOR, "duck", {**NEW_ANIMAL, "product_price": 20})
    assert updated["product_price"] == 20 and admin.sell(farmer, "duck_egg", 1).gold_delta == 20
    with pytest.raises(AlreadyExistsError):
        admin.create_animal_type(ACTOR, {**NEW_ANIMAL, "code": "goose", "product_code": "wool"})
    admin.create_animal_type(ACTOR, {**NEW_ANIMAL, "code": "goose", "name": "鵝", "product_code": "goose_egg",
                                     "product_name": "鵝蛋"})
    admin.delete_animal_type(ACTOR, "goose")


def test_quest_item_filter_uses_db_products(admin, farmer, clock):
    admin.create_animal_type(ACTOR, NEW_ANIMAL)
    group = admin.get_home_group(farmer)[0]
    terms = dict(group_id=group, name="鴨蛋任務", quest_type="animal", target=10, reward_mode="fixed",
                 reward_type="gold", reward_amount=10, starts_at=clock.now(), ends_at=clock.now() + timedelta(days=1))
    quest = admin.create_quest(ACTOR, **terms, item_filter="鴨蛋")
    assert quest["item_filter"] == "duck_egg" and "鴨蛋" in quest["goal_text"]
    with pytest.raises(ValidationError):
        admin.create_quest(ACTOR, **terms, item_filter="nope")


# ---------- 調整玩家 ----------


def test_set_inventory_materials_use_material_bag(admin, farmer):
    from app.game import exploration

    cap = balance.INITIAL_WAREHOUSE_CAPACITY
    admin.set_inventory(ACTOR, farmer, "egg", "normal", "none", cap)  # 倉庫全滿
    admin.set_inventory(ACTOR, farmer, "wood", "normal", "none", 10)  # 素材放素材背包，不受倉庫限制
    assert admin.get_backpack(farmer).material_used == 10
    with pytest.raises(WarehouseFullError, match="素材背包"):
        admin.set_inventory(ACTOR, farmer, "stone", "normal", "none", exploration.MATERIAL_BAG_CAPACITY)


def test_set_inventory_add_edit_delete(admin, farmer, session_factory):
    admin.set_inventory(ACTOR, farmer, "fertilizer", "normal", "none", 3)
    admin.set_inventory(ACTOR, farmer, "wheat", "legendary", "golden", 2)
    admin.set_inventory(ACTOR, farmer, "fertilizer", "normal", "none", 1)
    assert admin.set_inventory(ACTOR, farmer, "fertilizer", "normal", "none", 1)["changed"] is False
    inv = {(i.item_code, i.display_name, i.quantity) for i in admin.get_inventory(farmer).items}
    assert inv == {("fertilizer", "化肥", 1), ("wheat", "小麥【傳說・黃金作物】", 2)}
    admin.set_inventory(ACTOR, farmer, "wheat", "legendary", "golden", 0)
    assert [i.item_code for i in admin.get_inventory(farmer).items] == ["fertilizer"]
    adjust = txs(session_factory, "admin_adjust")
    assert [t.quantity for t in adjust] == [3, 2, 2, 2] and all(t.gold_delta == 0 for t in adjust)
    assert "管理員調整" in adjust[0].description
    with pytest.raises(WarehouseFullError):
        admin.set_inventory(ACTOR, farmer, "egg", "normal", "none", balance.INITIAL_WAREHOUSE_CAPACITY)
    with pytest.raises(NotFoundError):
        admin.set_inventory(ACTOR, farmer, "gold_bar", "normal", "none", 1)
    with pytest.raises(ValidationError):
        admin.set_inventory(ACTOR, farmer, "seed_wheat", "rare", "none", 1)
    with pytest.raises(ValidationError):
        admin.set_inventory(ACTOR, farmer, "wheat", "normal", "variant", 1)
    with pytest.raises(ValidationError):
        admin.set_inventory(ACTOR, farmer, "amber_wheat", "normal", "none", 1)
    with pytest.raises(NotFoundError):
        admin.set_inventory(ACTOR, 999, "wheat", "normal", "none", 1)


def test_plots_add_delete_and_steal_history_kept(admin, clock, session_factory):
    group = admin.create_group("群").id
    thief, victim = make_farmer(admin, "小偷", group), make_farmer(admin, "苦主", group)
    grow(admin, victim, 4, "wheat")
    clock.advance(60)
    admin.steal_crop(group, thief, victim, 4, 1)
    with pytest.raises(ValidationError, match="還有作物"):
        admin.delete_plot(ACTOR, victim, 4)
    admin.clear_plot_for_player(ACTOR, victim, 4)
    admin.delete_plot(ACTOR, victim, 4)
    with session_factory() as s:
        record = s.scalars(select(StealRecord)).one()
        assert record.plot_id is None and record.plot_number == 4
    assert [p.plot_number for p in admin.get_farm_status(victim).plots] == [1, 2, 3]
    added = admin.add_plots(ACTOR, victim, 2)
    assert added["added"] == [4, 5] and added["total"] == 5
    with pytest.raises(ValidationError):
        admin.add_plots(ACTOR, victim, balance.ADMIN_MAX_PLOTS)
    with pytest.raises(NotFoundError):
        admin.delete_plot(ACTOR, victim, 99)


def test_plant_for_player_ready_now(admin, farmer, clock):
    result = admin.plant_for_player(ACTOR, farmer, 2, "strawberry", "rare", "giant", ready_now=True)
    assert result["quantity"] == 10
    harvested = admin.harvest_crop(farmer, 2)
    assert harvested.quantity == 10 and "稀有・巨型作物" in harvested.message
    admin.plant_for_player(ACTOR, farmer, 3, "wheat", watered=True)
    assert admin.get_farm_status(farmer).plots[2].state.value == "growing"
    admin.plant_for_player(ACTOR, farmer, 1, "wheat", "normal", "variant")
    with pytest.raises(ValidationError, match="已經有作物"):
        admin.plant_for_player(ACTOR, farmer, 1, "wheat")
    admin.water_crop(farmer, 1)
    clock.advance(60)
    assert admin.harvest_crop(farmer, 1).item_code == "amber_wheat"
    admin.set_crop_active(ACTOR, "carrot", False)
    with pytest.raises(ValidationError, match="停用"):
        admin.plant_for_player(ACTOR, farmer, 1, "carrot")
    with pytest.raises(ValidationError):
        admin.clear_plot_for_player(ACTOR, farmer, 1)


def test_buildings_and_animals(admin, farmer, clock, session_factory):
    admin.add_building(ACTOR, farmer, "barn")
    admin.add_animals_for_player(ACTOR, farmer, "chicken", 4)
    with pytest.raises(ValidationError, match="容量不足"):
        admin.add_animals_for_player(ACTOR, farmer, "cow", 1)
    with pytest.raises(ValidationError, match="請先刪除動物"):
        admin.delete_building(ACTOR, farmer, "barn", 1)
    admin.delete_animal_for_player(ACTOR, farmer, 4)
    assert len(admin.list_animals(farmer)) == 3
    with pytest.raises(NotFoundError):
        admin.delete_animal_for_player(ACTOR, farmer, 4)
    # 加工中的設施不能刪除
    admin.add_building(ACTOR, farmer, "mill")
    with pytest.raises(AlreadyExistsError):
        admin.add_building(ACTOR, farmer, "mill")
    admin.set_inventory(ACTOR, farmer, "wheat", "normal", "none", 3)
    admin.start_craft(farmer, "flour", 1)
    with pytest.raises(ValidationError, match="未領取"):
        admin.delete_building(ACTOR, farmer, "mill", 1)
    clock.advance(1800)
    admin.collect_craft(farmer, 1)
    admin.delete_building(ACTOR, farmer, "mill", 1)
    with pytest.raises(InsufficientItemsError):
        admin.sell(farmer, "flour", 2)
    assert {"add_building", "add_animals", "delete_animal", "delete_building"} <= set(audit_actions(session_factory))


def test_set_gold_and_level(admin, farmer, session_factory):
    result = admin.set_farm_values(ACTOR, farmer, gold=12345, level=5)
    assert result == {"gold": 12345, "level": 5, "changed": True}
    status = admin.get_farm_status(farmer)
    assert status.gold == 12345 and status.level == 5 and len(status.plots) == balance.INITIAL_PLOTS  # 土地不變
    # 品質加成依新等級計算
    assert admin.quality_table(farmer)["level"] == 5
    assert admin.set_farm_values(ACTOR, farmer, gold=12345)["changed"] is False
    admin.set_farm_values(ACTOR, farmer, gold=100)
    # 金幣差額記入交易紀錄，所有 gold_delta 加總仍等於目前金幣
    with session_factory() as s:
        total = sum(t.gold_delta for t in s.scalars(select(GameTransaction)))
    assert total == 100
    assert [t.gold_delta for t in txs(session_factory, "admin_adjust")] == [12345 - 500, 100 - 12345]
    for bad in ({"gold": -1}, {"level": 0}, {"level": balance.ADMIN_MAX_LEVEL + 1}, {}):
        with pytest.raises(ValidationError):
            admin.set_farm_values(ACTOR, farmer, **bad)
    with pytest.raises(NotFoundError):
        admin.set_farm_values(ACTOR, 999, gold=1)
    assert "set_farm_values" in audit_actions(session_factory)


def test_admin_adjust_does_not_score_quests(admin, farmer, clock):
    group = admin.get_home_group(farmer)[0]
    quest = admin.create_quest(
        ACTOR, group_id=group, name="收成任務", quest_type="harvest", target=5, reward_mode="fixed",
        reward_type="gold", reward_amount=10, starts_at=clock.now(), ends_at=clock.now() + timedelta(days=1),
    )
    admin.in_group_context(group).set_inventory(ACTOR, farmer, "wheat", "normal", "none", 10)
    assert admin.group_quests(group, farmer)[0]["progress"] == 0
    assert quest["progress"] == 0


# ---------- API ----------


@pytest.fixture
def client(session_factory, clock):
    app = create_app(
        session_factory,
        clock=clock,
        refresh_seconds=0.2,
        allowed_hosts=["testserver"],
        admin_auth=AdminAuth("admin", hash_password(PASSWORD)),
    )
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=ADMIN).status_code
        yield c


def test_admin_api_end_to_end(client, admin, farmer):
    res = client.post("/api/admin/crops", json=NEW_CROP, headers=ADMIN)
    assert res.status_code == 201
    assert client.post("/api/admin/crops", json=NEW_CROP, headers=ADMIN).status_code == 400
    assert client.post("/api/admin/crops", json={**NEW_CROP, "code": "Bad Code"}, headers=ADMIN).status_code == 422
    off = client.put("/api/admin/crops/pumpkin/active", json={"active": False}, headers=ADMIN)
    assert off.json()["active"] is False
    assert client.delete("/api/admin/crops/pumpkin", headers=ADMIN).status_code == 200
    assert client.post("/api/admin/animal-types", json=NEW_ANIMAL, headers=ADMIN).status_code == 201

    event = {
        "name": "日間特賣", "schedule_type": "daily", "start_time": "08:00", "end_time": "24:00",
        "sell_price_pct": 120, "crop_code": "wheat",
    }
    res = client.post("/api/admin/events", json=event, headers=ADMIN)
    assert res.status_code == 201 and res.json()["active_now"] is True  # 測試時鐘為台灣時間 08:00
    ev_id = res.json()["id"]
    assert client.post("/api/admin/events", json={**event, "end_time": "25:00"}, headers=ADMIN).status_code == 422
    assert client.post("/api/admin/events", json={**event, "end_time": "08:00"}, headers=ADMIN).status_code == 400
    weekly = {**event, "schedule_type": "weekly", "weekdays": [5, 6]}
    assert client.put(f"/api/admin/events/{ev_id}", json=weekly, headers=ADMIN).json()["weekdays"] == 0b1100000
    assert client.put(f"/api/admin/events/{ev_id}/enabled", json={"enabled": False}, headers=ADMIN).status_code == 200

    base = f"/api/admin/players/{farmer}"
    assert client.put(f"{base}/farm", json={"gold": 777, "level": 3}, headers=ADMIN).json()["gold"] == 777
    assert client.put(f"{base}/farm", json={"gold": -5}, headers=ADMIN).status_code == 422
    assert client.put(f"{base}/inventory", json={"item_code": "duck_egg", "quantity": 4}, headers=ADMIN).status_code \
        == 200
    assert client.post(f"{base}/plots", json={"count": 2}, headers=ADMIN).json()["total"] == 6
    assert client.delete(f"{base}/plots/6", headers=ADMIN).status_code == 200
    plant = {"crop_code": "tomato", "quality": "good", "ready_now": True}
    assert client.put(f"{base}/plots/5/crop", json=plant, headers=ADMIN).status_code == 200
    assert client.put(f"{base}/plots/5/crop", json=plant, headers=ADMIN).status_code == 400
    assert client.post(f"{base}/buildings", json={"building_code": "barn"}, headers=ADMIN).status_code == 201
    assert client.post(f"{base}/animals", json={"animal_code": "duck", "quantity": 2}, headers=ADMIN).status_code \
        == 201
    assert client.delete(f"{base}/animals/2", headers=ADMIN).status_code == 200
    assert client.delete(f"{base}/buildings/barn/1", headers=ADMIN).status_code == 400  # 還有 1 隻動物
    assert client.delete("/api/admin/players/999/plots/1", headers=ADMIN).status_code == 404

    farm = client.get(f"/api/monitor/players/{farmer}/admin-farm").json()
    assert [p["plot_number"] for p in farm["plots"]] == [1, 2, 3, 4, 5]
    assert farm["plots"][4]["state"] == "ready" and farm["plots"][4]["quality"] == "good"
    assert farm["gold"] == 777 and farm["level"] == 3
    assert farm["animal_capacity"] == 4 and [a["name"] for a in farm["animals"]] == ["鴨"]
    assert [(i["name"], i["quantity"]) for i in farm["inventory"]] == [("鴨蛋", 4)]
    assert client.get("/api/monitor/players/999/admin-farm").status_code == 404

    catalog = client.get("/api/monitor/catalog").json()
    assert "duck" in [a["code"] for a in catalog["animals"]]
    assert catalog["events"][0]["crop_code"] == "wheat" and catalog["events"][0]["enabled"] is False
    assert {"code": "duck_egg", "kind": "animal_product", "name": "鴨蛋"} in catalog["items"]
    assert client.delete(f"/api/admin/events/{ev_id}", headers=ADMIN).status_code == 200


def test_admin_transfer_group_api(client, admin):
    a = admin.create_group("A 群", "123456789012345678").id
    b = admin.create_group("B 群").id
    pid = admin.ensure_external_player("discord", "987654321098765432", "Discord 玩家")
    admin.create_farm(pid, home_group_id=a)
    base = f"/api/admin/players/{pid}"
    assert client.post(f"{base}/transfer-group", json={"group_id": b, "reason": ""}, headers=ADMIN).status_code == 422
    assert client.post(f"{base}/transfer-group", json={"group_id": b, "reason": "x"}).status_code == 403  # 缺少標頭
    res = client.post(f"{base}/transfer-group", json={"group_id": b, "reason": "客服"}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["home_group_id"] == b
    again = client.post(f"{base}/transfer-group", json={"group_id": a, "reason": "再轉"}, headers=ADMIN)
    assert again.status_code == 400 and "冷卻中" in again.json()["detail"]
    body = {"group_id": a, "reason": "再轉", "skip_cooldown": True}
    assert client.post(f"{base}/transfer-group", json=body, headers=ADMIN).json()["cooldown_bypassed"] is True

    farm = client.get(f"/api/monitor/players/{pid}/admin-farm").json()
    assert farm["home_group_id"] == a and farm["next_group_transfer_at"] is not None
    assert farm["external_user_id"] == "987654321098765432"  # Discord ID 以字串輸出，不會失去精度
    groups = client.get("/api/monitor/groups").json()
    assert any(g.get("discord_guild_id") == "123456789012345678" for g in groups)
