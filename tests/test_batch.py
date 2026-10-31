"""GameService.run_batch：面板一次讀取整頁資料時，多個服務呼叫共用同一筆交易。"""

from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

from app.discord_bot.runner import RetryingService
from app.game.errors import GameError, ValidationError


def db_error(state):
    return DBAPIError("stmt", {}, SimpleNamespace(sqlstate=state))


@pytest.fixture
def begins(engine):
    count = []
    listener = lambda _conn: count.append(1)  # noqa: E731
    event.listen(engine, "begin", listener)
    yield count
    event.remove(engine, "begin", listener)


def test_batch_uses_one_transaction(service, player_id, begins):
    def load():
        return (service.get_farm_status(player_id), service.get_backpack(player_id), service.active_events(),
                service.in_group_context(1).get_market_prices())

    begins.clear()
    service.get_farm_status(player_id)
    service.get_backpack(player_id)
    assert len(begins) == 2  # 不在共用交易中：各開一筆
    begins.clear()
    status, backpack, _events, prices = service.run_batch(load)
    assert len(begins) == 1 and status.plots and prices
    assert not service.in_batch()


def test_batch_error_rolls_back_everything(service, player_id):
    gold = service.get_farm_status(player_id).gold

    def buy_then_fail():
        service.buy_seeds(player_id, "wheat", 1)
        raise ValidationError("中途失敗")

    with pytest.raises(ValidationError):
        service.run_batch(buy_then_fail)
    assert service.get_farm_status(player_id).gold == gold  # 前面的購買也一起回滾


def test_batch_swallowed_error_is_not_committed(service, player_id):
    gold = service.get_farm_status(player_id).gold

    def swallow():
        service.buy_seeds(player_id, "wheat", 1)
        try:
            service.harvest_crop(player_id, 1)  # 空地：在交易中才發現錯誤
        except GameError:
            pass
        return "done"

    with pytest.raises(RuntimeError):
        service.run_batch(swallow)
    assert service.get_farm_status(player_id).gold == gold


def test_batch_retries_whole_function_on_deadlock(service, player_id):
    svc = RetryingService(service, sleep=lambda _s: None)
    calls = []

    def load():
        calls.append(1)
        svc.get_farm_status(player_id)  # 共用交易中：內層不自行重試
        if len(calls) == 1:
            raise db_error("40P01")
        return "ok"

    assert svc.run_batch(load) == "ok" and len(calls) == 2
