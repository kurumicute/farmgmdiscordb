"""自動播種機、自動販售機、倉庫擴大卷，以及免澆水／大風颳走活動。"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import Farm, GameTransaction
from app.game import balance
from app.game.errors import InsufficientGoldError, ValidationError
from app.game.rules import PlotState
from app.repositories import farm_stats as farm_stats_repo
from app.services.admin_service import AdminService
from tests.conftest import grow


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def rich(admin, player_id):
    admin.set_farm_values("root", player_id, gold=5_000_000)
    return player_id


def states(service, pid):
    return [(p.state, p.crop_code) for p in service.get_farm_status(pid).plots]


def invariant(session_factory):
    with session_factory() as s:
        assert s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0


def test_auto_planter_fills_empty_plots_from_backpack(service, rich, clock, session_factory):
    service.buy_seeds(rich, "carrot", 2)
    service.buy_seeds(rich, "wheat", 1)
    result = service.buy_item(rich, balance.TOOL_AUTO_PLANTER)
    assert "自動播種機種下 3 格" in result.message
    planted = [code for state, code in states(service, rich) if state != PlotState.EMPTY]
    assert sorted(planted) == ["carrot", "carrot", "wheat"]  # 只會用背包裡的種子
    assert service.get_backpack(rich).seeds == []
    # 收成後立即補種；沒有種子時說明原因，空地保留
    service.water_crop(rich, 1)
    clock.advance(700)  # 測試作物 60–180 秒成熟、成熟 30 分鐘後才乾枯
    message = service.harvest_crop(rich, 1).message
    assert "沒有種子可以種" in message
    service.buy_seeds(rich, "tomato", 5)  # 買種子也會觸發，把 2 格空地種滿
    assert all(state != PlotState.EMPTY for state, _c in states(service, rich))
    assert sum(x.quantity for x in service.get_backpack(rich).seeds) == 3
    # 停用後不再自動播種
    service.set_tool_enabled(rich, "auto_planter", False)
    invariant(session_factory)


def test_auto_seller_sells_harvest_without_using_warehouse(service, rich, clock, session_factory):
    service.buy_item(rich, balance.TOOL_AUTO_SELLER)
    gold = service.get_farm_status(rich).gold
    grow(service, rich, 1, "carrot")
    clock.advance(180)
    message = service.harvest_crop(rich, 1).message
    assert "自動販售機賣出（單價 25），金幣 +75" in message
    assert service.get_farm_status(rich).gold == gold - 20 + 75
    assert service.get_backpack(rich).produce == []
    with session_factory() as s:
        sell = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "sell")).one()
        assert sell.description.startswith("自動販售機：") and sell.unit_price == 25
    invariant(session_factory)


def test_auto_seller_ignores_full_warehouse(service, admin, rich, clock):
    service.buy_item(rich, balance.TOOL_AUTO_SELLER)
    grow(service, rich, 1, "wheat")
    admin.set_inventory("root", rich, "seed_carrot", "normal", "none", balance.INITIAL_WAREHOUSE_CAPACITY)
    clock.advance(60)
    assert "自動販售機賣出" in service.harvest_crop(rich, 1).message


def test_warehouse_expansion_prices_and_cap(service, rich, session_factory):
    paid = []
    for _ in range(5):
        before = service.get_farm_status(rich).gold
        service.expand_warehouse(rich)
        paid.append(before - service.get_farm_status(rich).gold)
    assert paid == [4_000, 20_000, 50_000, 100_000, 200_000]
    assert service.get_farm_status(rich).warehouse_capacity == 300
    with pytest.raises(ValidationError, match="最大容量"):
        service.expand_warehouse(rich)
    invariant(session_factory)


def test_warehouse_expansion_needs_gold(service, player_id):
    with pytest.raises(InsufficientGoldError):
        service.expand_warehouse(player_id)
    assert service.get_farm_status(player_id).warehouse_capacity == 50


def event(admin, clock, **effects):
    return admin.create_event("root", {
        "name": "測試活動", "schedule_type": "once", "starts_at": clock.now() - timedelta(hours=1),
        "ends_at": clock.now() + timedelta(days=1), **effects,
    })


def test_free_water_event_starts_growing_on_plant(service, admin, player_id, clock):
    view = event(admin, clock, free_water=1)
    assert "免澆水" in view["effects_text"]
    service.buy_seeds(player_id, "wheat", 1)
    assert "已自動澆水" in service.plant_crop(player_id, 1, "wheat").message
    assert states(service, player_id)[0] == (PlotState.GROWING, "wheat")


def test_wind_event_blows_away_but_keeps_one(service, admin, player_id, clock, session_factory):
    event(admin, clock, harvest_loss=1)
    grow(service, player_id, 1, "carrot")
    grow(service, player_id, 2, "carrot")
    clock.advance(180)
    message = service.harvest_crop(player_id, 1).message
    assert "大風颳走 1 個" in message and "獲得 2 個" in message
    admin.create_event("root", {"name": "超強風", "schedule_type": "once", "harvest_loss": 10,
                                "starts_at": clock.now() - timedelta(hours=1), "ends_at": clock.now() + timedelta(1)})
    assert "獲得 1 個" in service.harvest_crop(player_id, 2).message  # 至少保留 1 個
    invariant(session_factory)


def test_new_event_fields_validated(admin, clock):
    with pytest.raises(ValidationError):
        event(admin, clock, harvest_loss=11)
    with pytest.raises(ValidationError):
        event(admin, clock, free_water=2)


def test_earthworm_lasts_three_days(service, player_id, clock):
    service.buy(player_id, "蚯蚓", 1)
    assert "剩餘 3 天" in service.use_item(player_id, "earthworm").message
