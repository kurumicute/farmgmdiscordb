"""排行用統計表（farm_stats／farm_daily_stats）：與交易紀錄重算一致、排行結果與直接加總交易紀錄相同。"""

from datetime import timedelta

import pytest
from sqlalchemy import update

from app.db.models import FarmStats
from app.repositories import farm_stats as farm_stats_repo
from app.repositories import leaderboard as leaderboard_repo
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer

DAY = timedelta(days=1)


@pytest.fixture
def played(service, session_factory, clock):
    """兩個群組、四位玩家，跨數天販售作物、動物產品，並有管理員調整（非經營金幣）。"""
    admin = AdminService(session_factory, clock)
    a = service.create_group("A 群").id
    b = service.create_group("B 群").id
    players = [make_farmer(service, n, g) for n, g in (("甲", a), ("乙", a), ("丙", b), ("丁", b))]
    for day in range(3):
        for i, pid in enumerate(players):
            for _ in range(i + 1):
                grow(service, pid, 1, "wheat")
                clock.advance(61)
                service.in_group_context(a if i < 2 else b).harvest_crop(pid, 1)
            service.sell(pid, "wheat", i + 1)
            if i % 2 == 0:
                admin.set_inventory("root", pid, "egg", "normal", "none", 3)
                service.sell(pid, "egg", 2)
        admin.set_farm_values("root", players[3], gold=5000 + day)
        clock.advance(DAY.total_seconds())
    return {"A": a, "B": b, "players": players}


def test_stats_match_transactions(played, session_factory):
    with session_factory() as s:
        result = farm_stats_repo.check(s)
    assert result["farm_stats_mismatches"] == 0
    assert result["farm_daily_stats_mismatches"] == 0


def test_rebuild_restores_corrupted_stats(played, session_factory):
    with session_factory.begin() as s:
        s.execute(update(FarmStats).values(crop_sold_quantity=999))
    with session_factory() as s:
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 4
    with session_factory.begin() as s:
        counts = farm_stats_repo.rebuild(s)
    assert counts["farm_stats"] == 4 and counts["farm_daily_stats"] > 0
    with session_factory() as s:
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0


@pytest.mark.parametrize("kind", leaderboard_repo.KINDS)
@pytest.mark.parametrize("period", ["all", "day", "week", "month"])
def test_rankings_equal_transaction_scan(played, session_factory, clock, service, monkeypatch, kind, period):
    since, until = service.view_bounds(period, clock.now() - DAY)  # 前一天：有資料的期間
    with session_factory() as s:
        fast_people = leaderboard_repo.ranking(s, kind, None, 50, since, until)
        fast_group = leaderboard_repo.ranking(s, kind, played["A"], 50, since, until)
        fast_groups = leaderboard_repo.group_ranking(s, kind, 50, since, until)
    monkeypatch.setattr(leaderboard_repo, "_stats", leaderboard_repo._tx_stats)
    with session_factory() as s:
        assert leaderboard_repo.ranking(s, kind, None, 50, since, until) == fast_people
        assert leaderboard_repo.ranking(s, kind, played["A"], 50, since, until) == fast_group
        assert leaderboard_repo.group_ranking(s, kind, 50, since, until) == fast_groups
    assert any(r["score"] > 0 for r in fast_people)


def test_non_aligned_period_falls_back_to_transactions(played, session_factory, clock):
    since = clock.now() - timedelta(hours=30)  # 不是台灣午夜：直接加總交易紀錄
    with session_factory() as s:
        rows = leaderboard_repo.ranking(s, "goods", None, 50, since, clock.now())
    assert all(r["sales_income"] >= 0 for r in rows)
