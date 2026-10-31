"""群組、偷菜與排行榜。"""

import threading

import pytest
from sqlalchemy import func, select, update

from app.db.models import Farm, GameTransaction, StealRecord
from app.game import balance
from app.game.errors import (
    AlreadyExistsError,
    AlreadyStolenError,
    InsufficientItemsError,
    NotGroupMemberError,
    PlotEmptyError,
    PlotNotReadyError,
    PlotNotWateredError,
    PlotWitheredError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import PlotState
from tests.conftest import grow, make_farmer


@pytest.fixture
def world(service):
    """農場主要群組：a、b、c 屬 g1；d 屬 g2；e 屬 g3。另外 a 也加入 g2（但農場仍屬 g1）。"""
    g1 = service.create_group("一號群").id
    g2 = service.create_group("二號群").id
    g3 = service.create_group("三號群").id
    ids = {name: make_farmer(service, name, g1) for name in ("a", "b", "c")}
    ids["d"] = make_farmer(service, "d", g2)
    ids["e"] = make_farmer(service, "e", g3)
    service.add_member(g2, ids["a"])
    return {**ids, "g1": g1, "g2": g2, "g3": g3}


def qty(service, pid, code):
    return next((i.quantity for i in service.get_inventory(pid).items if i.item_code == code), 0)


def plot_of(service, pid, number):
    return service.get_farm_status(pid).plots[number - 1]


# ---------- 群組 ----------


def test_multiple_groups_and_player_in_many_groups(service, world):
    groups = service.list_groups(world["a"])
    assert [(g.name, g.member_count, g.is_member) for g in groups] == [
        ("一號群", 3, True), ("二號群", 2, True), ("三號群", 1, False)
    ]
    assert [g.is_member for g in service.list_groups(world["b"])] == [True, False, False]
    assert service.add_member(world["g1"], world["a"]) is False  # 重複加入不會重複建立
    members = service.group_members(world["g1"])
    assert [m["player_name"] for m in members] == ["a", "b", "c"]
    assert members[0]["farm"]["gold"] == 500


def test_remove_member_keeps_farm_and_history(service, world, clock, session_factory):
    grow(service, world["c"], 1, "wheat")
    assert service.remove_member(world["g1"], world["c"]) is True
    status = service.get_farm_status(world["c"])
    assert status.gold == 490 and status.plots[0].state == PlotState.GROWING
    with session_factory() as s:
        assert s.scalar(select(func.count()).where(GameTransaction.farm_id == status.farm_id)) == 4
    names = [r["player_name"] for r in service.leaderboard("money", group_id=world["g1"], player_id=world["a"])]
    assert "c" not in names
    assert "c" in [r["player_name"] for r in service.leaderboard("money")]


def test_discord_guild_mapping(service):
    g = service.create_group("Discord 伺服器", discord_guild_id="123456789012345678")
    assert service.get_group_by_guild("123456789012345678").id == g.id
    assert service.get_group_by_guild("999") is None
    with pytest.raises(AlreadyExistsError):
        service.create_group("重複", discord_guild_id="123456789012345678")
    with pytest.raises(ValidationError):
        service.create_group("非法", discord_guild_id="abc")


# ---------- 偷菜目標 ----------


def test_steal_targets_only_valid_plots_in_current_group(service, world, clock):
    a, b, c, d = world["a"], world["b"], world["c"], world["d"]
    grow(service, c, 1, "strawberry")
    clock.advance(600 + balance.WITHER_SECONDS)  # c 的草莓已乾枯
    grow(service, b, 1, "wheat")  # 成熟
    grow(service, a, 1, "wheat")  # 自己的
    grow(service, d, 1, "wheat")  # 其他群組
    grow(service, c, 2, "wheat")  # 之後被偷光
    clock.advance(60)
    grow(service, b, 2, "carrot")  # 生長中
    service.buy_seeds(b, "tomato", 1)
    service.plant_crop(b, 3, "tomato")  # 等待澆水
    service.steal_crop(world["g1"], b, c, 2, 2)  # c 第 2 格被偷光 → 空地

    assert plot_of(service, c, 1).state == PlotState.WITHERED
    targets = service.steal_targets(world["g1"], a)
    assert [(t.player_id, t.plot_number, t.crop_name, t.remaining_quantity, t.max_steal) for t in targets] == [
        (b, 1, "小麥", 2, 2)
    ]
    assert targets[0].farm_name == "b的農場"
    # 另一個群組只看得到該群組成員
    assert [(t.player_id, t.plot_number) for t in service.steal_targets(world["g2"], a)] == [(d, 1)]
    # 非成員不能查看
    with pytest.raises(NotGroupMemberError):
        service.steal_targets(world["g1"], world["e"])


def test_steal_target_max_respects_remaining_and_capacity(service, world, clock, session_factory):
    a, b, c = world["a"], world["b"], world["c"]
    grow(service, b, 1, "wheat")
    clock.advance(60)
    service.steal_crop(world["g1"], c, b, 1, 1)
    assert service.steal_targets(world["g1"], a)[0].max_steal == 1  # 只剩 1 個

    grow(service, b, 2, "strawberry")
    clock.advance(600)
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == a).values(gold=10_000))
    service.buy_seeds(a, "wheat", 49)  # 背包 49/50
    targets = {t.plot_number: t for t in service.steal_targets(world["g1"], a)}
    assert targets[2].remaining_quantity == 5 and targets[2].max_steal == 1
    service.buy_seeds(a, "wheat", 1)  # 背包滿
    assert service.steal_targets(world["g1"], a) == []


