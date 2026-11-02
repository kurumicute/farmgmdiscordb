"""釣魚系統：釣具商店、裝備、拋竿扣餌、咬鉤反應、拉扯、魚簍、販售、分享、權限、設定快照、Session 清理與併發。"""

import random
import threading

import pytest
from sqlalchemy import func, select, text

from app.db.models import FarmFish, FishingSession, FishShare, GameTransaction
from app.game import fishing
from app.game.errors import (
    AlreadyExistsError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
)
from app.services.admin_service import AdminService
from tests.conftest import make_farmer

BAMBOO = {"reel_power": 10, "control_pct": 0, "reaction_bonus_ms": 0}


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def fisher(session_factory, clock):
    """固定亂數來源的服務，結果可重現。"""
    return AdminService(session_factory, clock, fishing_rng=random.Random(7).random)


def settings_with(**changes) -> dict:
    s = {**fishing.DEFAULT_SETTINGS, "difficulty": {r: dict(v) for r, v in fishing.DEFAULT_DIFFICULTY.items()}}
    s.update(changes)
    return s


def equip_starter(svc, pid, bait=10):
    svc.set_farm_values("test", pid, gold=10_000)
    svc.buy_fishing_gear(pid, "bamboo_rod")
    svc.buy_fishing_gear(pid, "basic_line")
    svc.buy_fishing_gear(pid, "worm_bait", bait)


def bait_left(svc, pid) -> int:
    return svc.fishing_overview(pid)["bait_quantity"]


def cast_and_hook(svc, clock, pid) -> dict:
    view = svc.start_fishing(pid)
    clock._now = view["bite_at"]
    return svc.hook_fish(pid, view["id"])


def fight(svc, pid, view, rod=BAMBOO, policy=fishing.smart_action) -> dict:
    for _ in range(300):
        if view["phase"] != "fighting":
            return view
        state = {k: view[k] for k in ("intent", "tension", "distance", "stamina", "max_stamina", "max_tension")}
        view = svc.fishing_action(pid, view["id"], policy(state, None, rod), view["turn"])
    raise AssertionError("拉扯沒有結束")


# ---------- 釣具商店與裝備 ----------


def test_buy_gear_uses_farm_gold_and_auto_equips(admin, player_id):
    admin.set_farm_values("test", player_id, gold=1000)
    r = admin.buy_fishing_gear(player_id, "bamboo_rod")
    assert r.gold == 900 and r.gold_delta == -100
    admin.buy_fishing_gear(player_id, "basic_line")
    admin.buy_fishing_gear(player_id, "worm_bait", 5)
    ov = admin.fishing_overview(player_id)
    assert ov["rod"]["name"] == "竹製魚竿" and ov["line"]["name"] == "普通魚線" and ov["bait_quantity"] == 5
    assert ov["gold"] == 1000 - 100 - 50 - 15
    with pytest.raises(AlreadyExistsError):
        admin.buy_fishing_gear(player_id, "bamboo_rod")
    with pytest.raises(ValidationError):
        admin.buy_fishing_gear(player_id, "iron_rod")  # 需要 Lv.4
    shop = {i["code"]: i for i in admin.fishing_shop(player_id)["items"]}
    assert shop["bamboo_rod"]["owned"] and shop["bamboo_rod"]["equipped"]
    assert not shop["iron_rod"]["unlocked"] and shop["worm_bait"]["quantity"] == 5


def test_buy_gear_checks_gold_stock_and_records(admin, player_id, session_factory):
    admin.set_farm_values("test", player_id, gold=50)
    with pytest.raises(InsufficientGoldError):
        admin.buy_fishing_gear(player_id, "bamboo_rod")
    admin.update_fishing_gear("test", "worm_bait", {
        "name": "普通蚯蚓", "emoji": "🪱", "description": "", "price": 3, "required_level": 1, "stock": 2,
        "effects": {"rare_bp": 0, "epic_bp": 0, "legendary_bp": 0, "bite_speed_pct": 0}, "sort_order": 90,
    })
    with pytest.raises(InsufficientItemsError):
        admin.buy_fishing_gear(player_id, "worm_bait", 3)
    admin.buy_fishing_gear(player_id, "worm_bait", 2)
    assert {i["code"]: i for i in admin.fishing_shop(player_id)["items"]}["worm_bait"]["stock"] == 0
    with session_factory() as s:
        tx = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "fishing_buy")).one()
        assert tx.gold_delta == -6 and tx.quantity == 2


