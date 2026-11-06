"""一鍵操作（種滿、全部澆水、全部收成、全部清除）在同一筆交易中完成。"""

import pytest
from sqlalchemy import func, select

from app.db.models import Farm, GameTransaction
from app.game import balance
from app.game.errors import InsufficientGoldError, ValidationError, WarehouseFullError
from app.game.rules import PlotState
from app.services.admin_service import AdminService


def states(service, pid):
    return [p.state for p in service.get_farm_status(pid).plots]


def test_plant_water_harvest_all(service, player_id, clock, session_factory):
    result = service.plant_all(player_id, "wheat")
    assert "在 4 格種下小麥（購買 4 顆種子）" in result.message
    assert states(service, player_id) == [PlotState.WAITING_WATER] * 4
    assert "已為 4 格澆水" in service.water_all(player_id).message
    clock.advance(61)
    assert "收成 4 格，共 8 個作物" in service.harvest_all(player_id).message
    assert states(service, player_id) == [PlotState.EMPTY] * 4
    with pytest.raises(ValidationError):
        service.harvest_all(player_id)
    with session_factory() as s:
        assert s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))


def test_plant_all_limited_by_gold(service, player_id, session_factory, clock):
    AdminService(session_factory, clock).set_farm_values("root", player_id, gold=25)
    assert "金幣不夠，還有 2 格空著" in service.plant_all(player_id, "wheat").message
    AdminService(session_factory, clock).set_farm_values("root", player_id, gold=0)
    with pytest.raises(InsufficientGoldError):
        service.plant_all(player_id, "carrot")


def test_harvest_all_stops_when_warehouse_full(service, player_id, session_factory, clock):
    admin = AdminService(session_factory, clock)
    service.plant_all(player_id, "strawberry")  # 每格 5 個
    service.water_all(player_id)
    clock.advance(600)
    admin.set_inventory("root", player_id, "seed_wheat", "normal", "none", balance.INITIAL_WAREHOUSE_CAPACITY - 12)
    message = service.harvest_all(player_id).message
    assert "收成 2 格，共 10 個作物" in message and "還有 2 格沒收" in message
    assert states(service, player_id).count(PlotState.READY) == 2  # 放不下的留在農田，不會遺失
    admin.set_inventory("root", player_id, "seed_wheat", "normal", "none", balance.INITIAL_WAREHOUSE_CAPACITY - 10)
    with pytest.raises(WarehouseFullError):
        service.harvest_all(player_id)


def test_clear_all_withered(service, player_id, clock):
    service.plant_all(player_id, "wheat")
    service.water_all(player_id)
    clock.advance(60 + balance.WITHER_SECONDS + 1)
    assert "已清除 4 格" in service.clear_all_withered(player_id).message
    assert states(service, player_id) == [PlotState.EMPTY] * 4


def test_harvest_all_quest_completes_mid_loop(session_factory, clock, home_group, player_id):
    """一鍵收成時任務清單只查一次：中途達標完成後，後面幾格不再計入（與逐格重查的結果相同）。"""
    from datetime import timedelta

    admin = AdminService(session_factory, clock)
    admin.create_quest("root", group_id=home_group, name="收成任務", quest_type="harvest", target=3,
                       reward_mode="fixed", reward_type="gold", reward_amount=10,
                       starts_at=clock.now(), ends_at=clock.now() + timedelta(days=1))
    svc = admin.in_group_context(home_group)
    svc.plant_all(player_id, "wheat")  # 4 格，每格 2 個
    svc.water_all(player_id)
    clock.advance(61)
    assert svc.harvest_all(player_id).quantity == 8
    quest = admin.group_quests(home_group, player_id)[0]
    assert quest["completed_at"] is not None and quest["progress"] == 4  # 第 2 格達標，第 3、4 格不計
