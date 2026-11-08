"""每日簽到：台灣日期、連續天數、7 天一輪的獎勵、倉庫滿改發金幣、交易紀錄不變式與面板自動簽到。"""

import pytest
from sqlalchemy import func, select

from app.db.models import DailyCheckin, Farm, GameTransaction
from app.discord_bot.panels import FarmPanel
from app.game import balance, checkin
from app.services.admin_service import AdminService
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import cmds, make_player  # noqa: F401  cmds 是 fixture

DAY = 24 * 3600


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def pid(admin):
    gid = admin.create_group("主群組").id
    player = admin.create_player("阿明")
    admin.create_farm(player.id, home_group_id=gid)
    return player.id


def gold(session_factory, pid):
    with session_factory() as s:
        return s.scalar(select(Farm.gold).where(Farm.player_id == pid))


def assert_gold_matches_transactions(session_factory):
    with session_factory() as s:
        assert s.scalar(select(func.sum(GameTransaction.gold_delta))) == s.scalar(select(func.sum(Farm.gold)))


def test_first_checkin_and_once_per_day(admin, pid, clock, session_factory):
    r = admin.daily_checkin(pid)
    assert r["new"] and r["streak"] == 1 and r["reward_text"] == "100 金幣"
    assert gold(session_factory, pid) == balance.INITIAL_GOLD + 100
    # 同一天（台灣時間 08:00 → 23:59）再呼叫不會重複發獎
    clock.advance(15 * 3600 + 59 * 60)
    again = admin.daily_checkin(pid)
    assert again["new"] is False and again["next_at"] == r["next_at"]
    assert gold(session_factory, pid) == balance.INITIAL_GOLD + 100
    status = admin.checkin_status(pid)
    assert status["checked_today"] and status["streak"] == 1
    assert status["next_reward_text"] == checkin.reward_text(checkin.rewards()[1])
    # 台灣時間跨過 00:00 就是新的一天
    clock.advance(60)
    assert admin.checkin_status(pid)["checked_today"] is False
    r2 = admin.daily_checkin(pid)
    assert r2["new"] and r2["streak"] == 2
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(DailyCheckin)) == 2
    inv = admin.get_inventory(pid)
    assert any(e.item_code == balance.ITEM_FERTILIZER and e.quantity == 3 for e in inv.items)
    assert_gold_matches_transactions(session_factory)


def test_streak_resets_after_missing_a_day_and_cycles(admin, pid, clock):
    for day in range(1, 9):
        r = admin.daily_checkin(pid)
        assert r["streak"] == day
        assert r["reward_text"] == checkin.reward_text(checkin.rewards()[(day - 1) % 7])
        clock.advance(DAY)
    clock.advance(DAY)  # 中斷一天
    assert admin.checkin_status(pid)["streak"] == 0
    assert admin.daily_checkin(pid)["streak"] == 1


def test_full_warehouse_converts_items_to_gold(admin, pid, clock, session_factory):
    admin.daily_checkin(pid)
    clock.advance(DAY)
    with session_factory.begin() as s:
        s.query(Farm).filter(Farm.player_id == pid).update({"warehouse_capacity": 1})
    before = gold(session_factory, pid)
    r = admin.daily_checkin(pid)  # 第 2 天：化肥 ×3 放不下 → 3 × 化肥價格
    assert r["converted"] and r["reward_text"] == f"{3 * checkin.item_gold_value(balance.ITEM_FERTILIZER)} 金幣"
    assert gold(session_factory, pid) == before + 3 * checkin.item_gold_value(balance.ITEM_FERTILIZER)
    assert_gold_matches_transactions(session_factory)


def test_no_farm_returns_none(admin):
    player = admin.create_player("沒農場")
    assert admin.daily_checkin(player.id) is None


def test_panel_auto_checkin_once(cmds, monkeypatch, session_factory):  # noqa: F811
    monkeypatch.setattr(FarmPanel, "auto_checkin", True)
    p = make_player(cmds, ALICE)
    assert "每日簽到成功" in p.text and "連續第 **1** 天" in p.text
    assert "每日簽到" not in p.text.split("每日簽到成功", 1)[1]  # 總覽不再有簽到欄位（移到成就圖鑑）
    p.go("plots")
    p.go("main")
    assert "每日簽到成功" not in p.text
    p.click("成就圖鑑")
    assert "今天已簽到" in p.text and "連續 **1** 天" in p.text
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(DailyCheckin)) == 1
