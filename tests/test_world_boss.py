"""世界 Boss（群組後勤活動）：查看、捐獻（作物／漁獲／素材）、數量檢查、重複提交、群組隔離、排行榜、
HP 與補給進度、活動結束、領獎、管理網站、介紹網站、Bot 面板與重啟後資料保留。"""

import asyncio
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth import AdminAuth, hash_password
from app.clock import TAIPEI_TZ
from app.db.models import FarmFish, GameTransaction, WorldBossContribution, WorldBossRewardClaim
from app.discord_bot.commands import FarmCommands
from app.discord_bot.runner import ServiceRunner
from app.game import fishing
from app.game import world_boss as wb
from app.game.errors import (
    AlreadyExistsError,
    InsufficientItemsError,
    NotFoundError,
    NotGroupMemberError,
    ValidationError,
)
from app.main import create_app
from app.services.admin_service import AdminService
from app.site.server import create_site_app
from tests.conftest import make_farmer
from tests.test_discord_bot import ALICE, BOB, GUILD_A

PASSWORD = "world-boss-admin-pass"
ADMIN = {"X-Farmgm-Admin": "1"}
SIMPLE_REWARDS = {
    "min_points": 0,
    "victory": {"gold": 100, "items": {}},
    "participation": {"gold": 10, "items": {}},
    "bonus_points": 10,
    "bonus": {"gold": 5, "items": {"wood": 1}},
    "rank": [{"gold": 50, "items": {}}],
    "title": "屠龍英雄",
    "title_top": 1,
}


@pytest.fixture
def game(session_factory, clock):
    return AdminService(session_factory, clock, fishing_rng=random.Random(9).random)


def make_boss(game, clock, group_id, *, hp=100_000, hours=72, start_in=0, rewards=None, settings=None) -> int:
    now = clock.now()
    view = game.create_world_boss("admin", {
        "group_id": group_id, "name": "深淵魔龍", "level": 50, "max_hp": hp,
        "starts_at": now + timedelta(seconds=start_in), "ends_at": now + timedelta(hours=hours),
        "rewards": rewards, "settings": settings,
    })
    return view["event_id"]


def give(game, pid, code, qty, quality="normal", mutation="none"):
    game.set_inventory("test", pid, code, quality, mutation, qty)


def donate(game, gid, pid, eid, category, code, qty, req=None, **kw):
    return game.boss_donate(gid, pid, eid, category, code, qty, req or uuid.uuid4().hex, **kw)


def catch_one(game, clock, pid):
    view = game.start_fishing(pid)
    clock._now = view["bite_at"]
    view = game.hook_fish(pid, view["id"])
    while view["phase"] == "fighting":
        state = {k: view[k] for k in ("intent", "tension", "distance", "stamina", "max_stamina", "max_tension")}
        view = game.fishing_action(pid, view["id"], fishing.smart_action(state, None, {"reel_power": 10}),
                                   view["turn"])
    return view


def supply(view, code):
    return next(s for s in view["supplies"] if s["code"] == code)


# ---------- 1. 查看 ----------


def test_player_can_view_boss(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    assert game.boss_current(home_group, pid) is None
    eid = make_boss(game, clock, home_group)
    v = game.boss_current(home_group, pid)
    assert v["event_id"] == eid and v["name"] == "深淵魔龍" and "level" not in v
    assert v["hp"] == v["max_hp"] == 100_000 and v["state"] == "active" and v["state_label"] == "進行中"
    assert [s["code"] for s in v["supplies"]] == list(wb.SUPPLIES)
    assert supply(v, "food")["target"] == wb.DEFAULT_SETTINGS["supply_targets"]["food"]
    assert v["me"]["points"] == 0 and v["me"]["rank"] is None and v["me"]["claim_state"] == "pending"


def test_scheduled_event_cannot_receive_donations(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group, start_in=3600)
    give(game, pid, "carrot", 5)
    assert game.boss_current(home_group, pid)["state"] == "scheduled"
    with pytest.raises(ValidationError, match="尚未開始"):
        donate(game, home_group, pid, eid, "crop", "carrot", 1)
    clock.advance(3600)
    assert donate(game, home_group, pid, eid, "crop", "carrot", 1)["points"] == 3


# ---------- 2–4. 捐獻農作物、漁獲、素材 ----------


def test_donate_crops_updates_supply_points_hp_and_log(game, clock, home_group, session_factory):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    give(game, pid, "carrot", 10)
    opts = game.boss_donation_options(home_group, pid, eid, "crop")
    assert [(o["code"], o["held"], o["points_min"], o["supply"]) for o in opts] == [("carrot", 10, 3, "food")]
    r = donate(game, home_group, pid, eid, "crop", "carrot", 4)
    # 胡蘿蔔售價 25 → 每個 3 分；立即傷害 = 12 分 × 2 × 攻擊倍率 1
    assert r["points"] == 12 and r["damage"] == 24 and not r["duplicate"]
    v = game.boss_current(home_group, pid)
    assert supply(v, "food")["progress"] == 12 and v["hp"] == 100_000 - 24
    assert v["me"]["points"] == 12 and v["me"]["quantity"] == 4 and v["me"]["rank"] == 1
    assert game.get_backpack(pid).produce[0].quantity == 6
    with session_factory() as s:
        tx = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "boss_donate")).one()
        assert tx.item_code == "carrot" and tx.quantity == 4 and tx.gold_delta == 0
        assert s.scalar(select(func.count()).select_from(WorldBossContribution)) == 1


