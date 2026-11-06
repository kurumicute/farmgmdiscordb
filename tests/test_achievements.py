"""成就與稱號：歷史進度計算、領取一次、獎勵與交易紀錄不變式、稱號選用與顯示、面板操作。"""

import pytest
from sqlalchemy import func, select, update

from app.db.models import Farm, FarmAchievement, FarmPlot, GameTransaction
from app.discord_bot.panels import FarmPanel
from app.game import achievements, balance
from app.game.errors import NotFoundError, ValidationError
from app.services.admin_service import AdminService
from tests.conftest import make_farmer
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import cmds, make_player  # noqa: F401  cmds 是 fixture


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def pid(admin, home_group, session_factory):
    p = make_farmer(admin, "成就玩家", home_group)
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == p).values(gold=100_000, warehouse_capacity=10_000))
    return p


def harvest_wheat(admin, clock, session_factory, pid, *, quality="normal", rounds=1):
    for _ in range(rounds):
        admin.plant_all(pid, "wheat")
        admin.water_all(pid)
        with session_factory.begin() as s:
            s.execute(update(FarmPlot).where(FarmPlot.crop_type_id.is_not(None)).values(quality=quality))
        clock.advance(600)
        admin.harvest_all(pid)


def state(admin, pid, code):
    return next(r for r in admin.achievements(pid)["rows"] if r["code"] == code)


def reward_gold(session_factory):
    """成就獎勵寫入交易紀錄的金幣合計（測試前置直接改了金幣，所以比對這次增加的部分）。"""
    with session_factory() as s:
        return s.scalar(select(func.coalesce(func.sum(GameTransaction.gold_delta), 0))
                        .where(GameTransaction.event_type == "achievement_reward"))


def test_definitions_are_consistent():
    assert 30 <= len(achievements.ACHIEVEMENTS) <= 40
    assert all(a.description and achievements.reward_text(a) != "無" for a in achievements.ACHIEVEMENTS)


def test_progress_counts_history_and_claim_once(admin, pid, clock, session_factory):
    assert state(admin, pid, "crops_100")["state"] == "progress"
    harvest_wheat(admin, clock, session_factory, pid, rounds=3)  # 4 格 × 收成量 × 3 輪
    crops = state(admin, pid, "crops_100")
    if crops["value"] < 100:  # 收成量依作物設定，保險起見補到 100
        harvest_wheat(admin, clock, session_factory, pid, rounds=-(-(100 - crops["value"]) // 4))
    assert state(admin, pid, "crops_100")["state"] == "ready"
    assert state(admin, pid, "level_5")["state"] == "progress"
    data = admin.achievements(pid)
    assert data["ready"] >= 1 and data["claimed"] == 0

    before = admin.get_farm_status(pid).gold
    r = admin.claim_achievements(pid)
    assert "初出茅廬" in r.message
    assert admin.get_farm_status(pid).gold == before + 200 == before + reward_gold(session_factory)
    assert state(admin, pid, "crops_100")["state"] == "claimed"
    with pytest.raises(ValidationError, match="沒有可以領取"):
        admin.claim_achievements(pid)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmAchievement)) == data["ready"]


def test_title_from_achievement_and_display(admin, pid, clock, session_factory, home_group):
    harvest_wheat(admin, clock, session_factory, pid, quality="legendary")
    assert state(admin, pid, "legendary_crop")["state"] == "ready"
    admin.claim_achievements(pid)
    owned = admin.titles(pid)["owned"]
    assert "傳說農夫" in [t["title"] for t in owned]
    assert next(t for t in owned if t["title"] == "傳說農夫")["source"] == "傳說農夫"
    # 傳說農夫：巡查券 ×3 進背包
    inv = admin.get_inventory(pid)
    assert any(e.item_code == balance.ITEM_PATROL_TICKET and e.quantity == 3 for e in inv.items)

    with pytest.raises(NotFoundError):
        admin.set_title(pid, "不存在的稱號")
    admin.set_title(pid, "傳說農夫")
    assert admin.get_farm_status(pid).title == "傳說農夫"
    assert admin.player_titles([pid]) == {pid: "傳說農夫"}
    admin.set_title(pid, None)
    assert admin.get_farm_status(pid).title is None and admin.player_titles([pid]) == {}


def test_full_warehouse_converts_item_rewards(admin, pid, clock, session_factory):
    harvest_wheat(admin, clock, session_factory, pid, quality="legendary")
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == pid).values(warehouse_capacity=1))
    before = admin.get_farm_status(pid).gold
    r = admin.claim_achievements(pid)
    assert "改發等值金幣" in r.message
    gained = 3 * balance.ITEMS[balance.ITEM_PATROL_TICKET][1]
    assert admin.get_farm_status(pid).gold == before + gained == before + reward_gold(session_factory)


def test_panel_claim_and_choose_title(cmds, admin, clock, session_factory):  # noqa: F811
    p = make_player(cmds, ALICE)
    pid = p.panel.player_id
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == pid).values(gold=100_000, warehouse_capacity=10_000))
    harvest_wheat(admin, clock, session_factory, pid, quality="legendary")
    p.click("成就圖鑑")
    assert "成就與圖鑑" in p.text and "可領取" in p.text
    p.click("成就")
    p.choose("選擇成就分類", "farm")
    assert "傳說農夫" in p.text and "可以領取" in p.text
    p.click("領取全部")
    assert "領取" in p.text and "傳說農夫" in p.text
    p.click("稱號")
    p.choose("選擇要顯示的稱號", "傳說農夫")
    assert "已設定稱號" in p.text
    p.go("main")
    assert "〔傳說農夫〕" in p.text
    p.go("board")
    assert "〔傳說農夫〕" in p.text


def test_auto_checkin_feeds_streak_achievement(cmds, monkeypatch, admin, clock):  # noqa: F811
    monkeypatch.setattr(FarmPanel, "auto_checkin", True)
    p = make_player(cmds, ALICE)
    pid = p.panel.player_id
    for _ in range(6):
        clock.advance(24 * 3600)
        admin.daily_checkin(pid)
    assert state(admin, pid, "streak_7")["state"] == "ready"


def test_action_counts_come_from_farm_stats(session_factory, clock, home_group):
    """偷菜／賭場次數由 farm_stats 累計（不掃描交易紀錄），與交易紀錄重算一致。"""
    from app.repositories import farm_stats as farm_stats_repo
    from tests.conftest import grow

    game = AdminService(session_factory, clock, dice=lambda: 7)
    owner = make_farmer(game, "農夫", home_group)
    thief = make_farmer(game, "小偷", home_group)
    grow(game, owner, 1, "carrot")
    clock.advance(600)
    game.steal_crop(home_group, thief, owner, 1, 1)
    game.set_inventory("root", thief, "wheat", "rare", "none", 5)
    game.gamble(thief, "wheat", "rare", "none", 1, "big")
    game.gamble(thief, "wheat", "rare", "none", 1, "small")
    assert state(game, thief, "steal_20")["value"] == 1
    assert state(game, thief, "gamble_50")["value"] == 2
    with session_factory() as s:
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0
