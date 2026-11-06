"""寵物：領養與升級價格、貓的收成加成、狗擋下偷菜（設施沒擋住時再判定）、農場圖、面板操作。"""

import io

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.db.models import Farm, GameTransaction, StealRecord
from app.discord_bot import farm_image
from app.game import pets
from app.game.errors import AlreadyExistsError, InsufficientGoldError, NotFoundError, ValidationError
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import cmds, make_player  # noqa: F401  cmds 是 fixture


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def pid(admin, home_group):
    p = make_farmer(admin, "飼主", home_group)
    admin.set_farm_values("root", p, gold=1_000_000)  # 經 admin_adjust 寫交易紀錄
    return p


def gold(admin, pid):
    return admin.get_farm_status(pid).gold


def gold_matches_transactions(session_factory):
    with session_factory() as s:
        return s.scalar(select(func.sum(GameTransaction.gold_delta))) == s.scalar(select(func.sum(Farm.gold)))


def test_rules():
    assert [pets.bonus_pct(lv) for lv in (None, 0, 1, 2, 3, 4)] == [0, 0, 5, 10, 15, 20]
    assert [pets.upgrade_price(lv) for lv in range(5)] == [10_000, 20_000, 30_000, 40_000, None]
    assert pets.bonus_quantity(5, 10, 0.49) == 1 and pets.bonus_quantity(5, 10, 0.5) == 0  # 0.5 個 → 50% 多 1 個
    assert pets.bonus_quantity(20, 20, 0.99) == 4 and pets.bonus_quantity(3, 0, 0.0) == 0
    assert pets.blocker_name(pets.DOG_BLOCKER) == "狗狗" and pets.blocker_name("scarecrow") == "稻草人"


def test_buy_and_upgrade(admin, pid, session_factory, home_group):
    with pytest.raises(NotFoundError):
        admin.upgrade_pet(pid, pets.CAT)  # 還沒領養
    before = gold(admin, pid)
    assert "領養了貓咪" in admin.buy_pet(pid, pets.CAT).message
    assert gold(admin, pid) == before - pets.BUY_PRICE
    with pytest.raises(AlreadyExistsError):
        admin.buy_pet(pid, pets.CAT)
    for level, price in enumerate(pets.UPGRADE_PRICES, 1):
        before = gold(admin, pid)
        assert f"Lv{level}" in admin.upgrade_pet(pid, pets.CAT).message
        assert gold(admin, pid) == before - price
    with pytest.raises(ValidationError, match="最高等級"):
        admin.upgrade_pet(pid, pets.CAT)
    status = {x["code"]: x for x in admin.pet_status(pid)}
    assert status["cat"]["level"] == 4 and status["cat"]["pct"] == 20 and status["cat"]["next_price"] is None
    assert not status["dog"]["owned"] and status["dog"]["next_price"] == pets.BUY_PRICE
    assert admin.get_farm_status(pid).pets == {"cat": 4}
    with pytest.raises(NotFoundError):
        admin.buy_pet(pid, "hamster")
    poor = make_farmer(admin, "窮人", home_group)
    with pytest.raises(InsufficientGoldError):
        admin.buy_pet(poor, pets.DOG)
    assert gold_matches_transactions(session_factory)


def test_cat_adds_harvest(admin, pid, clock):
    grow(admin, pid, 1, "strawberry")  # 草莓收成 5 個
    clock.advance(600)
    assert admin.harvest_crop(pid, 1).quantity == 5  # 沒有貓
    admin.buy_pet(pid, pets.CAT)
    grow(admin, pid, 1, "strawberry")
    clock.advance(600)
    assert admin.harvest_crop(pid, 1).quantity == 5  # Lv0 還沒有加成
    admin.upgrade_pet(pid, pets.CAT)
    admin.upgrade_pet(pid, pets.CAT)  # Lv2：+10% → 5 × 10% = 0.5 個
    admin._pet_rng = lambda: 0.0
    grow(admin, pid, 1, "strawberry")
    clock.advance(600)
    r = admin.harvest_crop(pid, 1)
    assert r.quantity == 6 and "貓咪趕走老鼠 +1" in r.message
    admin._pet_rng = lambda: 0.99
    grow(admin, pid, 1, "strawberry")
    clock.advance(600)
    assert admin.harvest_crop(pid, 1).quantity == 5