def test_donate_quality_crop_scores_by_price(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    give(game, pid, "carrot", 2, quality="legendary")
    opt = game.boss_donation_options(home_group, pid, eid, "crop")[0]
    assert opt["quality"] == "legendary" and opt["points_min"] > 3
    r = donate(game, home_group, pid, eid, "crop", "carrot", 2, quality="legendary")
    assert r["points"] == opt["points_min"] * 2


def test_donate_fish(game, clock, home_group, session_factory):
    game.update_fishing_settings("test", {**fishing.DEFAULT_SETTINGS, "base_success_pct": 100})
    pid = make_farmer(game, "釣魚高手", home_group)
    game.set_farm_values("test", pid, gold=5000)
    for code, q in (("bamboo_rod", 1), ("basic_line", 1), ("worm_bait", 5)):
        game.buy_fishing_gear(pid, code, q)
    landed = catch_one(game, clock, pid)
    assert landed["status"] == "landed"
    eid = make_boss(game, clock, home_group)
    opts = game.boss_donation_options(home_group, pid, eid, "fish")
    assert len(opts) == 1 and opts[0]["held"] == 1 and opts[0]["supply"] == "combat"
    r = donate(game, home_group, pid, eid, "fish", landed["fish"]["code"], 1)
    assert r["points"] == wb.price_points(landed["fish"]["price"], 10)
    assert supply(game.boss_current(home_group, pid), "combat")["progress"] == r["points"]
    with session_factory() as s:
        assert s.scalar(select(FarmFish.donated_at)) is not None
    # 捐出的魚離開魚簍，不能再賣
    assert game.boss_donation_options(home_group, pid, eid, "fish") == []
    with pytest.raises(ValidationError):
        game.sell_fish(pid, None)


def test_donate_materials_goods_and_special(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    give(game, pid, "wood", 5)
    give(game, pid, "herb", 2)
    give(game, pid, "crystal", 1)
    give(game, pid, "bread", 1)
    assert donate(game, home_group, pid, eid, "material", "wood", 5)["points"] == 10
    assert donate(game, home_group, pid, eid, "material", "herb", 2)["points"] == 12
    assert donate(game, home_group, pid, eid, "special", "crystal", 1)["points"] == 15
    assert donate(game, home_group, pid, eid, "goods", "bread", 1)["points"] == 20
    v = game.boss_current(home_group, pid)
    assert supply(v, "equipment")["progress"] == 25 and supply(v, "medical")["progress"] == 12
    assert supply(v, "combat")["progress"] == 20
    with pytest.raises(ValidationError, match="不能用這個類別"):
        donate(game, home_group, pid, eid, "goods", "wood", 1)  # 類別不符
    give(game, pid, "fake_coin", 3)
    with pytest.raises(ValidationError):
        donate(game, home_group, pid, eid, "material", "fake_coin", 1)  # 假幣不能捐


# ---------- 5–6. 背包不足、重複提交 ----------


def test_cannot_donate_more_than_held(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    give(game, pid, "carrot", 3)
    with pytest.raises(InsufficientItemsError):
        donate(game, home_group, pid, eid, "crop", "carrot", 4)
    with pytest.raises(InsufficientItemsError):
        donate(game, home_group, pid, eid, "material", "wood", 1)
    v = game.boss_current(home_group, pid)
    assert v["total_points"] == 0 and v["hp"] == v["max_hp"]
    assert game.get_backpack(pid).produce[0].quantity == 3


def test_per_item_limit(game, clock, home_group):
    settings = {**wb.DEFAULT_SETTINGS, "limits": {"per_item_max": 5, "per_action_max": 99}}
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group, settings=settings)
    give(game, pid, "carrot", 20)
    donate(game, home_group, pid, eid, "crop", "carrot", 4)
    with pytest.raises(ValidationError, match="還可以捐 1 個"):
        donate(game, home_group, pid, eid, "crop", "carrot", 2)
    assert game.boss_donation_options(home_group, pid, eid, "crop")[0]["remaining"] == 1


def test_duplicate_request_is_processed_once(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    give(game, pid, "carrot", 10)
    first = donate(game, home_group, pid, eid, "crop", "carrot", 3, req="double-click-0001")
    again = donate(game, home_group, pid, eid, "crop", "carrot", 3, req="double-click-0001")
    assert not first["duplicate"] and again["duplicate"]
    # 同時送出兩次相同請求（例如網路重送）：活動列鎖 + 唯一限制保證只扣一次
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(
            lambda _: donate(game, home_group, pid, eid, "crop", "carrot", 2, req="concurrent-0001"), range(4)))
    assert sum(not r["duplicate"] for r in results) == 1
    assert game.get_backpack(pid).produce[0].quantity == 10 - 3 - 2
    assert game.boss_current(home_group, pid)["me"]["quantity"] == 5
    with pytest.raises(ValidationError):
        donate(game, home_group, pid, eid, "crop", "carrot", 1, req="bad id!")


# ---------- 7. 群組隔離 ----------


def test_groups_are_isolated(game, clock, home_group):
    other = game.create_group("另一個伺服器").id
    alice = make_farmer(game, "阿明", home_group)
    bob = make_farmer(game, "小華", other)
    eid = make_boss(game, clock, home_group)
    give(game, bob, "carrot", 5)
    assert game.boss_current(other, bob) is None  # 另一群組看不到這場活動
    with pytest.raises(NotFoundError):
        donate(game, other, bob, eid, "crop", "carrot", 1)  # 以自己群組的身分捐到別的群組
    with pytest.raises(NotGroupMemberError):
        game.boss_current(home_group, bob)  # 不是成員
    with pytest.raises(NotGroupMemberError):
        donate(game, home_group, bob, eid, "crop", "carrot", 1)
    with pytest.raises(NotFoundError):
        game.boss_leaderboard(other, bob, eid)
    other_eid = make_boss(game, clock, other)
    give(game, alice, "carrot", 2)
    donate(game, home_group, alice, eid, "crop", "carrot", 2)
    assert game.boss_current(other, bob)["event_id"] == other_eid
    assert game.boss_current(other, bob)["total_points"] == 0


def test_one_open_event_per_group(game, clock, home_group):
    make_boss(game, clock, home_group)
    with pytest.raises(AlreadyExistsError):
        make_boss(game, clock, home_group)


# ---------- 8. 排行榜 ----------


def test_leaderboard_values(game, clock, home_group):
    ids = [make_farmer(game, name, home_group) for name in ("阿明", "小華", "阿美")]
    eid = make_boss(game, clock, home_group)
    for pid in ids:
        give(game, pid, "carrot", 20)
        give(game, pid, "wood", 20)
    donate(game, home_group, ids[1], eid, "crop", "carrot", 5)   # 15 分
    clock.advance(1)
    donate(game, home_group, ids[0], eid, "material", "wood", 4)  # 8 分
    clock.advance(1)
    donate(game, home_group, ids[2], eid, "crop", "carrot", 5)   # 15 分（同分，較晚）
    donate(game, home_group, ids[0], eid, "crop", "carrot", 1)   # 阿明合計 11 分
    board = game.boss_leaderboard(home_group, ids[0], eid)
    assert [(r["rank"], r["player_name"], r["points"], r["quantity"]) for r in board["rows"]] == [
        (1, "小華", 15, 5), (2, "阿美", 15, 5), (3, "阿明", 11, 5)]
    assert board["me"]["rank"] == 3 and board["participants"] == 3


# ---------- 9. HP 與補給 ----------


def test_hp_decreases_over_time_by_supplies(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    clock.advance(3600)
    v = game.boss_current(home_group, pid)
    # 沒有補給：1500 × 攻擊 1 × 出勤率 30% = 450／小時
    assert v["damage_per_hour"] == 450 and v["hp"] == 100_000 - 450 and v["uptime_pct"] == 30.0
    # 補滿糧食（2000 分）→ 出勤率 100%
    settings = {**wb.DEFAULT_SETTINGS, "items": {**wb.DEFAULT_SETTINGS["items"], "carrot": {
        "supply": "food", "points": 100}}}
    game.update_world_boss_settings("admin", eid, settings)
    give(game, pid, "carrot", 20)
    donate(game, home_group, pid, eid, "crop", "carrot", 20)
    v = game.boss_current(home_group, pid)
    assert supply(v, "food")["ratio_pct"] == 100.0 and v["uptime_pct"] == 100.0 and v["damage_per_hour"] == 1500
    hp = v["hp"]
    clock.advance(1800)
    assert game.boss_current(home_group, pid)["hp"] == hp - 750
    # 戰鬥補給有上限：超過目標 150% 後不再增加傷害
    stats = wb.combat_stats({"food": 0, "medical": 0, "combat": 10**9, "equipment": 0}, wb.DEFAULT_SETTINGS)
    assert stats["attack_mult"] == pytest.approx(2.5)


def test_simulate_keeps_fractional_damage():
    from datetime import UTC, datetime
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    hp, tick = 1000, t0
    for i in range(1, 11):  # 每秒讀取一次，每小時 1800 傷害 = 每秒 0.5
        r = wb.simulate(hp, tick, t0, t0 + timedelta(hours=1), 1800, t0 + timedelta(seconds=i))
        hp, tick = r["hp"], r["last_tick_at"]
    assert hp == 995


def test_boss_defeated_ends_event(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group, hp=500)
    clock.advance(3600)  # 450 傷害
    assert game.boss_current(home_group, pid)["hp"] == 50
    give(game, pid, "bread", 2)
    r = donate(game, home_group, pid, eid, "goods", "bread", 2)  # 40 分 × 2 × 倍率 > 50
    assert r["defeated"] and r["damage"] == 50
    v = game.boss_current(home_group, pid)
    assert v["state"] == "defeated" and v["hp"] == 0 and v["me"]["claim_state"] == "ready"


# ---------- 10. 結束後不能捐獻 ----------


def test_no_donations_after_end(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group, hours=2)
    give(game, pid, "carrot", 5)
    donate(game, home_group, pid, eid, "crop", "carrot", 1)
    clock.advance(2 * 3600 + 1)
    with pytest.raises(ValidationError, match="已結束"):
        donate(game, home_group, pid, eid, "crop", "carrot", 1)
    v = game.boss_current(home_group, pid)
    assert v["state"] == "ended" and v["end_reason"] == "timeout"
    assert game.get_backpack(pid).produce[0].quantity == 4


def test_background_settlement_finalizes(game, clock, home_group):
    eid = make_boss(game, clock, home_group, hours=1)
    clock.advance(3601)
    result = game.run_background_settlement()
    assert result["world_boss"] == [eid]
    assert game.run_background_settlement()["world_boss"] == []


# ---------- 11. 獎勵 ----------


def test_rewards_claimed_once(game, clock, home_group, session_factory):
    a = make_farmer(game, "阿明", home_group)
    b = make_farmer(game, "小華", home_group)
    c = make_farmer(game, "阿美", home_group)
    eid = make_boss(game, clock, home_group, hp=100, rewards=SIMPLE_REWARDS)
    give(game, a, "carrot", 4)
    give(game, b, "carrot", 1)
    donate(game, home_group, a, eid, "crop", "carrot", 4)  # 12 分：第 1 名、達標
    donate(game, home_group, b, eid, "crop", "carrot", 1)  # 3 分
    with pytest.raises(ValidationError, match="還沒結束"):
        game.boss_claim(home_group, a, eid)
    clock.advance(3600)  # 450 傷害 → 擊敗
    gold_a = game.get_farm_status(a).gold
    r = game.boss_claim(home_group, a, eid)
    assert r.gold_delta == 100 + 5 + 50 and "屠龍英雄" in r.message
    assert game.get_farm_status(a).gold == gold_a + 155
    assert any(m.code == "wood" and m.quantity == 1 for m in game.get_backpack(a).materials)
    assert game.boss_current(home_group, a)["me"]["title"] == "屠龍英雄"
    assert [t["title"] for t in game.titles(a)["owned"]] == ["屠龍英雄"]  # 可在成就圖鑑選用
    game.set_title(a, "屠龍英雄")
    assert game.get_farm_status(a).title == "屠龍英雄"
    with pytest.raises(AlreadyExistsError):
        game.boss_claim(home_group, a, eid)
    with pytest.raises(ValidationError, match="沒有參與"):
        game.boss_claim(home_group, c, eid)
    # 同時多次領取：只有一次成功
    with ThreadPoolExecutor(4) as pool:
        def claim(_):
            try:
                return game.boss_claim(home_group, b, eid).gold_delta
            except AlreadyExistsError:
                return None
        results = list(pool.map(claim, range(4)))
    assert [x for x in results if x is not None] == [100]
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(WorldBossRewardClaim)) == 2
        assert s.scalar(select(func.sum(GameTransaction.gold_delta))
                        .where(GameTransaction.event_type == "boss_reward")) == 255


def test_participation_reward_when_not_defeated(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group, hours=1, rewards={**SIMPLE_REWARDS, "bonus_points": 0, "rank": []})
    give(game, pid, "carrot", 1)
    donate(game, home_group, pid, eid, "crop", "carrot", 1)
    clock.advance(3601)
    r = game.boss_claim(home_group, pid, eid)
    assert r.gold_delta == 10 and "參與獎勵" in r.message and "稱號" not in r.message


def test_below_min_points_gets_no_reward(game, clock, home_group, session_factory):
    """積分未達參與門檻：勝利、達標、排名獎勵都沒有，也不會出現在可領取清單。"""
    a = make_farmer(game, "阿明", home_group)
    b = make_farmer(game, "小華", home_group)
    rewards = {**SIMPLE_REWARDS, "min_points": 12, "rank": [{"gold": 50, "items": {}}] * 2}
    eid = make_boss(game, clock, home_group, hp=100, rewards=rewards)
    give(game, a, "carrot", 4)
    give(game, b, "carrot", 1)
    assert donate(game, home_group, a, eid, "crop", "carrot", 3)["points"] == 9  # 未達 12
    assert game.boss_current(home_group, a)["me"]["claim_state"] == "pending"
    donate(game, home_group, b, eid, "crop", "carrot", 1)  # 3 分：第 2 名，但未達門檻
    donate(game, home_group, a, eid, "crop", "carrot", 1)  # 阿明 12 分：剛好達門檻
    clock.advance(3600)
    view_b = game.boss_current(home_group, b)
    assert view_b["me"]["claim_state"] == "below_min" and view_b["rewards_text"]["min_points"] == 12
    gold_b = game.get_farm_status(b).gold
    with pytest.raises(ValidationError, match="未達參與門檻"):
        game.boss_claim(home_group, b, eid)
    assert game.get_farm_status(b).gold == gold_b
    assert game.boss_claim(home_group, a, eid).gold_delta == 100 + 5 + 50
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(WorldBossRewardClaim)) == 1
    # 下一場的「可領取」清單不包含未達門檻的活動
    make_boss(game, clock, home_group, rewards=rewards)
    assert game.boss_current(home_group, b)["claimable"] == []


def test_compose_rewards_min_points():
    r = {**wb.DEFAULT_REWARDS, "min_points": 30}
    assert wb.compose_rewards(r, defeated=True, points=29, rank=1) == {"gold": 0, "items": {}, "title": None,
                                                                       "parts": []}
    assert wb.compose_rewards(r, defeated=True, points=30, rank=1)["gold"] > 0
    old = {k: v for k, v in r.items() if k != "min_points"}  # 0022 以前結束的活動：沒有門檻
    assert wb.compose_rewards(old, defeated=False, points=1, rank=5)["gold"] == old["participation"]["gold"]
    assert wb.validate_rewards({}, lambda c: True)["min_points"] == wb.DEFAULT_REWARDS["min_points"]
    assert wb.abyss_dragon()["rewards"]["min_points"] == 30


# ---------- 12. 管理功能（服務層） ----------


def test_admin_pause_resume_end_and_validation(game, clock, home_group, session_factory):
    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group)
    give(game, pid, "carrot", 5)
    assert game.world_boss_action("admin", eid, "pause")["state"] == "paused"
    with pytest.raises(ValidationError, match="已暫停"):
        donate(game, home_group, pid, eid, "crop", "carrot", 1)
    clock.advance(3600)
    assert game.world_boss_action("admin", eid, "resume")["hp"] == 100_000  # 暫停期間不扣 HP
    v = game.update_world_boss("admin", eid, {"name": "冰霜巨龍", "level": 60, "max_hp": 120_000})
    assert v["name"] == "冰霜巨龍" and v["max_hp"] == 120_000 and v["hp"] == 120_000
    with pytest.raises(ValidationError):
        game.update_world_boss_settings("admin", eid, {**wb.DEFAULT_SETTINGS, "items": {"seed_wheat": {
            "supply": "food", "points": 1}}})
    assert game.world_boss_action("admin", eid, "end")["state"] == "ended"
    with pytest.raises(ValidationError, match="已經結束"):
        game.world_boss_action("admin", eid, "resume")
    with session_factory() as s:
        from app.db.models import AdminAction
        actions = list(s.scalars(select(AdminAction.action).where(AdminAction.target_type == "world_boss")))
    assert actions == ["world_boss_create", "world_boss_pause", "world_boss_resume", "world_boss_update",
                       "world_boss_end"]
    make_boss(game, clock, home_group)  # 結束後可以再開新的一場


