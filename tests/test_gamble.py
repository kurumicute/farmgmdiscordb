"""賭場：比大小（×2）、猜數字（×5）、只限稀有以上或特殊突變的作物、獎品存放與賣出。"""

import pytest
from sqlalchemy import func, select

from app.db.models import Farm, GameTransaction
from app.game.errors import InsufficientItemsError, NotFoundError, ValidationError
from app.repositories import farm_stats as farm_stats_repo
from app.services.admin_service import AdminService
from tests.conftest import make_farmer


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


def casino(session_factory, clock, roll):
    return AdminService(session_factory, clock, dice=lambda: roll)


def give(admin, pid, code="wheat", quality="rare", mutation="none", qty=5):
    admin.set_inventory("root", pid, code, quality, mutation, qty)


def invariant(session_factory):
    with session_factory() as s:
        assert s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0


def test_only_rare_or_special_crops(admin, player_id, session_factory, clock):
    give(admin, player_id, quality="normal")
    give(admin, player_id, quality="good")
    give(admin, player_id, code="carrot", quality="normal", mutation="golden", qty=2)
    give(admin, player_id, code="amber_wheat", quality="normal", mutation="variant", qty=1)
    names = sorted(o["name"] for o in admin.gamble_options(player_id))
    assert names == ["琥珀小麥", "胡蘿蔔【黃金作物】"]
    with pytest.raises(ValidationError, match="只能用"):
        casino(session_factory, clock, 7).gamble(player_id, "wheat", "good", "none", 1, "big")


def test_big_win_moves_crops_to_prizes(admin, player_id, session_factory, clock):
    give(admin, player_id, qty=5)
    used = admin.get_backpack(player_id).used
    result = casino(session_factory, clock, 7).gamble(player_id, "wheat", "rare", "none", 3, "big")
    assert result["won"] and result["roll"] == 7 and result["multiplier"] == 2
    bp = admin.get_backpack(player_id)
    assert [(x.quantity, x.quality) for x in bp.produce] == [(2, "rare")]
    assert [(p["quantity"], p["multiplier"], p["unit_price"]) for p in bp.prizes] == [(3, 2, 22 * 2)]
    assert bp.used == used  # 獎品仍佔倉庫空間，總數不變
    with session_factory() as s:
        tx = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "gamble")).one()
        assert "擲出 7" in tx.description and tx.gold_delta == 0


def test_small_loss_takes_crops(admin, player_id, session_factory, clock):
    give(admin, player_id, qty=5)
    result = casino(session_factory, clock, 7).gamble(player_id, "wheat", "rare", "none", 5, "small")
    assert not result["won"] and "輸了" in result["message"]
    bp = admin.get_backpack(player_id)
    assert bp.produce == [] and bp.prizes == []
    invariant(session_factory)


def test_exact_guess_pays_five_times_and_validates(admin, player_id, session_factory, clock):
    give(admin, player_id, code="carrot", quality="normal", mutation="golden", qty=2)
    svc = casino(session_factory, clock, 3)
    with pytest.raises(ValidationError):
        svc.gamble(player_id, "carrot", "normal", "golden", 1, "exact", 11)
    assert not svc.gamble(player_id, "carrot", "normal", "golden", 1, "exact", 4)["won"]
    assert svc.gamble(player_id, "carrot", "normal", "golden", 1, "exact", 3)["multiplier"] == 5
    prize = admin.list_prizes(player_id)[0]
    assert prize["multiplier"] == 5 and prize["quantity"] == 1
    with pytest.raises(InsufficientItemsError):
        svc.gamble(player_id, "carrot", "normal", "golden", 1, "big")


def test_sell_prize_price_and_ownership(admin, player_id, home_group, session_factory, clock):
    give(admin, player_id, qty=4)
    casino(session_factory, clock, 9).gamble(player_id, "wheat", "rare", "none", 4, "big")
    prize = admin.list_prizes(player_id)[0]
    other = make_farmer(admin, "別人", home_group)
    with pytest.raises(NotFoundError):  # 不能賣別人的獎品
        admin.sell_prize(other, prize["id"], 1)
    gold = admin.get_farm_status(player_id).gold
    result = admin.sell_prize(player_id, prize["id"], 3)
    assert result.gold_delta == 3 * 44 and admin.get_farm_status(player_id).gold == gold + 132
    assert admin.list_prizes(player_id)[0]["quantity"] == 1
    with pytest.raises(InsufficientItemsError):
        admin.sell_prize(player_id, prize["id"], 2)
    invariant(session_factory)
