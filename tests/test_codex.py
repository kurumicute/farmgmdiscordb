"""圖鑑：收成、偷菜、收集動物產品、探險素材自動登記；累計數量、最高品質與分類統計；面板頁面。"""

import pytest
from sqlalchemy import update

from app.db.models import Farm, FarmPlot
from app.game import exploration
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer

DAY = 24 * 3600


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock, explore_rng=lambda: 0.0)


@pytest.fixture
def pid(admin, home_group, session_factory):
    p = make_farmer(admin, "收藏家", home_group)
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == p).values(gold=100_000))
    return p


def category(admin, pid, key):
    return next(c for c in admin.codex(pid)["categories"] if c["key"] == key)


def entry(admin, pid, key, code):
    return next(e for e in category(admin, pid, key)["entries"] if e["code"] == code)


def test_empty_codex_lists_everything_hidden(admin, pid):
    c = admin.codex(pid)
    assert c["discovered"] == 0 and c["total"] > 0
    assert [x["key"] for x in c["categories"]] == ["crop", "animal", "goods", "material", "fish"]
    crops = category(admin, pid, "crop")
    codes = {e["code"] for e in crops["entries"]}
    assert {"wheat", "amber_wheat"} <= codes  # 作物與珍稀變種各一項
    assert category(admin, pid, "material")["total"] == len(exploration.MATERIALS)


def test_harvest_registers_quantity_and_best_quality(admin, pid, clock, session_factory):
    grow(admin, pid, 1, "wheat")
    clock.advance(600)
    q1 = admin.harvest_crop(pid, 1).quantity
    e = entry(admin, pid, "crop", "wheat")
    assert e["got"] and e["quantity"] == q1 and e["best_quality"] == "普通"
    # 下一批是傳說品質：最高品質更新、數量累加
    grow(admin, pid, 1, "wheat")
    with session_factory.begin() as s:
        s.execute(update(FarmPlot).where(FarmPlot.plot_number == 1).values(quality="legendary"))
    clock.advance(600)
    q2 = admin.harvest_crop(pid, 1).quantity
    e = entry(admin, pid, "crop", "wheat")
    assert e["quantity"] == q1 + q2 and e["best_quality"] == "傳說"
    assert category(admin, pid, "crop")["discovered"] == 1


def test_animal_and_exploration_register(admin, pid, clock):
    admin.build(pid, "畜舍")
    admin.buy_animals(pid, "雞", 1)
    admin.feed_animal(pid, 1)
    clock.advance(DAY)
    admin.collect_animal(pid, 1)
    assert entry(admin, pid, "animal", "egg")["got"]

    admin.explore(pid, "grassland")
    clock.advance(exploration.AREAS["grassland"].duration_seconds)
    items = admin.claim_exploration(pid)["items"]
    for item in items:
        if item["code"] in exploration.MATERIALS:
            assert entry(admin, pid, "material", item["code"])["quantity"] == item["quantity"]


def test_steal_registers_for_thief(admin, pid, home_group, clock):
    thief = make_farmer(admin, "小偷", home_group)
    grow(admin, pid, 1, "carrot")
    clock.advance(600)
    admin.steal_crop(home_group, thief, pid, 1, 1)
    assert entry(admin, thief, "crop", "carrot")["got"]
    assert not entry(admin, pid, "crop", "carrot")["got"]  # 被偷的人沒有取得