# ---------- 14. 重啟後資料仍存在 ----------


def test_data_survives_restart(session_factory, clock, home_group):
    first = AdminService(session_factory, clock)
    pid = make_farmer(first, "阿明", home_group)
    eid = make_boss(first, clock, home_group)
    give(first, pid, "carrot", 5)
    donate(first, home_group, pid, eid, "crop", "carrot", 5)
    before = first.boss_current(home_group, pid)
    del first
    restarted = AdminService(session_factory, clock)  # 新的程序：沒有任何記憶體狀態
    after = restarted.boss_current(home_group, pid)
    assert after["hp"] == before["hp"] and after["me"]["points"] == 15
    assert supply(after, "food")["progress"] == 15


# ---------- 12. 管理監控網站 ----------


@pytest.fixture
def client(session_factory, clock):
    app = create_app(session_factory, clock=clock, refresh_seconds=0.2, allowed_hosts=["testserver"],
                     admin_auth=AdminAuth("admin", hash_password(PASSWORD)))
    with TestClient(app) as c:
        yield c


def test_admin_website_manages_boss(client, game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    now = clock.now()
    body = {"group_id": home_group, "name": "深淵魔龍", "level": 50, "max_hp": 5000,
            "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=24)).isoformat()}
    assert client.post("/api/admin/world-boss", json=body, headers=ADMIN).status_code == 401  # 未登入
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                       headers=ADMIN).status_code == 200
    assert client.post("/api/admin/world-boss", json=body).status_code == 403  # 缺少 CSRF 標頭
    res = client.post("/api/admin/world-boss", json=body, headers=ADMIN)
    assert res.status_code == 201, res.text
    eid = res.json()["event_id"]
    assert client.post("/api/admin/world-boss", json=body, headers=ADMIN).status_code == 400  # 同群組只能一場
    give(game, pid, "carrot", 3)
    donate(game, home_group, pid, eid, "crop", "carrot", 3)
    listing = client.get("/api/monitor/world-boss").json()
    assert listing["events"][0]["event_id"] == eid and listing["events"][0]["participants"] == 1
    donatable = {d["code"]: d["category"] for d in listing["defaults"]["donatable"]}
    assert donatable["carrot"] == "crop" and donatable["crystal"] == "special" and "fake_coin" not in donatable
    detail = client.get(f"/api/monitor/world-boss/{eid}").json()
    assert detail["ranking"][0]["player_name"] == "阿明" and detail["contributions"][0]["quantity"] == 3
    assert detail["total_points"] == 9 and detail["claim_rows"] == []
    settings = {**wb.DEFAULT_SETTINGS, "supply_targets": {"food": 10, "medical": 10, "combat": 10, "equipment": 10}}
    res = client.put(f"/api/admin/world-boss/{eid}/settings", json=settings, headers=ADMIN)
    assert res.status_code == 200 and res.json()["supplies"][0]["target"] == 10
    res = client.put(f"/api/admin/world-boss/{eid}/rewards", json=SIMPLE_REWARDS, headers=ADMIN)
    assert res.status_code == 200
    bad = {**SIMPLE_REWARDS, "victory": {"gold": 1, "items": {"no_such_item": 1}}}
    assert client.put(f"/api/admin/world-boss/{eid}/rewards", json=bad, headers=ADMIN).status_code == 400
    assert client.post(f"/api/admin/world-boss/{eid}/pause", headers=ADMIN).json()["state"] == "paused"
    assert client.post(f"/api/admin/world-boss/{eid}/resume", headers=ADMIN).json()["state"] == "active"
    assert client.post(f"/api/admin/world-boss/{eid}/end", headers=ADMIN).json()["state"] == "ended"
    assert client.post(f"/api/admin/world-boss/{eid}/explode", headers=ADMIN).status_code == 422
    assert client.get("/api/monitor/world-boss/999999").status_code == 404
    page = client.get("/").text
    assert 'data-tab="worldboss"' in page and 'data-tab-panel="worldboss"' in page


def test_monitor_projection_is_read_only(client, game, clock, home_group, session_factory):
    eid = make_boss(game, clock, home_group)
    clock.advance(3600)
    detail = client.get(f"/api/monitor/world-boss/{eid}").json()
    assert detail["hp"] == 100_000 - 450  # 監控即時推算
    with session_factory() as s:
        from app.db.models import WorldBossEvent
        assert s.get(WorldBossEvent, eid).hp == 100_000  # 但沒有寫回資料庫


# ---------- 13. 介紹網站 ----------


def test_site_shows_world_boss_guide(session_factory):
    with TestClient(create_site_app(session_factory)) as c:
        guide = c.get("/api/guide").json()["world_boss"]
        assert [s["code"] for s in guide["supplies"]] == list(wb.SUPPLIES)
        assert guide["gold_per_point"] == wb.DEFAULT_SETTINGS["gold_per_point"]
        assert len(guide["categories"]) == 5 and guide["rewards"]["rank"]
        assert guide["source"]["kind"] == "default" and guide["current"] is None
        page = c.get("/").text
        assert 'id="world-boss"' in page and "世界 Boss" in page and 'id="g-boss-supplies"' in page
        assert 'id="c-boss"' in page and 'id="g-boss-now"' in page


def test_site_guide_follows_live_global_boss(session_factory, game, clock, home_group):
    """介紹網站的世界 Boss 數值跟著目前的全服活動（與 Discord 活動說明一致），並顯示本場 HP；伺服器活動不公開。"""
    from app.site import server as site_server

    make_boss(game, clock, home_group, hp=999, rewards={**SIMPLE_REWARDS, "title": "伺服器限定"})
    settings = {**wb.DEFAULT_SETTINGS, "supply_targets": {"food": 111, "medical": 222, "combat": 333, "equipment": 444}}
    now = clock.now()
    eid = game.create_world_boss("admin", {
        "group_id": None, "name": "全服測試龍", "max_hp": 5_000, "starts_at": now,
        "ends_at": now + timedelta(hours=3), "rewards": {**wb.abyss_dragon()["rewards"]}, "settings": settings,
    })["event_id"]
    pid = make_farmer(game, "阿明", home_group)
    give(game, pid, "carrot", 2)
    donate(game, None, pid, eid, "crop", "carrot", 2)
    with session_factory() as s:
        guide = site_server.build_guide(s, clock.now())["world_boss"]
    assert guide["source"] == {"kind": "event", "name": "全服測試龍"}
    assert [x["target"] for x in guide["supplies"]] == [111, 222, 333, 444]
    assert guide["rewards"]["victory"].startswith("1,200 金幣") and guide["rewards"]["title"] == "降龍勇士"
    assert guide["duration_minutes"] == 180
    cur = guide["current"]
    assert cur["name"] == "全服測試龍" and cur["max_hp"] == 5_000 and cur["hp"] < 5_000
    assert cur["participants"] == 1 and "player" not in str(cur).lower()
    assert "伺服器限定" not in str(guide)  # 伺服器自己的活動不出現在介紹網站


# ---------- Discord 面板 ----------


@pytest.fixture
def cmds(game):
    runner = ServiceRunner(4)
    yield FarmCommands(game, runner)
    runner.shutdown()


def test_panel_donate_flow_and_double_click(cmds, game, clock):
    from tests.test_discord_panel import make_player, run

    p = make_player(cmds, ALICE, GUILD_A, name="阿明")
    gid = game.get_group_by_guild(str(GUILD_A)).id
    p.click("世界Boss")
    assert "目前沒有世界 Boss 活動" in p.text
    eid = make_boss(game, clock, gid, rewards={**wb.DEFAULT_REWARDS, "min_points": 0})  # 只捐少量也能領
    give(game, p.panel.player_id, "carrot", 10)
    p.click("更新狀態")
    # 介紹區：Boss 簡介、我的參與與入口；補給細節在戰鬥區
    for text in ("深淵魔龍", "HP", "前往戰場", "我的參與"):
        assert text in p.text, text
    assert "後勤補給進度" not in p.text
    assert {"進入深淵魔龍", "查看排行榜", "領取獎勵", "活動說明", "更新狀態"} <= set(p.labels())
    p.click("進入深淵魔龍")
    for text in ("戰鬥區", "群組後勤補給進度", "糧食補給", "醫療補給", "戰鬥補給", "裝備補給", "前線戰況",
                 "個人貢獻", "HP"):
        assert text in p.text, text
    assert {"捐獻物資", "領取獎勵", "更新狀態", "回介紹區"} <= set(p.labels())
    p.click("捐獻物資")
    p.choose("捐獻類別", "crop")
    p.choose("選擇物品", "0")
    assert "胡蘿蔔" in p.text and "持有 10" in p.text
    p.click("× 5")
    assert "確認捐獻" in p.text
    p.click("取消")
    assert "沒有扣除任何物品" in p.text
    assert game.get_backpack(p.panel.player_id).produce[0].quantity == 10
    p.choose("選擇物品", "0")
    p.click("× 5")
    confirm = p.button("確認捐獻")
    first, second = p._interaction(), p._interaction()
    run(confirm.callback(first))
    run(confirm.callback(second))  # 舊畫面上的同一個按鈕再按一次
    p._update(first)
    assert "獲得 15 積分" in p.text
    assert game.get_backpack(p.panel.player_id).produce[0].quantity == 5
    assert game.boss_current(gid, p.panel.player_id)["me"]["quantity"] == 5
    p.click("回戰鬥區")
    assert "戰鬥區" in p.text and "貢獻積分 **15**" in p.text
    p.click("回介紹區")
    p.click("查看排行榜")
    assert "阿明" in p.text and "15 分" in p.text
    p.click("回介紹區")
    p.click("活動說明")
    assert "補給類型與效果" in p.text and "獎勵制度" in p.text
    p.click("回介紹區")
    game.world_boss_action("admin", eid, "end")
    p.click("更新狀態")
    assert "已結束" in p.text
    p.click("領取獎勵")
    assert "領取「深淵魔龍」" in p.text
    assert "已領取" in p.text


def test_panel_fish_multi_select_donates_all(cmds, game, clock, session_factory):
    """漁獲用勾選：勾選的魚種全部捐出，一次確認；連點確認只捐一次，取消不扣。"""
    from tests.test_discord_panel import make_player, run

    p = make_player(cmds, ALICE, GUILD_A)
    pid = p.panel.player_id
    game.update_fishing_settings("test", {**fishing.DEFAULT_SETTINGS, "base_success_pct": 100})
    game.set_farm_values("test", pid, gold=5000)
    for code, q in (("bamboo_rod", 1), ("basic_line", 1), ("worm_bait", 5)):
        game.buy_fishing_gear(pid, code, q)
    caught = [catch_one(game, clock, pid) for _ in range(4)]
    assert all(c["status"] == "landed" for c in caught)
    eid = make_boss(game, clock, game.get_group_by_guild(str(GUILD_A)).id)
    p.click("世界Boss")
    p.click("進入深淵魔龍")
    p.click("捐獻物資")
    p.choose("捐獻類別", "fish")
    assert "勾選要捐的魚種" in p.text
    values = p.options("勾選要全部捐出的魚種")
    assert values
    p.choose("勾選要全部捐出的魚種", values)  # 全部勾選
    assert "確認捐獻以下" in p.text and "共 4 隻" in p.text
    p.click("取消")
    assert "沒有扣除任何物品" in p.text
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmFish).where(FarmFish.donated_at.is_not(None))) == 0
    p.choose("捐獻類別", "fish")
    p.choose("勾選要全部捐出的魚種", values)
    confirm = p.button("確認捐獻")
    first, second = p._interaction(), p._interaction()
    run(confirm.callback(first))
    run(confirm.callback(second))  # 舊畫面上的同一個按鈕再按一次
    p._update(first)
    expected = sum(wb.price_points(c["fish"]["price"], 10) for c in caught)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmFish).where(FarmFish.donated_at.is_not(None))) == 4
        assert s.scalar(select(func.count()).select_from(WorldBossContribution)) == len(values)
    assert game.boss_current(game.get_group_by_guild(str(GUILD_A)).id, pid, eid)["me"]["points"] == expected
    assert game.boss_donation_options(game.get_group_by_guild(str(GUILD_A)).id, pid, eid, "fish") == []


