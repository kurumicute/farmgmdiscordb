"""古代遺跡（群組修復）：規則、捐獻（部分捐獻、不足、非目前階段）、階段推進、完成發稱號、遺跡區域的進入限制。"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import Farm, FarmTitle, GameTransaction
from app.game import exploration, ruins
from app.game.errors import InsufficientItemsError, ValidationError
from app.repositories import inventory as inventory_repo
from app.services.ranking_service import RankingService
from tests.conftest import make_farmer
from tests.test_exploration import Seq

SMALL_STAGES = (
    ruins.Stage("清除碎石", "", {"stone": 5, "wood": 5}),
    ruins.Stage("修補石柱", "", {"copper_ore": 3}),
    ruins.Stage("喚醒遺跡核心", "", {"gear": 2}),
)


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setattr(ruins, "STAGES", SMALL_STAGES)


@pytest.fixture
def explorer(session_factory, clock):
    return RankingService(session_factory, clock, explore_rng=lambda: 0.0)


def give(session_factory, player_id, item, qty):
    with session_factory.begin() as s:
        farm = s.scalar(select(Farm).where(Farm.player_id == player_id))
        inventory_repo.add(s, farm.id, item, qty)


def have(session_factory, player_id, item):
    with session_factory.begin() as s:
        farm = s.scalar(select(Farm).where(Farm.player_id == player_id))
        return inventory_repo.get_quantity(s, farm.id, item)


def set_level(session_factory, player_id, level):
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == player_id).values(level=level))


def total_gold_matches_transactions(session_factory):
    with session_factory.begin() as s:
        return s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))


# ---------- 規則 ----------


def test_rules_progress_and_remaining():
    assert ruins.COMPLETED == 3 and ruins.stage_info(3) is None
    assert ruins.percent(0, {}) == 0 and ruins.percent(3, {}) == 100
    assert ruins.remaining(0, {"stone": 100}) == {"stone": 50, "wood": 150}
    assert ruins.percent(1, {}) == 33 and not ruins.stage_done(0, {"stone": 150})
    assert ruins.stage_done(0, {"stone": 150, "wood": 999})
    assert len(ruins.TITLE) <= 20
    for st in ruins.STAGES:  # 每種素材都是山洞掉得到的素材
        assert set(st.materials) <= set(exploration.MATERIALS) - {exploration.FAKE_COIN}


def test_ruins_area_uses_forest_template():
    area, forest = exploration.AREAS["ruins"], exploration.AREAS["forest"]
    assert area.needs_ruins and not any(a.needs_ruins for c, a in exploration.AREAS.items() if c != "ruins")
    assert area.unlock_level == 4 and area.duration_seconds == 24 * 3600 and area.rolls == forest.rolls
    assert exploration.possible_items(area) == exploration.possible_items(forest)  # 掉落和古老森林一樣
    assert exploration.possible_items(area, 4) == exploration.possible_items(forest, 5)  # 等級門檻提前 1 級
    assert exploration.possible_items(area, 4)  # Lv.4 進得去就拿得到東西
    rng = Seq(*[0.0] * 50)
    assert len(exploration.explore("ruins", 4, rng, extra_rolls=2).types) == forest.rolls + 2


# ---------- 服務 ----------


def test_donate_partial_stage_advance_and_completion(service, session_factory, home_group, clock, small):
    a = make_farmer(service, "阿明", home_group)
    b = make_farmer(service, "小華", home_group)
    give(session_factory, a, "stone", 10)
    give(session_factory, b, "wood", 5)
    give(session_factory, b, "copper_ore", 3)
    give(session_factory, a, "gear", 2)

    st = service.ruins_status(a)
    assert st["stage"] == 0 and st["percent"] == 0 and not st["completed"] and st["mine"] == 0
    assert {m["code"]: (m["need"], m["have"]) for m in st["materials"]} == {"stone": (5, 10), "wood": (5, 0)}

    r = service.donate_ruins(a, "stone", 8)  # 只差 5 個：只捐 5，其餘留在背包
    assert r.quantity == 5 and "其餘留在背包" in r.message
    assert have(session_factory, a, "stone") == 5
    with pytest.raises(ValidationError, match="已經夠了"):
        service.donate_ruins(a, "stone", 1)
    with pytest.raises(ValidationError, match="只需要"):
        service.donate_ruins(a, "gear", 1)  # 不是目前階段的素材
    with pytest.raises(InsufficientItemsError):
        service.donate_ruins(a, "wood", 1)

    r = service.donate_ruins(b, "wood", 5)
    assert "下一階段" in r.message and service.ruins_status(a)["stage"] == 1
    service.donate_ruins(b, "copper_ore", 3)
    r = service.donate_ruins(a, "gear", 2)
    assert "修復完成" in r.message
    st = service.ruins_status(b)
    assert st["completed"] and st["percent"] == 100 and st["completed_at"] == clock.now()
    assert [(name, qty) for name, _t, qty in st["top"]] == [("小華的農場", 8), ("阿明的農場", 7)]
    with pytest.raises(ValidationError, match="已經修復完成"):
        service.donate_ruins(a, "stone", 1)

    with session_factory.begin() as s:  # 出過力的成員都拿到稱號；交易紀錄有捐獻、金幣不變式成立
        titles = s.execute(select(FarmTitle.title, FarmTitle.source)).all()
        donated = s.scalar(select(func.sum(GameTransaction.quantity))
                           .where(GameTransaction.event_type == "ruins_donate"))
    assert sorted(titles) == [(ruins.TITLE, ruins.TITLE_SOURCE)] * 2
    assert donated == 15 and total_gold_matches_transactions(session_factory)


def test_contributor_without_completion_gets_no_title(service, session_factory, home_group, small):
    a = make_farmer(service, "阿明", home_group)
    give(session_factory, a, "stone", 5)
    service.donate_ruins(a, "stone", 5)
    with session_factory.begin() as s:
        assert s.scalar(select(func.count()).select_from(FarmTitle)) == 0


def test_ruins_area_requires_own_group_repaired(explorer, session_factory, home_group, clock, small):
    a = make_farmer(explorer, "阿明", home_group)
    other = explorer.create_group("別的群組").id
    c = make_farmer(explorer, "小美", other)
    for pid in (a, c):
        set_level(session_factory, pid, 4)
    with pytest.raises(ValidationError, match="還沒修好"):
        explorer.explore(a, "ruins")
    area = next(x for x in explorer.exploration_overview(a)["areas"] if x["code"] == "ruins")
    assert not area["unlocked"] and area["needs_ruins"] and not area["ruins_open"]

    for item, n in (("stone", 5), ("wood", 5), ("copper_ore", 3), ("gear", 2)):
        give(session_factory, a, item, n)
        explorer.donate_ruins(a, item, n)

    with pytest.raises(ValidationError, match="還沒修好"):  # 別的群組沒修，不能進
        explorer.explore(c, "ruins")
    set_level(session_factory, a, 3)
    with pytest.raises(ValidationError, match="Lv.4"):
        explorer.explore(a, "ruins")
    set_level(session_factory, a, 4)
    explorer.explore(a, "grassland")  # 一般探險進行中，遺跡照樣可以額外探索
    r = explorer.explore(a, "古代遺跡")
    assert r["instant"] and r["area_name"] == "古代遺跡" and r["extra_rolls"] == 1 and r["bonus_rolls"] == 0
    assert r["items"] == [{"code": "wood", "name": "木材", "quantity": 15, "rarity": "普通"}]  # 森林模板 4 份 + 多 1 份
    assert have(session_factory, a, "wood") == 15  # 立即放進背包
    with pytest.raises(ValidationError, match="今天已經探索過"):
        explorer.explore_ruins(a)
    area = next(x for x in explorer.exploration_overview(a)["areas"] if x["code"] == "ruins")
    assert area["unlocked"] and not area["ready"] and area["next_at"] == clock.now() + timedelta(days=1)
    clock.advance(exploration.AREAS["ruins"].duration_seconds)
    assert explorer.explore_ruins(a)["items"]  # 冷卻 1 天後可以再探索
    assert explorer.claim_exploration(a)["area"] == "grassland"  # 一般探險不受影響


# ---------- 遺跡守護者 ----------

DAY = 24 * 3600


def repair(svc, session_factory, pid):
    for item, n in (("stone", 5), ("wood", 5), ("copper_ore", 3), ("gear", 2)):
        give(session_factory, pid, item, n)
        svc.donate_ruins(pid, item, n)


def test_guardian_rules():
    from datetime import UTC, date, datetime

    done = datetime(2026, 1, 1, tzinfo=UTC)  # 台灣週四
    assert ruins.week_start(done) == date(2025, 12, 29) and ruins.first_guardian_week(done) == date(2026, 1, 5)
    assert not ruins.guardian_active(done, datetime(2026, 1, 4, 15, 59, tzinfo=UTC))  # 台灣週日 23:59
    assert ruins.guardian_active(done, datetime(2026, 1, 4, 16, 0, tzinfo=UTC))
    assert ruins.weeks_to_settle(done, None, datetime(2026, 1, 12, tzinfo=UTC)) == [date(2026, 1, 5)]
    assert ruins.weeks_to_settle(done, date(2026, 1, 5), datetime(2026, 1, 19, tzinfo=UTC)) == [date(2026, 1, 12)]
    assert ruins.guardian_hp(1) == 4000 and ruins.guardian_hp(10_000) == ruins.GUARDIAN_HP_MAX == 40000
    assert ruins.material_damage("stone") == 3 and ruins.material_damage("machine_core") == 80
    assert ruins.material_damage(exploration.FAKE_COIN) is None and ruins.material_damage("wheat") is None


def test_guardian_attack_defeat_bonus_and_collapse(explorer, session_factory, home_group, clock, small):
    a = make_farmer(explorer, "阿明", home_group)
    set_level(session_factory, a, 4)
    repair(explorer, session_factory, a)  # 2026-01-01（台灣週四）修好
    g = explorer.ruins_status(a)["guardian"]
    assert not g["active"] and g["hp_max"] == 4000
    give(session_factory, a, "ancient_core", 60)
    with pytest.raises(ValidationError, match="還沒出現"):
        explorer.attack_guardian(a, "ancient_core", 1)

    clock.advance(4 * DAY)  # 台灣週一 08:00：守護者出現
    g = explorer.ruins_status(a)["guardian"]
    assert g["active"] and not g["defeated"] and {w["code"] for w in g["weapons"]} == {"ancient_core"}
    with pytest.raises(ValidationError, match="假幣"):
        explorer.attack_guardian(a, exploration.FAKE_COIN, 1)
    r = explorer.attack_guardian(a, "ancient_core", 10)
    assert "**800**" in r.message and explorer.ruins_status(a)["guardian"]["damage"] == 800
    r = explorer.attack_guardian(a, "ancient_core", 50)  # 剩 3200 HP：只需要 40 個
    assert r.quantity == 40 and "倒下了" in r.message and have(session_factory, a, "ancient_core") == 10
    with pytest.raises(ValidationError, match="已經被打倒"):
        explorer.attack_guardian(a, "ancient_core", 1)
    trip = explorer.explore(a, "ruins")
    assert trip["bonus_rolls"] == 2 and trip["items"][0]["quantity"] == 3 * (4 + 1 + 2)  # 獎勵加倍：多 2 份
    with session_factory.begin() as s:
        attacks = s.scalar(select(func.count()).select_from(GameTransaction)
                           .where(GameTransaction.event_type == "ruins_guardian"))
    assert attacks == 2 and total_gold_matches_transactions(session_factory)

    clock.advance(7 * DAY)  # 第二週：探索遺跡自動造成傷害，但沒打倒
    got = explorer.explore_ruins(a)
    assert "造成 200 點傷害" in got["guardian_note"] and got["bonus_rolls"] == 0
    assert explorer.ruins_status(a)["guardian"]["damage"] == 200
    clock.advance(7 * DAY)  # 第二週結束：遺跡崩塌回第三階段
    st = explorer.ruins_status(a)
    assert st["stage"] == 2 and not st["completed"] and st["collapsed_at"] is not None and st["guardian"] is None
    with pytest.raises(ValidationError, match="還沒修好"):
        explorer.explore(a, "ruins")
    give(session_factory, a, "gear", 2)
    explorer.donate_ruins(a, "gear", 2)  # 重新捐核心就修好
    assert explorer.ruins_status(a)["completed"]