def test_equip_switches_within_kind(admin, player_id):
    admin.set_farm_values("test", player_id, gold=10_000, level=3)
    admin.buy_fishing_gear(player_id, "bamboo_rod")
    admin.buy_fishing_gear(player_id, "wooden_rod")
    assert admin.fishing_overview(player_id)["rod"]["code"] == "bamboo_rod"  # 已有裝備時不自動換
    admin.equip_fishing_gear(player_id, "wooden_rod")
    shop = {i["code"]: i for i in admin.fishing_shop(player_id)["items"]}
    assert shop["wooden_rod"]["equipped"] and not shop["bamboo_rod"]["equipped"]
    with pytest.raises(NotFoundError):
        admin.equip_fishing_gear(player_id, "pro_rod")


# ---------- 拋竿、咬鉤與拉扯 ----------


def test_cast_requires_gear_and_deducts_one_bait(admin, clock, player_id):
    with pytest.raises(ValidationError, match="還沒有裝備"):
        admin.start_fishing(player_id)
    equip_starter(admin, player_id, bait=3)
    view = admin.start_fishing(player_id)
    assert view["phase"] == "waiting" and bait_left(admin, player_id) == 2
    with pytest.raises(ValidationError, match="已經在釣魚"):
        admin.start_fishing(player_id)
    assert bait_left(admin, player_id) == 2  # 失敗的拋竿不扣餌
    with pytest.raises(ValidationError, match="還沒上鉤"):
        admin.hook_fish(player_id, view["id"])


def test_reaction_timeout_means_fish_escapes(admin, clock, player_id, session_factory):
    equip_starter(admin, player_id)
    view = admin.start_fishing(player_id)
    clock._now = view["react_deadline"]
    clock.advance(fishing.REACTION_GRACE_SECONDS + 1)
    late = admin.hook_fish(player_id, view["id"])
    assert late["status"] == "missed" and late["fish"] is None
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmFish)) == 0
    # 可以再拋竿
    assert admin.start_fishing(player_id)["phase"] == "waiting"


def test_sensible_play_lands_fish_once(fisher, clock, player_id, session_factory):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=100))
    equip_starter(fisher, player_id)
    view = cast_and_hook(fisher, clock, player_id)
    assert view["phase"] == "fighting" and view["turn"] == 0
    result = fight(fisher, player_id, view)
    assert result["status"] == "landed"
    fish = result["fish"]
    ftype = next(f for f in fishing.INITIAL_FISH if f[0] == fish["code"])
    assert ftype[4] <= fish["price"] <= ftype[5]
    # 重複送出同一個操作：Session 已結束，不會再發魚
    again = fisher.fishing_action(player_id, result["id"], "reel", result["turn"] - 1)
    assert again["status"] == "landed"
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmFish)) == 1
    assert fisher.fish_basket(player_id)["fish"][0]["id"] == fish["id"]


def test_stale_turn_is_not_applied_twice(fisher, clock, player_id):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=100))
    equip_starter(fisher, player_id)
    view = cast_and_hook(fisher, clock, player_id)
    first = fisher.fishing_action(player_id, view["id"], "wait", 0)
    assert first["turn"] == 1
    dup = fisher.fishing_action(player_id, view["id"], "wait", 0)  # 重複點擊（畫面上的回合還是 0）
    assert dup.get("stale") and dup["turn"] == 1


def test_action_timeout_and_give_up(admin, clock, player_id):
    equip_starter(admin, player_id)
    view = cast_and_hook(admin, clock, player_id)
    clock.advance(fishing.DEFAULT_SETTINGS["action_timeout_seconds"] + fishing.REACTION_GRACE_SECONDS + 1)
    assert admin.fishing_state(player_id)["status"] == "timeout"
    view = admin.start_fishing(player_id)
    gave_up = admin.give_up_fishing(player_id, view["id"])
    assert gave_up["status"] == "abandoned"


