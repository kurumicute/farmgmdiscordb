import threading

import pytest
from sqlalchemy import func, select, update

from app.db.models import Farm, FarmPlot, GameTransaction, InventoryItem
from app.game import balance
from app.game.errors import (
    AlreadyExistsError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    PlotEmptyError,
    PlotNotReadyError,
    PlotOccupiedError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import PlotState
from app.repositories import transactions as tx_repo
from app.services.game_service import GameService


def plant_watered(service, pid, plot, crop):
    service.plant_crop(pid, plot, crop)
    return service.water_crop(pid, plot)


def qty(service, pid, code):
    return next((i.quantity for i in service.get_inventory(pid).items if i.item_code == code), 0)


def test_create_player_and_farm(service, home_group):
    player = service.create_player("小花")
    status = service.create_farm(player.id, home_group_id=home_group)
    assert status.home_group_id == home_group
    assert status.level == 1
    assert status.gold == balance.INITIAL_GOLD == 500
    assert len(status.plots) == 4 and status.empty_plots == 4
    assert status.warehouse_capacity == 50 and status.warehouse_used == 0
    assert service.get_inventory(player.id).items == []
    with pytest.raises(AlreadyExistsError):
        service.create_farm(player.id, home_group_id=home_group)


def test_operations_require_farm(service):
    player = service.create_player("沒農場")
    with pytest.raises(NotFoundError):
        service.buy_seeds(player.id, "wheat", 1)
    with pytest.raises(NotFoundError):
        service.get_farm_status(999)


def test_buy_seeds_updates_gold_and_inventory(service, player_id):
    result = service.buy_seeds(player_id, "wheat", 5)
    assert result.gold == 450 and result.gold_delta == -50
    assert qty(service, player_id, "seed_wheat") == 5
    service.buy_seeds(player_id, "小麥", 1)  # 中文名稱也可以
    assert qty(service, player_id, "seed_wheat") == 6


def test_buy_without_enough_gold_fails_and_changes_nothing(service, player_id):
    with pytest.raises(InsufficientGoldError):
        service.buy_seeds(player_id, "strawberry", 9)  # 540 > 500
    assert service.get_farm_status(player_id).gold == 500
    assert service.get_inventory(player_id).items == []


def test_invalid_input(service, player_id):
    with pytest.raises(ValidationError):
        service.buy_seeds(player_id, "wheat", 0)
    with pytest.raises(NotFoundError):
        service.buy_seeds(player_id, "banana", 1)
    with pytest.raises(NotFoundError):
        plant_watered(service, player_id, 99, "wheat")


def test_plant_on_empty_plot(service, player_id, clock):
    service.buy_seeds(player_id, "wheat", 1)
    service.plant_crop(player_id, 1, "wheat")
    plot = service.get_farm_status(player_id).plots[0]
    assert plot.state == PlotState.WAITING_WATER and plot.remaining_quantity == 2
    clock.advance(600)  # 未澆水不會生長
    assert service.get_farm_status(player_id).plots[0].state == PlotState.WAITING_WATER
    service.water_crop(player_id, 1)
    plot = service.get_farm_status(player_id).plots[0]
    assert plot.state == PlotState.GROWING and plot.crop_code == "wheat"
    assert plot.watered_at == clock.now()
    assert plot.remaining_seconds == 60
    assert qty(service, player_id, "seed_wheat") == 0


def test_plant_without_seed_fails(service, player_id):
    with pytest.raises(InsufficientItemsError):
        plant_watered(service, player_id, 1, "wheat")


def test_cannot_plant_occupied_plot(service, player_id):
    service.buy_seeds(player_id, "wheat", 2)
    plant_watered(service, player_id, 1, "wheat")
    with pytest.raises(PlotOccupiedError):
        plant_watered(service, player_id, 1, "wheat")
    assert qty(service, player_id, "seed_wheat") == 1  # 失敗不扣種子


def test_cannot_harvest_unripe_or_empty(service, player_id, clock):
    with pytest.raises(PlotEmptyError):
        service.harvest_crop(player_id, 1)
    service.buy_seeds(player_id, "carrot", 1)
    plant_watered(service, player_id, 1, "carrot")
    clock.advance(179)
    with pytest.raises(PlotNotReadyError):
        service.harvest_crop(player_id, 1)


def test_harvest_only_once(service, player_id, clock):
    service.buy_seeds(player_id, "wheat", 1)
    plant_watered(service, player_id, 1, "wheat")
    clock.advance(60)
    result = service.harvest_crop(player_id, 1)
    assert result.quantity == 2
    assert qty(service, player_id, "wheat") == 2
    with pytest.raises(PlotEmptyError):
        service.harvest_crop(player_id, 1)
    assert qty(service, player_id, "wheat") == 2
    assert service.get_farm_status(player_id).plots[0].state == PlotState.EMPTY


def test_buy_beyond_warehouse_capacity_fails(service, player_id, session_factory):
    with session_factory.begin() as s:
        s.execute(update(Farm).values(gold=10_000))
    service.buy_seeds(player_id, "wheat", 49)  # 倉庫 49/50
    with pytest.raises(WarehouseFullError):
        service.buy_seeds(player_id, "wheat", 2)
    assert service.get_farm_status(player_id).gold == 10_000 - 490
    assert qty(service, player_id, "seed_wheat") == 49


def test_warehouse_full_keeps_crop_on_plot(service, player_id, clock):
    service.buy_seeds(player_id, "wheat", 1)
    plant_watered(service, player_id, 1, "wheat")
    service.buy_seeds(player_id, "wheat", 49)  # 倉庫 49/50，金幣剛好用完
    clock.advance(60)
    with pytest.raises(WarehouseFullError):
        service.harvest_crop(player_id, 1)  # 收成 2 個放不下
    plot = service.get_farm_status(player_id).plots[0]
    assert plot.state == PlotState.READY  # 作物仍在土地上，沒有遺失
    assert qty(service, player_id, "wheat") == 0
    plant_watered(service, player_id, 2, "wheat")  # 用掉 1 顆種子騰出空間 → 48/50
    service.harvest_crop(player_id, 1)
    assert qty(service, player_id, "wheat") == 2
    assert service.get_inventory(player_id).used == 50


def test_sell_updates_gold_and_inventory(service, player_id, clock):
    service.buy_seeds(player_id, "wheat", 1)  # 490
    plant_watered(service, player_id, 1, "wheat")
    clock.advance(60)
    service.harvest_crop(player_id, 1)
    with pytest.raises(InsufficientItemsError):
        service.sell_crop(player_id, "wheat", 3)
    result = service.sell_crop(player_id, "wheat", 2)
    assert result.gold == 490 + 30 and result.gold_delta == 30
    assert qty(service, player_id, "wheat") == 0


def test_upgrade_adds_plots(service, player_id):
    result = service.upgrade_farm(player_id)  # 第 5 格 500 金幣
    status = service.get_farm_status(player_id)
    assert result.gold == 0 and status.gold == 0
    assert "下一次擴建需要 1000 金幣" in result.message
    assert status.level == 2
    assert [p.plot_number for p in status.plots] == [1, 2, 3, 4, 5]
    with pytest.raises(InsufficientGoldError, match="1000"):
        service.upgrade_farm(player_id)
    assert len(service.get_farm_status(player_id).plots) == 5


def test_upgrade_price_ladder():
    assert [balance.upgrade_price(n) for n in range(4, 17)] == [*balance.UPGRADE_PRICES, None]
    assert balance.upgrade_price(1) == balance.UPGRADE_PRICES[0]  # 管理員刪減後低於初始格數
    assert len(balance.UPGRADE_PRICES) == balance.MAX_PLOTS - balance.INITIAL_PLOTS


def test_upgrade_stops_at_max_plots(service, player_id, session_factory):
    with session_factory() as s:
        s.execute(update(Farm).values(gold=1_000_000))
        s.commit()
    while len(service.get_farm_status(player_id).plots) < balance.MAX_PLOTS:
        service.upgrade_farm(player_id)
    before = service.get_farm_status(player_id)
    assert len(before.plots) == balance.MAX_PLOTS
    assert before.gold == 1_000_000 - sum(balance.UPGRADE_PRICES)
    assert before.level == 1 + len(balance.UPGRADE_PRICES)
    with session_factory() as s:
        records = s.scalar(select(func.count()).select_from(GameTransaction))
    with pytest.raises(ValidationError, match="上限"):
        service.upgrade_farm(player_id)
    after = service.get_farm_status(player_id)
    assert (after.gold, after.level, len(after.plots)) == (before.gold, before.level, balance.MAX_PLOTS)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(GameTransaction)) == records


