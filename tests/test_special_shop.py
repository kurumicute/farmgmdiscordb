"""特賣與升級：溫室（分級永久加速）、每週特賣（輪替、限購、稀有種子）、稱號商店；交易紀錄與面板操作。"""

from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from app.clock import TAIPEI_TZ
from app.db.models import Farm, GameTransaction
from app.game import special_shop
from app.game.errors import AlreadyExistsError, InsufficientGoldError, NotFoundError, ValidationError
from app.services.admin_service import AdminService
from tests.conftest import make_farmer
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import cmds, make_player  # noqa: F401  cmds 是 fixture

WEEK = 7 * 24 * 3600


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


def give_gold(admin, pid, gold):
    admin.set_farm_values("root", pid, gold=gold)  # 經 admin_adjust 寫交易紀錄


@pytest.fixture
def pid(admin, home_group):
    p = make_farmer(admin, "買家", home_group)
    give_gold(admin, p, 2_000_000)
    return p


def gold_matches_transactions(session_factory):
    with session_factory() as s:
        return s.scalar(select(func.sum(GameTransaction.gold_delta))) == s.scalar(select(func.sum(Farm.gold)))


def test_week_start_and_offers_rotate():
    # 2026-01-01（週四）台灣時間 → 該週週一 2025-12-29；週日 23:59 仍是同一週
    assert special_shop.week_start(datetime(2026, 1, 1, tzinfo=TAIPEI_TZ)) == date(2025, 12, 29)
    assert special_shop.week_start(datetime(2026, 1, 4, 23, 59, tzinfo=TAIPEI_TZ)) == date(2025, 12, 29)
    assert special_shop.week_start(datetime(2026, 1, 5, tzinfo=TAIPEI_TZ)) == date(2026, 1, 5)
    crops = [special_shop.CropRef(c, c, p, b) for c, p, b in (
        ("wheat", 5, True), ("carrot", 12, True), ("tomato", 25, True), ("strawberry", 40, True),
        ("amber_melon", 200, False), ("moonflower", 500, False))]
    w0 = special_shop.weekly_offers(date(2026, 1, 5), crops)
    w1 = special_shop.weekly_offers(date(2026, 1, 12), crops)
    assert [o.crop_code for o in w0] == ["wheat", "carrot", "amber_melon"]
    assert [o.crop_code for o in w1] == ["tomato", "strawberry", "moonflower"]
    assert (w0[1].price, w0[1].limit) == (9, special_shop.NORMAL_LIMIT)  # 12 × 0.7 = 8.4 → 9
    assert (w1[2].price, w1[2].limit, w1[2].rare) == (600, special_shop.RARE_LIMIT, True)


def test_greenhouse_levels_speed_up_growth(admin, pid, clock, session_factory):
    admin.buy_seeds(pid, "strawberry", 2)
    admin.plant_crop(pid, 1, "strawberry")
    admin.water_crop(pid, 1)
    base = admin.get_farm_status(pid).plots[0].remaining_seconds
    for level, price in enumerate(special_shop.GREENHOUSE_PRICES, 1):
        before = admin.get_farm_status(pid).gold
        assert f"{level} 級" in admin.upgrade_greenhouse(pid).message
        assert admin.get_farm_status(pid).gold == before - price
    with pytest.raises(ValidationError, match="最高等級"):
        admin.upgrade_greenhouse(pid)
    admin.plant_crop(pid, 2, "strawberry")
    admin.water_crop(pid, 2)
    fast = admin.get_farm_status(pid).plots[1].remaining_seconds
    pct = special_shop.greenhouse_pct(special_shop.GREENHOUSE_MAX_LEVEL)
    assert fast == base * (100 - pct) // 100
    assert admin.special_shop(pid)["greenhouse"]["next_price"] is None
    assert gold_matches_transactions(session_factory)


def test_greenhouse_needs_gold(admin, home_group):
    poor = make_farmer(admin, "窮人", home_group)
    with pytest.raises(InsufficientGoldError):
        admin.upgrade_greenhouse(poor)


def test_weekly_deal_limit_resets_next_week(admin, pid, clock, session_factory):
    view = admin.special_shop(pid)["weekly"]
    normal = next(o for o in view["offers"] if not o["rare"])
    rare = next(o for o in view["offers"] if o["rare"])
    with pytest.raises(NotFoundError):
        admin.buy_weekly_deal(pid, "no_such_crop", 1)
    before = admin.get_farm_status(pid).gold
    admin.buy_weekly_deal(pid, normal["crop_code"], normal["limit"])
    assert admin.get_farm_status(pid).gold == before - normal["price"] * normal["limit"]
    with pytest.raises(ValidationError, match="限購"):
        admin.buy_weekly_deal(pid, normal["crop_code"], 1)
    admin.buy_weekly_deal(pid, rare["crop_code"], 2)
    with pytest.raises(ValidationError, match="還可以買 1 顆"):
        admin.buy_weekly_deal(pid, rare["crop_code"], 2)
    bp = admin.get_backpack(pid)
    assert any(x.code == normal["crop_code"] and x.quantity == normal["limit"] for x in bp.seeds)
    offers = {o["crop_code"]: o for o in admin.special_shop(pid)["weekly"]["offers"]}
    assert offers[normal["crop_code"]]["bought"] == normal["limit"] and offers[rare["crop_code"]]["bought"] == 2
    # 下一週換商品，購買量重新計算
    clock.advance(WEEK)
    nxt = admin.special_shop(pid)["weekly"]
    assert nxt["week_start"] != view["week_start"] and all(o["bought"] == 0 for o in nxt["offers"])
    assert gold_matches_transactions(session_factory)


def test_title_shop(admin, pid, session_factory, home_group):
    title, price = special_shop.TITLE_SHOP[0]
    before = admin.get_farm_status(pid).gold
    assert title in admin.buy_title(pid, title).message
    assert admin.get_farm_status(pid).gold == before - price
    assert next(t for t in admin.titles(pid)["owned"] if t["title"] == title)["source"] == "稱號商店"
    with pytest.raises(AlreadyExistsError):
        admin.buy_title(pid, title)
    with pytest.raises(NotFoundError):
        admin.buy_title(pid, "免費稱號")
    poor = make_farmer(admin, "窮人", home_group)
    with pytest.raises(InsufficientGoldError):
        admin.buy_title(poor, title)
    admin.set_title(pid, title)
    assert admin.get_farm_status(pid).title == title
    assert gold_matches_transactions(session_factory)


def test_panel_special_shop(cmds, admin, session_factory):  # noqa: F811
    p = make_player(cmds, ALICE)
    pid = p.panel.player_id
    give_gold(admin, pid, 2_000_000)
    p.go("shop")
    p.click("特賣與升級")
    assert "特賣與升級" in p.text and "溫室" in p.text and "本週特賣" in p.text and "稱號商店" in p.text
    p.choose("選擇要買的商品", "greenhouse")
    p.click("確認購買")
    assert "溫室升到 1 級" in p.text
    title = special_shop.TITLE_SHOP[0][0]
    p.choose("選擇要買的商品", f"title:{title}")
    p.click("確認購買")
    assert f"買到稱號「{title}」" in p.text
    offer = admin.special_shop(pid)["weekly"]["offers"][0]
    p.choose("選擇要買的商品", f"weekly:{offer['crop_code']}")
    p.click("買 3")
    assert "買到 3 顆" in p.text
    with session_factory() as s:
        assert s.scalar(select(Farm.greenhouse_level).where(Farm.player_id == pid)) == 1