def test_dog_blocks_steal_after_defenses(session_factory, clock, home_group):
    rolls = iter([0.1, 0.5])
    game = AdminService(session_factory, clock, rng=lambda: next(rolls))
    owner = make_farmer(game, "飼主", home_group)
    thief = make_farmer(game, "小偷", home_group)
    thief2 = make_farmer(game, "小偷二號", home_group)
    game.set_farm_values("root", owner, gold=200_000)  # 領養 + 升到 Lv4 共 105,000
    game.buy_pet(owner, pets.DOG)
    for _ in range(4):
        game.upgrade_pet(owner, pets.DOG)  # Lv4：20%
    grow(game, owner, 1, "strawberry")
    clock.advance(600)
    r = game.steal_crop(home_group, thief, owner, 1, 1)  # 沒有防守設施：狗擲 0.1 < 0.2 → 擋下
    assert not r.success and "狗狗" in r.message
    r2 = game.steal_crop(home_group, thief2, owner, 1, 1)  # 0.5 ≥ 0.2 → 偷到
    assert r2.success and r2.quantity == 1
    with session_factory() as s:
        blocked = s.scalar(select(StealRecord).where(StealRecord.outcome == "blocked"))
        assert blocked.blocked_by == pets.DOG_BLOCKER and blocked.block_chance == pytest.approx(0.2)
    assert any("狗狗" in r["blocked_by_name"] for r in game_monitor_steals(session_factory, clock))


def game_monitor_steals(session_factory, clock):
    from app.services.monitor_service import MonitorService

    snap = MonitorService(session_factory, clock).steals(limit=10)
    return [r for r in snap if r["blocked_by"]]


def test_farm_image_shows_pets():
    plots = [{"crop": None, "state": "empty"}] * 4
    plain = Image.open(io.BytesIO(farm_image.farm_png(plots)))
    with_pets = Image.open(io.BytesIO(farm_image.farm_png(plots, {"dog": 0, "cat": 2})))
    band = farm_image.manifest()["pets"]["band_height"]
    assert with_pets.size == (plain.width, plain.height + band)


def test_panel_adopt_and_upgrade(cmds, admin):  # noqa: F811
    p = make_player(cmds, ALICE)
    pid = p.panel.player_id
    admin.set_farm_values("root", pid, gold=100_000)
    p.go("animals")
    assert "寵物" in p.text and "還沒有領養" in p.text
    p.click("寵物（貓狗）")
    assert "🐾" in p.text and "領養" in p.text
    assert "每格實際多收" in p.text and "升到 Lv1（+5%）後" in p.text and "小麥（每格 2 個）：10% 機率 +1" in p.text
    p.click("領養狗狗")
    assert "再按一次" in p.text
    p.click("確認領養狗狗")
    assert "領養了狗狗" in p.text and p.panel.files  # 寵物頁附上有寵物的農場圖
    p.click("升級 Lv1狗狗")
    p.click("確認升級 Lv1狗狗")
    assert "Lv1" in p.text and "5%" in p.text
    assert admin.get_farm_status(pid).pets == {"dog": 1}


def test_cat_odds_text_on_pets_page(admin, pid, cmds):  # noqa: F811
    assert pets.odds_text(2, 20) == "40% 機率 +1"  # 小麥滿級
    assert pets.odds_text(5, 20) == "固定 +1"  # 草莓滿級
    assert pets.odds_text(4, 20) == "80% 機率 +1"
    assert pets.odds_text(5, 25) == "固定 +1，另有 25% 機率再 +1"
    assert pets.odds_text(3, 15) == "45% 機率 +1" and pets.odds_text(1, 0) == "不會多"
    cat = next(x for x in admin.pet_status(pid) if x["code"] == pets.CAT)
    assert not cat["odds"]["current"] and cat["odds"]["pct"] == pets.PCT_PER_LEVEL  # 還沒領養：看 Lv1
    admin.buy_pet(pid, pets.CAT)
    for _ in range(pets.MAX_LEVEL):
        admin.upgrade_pet(pid, pets.CAT)
    cat = next(x for x in admin.pet_status(pid) if x["code"] == pets.CAT)
    odds = {c["code"]: c["text"] for c in cat["odds"]["crops"]}
    assert cat["odds"]["current"] and odds["wheat"] == "40% 機率 +1" and odds["strawberry"] == "固定 +1"
