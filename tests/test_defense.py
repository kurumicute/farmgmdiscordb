"""防盜與農場防守。"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.auth import AdminAuth
from app.cli.commands import CommandHandler
from app.db.models import Farm, GameTransaction, StealRecord
from app.db.session import make_engine, make_session_factory
from app.game import balance, rules
from app.game.errors import (
    AlreadyExistsError,
    AlreadyStolenError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.main import create_app
from app.services.social_service import SocialService
from tests.conftest import grow, make_farmer

ALL = [("scarecrow", 0.25), ("guard_dog", 0.30), ("fence", 0.20)]


class Dice:
    """可控制的亂數：依序回傳 values（用完後重複最後一個），並記錄呼叫次數。"""

    def __init__(self, *values: float) -> None:
        self.values = list(values) or [0.99]
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.calls += 1
            return self.values[min(self.calls - 1, len(self.values) - 1)]


def no_roll() -> float:
    raise AssertionError("不該進行防守判定")


@pytest.fixture
def world(service, session_factory):
    g = service.create_group("防守群").id
    thief = make_farmer(service, "小偷", g)
    thief2 = make_farmer(service, "小偷二號", g)
    owner = make_farmer(service, "農夫", g)
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == owner).values(gold=10_000))
    return {"g": g, "thief": thief, "thief2": thief2, "owner": owner}


def with_dice(session_factory, clock, dice) -> SocialService:
    return SocialService(session_factory, clock, rng=dice)


def ripe(service, clock, owner, plots, crop="strawberry"):
    for n in plots:
        grow(service, owner, n, crop)
    clock.advance(600)


def qty(service, pid, code):
    return next((i.quantity for i in service.get_inventory(pid).items if i.item_code == code), 0)


def remaining(service, pid, n):
    return service.get_farm_status(pid).plots[n - 1].remaining_quantity


def records(session_factory):
    with session_factory() as s:
        return [(r.thief_player_id, r.plot_number, r.outcome, r.blocked_by) for r in s.scalars(select(StealRecord))]


# ---------- 判定規則（純函式） ----------


def test_resolve_defense_order_intervals_and_cap():
    # 稻草人 [0, .25)、警戒犬 [.25, .475)、防盜圍欄 [.475, .58)
    assert rules.resolve_defense(ALL, 0.0) == ("scarecrow", pytest.approx(0.58))
    assert rules.resolve_defense(ALL, 0.2499)[0] == "scarecrow"
    assert rules.resolve_defense(ALL, 0.25)[0] == "guard_dog"
    assert rules.resolve_defense(ALL, 0.4749)[0] == "guard_dog"
    assert rules.resolve_defense(ALL, 0.475)[0] == "fence"
    assert rules.resolve_defense(ALL, 0.5799)[0] == "fence"
    assert rules.resolve_defense(ALL, 0.58)[0] is None
    assert rules.resolve_defense([], 0.0) == (None, 0.0)
    # 上限：整體阻止機率不會超過 cap
    blocked, chance = rules.resolve_defense(ALL, 0.5, cap=0.4)
    assert blocked is None and chance == pytest.approx(0.4)
    assert rules.resolve_defense(ALL, 0.399, cap=0.4)[0] == "guard_dog"
    assert balance.DEFENSE_MAX_BLOCK_CHANCE == 0.60


def test_defense_coverage():
    assert rules.defense_covers(3, 2, 2) and rules.defense_covers(3, 2, 4)
    assert not rules.defense_covers(3, 2, 1) and not rules.defense_covers(3, 2, 5)
    assert rules.defense_covers(None, None, 99)


# ---------- 購買與放置 ----------


def test_purchase_limits_and_gold(service, world):
    owner = world["owner"]
    assert "第 1 個" in service.buy_defense(owner, "稻草人").message
    assert "第 2 個" in service.buy_defense(owner, "scarecrow").message
    with pytest.raises(AlreadyExistsError):
        service.buy_defense(owner, "稻草人")
    service.buy_defense(owner, "警戒犬")
    with pytest.raises(AlreadyExistsError):
        service.buy_defense(owner, "dog")
    service.buy_defense(owner, "圍欄")
    with pytest.raises(AlreadyExistsError):
        service.buy_defense(owner, "fence")
    assert service.get_farm_status(owner).gold == 10_000 - 500 * 2 - 1500 - 1000
    with pytest.raises(NotFoundError):
        service.buy_defense(owner, "機關槍")
    with pytest.raises(InsufficientGoldError):
        service.buy_defense(world["thief"], "警戒犬")  # 只有 500 金幣
    assert service.defense_status(world["thief"])["devices"] == []
    assert service.get_farm_status(world["thief"]).gold == 500


def test_concurrent_purchases_respect_limit_and_charge_once(service, world, session_factory):
    owner = world["owner"]
    barrier = threading.Barrier(6)
    errors = []

    def buy(key):
        barrier.wait()
        try:
            service.buy_defense(owner, key)
        except AlreadyExistsError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=buy, args=(k,)) for k in ["稻草人"] * 4 + ["警戒犬"] * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert service.defense_status(owner)["owned"] == {"scarecrow": 2, "guard_dog": 1, "fence": 0}
    assert len(errors) == 3
    assert service.get_farm_status(owner).gold == 10_000 - 1000 - 1500
    with session_factory() as s:
        assert s.scalar(select(func.count()).where(GameTransaction.event_type == "buy_item")) == 3


def test_placement_and_status(service, world):
    owner = world["owner"]
    for d in ("稻草人", "稻草人", "圍欄", "警戒犬"):
        service.buy_defense(owner, d)
    st = service.defense_status(owner)
    assert {(d["code"], d["slot"], d["start_plot"], d["end_plot"]) for d in st["devices"]} == {
        ("scarecrow", 1, 1, 3),
        ("scarecrow", 2, 1, 3),
        ("fence", 1, 1, 4),
        ("guard_dog", 1, None, None),
    }
    # 兩個稻草人重疊只計算一次：稻草人＋警戒犬＋圍欄 = 58%
    assert st["plots"][0]["chance"] == pytest.approx(0.58)
    assert service.place_defense(owner, "稻草人", 2, slot=2).message.endswith("第 2–4 格。")
    with pytest.raises(ValidationError):
        service.place_defense(owner, "稻草人", 9)  # 只有 4 格土地
    with pytest.raises(ValidationError):
        service.place_defense(owner, "警戒犬", 1)  # 守護全部，不需放置
    with pytest.raises(ValidationError):
        service.place_defense(owner, "圍欄", 1, slot=2)  # 圍欄只有 1 組
    service.place_defense(owner, "圍欄", 3)
    chances = [p["chance"] for p in service.defense_status(owner)["plots"]]
    assert chances[0] == pytest.approx(1 - 0.75 * 0.7)  # 第 1 格：稻草人＋警戒犬
    assert chances[3] == pytest.approx(0.58)  # 第 4 格：稻草人#2＋警戒犬＋圍欄


def test_place_unowned_device(service, world):
    with pytest.raises(NotFoundError):
        service.place_defense(world["owner"], "稻草人", 1)
    service.buy_defense(world["owner"], "稻草人")
    with pytest.raises(NotFoundError):
        service.place_defense(world["owner"], "稻草人", 1, slot=2)


# ---------- 各設施判定 ----------


@pytest.mark.parametrize(
    ("device", "block_roll", "pass_roll"),
    [("稻草人", 0.2499, 0.25), ("警戒犬", 0.2999, 0.30), ("圍欄", 0.1999, 0.20)],
)
def test_each_device_blocks_by_its_chance(service, world, clock, session_factory, device, block_roll, pass_roll):
    g, thief, thief2, owner = world["g"], world["thief"], world["thief2"], world["owner"]
    service.buy_defense(owner, device)
    ripe(service, clock, owner, [1])

    blocked = with_dice(session_factory, clock, Dice(block_roll)).steal_crop(g, thief, owner, 1, 2)
    assert blocked.success is False and blocked.quantity == 0 and "偷菜失敗" in blocked.message
    # 防守成功：原主人作物不減少，偷菜者沒有獲得物品
    assert remaining(service, owner, 1) == 5 and qty(service, thief, "strawberry") == 0
    with pytest.raises(AlreadyStolenError):  # 被阻止也用掉這次機會，不會重新判定
        with_dice(session_factory, clock, no_roll).steal_crop(g, thief, owner, 1, 1)

    ok = with_dice(session_factory, clock, Dice(pass_roll)).steal_crop(g, thief2, owner, 1, 2)
    assert ok.success is True and qty(service, thief2, "strawberry") == 2
    assert remaining(service, owner, 1) == 3
    code = service.find_defense(device)
    assert sorted(r[2:] for r in records(session_factory)) == [("blocked", code), ("success", None)]


def test_all_devices_one_round_in_order(service, world, clock, session_factory):
    g, thief, thief2, owner = world["g"], world["thief"], world["thief2"], world["owner"]
    for d in ("稻草人", "警戒犬", "圍欄"):
        service.buy_defense(owner, d)
    ripe(service, clock, owner, [1, 2])
    dice = Dice(0.46)
    assert "警戒犬" in with_dice(session_factory, clock, dice).steal_crop(g, thief, owner, 1, 1).message
    assert dice.calls == 1  # 每次偷菜只判定一輪
    assert with_dice(session_factory, clock, Dice(0.59)).steal_crop(g, thief2, owner, 1, 1).success  # 超過 58%
    assert "防盜圍欄" in with_dice(session_factory, clock, Dice(0.5)).steal_crop(g, thief, owner, 2, 1).message
    with session_factory() as s:
        chances = list(s.scalars(select(StealRecord.block_chance)))
    assert len(chances) == 3 and all(c == pytest.approx(0.58) for c in chances)


def test_unrelated_plots_are_not_affected(service, world, clock, session_factory):
    g, thief, owner = world["g"], world["thief"], world["owner"]
    service.buy_defense(owner, "稻草人")  # 守護第 1–3 格
    ripe(service, clock, owner, [1, 4])
    # 第 4 格不在任何設施範圍：完全不擲骰，照常偷菜
    assert with_dice(session_factory, clock, no_roll).steal_crop(g, thief, owner, 4, 2).success
    # 第 1 格被稻草人守住，第 4 格的結果不影響第 1 格
    assert not with_dice(session_factory, clock, Dice(0.1)).steal_crop(g, thief, owner, 1, 2).success
    assert remaining(service, owner, 1) == 5 and remaining(service, owner, 4) == 3
    assert qty(service, thief, "strawberry") == 2


def test_backpack_full_rejected_before_defense(service, world, clock, session_factory):
    g, thief, owner = world["g"], world["thief"], world["owner"]
    service.buy_defense(owner, "警戒犬")
    ripe(service, clock, owner, [1])
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == thief).values(gold=10_000))
    service.buy_seeds(thief, "wheat", 49)  # 背包 49/50
    with pytest.raises(WarehouseFullError):
        with_dice(session_factory, clock, no_roll).steal_crop(g, thief, owner, 1, 2)
    assert records(session_factory) == []  # 沒有判定、沒有紀錄，機會沒有被消耗
    assert with_dice(session_factory, clock, Dice(0.99)).steal_crop(g, thief, owner, 1, 1).success


# ---------- 並行、重複請求、重啟 ----------


def test_concurrent_thieves_on_defended_plot(service, world, clock, session_factory):
    g, thief, thief2, owner = world["g"], world["thief"], world["thief2"], world["owner"]
    service.buy_defense(owner, "警戒犬")
    ripe(service, clock, owner, [1])
    svc = with_dice(session_factory, clock, Dice(0.1, 0.9))  # 先到者被擋，後到者成功
    barrier = threading.Barrier(2)
    results = {}

    def run(pid):
        barrier.wait()
        results[pid] = svc.steal_crop(g, pid, owner, 1, 2)

    threads = [threading.Thread(target=run, args=(p,)) for p in (thief, thief2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.success for r in results.values()) == [False, True]
    got = qty(service, thief, "strawberry") + qty(service, thief2, "strawberry")
    assert got == 2 and remaining(service, owner, 1) == 3  # 只發放一次，物品守恆


def test_duplicate_requests_judged_once(service, world, clock, session_factory):
    g, thief, owner = world["g"], world["thief"], world["owner"]
    service.buy_defense(owner, "稻草人")
    ripe(service, clock, owner, [1])
    dice = Dice(0.1)
    svc = with_dice(session_factory, clock, dice)
    barrier = threading.Barrier(6)
    outcomes = []

    def run():
        barrier.wait()
        try:
            outcomes.append(svc.steal_crop(g, thief, owner, 1, 2).success)
        except AlreadyStolenError:
            outcomes.append("dup")

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes, key=str) == [False] + ["dup"] * 5
    assert dice.calls == 1 and len(records(session_factory)) == 1
    assert remaining(service, owner, 1) == 5 and qty(service, thief, "strawberry") == 0


def test_restart_keeps_devices_and_outcomes(service, world, clock, session_factory, database_url):
    g, thief, owner = world["g"], world["thief"], world["owner"]
    service.buy_defense(owner, "稻草人")
    service.place_defense(owner, "稻草人", 2)
    ripe(service, clock, owner, [2])
    assert not with_dice(session_factory, clock, Dice(0.0)).steal_crop(g, thief, owner, 2, 1).success
    engine = make_engine(database_url)
    try:
        restarted = SocialService(make_session_factory(engine), clock, rng=no_roll)
        st = restarted.defense_status(owner)
        assert [(d["code"], d["start_plot"]) for d in st["devices"]] == [("scarecrow", 2)]
        with pytest.raises(AlreadyStolenError):  # 重啟後也不會重新判定
            restarted.steal_crop(g, thief, owner, 2, 1)
        assert restarted.get_farm_status(owner).gold == 10_000 - 500 - 60
    finally:
        engine.dispose()


def test_steal_targets_with_defense(service, world, clock, session_factory):
    g, thief, owner = world["g"], world["thief"], world["owner"]
    service.buy_defense(owner, "警戒犬")
    ripe(service, clock, owner, [1])
    assert [t.plot_number for t in service.steal_targets(g, thief)] == [1]  # 偷菜者看不出有防守
    with_dice(session_factory, clock, Dice(0.0)).steal_crop(g, thief, owner, 1, 1)
    assert service.steal_targets(g, thief) == []  # 已判定過，不再列出


# ---------- 防守紀錄與巡查券 ----------


def test_defense_log_and_patrol(service, world, clock, session_factory):
    g, thief, thief2, owner = world["g"], world["thief"], world["thief2"], world["owner"]
    service.buy_defense(owner, "稻草人")
    ripe(service, clock, owner, [1])
    grow(service, thief2, 1, "wheat")  # 另一座農場的偷菜紀錄，不該出現在農夫的巡查中
    clock.advance(60)
    with_dice(session_factory, clock, Dice(0.0)).steal_crop(g, thief, owner, 1, 2)
    with_dice(session_factory, clock, Dice(0.9)).steal_crop(g, thief2, owner, 1, 1)
    with_dice(session_factory, clock, no_roll).steal_crop(g, owner, thief2, 1, 1)

    log = service.defense_log(owner)
    assert [(r["plot_number"], r["blocked_by"], r["quantity"]) for r in log] == [(1, "scarecrow", 2)]
    assert "thief_name" not in log[0] and "thief_player_id" not in log[0]

    with pytest.raises(InsufficientItemsError):
        service.patrol(owner)
    service.buy_item(owner, "patrol_ticket", 2)
    rows = service.patrol(owner)
    assert sorted((r["thief_name"], r["outcome"]) for r in rows) == [("小偷", "blocked"), ("小偷二號", "success")]
    assert all(r["victim_player_id"] == owner for r in rows)  # 只能看自己的農場
    assert qty(service, owner, "patrol_ticket") == 1  # 只消耗一張
    clock.advance(balance.PATROL_DAYS * 86_400 + 1)
    assert service.patrol(owner) == []  # 超過 7 天的不再顯示
    assert qty(service, owner, "patrol_ticket") == 0


# ---------- 網頁監控與 CMD ----------


def test_monitor_defense_api_read_only(service, world, clock, session_factory):
    g, thief, thief2, owner = world["g"], world["thief"], world["thief2"], world["owner"]
    service.buy_defense(owner, "稻草人")
    service.buy_defense(owner, "警戒犬")
    ripe(service, clock, owner, [1])
    with_dice(session_factory, clock, Dice(0.0)).steal_crop(g, thief, owner, 1, 2)
    with_dice(session_factory, clock, Dice(0.9)).steal_crop(g, thief2, owner, 1, 1)
    app = create_app(session_factory, clock=clock, allowed_hosts=["testserver"], admin_auth=AdminAuth(None, None))
    with TestClient(app) as c:
        farms = c.get("/api/monitor/defenses").json()
        assert [(f["player_name"], [(d["name"], d["end_plot"]) for d in f["devices"]]) for f in farms] == [
            ("農夫", [("警戒犬", None), ("稻草人", 3)])
        ]
        rows = c.get("/api/monitor/steals").json()
        assert sorted((r["thief_name"], r["outcome"], r["blocked_by_name"], r["quantity"]) for r in rows) == [
            ("小偷", "blocked", "稻草人", 2),
            ("小偷二號", "success", None, 1),
        ]
        assert "block_chance" not in rows[0]
        assert len(c.get("/api/monitor/steals", params={"outcome": "blocked"}).json()) == 1
        assert c.get("/api/monitor/steals", params={"outcome": "hack"}).status_code == 422
        summary = c.get("/api/monitor/summary").json()
        assert summary["steals_today"] == 1 and summary["blocks_today"] == 1
        assert c.post("/api/monitor/defenses", json={}).status_code == 405
    assert service.get_farm_status(owner).gold == 10_000 - 2000 - 60


def test_cli_defense_commands(service, world, clock, session_factory):
    g, thief, owner = world["g"], world["thief"], world["owner"]
    cli = CommandHandler(with_dice(session_factory, clock, Dice(0.0)))
    assert "請先選擇玩家" in cli.handle("defense")
    cli.handle(f"player use {owner}")
    shop = cli.handle("defense shop")
    assert "稻草人" in shop and "1500" in shop and "60%" in shop and "巡查券" in shop
    assert "已購買稻草人" in cli.handle("defense buy 稻草人")
    assert "一次只能購買 1 個" in cli.handle("defense buy 警戒犬 2")
    assert "已購買 2 個巡查券" in cli.handle("defense buy 巡查券 2")
    assert "沒有「大砲」" in cli.handle("defense buy 大砲")
    assert "第 2–4 格" in cli.handle("defense place 稻草人 2")
    status = cli.handle("defense")
    assert "稻草人：持有 1/2" in status
    assert "第  1 格：  0%（無防守）" in status and "第  2 格： 25%（稻草人）" in status
    assert "目前沒有被阻止的偷菜紀錄" in cli.handle("defense log")
    for n in (2, 3):
        grow(service, owner, n, "strawberry")
    clock.advance(600)

    cli.handle(f"player use {thief}")
    cli.handle(f"group use {g}")
    assert "偷菜失敗！農夫 的稻草人守住了第 2 格" in cli.handle(f"steal {owner} 2 2")
    assert "已經偷過" in cli.handle(f"steal {owner} 2 1")
    assert "沒有巡查券" in cli.handle("patrol")

    cli.handle(f"player use {owner}")
    assert "被稻草人阻止" in cli.handle("defense log") and "小偷" not in cli.handle("defense log")
    report = cli.handle("patrol")
    assert "已消耗 1 張巡查券" in report and "小偷（ID" in report and "被稻草人阻止" in report
    assert "用法" in cli.handle(f"patrol {thief}")  # 不能巡查其他玩家