def test_failure_mid_transaction_rolls_back(service, player_id, monkeypatch, session_factory):
    def boom(*args, **kwargs):
        raise RuntimeError("模擬寫入交易紀錄失敗")

    monkeypatch.setattr(tx_repo, "add", boom)
    with pytest.raises(RuntimeError):
        service.buy_seeds(player_id, "wheat", 5)
    with pytest.raises(RuntimeError):
        service.upgrade_farm(player_id)
    monkeypatch.undo()
    status = service.get_farm_status(player_id)
    assert status.gold == 500 and len(status.plots) == 4 and status.level == 1
    assert service.get_inventory(player_id).items == []
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(GameTransaction)) == 1  # 只有建立農場


def _run_concurrently(n, fn):
    barrier = threading.Barrier(n)
    results = []

    def worker():
        barrier.wait()
        try:
            results.append(("ok", fn()))
        except Exception as exc:  # noqa: BLE001 - 收集所有結果
            results.append(("err", exc))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_harvest_only_once(service, player_id, clock, session_factory):
    service.buy_seeds(player_id, "wheat", 1)
    plant_watered(service, player_id, 1, "wheat")
    clock.advance(60)
    results = _run_concurrently(8, lambda: service.harvest_crop(player_id, 1))
    oks = [r for r in results if r[0] == "ok"]
    errs = [r[1] for r in results if r[0] == "err"]
    assert len(oks) == 1
    assert all(isinstance(e, PlotEmptyError) for e in errs)
    assert qty(service, player_id, "wheat") == 2
    with session_factory() as s:
        assert s.scalar(select(func.count()).where(GameTransaction.event_type == "harvest")) == 1