def test_panel_other_guild_cannot_see_event(cmds, game, clock):
    from tests.test_discord_bot import GUILD_B
    from tests.test_discord_panel import make_player

    a = make_player(cmds, ALICE, GUILD_A)
    make_boss(game, clock, game.get_group_by_guild(str(GUILD_A)).id)
    b = make_player(cmds, BOB, GUILD_B)
    b.click("世界Boss")
    assert "沒有世界 Boss 活動" in b.text
    a.click("世界Boss")
    assert "深淵魔龍" in a.text
    asyncio.run(asyncio.sleep(0))


# ---------- 全服活動 ----------


def make_global(game, clock, hp=100_000, hours=72):
    now = clock.now()
    return game.create_world_boss("admin", {
        "group_id": None, "name": "全服魔龍", "max_hp": hp,
        "starts_at": now, "ends_at": now + timedelta(hours=hours),
    })["event_id"]


def test_global_event_shared_by_all_groups(game, clock, home_group):
    other = game.create_group("另一個伺服器").id
    alice = make_farmer(game, "阿明", home_group)
    bob = make_farmer(game, "小華", other)
    eid = make_global(game, clock)
    for pid in (alice, bob):
        give(game, pid, "carrot", 5)
    va, vb = game.boss_current(home_group, alice), game.boss_current(other, bob)
    assert va["event_id"] == vb["event_id"] == eid and va["scope_label"] == "🌏 全服" and "level" not in va
    donate(game, home_group, alice, eid, "crop", "carrot", 5)
    donate(game, other, bob, eid, "crop", "carrot", 2)
    assert game.boss_current(None, bob)["event_id"] == eid  # 私訊也看得到全服活動
    board = game.boss_leaderboard(other, bob, eid)
    assert [r["player_name"] for r in board["rows"]] == ["阿明", "小華"]
    # 全服與伺服器活動同時存在：都看得到，可以切換；別的伺服器的活動仍然看不到
    local = make_boss(game, clock, home_group)
    view = game.boss_current(home_group, alice)
    assert {c["event_id"] for c in view["choices"]} == {eid, local}
    assert game.boss_current(home_group, alice, local)["event_id"] == local
    assert {c["event_id"] for c in game.boss_current(other, bob)["choices"]} == {eid}
    assert game.boss_current(other, bob, local)["event_id"] == eid  # 指定其他群組的活動會被忽略
    with pytest.raises(NotFoundError):
        donate(game, other, bob, local, "crop", "carrot", 1)
    with pytest.raises(AlreadyExistsError):
        make_global(game, clock)  # 手動建立的全服活動同時只能一場