def test_already_stolen_plot_not_listed_again(service, world, clock):
    grow(service, world["b"], 1, "strawberry")
    clock.advance(600)
    service.steal_crop(world["g1"], world["a"], world["b"], 1, 2)
    assert service.steal_targets(world["g1"], world["a"]) == []
    assert service.steal_targets(world["g1"], world["c"])[0].remaining_quantity == 3


# ---------- 偷菜操作 ----------


def test_steal_success_updates_both_sides(service, world, clock, session_factory):
    a, b = world["a"], world["b"]
    grow(service, b, 1, "strawberry")
    clock.advance(600)
    result = service.steal_crop(world["g1"], a, b, 1, 2)
    assert result.quantity == 2 and "偷走 2 個草莓" in result.message
    assert qty(service, a, "strawberry") == 2
    assert plot_of(service, b, 1).remaining_quantity == 3
    with session_factory() as s:
        rec = s.scalars(select(StealRecord)).one()
        assert (rec.group_id, rec.thief_player_id, rec.victim_player_id, rec.plot_number, rec.quantity) == (
            world["g1"], a, b, 1, 2
        )
        assert rec.created_at == clock.now()
        kinds = GameTransaction.event_type.in_(["steal", "stolen"])
        events = set(s.scalars(select(GameTransaction.event_type).where(kinds)))
        assert events == {"steal", "stolen"}
    # 主人收成拿到剩下的 3 個
    assert service.harvest_crop(b, 1).quantity == 3


def test_steal_until_empty_resets_plot(service, world, clock):
    grow(service, world["b"], 1, "wheat")
    clock.advance(60)
    result = service.steal_crop(world["g1"], world["a"], world["b"], 1, 2)
    assert "恢復為空地" in result.message
    assert plot_of(service, world["b"], 1).state == PlotState.EMPTY
    with pytest.raises(PlotEmptyError):
        service.harvest_crop(world["b"], 1)