def test_concurrent_purchases_never_overdraw(service, player_id, session_factory):
    # 每次 60 金幣，500 金幣最多只能成功 8 次
    results = _run_concurrently(12, lambda: service.buy_seeds(player_id, "strawberry", 1))
    oks = [r for r in results if r[0] == "ok"]
    errs = [r[1] for r in results if r[0] == "err"]
    assert len(oks) == 8
    assert all(isinstance(e, InsufficientGoldError) for e in errs)
    status = service.get_farm_status(player_id)
    assert status.gold == 500 - 8 * 60
    assert qty(service, player_id, "seed_strawberry") == 8


def test_concurrent_plant_same_plot(service, player_id):
    service.buy_seeds(player_id, "wheat", 5)
    results = _run_concurrently(5, lambda: plant_watered(service, player_id, 1, "wheat"))
    assert sum(1 for r in results if r[0] == "ok") == 1
    assert all(isinstance(r[1], PlotOccupiedError) for r in results if r[0] == "err")
    assert qty(service, player_id, "seed_wheat") == 4


def test_crop_state_restored_after_restart(service, player_id, clock, session_factory, database_url):
    from app.db.session import make_engine, make_session_factory

    service.buy_seeds(player_id, "tomato", 1)
    plant_watered(service, player_id, 3, "tomato")
    planted_at = clock.now()

    # 模擬程式重新啟動：全新的 Engine 與服務實例
    engine = make_engine(database_url)
    try:
        restarted = GameService(make_session_factory(engine), clock)
        clock.advance(120)
        plot = restarted.get_farm_status(player_id).plots[2]
        assert plot.state == PlotState.GROWING and plot.remaining_seconds == 180
        assert plot.planted_at == planted_at
        clock.advance(180)
        assert restarted.get_farm_status(player_id).plots[2].state == PlotState.READY
        assert restarted.harvest_crop(player_id, 3).quantity == 4
    finally:
        engine.dispose()


def test_gold_equals_sum_of_transaction_deltas(service, player_id, clock, session_factory):
    service.buy_seeds(player_id, "wheat", 3)
    plant_watered(service, player_id, 1, "wheat")
    clock.advance(60)
    service.harvest_crop(player_id, 1)
    service.sell_crop(player_id, "wheat", 2)
    with session_factory() as s:
        gold = s.scalar(select(Farm.gold))
        delta_sum = s.scalar(select(func.sum(GameTransaction.gold_delta)))
        assert gold == delta_sum == 500 - 30 + 30
        events = list(s.scalars(select(GameTransaction.event_type).order_by(GameTransaction.id)))
        assert events == ["create_farm", "buy_seed", "plant", "water", "harvest", "sell"]
        assert s.scalar(select(func.count()).select_from(FarmPlot)) == 4
        assert s.scalar(select(func.sum(InventoryItem.quantity))) == 2  # 2 顆種子