# ---------- 固定出現的世界 Boss（排程） ----------


def schedule_data(**kw):
    preset = wb.abyss_dragon()
    preset["anchor_date"] = date(2026, 1, 1)  # 測試時鐘從 2026-01-01 08:00（台灣）開始
    preset.update(kw)
    return {**preset, "group_id": kw.get("group_id")}


def test_slot_after_uses_taipei_time():
    from datetime import UTC, datetime
    anchor = date(2026, 1, 1)
    before = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)  # 台灣 08:00
    assert wb.slot_after(anchor, 21, 0, 2, 180, before) == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    during = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)  # 台灣 22:00，還在 21:00–24:00 之間
    assert wb.slot_after(anchor, 21, 0, 2, 180, during) == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    after = datetime(2026, 1, 1, 16, 0, tzinfo=UTC)  # 已結束 → 兩天後
    assert wb.slot_after(anchor, 21, 0, 2, 180, after) == datetime(2026, 1, 3, 13, 0, tzinfo=UTC)
    assert wb.slot_after(anchor, 21, 0, 2, 180, before, datetime(2026, 1, 1, 13, 0, tzinfo=UTC)) == \
        datetime(2026, 1, 3, 13, 0, tzinfo=UTC)


def test_schedule_spawns_each_occurrence_once(game, clock, home_group):
    pid = make_farmer(game, "阿明", home_group)
    sched = game.create_world_boss_schedule("admin", schedule_data())
    assert sched["group_name"] == "全服" and sched["next_event"] is not None
    first = sched["next_event"]
    assert first["starts_at"].astimezone(TAIPEI_TZ).strftime("%m-%d %H:%M") == "01-01 21:00"
    assert first["ends_at"] - first["starts_at"] == timedelta(hours=3)
    assert game.spawn_due_world_bosses() == []  # 同一場不會重複建立
    v = game.boss_current(home_group, pid)
    assert v["event_id"] == first["event_id"] and v["state"] == "scheduled" and v["name"] == "深淵魔龍"
    # 出現前個別修改這一場的時間與獎勵
    new_start = first["starts_at"] - timedelta(hours=1)
    game.update_world_boss("admin", first["event_id"], {"name": "深淵魔龍", "max_hp": 20_000,
                                                         "starts_at": new_start, "ends_at": first["ends_at"]})
    game.update_world_boss_rewards("admin", first["event_id"], SIMPLE_REWARDS)
    v = game.boss_current(home_group, pid)
    assert v["starts_at"] == new_start and v["max_hp"] == 20_000 and v["rewards"]["title"] == "屠龍英雄"
    # 這一場結束後建立下一場（兩天後 21:00），且套用排程的原本設定
    clock._now = first["ends_at"] + timedelta(seconds=1)
    result = game.run_background_settlement()
    assert result["world_boss"] == [first["event_id"]] and len(result["world_boss_spawned"]) == 1
    nxt = game.boss_current(home_group, pid)
    assert nxt["state"] == "scheduled" and nxt["max_hp"] == 15_000
    assert nxt["starts_at"] - first["starts_at"] == timedelta(days=2)
    assert nxt["rewards"]["title"] == "降龍勇士"
    # 停用排程：結束現在這一場後不再產生新的
    game.set_world_boss_schedule_enabled("admin", sched["schedule_id"], False)
    game.world_boss_action("admin", nxt["event_id"], "end")
    assert game.spawn_due_world_bosses() == []