def test_steal_validation(service, world, clock, session_factory):
    a, b, d, g1 = world["a"], world["b"], world["d"], world["g1"]
    grow(service, b, 1, "wheat")
    grow(service, d, 1, "strawberry")
    service.buy_seeds(b, "wheat", 1)
    service.plant_crop(b, 2, "wheat")  # 等待澆水
    grow(service, b, 3, "tomato")
    clock.advance(301)
    grow(service, b, 4, "carrot")  # 生長中

    for bad in (0, 3, -1):
        with pytest.raises(ValidationError):
            service.steal_crop(g1, a, b, 1, bad)
    with pytest.raises(ValidationError):
        service.steal_crop(g1, b, b, 1, 1)
    # 猜測其他群組玩家的 ID 與土地編號也無法繞過
    with pytest.raises(NotGroupMemberError) as info:
        service.steal_crop(g1, a, d, 1, 1)
    assert "草莓" not in str(info.value)
    with pytest.raises(NotGroupMemberError):
        service.steal_crop(g1, world["e"], b, 3, 1)
    with pytest.raises(PlotNotWateredError):
        service.steal_crop(g1, a, b, 2, 1)
    with pytest.raises(PlotNotReadyError):
        service.steal_crop(g1, a, b, 4, 1)

    service.steal_crop(g1, a, b, 3, 1)
    with pytest.raises(AlreadyStolenError):
        service.steal_crop(g1, a, b, 3, 1)  # 重複指令
    # 今天已偷過 b 的 1 格田，b 的其他土地也不能再偷
    with pytest.raises(AlreadyStolenError) as info:
        service.steal_crop(g1, a, b, 1, 1)
    assert "每天只能偷同一位玩家 1 格田" in str(info.value)

    # 背包滿（另一位偷菜者 c，今天還沒偷過 b）
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == world["c"]).values(gold=10_000))
    service.buy_seeds(world["c"], "wheat", 50 - sum(i.quantity for i in service.get_inventory(world["c"]).items))
    with pytest.raises(WarehouseFullError):
        service.steal_crop(g1, world["c"], b, 1, 1)

    clock.advance(600 + balance.WITHER_SECONDS)
    with pytest.raises(PlotWitheredError):
        service.steal_crop(g1, world["c"], b, 1, 1)


def test_steal_one_plot_per_victim_per_day(service, world, clock):
    g1, a, b, c = world["g1"], world["a"], world["b"], world["c"]
    for plot in (1, 2):
        grow(service, b, plot, "wheat")
    grow(service, c, 1, "wheat")
    clock.advance(60)
    assert {(t.player_id, t.plot_number) for t in service.steal_targets(g1, a)} == {(b, 1), (b, 2), (c, 1)}
    service.steal_crop(g1, a, b, 1, 1)
    # 今天偷過 b 一格後，b 的其他土地不再列出；其他玩家不受影響
    assert {(t.player_id, t.plot_number) for t in service.steal_targets(g1, a)} == {(c, 1)}
    with pytest.raises(AlreadyStolenError):
        service.steal_crop(g1, a, b, 2, 1)
    service.steal_crop(g1, a, c, 1, 1)
    # 台灣時間隔天 0 點後重新計算（測試起點為台灣 08:00，再過 16 小時即隔天 0 點）
    clock.advance(16 * 3600 - 60)
    grow(service, b, 3, "wheat")
    clock.advance(60)
    assert service.steal_crop(g1, a, b, 3, 1).success


def test_steal_more_than_remaining(service, world, clock):
    grow(service, world["b"], 1, "wheat")
    clock.advance(60)
    service.steal_crop(world["g1"], world["c"], world["b"], 1, 1)
    with pytest.raises(InsufficientItemsError):
        service.steal_crop(world["g1"], world["a"], world["b"], 1, 2)
    assert plot_of(service, world["b"], 1).remaining_quantity == 1


def _concurrently(fns):
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:  # noqa: BLE001
            results[i] = ("err", exc)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_steals_never_duplicate(service, world, clock, session_factory):
    a, b, c, g1 = world["a"], world["b"], world["c"], world["g1"]
    grow(service, b, 1, "wheat")  # 剩餘 2
    clock.advance(60)
    results = _concurrently([lambda: service.steal_crop(g1, a, b, 1, 2), lambda: service.steal_crop(g1, c, b, 1, 2)])
    assert sorted(r[0] for r in results) == ["err", "ok"]
    assert all(isinstance(r[1], PlotEmptyError) for r in results if r[0] == "err")
    assert qty(service, a, "wheat") + qty(service, c, "wheat") == 2
    assert plot_of(service, b, 1).state == PlotState.EMPTY