def test_failures_under_sensible_play_are_only_slips(fisher, clock, player_id):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=50))
    equip_starter(fisher, player_id, bait=40)
    statuses = []
    for _ in range(30):
        view = cast_and_hook(fisher, clock, player_id)
        statuses.append(fight(fisher, player_id, view)["status"])
        if fisher.fish_basket(player_id)["fish"]:
            fisher.sell_fish(player_id, None)
    # 依提示操作時只會因為脫鉤失敗（50% 基礎成功率）
    assert set(statuses) <= {"landed", "slipped"} and "slipped" in statuses and "landed" in statuses


def test_settings_change_does_not_affect_active_session(admin, clock, player_id, session_factory):
    equip_starter(admin, player_id)
    view = admin.start_fishing(player_id)
    admin.update_fishing_settings("test", settings_with(reaction_seconds=15, action_timeout_seconds=60))
    with session_factory() as s:
        row = s.get(FishingSession, view["id"])
        assert row.settings_version == 0 and row.settings["reaction_seconds"] == 5
    admin.give_up_fishing(player_id, view["id"])
    view2 = cast_and_hook(admin, clock, player_id)
    with session_factory() as s:
        assert s.get(FishingSession, view2["id"]).settings["reaction_seconds"] == 15


def test_settings_validation_requires_100_percent(admin):
    bad = settings_with(rarity_bp={"common": 8000, "rare": 1500, "epic": 400, "legendary": 50})
    with pytest.raises(ValidationError, match="100%"):
        admin.update_fishing_settings("test", bad)
    with pytest.raises(ValidationError):
        admin.update_fishing_settings("test", settings_with(base_success_pct=120))


def test_basket_full_blocks_casting(admin, clock, player_id, session_factory):
    admin.update_fishing_settings("test", settings_with(basket_capacity=5))
    equip_starter(admin, player_id)
    view = admin.start_fishing(player_id)
    with session_factory.begin() as s:  # 直接放 5 隻魚進魚簍
        for i in range(5):
            s.execute(text(
                "INSERT INTO fishing_sessions (farm_id, status, settings_version, settings, gear, fish_code, rarity, "
                "weight_g, price, state, turns, started_at, bite_at, react_deadline, ended_at) "
                "SELECT farm_id, 'landed', 0, settings, gear, 'loach', 'common', 100, 5, state, 1, "
                "started_at - interval '1 hour' - make_interval(secs => :i), "
                "bite_at - interval '1 hour', react_deadline - interval '1 hour', now() FROM fishing_sessions "
                "WHERE id = :sid"), {"i": i, "sid": view["id"]})
        s.execute(text(
            "INSERT INTO farm_fish (farm_id, session_id, fish_code, rarity, weight_g, price, caught_at) "
            "SELECT farm_id, id, 'loach', 'common', 100, 5, now() FROM fishing_sessions WHERE status = 'landed'"))
    admin.give_up_fishing(player_id, view["id"])
    with pytest.raises(ValidationError, match="魚簍已滿"):
        admin.start_fishing(player_id)


# ---------- 販售與權限 ----------


def land_one(svc, clock, pid) -> dict:
    view = cast_and_hook(svc, clock, pid)
    result = fight(svc, pid, view)
    assert result["status"] == "landed"
    return result["fish"]


def test_sell_fish_adds_gold_once(fisher, clock, player_id, session_factory):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=100))
    equip_starter(fisher, player_id)
    a, b = land_one(fisher, clock, player_id), land_one(fisher, clock, player_id)
    gold = fisher.fishing_overview(player_id)["gold"]
    r = fisher.sell_fish(player_id, [a["id"]])
    assert r.gold == gold + a["price"]
    with pytest.raises(ValidationError):
        fisher.sell_fish(player_id, [a["id"]])  # 已賣出
    r = fisher.sell_fish(player_id, None)
    assert r.gold == gold + a["price"] + b["price"]
    with pytest.raises(ValidationError):
        fisher.sell_fish(player_id, None)
    with session_factory() as s:
        total = s.scalar(select(func.sum(GameTransaction.gold_delta)).where(GameTransaction.event_type == "fish_sell"))
        assert total == a["price"] + b["price"]
    # 賣掉的魚仍在圖鑑
    codex = fisher.fish_codex(player_id)
    assert codex["discovered"] >= 1


