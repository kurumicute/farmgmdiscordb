"""澆水、乾枯、道具、永久工具、限時效果與背包。"""

import threading

import pytest
from sqlalchemy import func, select, update

from app.db.models import Farm, FarmTool, GameTransaction
from app.db.session import make_engine, make_session_factory
from app.game import balance
from app.game.errors import (
    AlreadyExistsError,
    InsufficientGoldError,
    InsufficientItemsError,
    PlotWitheredError,
    ValidationError,
)
from app.game.rules import PlotState
from app.services.social_service import SocialService
from tests.conftest import grow


def rich(session_factory, player_id, gold=10_000):
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == player_id).values(gold=gold))


def plot(service, pid, n=1):
    return service.get_farm_status(pid).plots[n - 1]


def test_withered_crop_cannot_be_harvested_and_can_be_cleared(service, player_id, clock):
    grow(service, player_id, 1, "wheat")
    clock.advance(60 + balance.WITHER_SECONDS)
    assert plot(service, player_id).state == PlotState.WITHERED
    with pytest.raises(PlotWitheredError):
        service.harvest_crop(player_id, 1)
    assert "恢復為空地" in service.clear_withered(player_id, 1).message
    assert plot(service, player_id).state == PlotState.EMPTY
    with pytest.raises(ValidationError):
        service.clear_withered(player_id, 1)


def test_fertilizer_reduces_time_once(service, player_id, clock):
    service.buy(player_id, "化肥", 2)
    assert service.get_farm_status(player_id).gold == 500 - 80
    service.buy_seeds(player_id, "strawberry", 1)
    service.plant_crop(player_id, 1, "strawberry")
    with pytest.raises(ValidationError):  # 等待澆水時不能施肥
        service.use_item(player_id, "fertilizer", 1)
    service.water_crop(player_id, 1)
    clock.advance(200)  # 剩 400 秒
    service.use_item(player_id, "fertilizer", 1)
    p = plot(service, player_id)
    assert p.remaining_seconds == 300 and p.fertilized  # 剩餘時間減少 25%
    with pytest.raises(ValidationError):  # 同一株不可重複施肥
        service.use_item(player_id, "fertilizer", 1)
    backpack = service.get_backpack(player_id)
    assert [(c.code, c.quantity) for c in backpack.consumables] == [("fertilizer", 1)]
    clock.advance(299)
    assert plot(service, player_id).state == PlotState.GROWING
    clock.advance(1)
    assert plot(service, player_id).state == PlotState.READY


def test_fertilizer_requires_item(service, player_id):
    grow(service, player_id, 1, "wheat")
    with pytest.raises(InsufficientItemsError):
        service.use_item(player_id, "fertilizer", 1)


def test_earthworm_effect_lifecycle_and_restart(service, player_id, clock, database_url):
    service.buy(player_id, "蚯蚓", 2)
    eff = service.get_backpack(player_id).effects[0]
    assert eff.status == "inactive"
    service.use_item(player_id, "earthworm")
    start = clock.now()
    grow(service, player_id, 1, "wheat")
    assert plot(service, player_id).remaining_seconds == 45  # 60 × 0.75

    # 程式重新啟動：新的 Engine 與服務，效果期限不重設
    clock.advance(600)
    engine = make_engine(database_url)
    try:
        restarted = SocialService(make_session_factory(engine), clock)
        eff = restarted.get_backpack(player_id).effects[0]
        assert eff.status == "active" and eff.started_at == start
        assert eff.remaining_seconds == balance.EARTHWORM_DURATION_SECONDS - 600
        restarted.use_item(player_id, "earthworm")  # 有效期間再次使用：延長
        eff = restarted.get_backpack(player_id).effects[0]
        assert eff.remaining_seconds == 2 * balance.EARTHWORM_DURATION_SECONDS - 600
        clock.advance(2 * balance.EARTHWORM_DURATION_SECONDS)
        eff = restarted.get_backpack(player_id).effects[0]
        assert eff.status == "expired" and eff.remaining_seconds == 0
        restarted.clear_withered(player_id, 1)  # 這麼久沒收成，小麥已乾枯
        grow(restarted, player_id, 1, "wheat")
        assert plot(restarted, player_id).remaining_seconds == 60  # 到期後不再加速
    finally:
        engine.dispose()


def test_auto_waterer_permanent_no_double_charge(service, player_id, clock, session_factory, database_url):
    with pytest.raises(InsufficientGoldError):
        service.buy(player_id, "auto_waterer", 1)
    rich(session_factory, player_id, 1000)
    with pytest.raises(ValidationError):
        service.buy(player_id, "auto_waterer", 2)
    service.buy(player_id, "自動澆水器", 1)
    assert service.get_farm_status(player_id).gold == 200
    with pytest.raises(AlreadyExistsError):
        service.buy(player_id, "auto_waterer", 1)
    assert service.get_farm_status(player_id).gold == 200

    service.buy_seeds(player_id, "wheat", 2)
    assert "已自動澆水" in service.plant_crop(player_id, 1, "wheat").message
    assert plot(service, player_id).state == PlotState.GROWING

    engine = make_engine(database_url)
    try:
        restarted = SocialService(make_session_factory(engine), clock)
        tool = restarted.get_backpack(player_id).tools[0]
        assert tool.owned and tool.enabled
        restarted.set_tool_enabled(player_id, "auto_waterer", False)
        restarted.plant_crop(player_id, 2, "wheat")
        assert plot(restarted, player_id, 2).state == PlotState.WAITING_WATER
    finally:
        engine.dispose()
    assert service.get_farm_status(player_id).gold == 180  # 重啟與查詢都沒有額外扣款


def test_concurrent_tool_purchase_charges_once(service, player_id, session_factory):
    rich(session_factory, player_id, 5000)
    barrier = threading.Barrier(5)
    errors = []

    def buy():
        barrier.wait()
        try:
            service.buy(player_id, "auto_waterer", 1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=buy) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 4 and all(isinstance(e, AlreadyExistsError) for e in errors)
    assert service.get_farm_status(player_id).gold == 5000 - 800
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmTool)) == 1
        assert s.scalar(select(func.count()).where(GameTransaction.event_type == "buy_item")) == 1


def test_backpack_contents(service, player_id, clock, session_factory):
    bp = service.get_backpack(player_id)
    assert bp.is_empty and bp.used == 0
    assert [(t.name, t.owned) for t in bp.tools] == [
        ("自動澆水器", False), ("自動播種機", False), ("自動販售機", False)
    ]

    rich(session_factory, player_id)
    service.buy_seeds(player_id, "carrot", 3)
    service.buy(player_id, "fertilizer", 2)
    service.buy(player_id, "auto_waterer", 1)
    service.plant_crop(player_id, 1, "carrot")  # 自動澆水
    clock.advance(180)
    service.harvest_crop(player_id, 1)

    bp = service.get_backpack(player_id)
    assert not bp.is_empty
    assert [(x.name, x.quantity, x.price) for x in bp.seeds] == [("胡蘿蔔種子", 2, 20)]
    assert [(x.name, x.quantity, x.price) for x in bp.produce] == [("胡蘿蔔", 3, 25)]
    assert [(x.name, x.quantity) for x in bp.consumables] == [("化肥", 2)]
    assert "減少 25%" in bp.consumables[0].description
    assert bp.tools[0].owned and bp.tools[0].enabled
    assert bp.used == 7