def test_same_thief_concurrent_repeats_only_once(service, world, clock, session_factory):
    a, b, g1 = world["a"], world["b"], world["g1"]
    grow(service, b, 1, "strawberry")
    clock.advance(600)
    results = _concurrently([lambda: service.steal_crop(g1, a, b, 1, 1) for _ in range(6)])
    assert sum(1 for r in results if r[0] == "ok") == 1
    assert all(isinstance(r[1], AlreadyStolenError) for r in results if r[0] == "err")
    assert qty(service, a, "strawberry") == 1
    assert plot_of(service, b, 1).remaining_quantity == 4
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(StealRecord)) == 1


def test_steal_and_owner_harvest_race(service, world, clock):
    a, b, g1 = world["a"], world["b"], world["g1"]
    grow(service, b, 1, "strawberry")
    clock.advance(600)
    _concurrently([lambda: service.steal_crop(g1, a, b, 1, 2), lambda: service.harvest_crop(b, 1)])
    # 不論誰先，總量都等於 5，不會多也不會少
    assert qty(service, a, "strawberry") + qty(service, b, "strawberry") == 5


# ---------- 排行榜 ----------


def test_leaderboards(service, world, clock):
    a, b, c, d, g1 = world["a"], world["b"], world["c"], world["d"], world["g1"]
    grow(service, a, 1, "wheat")  # a: 490
    grow(service, b, 1, "strawberry")  # b: 440
    clock.advance(600)
    service.harvest_crop(a, 1)
    service.sell_crop(a, "wheat", 2)  # a: 520，販售 2 個 / 30
    service.steal_crop(g1, c, b, 1, 2)  # c 偷 2 個草莓，尚未販售
    service.harvest_crop(b, 1)  # b 收成 3 個，未販售

    sales = service.leaderboard("sales")
    assert [(r["player_name"], r["sold_quantity"], r["sales_income"]) for r in sales][:1] == [("a", 2, 30)]
    # 偷菜、收成、購買都不算販售
    assert {r["player_name"]: r["sold_quantity"] for r in sales} == {"a": 2, "b": 0, "c": 0, "d": 0, "e": 0}

    service.sell_crop(c, "strawberry", 2)  # 偷來的成功出售後才計入：2 個 / 50
    sales = service.leaderboard("sales")
    assert [(r["rank"], r["player_name"], r["sold_quantity"], r["sales_income"]) for r in sales[:2]] == [
        (1, "c", 2, 50),  # 數量相同時比較收入
        (2, "a", 2, 30),
    ]
    # 其餘 0 販售者依玩家 ID 排序
    assert [r["player_name"] for r in sales[2:]] == ["b", "d", "e"]

    money = service.leaderboard("money")
    assert [(r["player_name"], r["gold"]) for r in money] == [
        ("c", 550), ("a", 520), ("d", 500), ("e", 500), ("b", 440)
    ]
    assert money[0]["level"] == 1
    # 群組排行榜只含目前群組成員
    group_money = service.leaderboard("money", group_id=g1, player_id=a)
    assert [r["player_name"] for r in group_money] == ["c", "a", "b"]
    # a 雖然也是 g2 成員，但農場主要群組是 g1，因此不列入 g2 排行
    assert [r["player_name"] for r in service.leaderboard("sales", group_id=world["g2"], player_id=d)] == ["d"]
    with pytest.raises(NotGroupMemberError):
        service.leaderboard("money", group_id=g1, player_id=d)
    with pytest.raises(ValidationError):
        service.leaderboard("money", group_id=g1)
    # 資料不變時排名穩定
    assert service.leaderboard("money") == money