def test_schedule_validation_and_delete(game, clock, home_group):
    with pytest.raises(ValidationError):
        game.create_world_boss_schedule("admin", schedule_data(interval_days=0))
    with pytest.raises(ValidationError):
        game.create_world_boss_schedule("admin", schedule_data(spawn_hour=24))
    with pytest.raises(NotFoundError):
        game.create_world_boss_schedule("admin", schedule_data(group_id=999999))
    sched = game.create_world_boss_schedule("admin", schedule_data(group_id=home_group))
    assert sched["group_id"] == home_group and sched["next_event"]
    game.delete_world_boss_schedule("admin", sched["schedule_id"])
    pid = make_farmer(game, "阿明", home_group)
    v = game.boss_current(home_group, pid)
    assert v["state"] == "ended" and v["schedule_id"] is None  # 活動紀錄保留，尚未結束的那一場一併結束


# 每位玩家一次的捐獻（倉庫 48 格以內，木材、石塊放素材背包）：糧食 80、醫療 60、戰鬥 70、裝備 80 積分
BATCH = [("crop", "carrot", 20), ("crop", "tomato", 10), ("goods", "egg", 10), ("material", "herb", 5),
         ("goods", "bread", 2), ("goods", "flour", 6), ("material", "wood", 20), ("material", "stone", 20)]