def test_cannot_sell_or_share_other_players_fish(fisher, clock, player_id, home_group):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=100))
    equip_starter(fisher, player_id)
    fish = land_one(fisher, clock, player_id)
    other = make_farmer(fisher, "小華", home_group)
    with pytest.raises(ValidationError):
        fisher.sell_fish(other, [fish["id"]])
    with pytest.raises(NotFoundError):
        fisher.in_group_context(home_group).share_fish(other, fish["id"])
    assert fisher.fish_basket(player_id)["fish"][0]["id"] == fish["id"]


def test_share_fish_keeps_fish_and_needs_group(fisher, clock, player_id, home_group, session_factory):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=100))
    equip_starter(fisher, player_id)
    fish = land_one(fisher, clock, player_id)
    with pytest.raises(ValidationError, match="伺服器"):
        fisher.share_fish(player_id, fish["id"])  # 沒有群組（私訊）
    card = fisher.in_group_context(home_group).share_fish(player_id, fish["id"])
    assert card["name"] == fish["name"] and card["player_name"] == "阿明" and card["record"]["species"]
    with pytest.raises(ValidationError, match="剛剛"):
        fisher.in_group_context(home_group).share_fish(player_id, fish["id"])
    clock.advance(fishing.SHARE_COOLDOWN_SECONDS + 1)
    fisher.in_group_context(home_group).share_fish(player_id, fish["id"])
    assert len(fisher.fish_basket(player_id)["fish"]) == 1  # 分享不消耗
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FishShare)) == 2
    # 不是成員的群組不記錄為分享群組
    other_group = fisher.create_group("別的群組").id
    with pytest.raises(ValidationError, match="伺服器"):
        fisher.in_group_context(other_group).share_fish(player_id, fish["id"])


def test_leaderboard_and_codex(fisher, clock, player_id, home_group):
    fisher.update_fishing_settings("test", settings_with(base_success_pct=100))
    equip_starter(fisher, player_id)
    land_one(fisher, clock, player_id)
    board = fisher.fishing_leaderboard(home_group)
    assert board["catches"][0]["player_name"] == "阿明" and board["catches"][0]["count"] == 1
    assert board["best"][0]["player_name"] == "阿明"
    assert fisher.fishing_leaderboard(fisher.create_group("空群組").id)["catches"] == []


# ---------- Session 清理與併發 ----------


def test_cleanup_stale_sessions(admin, clock, player_id):
    equip_starter(admin, player_id)
    admin.start_fishing(player_id)
    assert admin.cleanup_fishing_sessions() == 0
    clock.advance(120)
    assert admin.cleanup_fishing_sessions() == 1
    assert admin.fishing_state(player_id)["status"] == "missed"


def test_concurrent_casts_deduct_bait_once(session_factory, clock, player_id):
    svc = AdminService(session_factory, clock)
    equip_starter(svc, player_id, bait=10)
    errors, ok = [], []
    barrier = threading.Barrier(6)

    def cast():
        barrier.wait()
        try:
            ok.append(svc.start_fishing(player_id)["id"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=cast) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 1 and len(errors) == 5
    assert bait_left(svc, player_id) == 9


def test_many_players_fish_at_the_same_time(session_factory, clock, home_group):
    svc = AdminService(session_factory, clock, fishing_rng=random.Random(3).random)
    svc.update_fishing_settings("test", settings_with(base_success_pct=100, reaction_seconds=15))
    players = [make_farmer(svc, f"玩家{i}", home_group) for i in range(8)]
    for pid in players:
        equip_starter(svc, pid)
    views = {pid: svc.start_fishing(pid) for pid in players}
    clock._now = max(v["bite_at"] for v in views.values())
    clock.advance(0.1)
    results, errors = {}, []

    def play(pid):
        try:
            view = svc.hook_fish(pid, views[pid]["id"])
            results[pid] = fight(svc, pid, view)["status"]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=play, args=(pid,)) for pid in players]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and set(results.values()) == {"landed"} and len(results) == 8