def _dragon_fight(game, clock, players: int) -> dict:
    groups = [game.create_group(f"伺服器{i}").id for i in range(3)]
    sched = game.create_world_boss_schedule("admin", schedule_data())
    event = sched["next_event"]
    clock._now = event["starts_at"]
    pids = []
    for i in range(players):
        gid = groups[i % 3]
        pid = make_farmer(game, f"玩家{i}", gid)
        for _cat, code, qty in BATCH:
            give(game, pid, code, qty)
        pids.append((gid, pid))

    def support(gp):
        gid, pid = gp
        for cat, code, qty in BATCH:
            donate(game, gid, pid, event["event_id"], cat, code, qty)

    with ThreadPoolExecutor(10) as pool:  # 10 位玩家同時操作
        list(pool.map(support, pids))
    clock._now = event["ends_at"] + timedelta(seconds=1)
    gid, pid = pids[0]
    return game.boss_current(gid, pid, event["event_id"])


def test_abyss_dragon_defeated_by_ten_players(game, clock):
    v = _dragon_fight(game, clock, 10)
    assert v["state"] == "defeated" and v["participants"] == 10 and v["hp"] == 0
    assert v["total_points"] == 10 * 290
    assert v["defeated_at"] < v["ends_at"]


@pytest.mark.parametrize("players", [1, 5])
def test_abyss_dragon_survives_small_groups(game, clock, players):
    v = _dragon_fight(game, clock, players)
    assert v["state"] == "ended" and v["hp"] > 0


def test_admin_website_schedules(client, game, clock, home_group):
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                       headers=ADMIN).status_code == 200
    body = {"name": "深淵魔龍", "group_id": None, "interval_days": 2, "spawn_hour": 21, "spawn_minute": 0,
            "anchor_date": "2026-01-01", "duration_minutes": 180, "max_hp": 15000, "enabled": True}
    res = client.post("/api/admin/world-boss-schedules", json=body, headers=ADMIN)
    assert res.status_code == 201, res.text
    sid = res.json()["schedule_id"]
    listing = client.get("/api/monitor/world-boss").json()
    sch = listing["schedules"][0]
    assert sch["name"] == "深淵魔龍" and sch["next_event"] and len(sch["upcoming_slots"]) == wb.SCHEDULE_PREVIEW
    assert sch["rewards"]["title"] == "降龍勇士"  # 沒有指定時套用深淵魔龍的預設獎勵
    assert listing["events"][0]["name"] == "深淵魔龍" and listing["events"][0]["schedule_id"] == sid
    res = client.put(f"/api/admin/world-boss-schedules/{sid}", json={**body, "max_hp": 18000,
                                                                     "rewards": SIMPLE_REWARDS}, headers=ADMIN)
    assert res.status_code == 200 and res.json()["max_hp"] == 18000
    assert client.put(f"/api/admin/world-boss-schedules/{sid}/enabled", json={"enabled": False},
                      headers=ADMIN).json()["enabled"] is False
    assert client.put(f"/api/admin/world-boss-schedules/{sid}", json={**body, "spawn_hour": 25},
                      headers=ADMIN).status_code == 422
    assert client.delete(f"/api/admin/world-boss-schedules/{sid}", headers=ADMIN).status_code == 200
    assert client.get("/api/monitor/world-boss").json()["schedules"] == []
    page = client.get("/").text
    assert 'id="wbsch-rows"' in page and 'id="wsc-name"' in page and "wbf-level" not in page


def test_panel_boss_image_only_in_intro_area(cmds, game, clock):
    """介紹區（主頁、排行榜、說明）附同一張固定圖：第一次上傳，區域內換頁沿用附件；戰鬥區與捐獻頁不附圖。"""
    from types import SimpleNamespace

    from app.discord_bot import farm_image
    from tests.test_discord_panel import last_edit, make_player, run

    assert farm_image.world_boss_png() == (farm_image.ASSETS / "scenes/world_boss.png").read_bytes()
    p = make_player(cmds, ALICE, GUILD_A)
    make_boss(game, clock, game.get_group_by_guild(str(GUILD_A)).id)
    existing = SimpleNamespace(filename="farm.png")

    def go(page):
        interaction = p._interaction()
        interaction.message = SimpleNamespace(attachments=[existing])
        p.panel.page = page
        run(p.panel.goto(interaction, page))
        p._update(interaction)
        return last_edit(interaction)["attachments"]

    interaction = p._interaction()
    interaction.message = SimpleNamespace(attachments=[existing])
    p.panel.page = "boss"
    run(p.panel.goto(interaction, "boss"))
    kw = last_edit(interaction)
    first = kw["attachments"]
    assert [f.filename for f in first] == ["farm.png"] and first[0] is not existing  # 從總覽換過來：上傳一次
    assert kw["embed"].image.url == "attachment://farm.png"
    for page in ("boss", "boss_board", "boss_help", "boss"):
        assert go(page) == [existing], page  # 介紹區同一張圖：沿用，不重新上傳
    for page in ("boss_battle", "boss_donate", "boss_battle"):
        assert go(page) == [], page  # 戰鬥區：不附圖片
        assert not p.panel.files
    assert go("boss") != [existing]  # 從戰鬥區回介紹區：重新附上圖片
    assert go("backpack") == []  # 離開世界 Boss 頁：移除


def test_panel_concurrent_double_click_no_error(cmds, game, clock, session_factory):
    """discord.py 會同時處理連點的兩次互動：兩個畫面重繪不能交錯（否則元件超過一列 5 個而出錯），捐獻也只算一次。"""
    from tests.test_discord_panel import make_player

    p = make_player(cmds, ALICE, GUILD_A)
    make_boss(game, clock, game.get_group_by_guild(str(GUILD_A)).id)
    give(game, p.panel.player_id, "carrot", 10)
    p.click("世界Boss")
    p.click("進入深淵魔龍")
    p.click("捐獻物資")
    p.choose("捐獻類別", "crop")
    p.choose("選擇物品", "0")
    p.click("× 5")
    confirm = p.button("確認捐獻")
    a, b = p._interaction(), p._interaction()

    async def both():
        await asyncio.gather(confirm.callback(a), confirm.callback(b))

    asyncio.run(both())
    for it in (a, b):
        edits = [kw for kind, _c, kw in it.log if kind == "edit" and "embed" in kw]
        assert edits and not (edits[-1]["embed"].title or "").startswith("⚠️"), edits[-1]["embed"].description
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(WorldBossContribution)) == 1
    assert game.get_backpack(p.panel.player_id).produce[0].quantity == 5
    # 一般按鈕（更新狀態）同時點兩下也不會出錯
    p.click("回戰鬥區")
    refresh = p.button("更新狀態")
    c, d = p._interaction(), p._interaction()

    async def twice():
        await asyncio.gather(refresh.callback(c), refresh.callback(d))

    asyncio.run(twice())
    for it in (c, d):
        edits = [kw for kind, _c, kw in it.log if kind == "edit" and "embed" in kw]
        assert edits and not (edits[-1]["embed"].title or "").startswith("⚠️")


def test_viewing_does_not_lock_or_write(game, clock, home_group, session_factory):
    """查看只推算 HP（不鎖活動列、不寫入），捐獻或背景結算時才寫回；顯示結果與寫回後一致。"""
    from app.db.models import WorldBossEvent

    pid = make_farmer(game, "阿明", home_group)
    eid = make_boss(game, clock, home_group, hours=2, rewards={**wb.DEFAULT_REWARDS, "min_points": 0})
    clock.advance(3600)
    assert game.boss_current(home_group, pid)["hp"] == 100_000 - 450
    assert game.boss_leaderboard(home_group, pid, eid)["participants"] == 0
    with session_factory() as s:
        assert s.get(WorldBossEvent, eid).hp == 100_000  # 查看沒有寫入
    give(game, pid, "carrot", 1)
    donate(game, home_group, pid, eid, "crop", "carrot", 1)
    with session_factory() as s:
        assert s.get(WorldBossEvent, eid).hp == 100_000 - 450 - 6  # 捐獻時把時間傷害一起寫回
    clock.advance(3601)
    v = game.boss_current(home_group, pid)
    assert v["state"] == "ended" and v["end_reason"] == "timeout" and v["ended_at"] == v["ends_at"]
    assert v["me"]["claim_state"] == "ready"  # 推算已結束就可以領獎（領獎時寫回結束狀態）
    with session_factory() as s:
        assert s.get(WorldBossEvent, eid).status == "active"
    assert game.boss_claim(home_group, pid, eid).gold_delta > 0
    with session_factory() as s:
        assert s.get(WorldBossEvent, eid).status == "ended"
