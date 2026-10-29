"""農場所屬群組轉移（7 天冷卻）：服務層、資料庫觸發器、並行、重啟與管理員轉移。"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models import AdminAction, Farm, FarmPlot, GameTransaction, InventoryItem
from app.db.session import make_engine, make_session_factory
from app.game import balance
from app.game.errors import GameError, HomeGroupLockedError, NotGroupMemberError, ValidationError
from app.services.admin_service import AdminService
from app.services.ranking_service import RankingService
from tests.conftest import grow, make_farmer

COOLDOWN = balance.GROUP_TRANSFER_COOLDOWN_SECONDS


@pytest.fixture
def groups(service):
    return [service.create_group(name).id for name in ("A 群", "B 群", "C 群")]


def names(rows):
    return [r["player_name"] for r in rows]


def gold_invariant(session_factory) -> None:
    with session_factory() as s:
        total = s.scalar(select(func.coalesce(func.sum(Farm.gold), 0)))
        deltas = s.scalar(select(func.coalesce(func.sum(GameTransaction.gold_delta), 0)))
    assert total == deltas


def test_first_transfer_free_then_seven_day_cooldown(service, groups, clock, session_factory):
    a, b, c = groups
    alice = make_farmer(service, "Alice", a)
    service.add_member(b, alice)
    service.add_member(c, alice)
    status = service.group_transfer_status(alice, b)
    assert status["can_transfer"] and status["is_member"] and status["home_group_name"] == "A 群"

    result = service.transfer_home_group(alice, b)
    assert "轉移到「B 群」" in result.message
    assert service.get_home_group(alice) == (b, "B 群")

    clock.advance(COOLDOWN - 1)
    with pytest.raises(HomeGroupLockedError, match="冷卻中") as info:
        service.transfer_home_group(alice, c)
    assert "下次可轉移時間" in str(info.value)
    status = service.group_transfer_status(alice, c)
    assert not status["can_transfer"] and status["remaining_seconds"] == 1

    clock.advance(1)
    service.transfer_home_group(alice, c)
    assert service.get_home_group(alice) == (c, "C 群")
    with session_factory() as s:
        events = s.scalars(
            select(GameTransaction.context_group_id)
            .where(GameTransaction.event_type == "transfer_group")
            .order_by(GameTransaction.id)
        ).all()
    assert events == [b, c]


def test_transfer_requires_membership_and_different_group(service, groups):
    a, b, _c = groups
    alice = make_farmer(service, "Alice", a)
    with pytest.raises(NotGroupMemberError):
        service.transfer_home_group(alice, b)
    with pytest.raises(ValidationError, match="已經屬於"):
        service.transfer_home_group(alice, a)
    assert service.get_home_group(alice) == (a, "A 群")


def test_transfer_keeps_assets_and_updates_rankings(service, groups, clock, session_factory):
    a, b, _c = groups
    alice = make_farmer(service, "Alice", a)
    anna = make_farmer(service, "Anna", a)
    bob = make_farmer(service, "Bob", b)
    service.add_member(b, alice)
    grow(service, alice, 1, "wheat")
    service.buy_seeds(alice, "carrot", 3)
    with session_factory() as s:
        farm_before = s.scalars(select(Farm).where(Farm.player_id == alice)).one()
        plots_before = s.scalar(select(func.count()).select_from(FarmPlot).where(FarmPlot.farm_id == farm_before.id))
        items_before = s.scalar(
            select(func.sum(InventoryItem.quantity)).where(InventoryItem.farm_id == farm_before.id)
        )
    assert sorted(names(service.leaderboard("money", group_id=a, player_id=anna))) == ["Alice", "Anna"]

    service.transfer_home_group(alice, b)

    with session_factory() as s:
        farm = s.scalars(select(Farm).where(Farm.player_id == alice)).one()
        assert farm.id == farm_before.id and farm.gold == farm_before.gold and farm.level == farm_before.level
        assert s.scalar(select(func.count()).select_from(FarmPlot).where(FarmPlot.farm_id == farm.id)) == plots_before
        items = s.scalar(select(func.sum(InventoryItem.quantity)).where(InventoryItem.farm_id == farm.id))
        assert items == items_before
        assert s.scalar(select(func.count()).select_from(Farm)) == 3
    # 群組排行與群組總排行立即依新群組計算
    assert names(service.leaderboard("money", group_id=a, player_id=anna)) == ["Anna"]
    assert sorted(names(service.leaderboard("money", group_id=b, player_id=bob))) == ["Alice", "Bob"]
    totals = {r["group_id"]: r["member_count"] for r in service.group_leaderboard("money")}
    assert totals == {a: 1, b: 2}
    gold_invariant(session_factory)


def test_database_trigger_enforces_cooldown(service, groups, clock, session_factory):
    a, b, c = groups
    alice = make_farmer(service, "Alice", a)
    service.add_member(b, alice)
    service.transfer_home_group(alice, b)
    with session_factory() as s:
        last = s.scalar(select(Farm.last_group_transfer_at).where(Farm.player_id == alice))

    def direct(**values):
        with session_factory.begin() as s:
            s.execute(update(Farm).where(Farm.player_id == alice).values(**values))

    with pytest.raises(IntegrityError, match="cooldown"):
        direct(home_group_id=c, last_group_transfer_at=last + timedelta(days=6))
    with pytest.raises(IntegrityError, match="in the future"):
        direct(home_group_id=c, last_group_transfer_at=last + timedelta(days=3650))
    with pytest.raises(IntegrityError, match="must record"):
        direct(home_group_id=c)
    # 不能單獨清空或倒退轉移時間來重設冷卻
    for value in (None, last - timedelta(days=30)):
        with pytest.raises(IntegrityError, match="without a transfer"):
            direct(last_group_transfer_at=value)
    # 冷卻已滿時直接更新也允許（資料庫層與服務層規則一致）
    direct(home_group_id=c, last_group_transfer_at=last + timedelta(days=7))
    assert service.get_home_group(alice) == (c, "C 群")


def test_concurrent_transfers_only_one_succeeds(service, groups, session_factory):
    a, b, c = groups
    alice = make_farmer(service, "Alice", a)
    service.add_member(b, alice)
    service.add_member(c, alice)
    barrier = threading.Barrier(2)
    outcomes = {}

    def attempt(target):
        barrier.wait()
        try:
            service.transfer_home_group(alice, target)
            outcomes[target] = "ok"
        except GameError as exc:
            outcomes[target] = type(exc).__name__

    threads = [threading.Thread(target=attempt, args=(g,)) for g in (b, c)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes.values()) == ["HomeGroupLockedError", "ok"]
    winner = next(g for g, v in outcomes.items() if v == "ok")
    assert service.get_home_group(alice)[0] == winner
    with session_factory() as s:
        assert s.scalar(select(func.count()).where(GameTransaction.event_type == "transfer_group")) == 1


def test_cooldown_survives_restart(service, groups, clock, database_url):
    a, b, c = groups
    alice = make_farmer(service, "Alice", a)
    service.add_member(b, alice)
    service.add_member(c, alice)
    service.transfer_home_group(alice, b)
    engine = make_engine(database_url)
    try:
        restarted = RankingService(make_session_factory(engine), clock)
        clock.advance(3600)
        with pytest.raises(HomeGroupLockedError):
            restarted.transfer_home_group(alice, c)
        assert restarted.group_transfer_status(alice)["remaining_seconds"] == COOLDOWN - 3600
    finally:
        engine.dispose()


def test_transfer_does_not_move_quest_progress(service, groups, clock):
    a, b, _c = groups
    alice = make_farmer(service, "Alice", a)
    service.add_member(b, alice)
    now = clock.now()
    quest_a = service.create_quest(
        "admin", group_id=a, name="A 收成", quest_type="harvest", target=100, reward_mode="fixed",
        reward_type="gold", reward_amount=100, starts_at=now, ends_at=now + timedelta(days=1),
    )["quest_id"]
    quest_b = service.create_quest(
        "admin", group_id=b, name="B 收成", quest_type="harvest", target=100, reward_mode="fixed",
        reward_type="gold", reward_amount=100, starts_at=now, ends_at=now + timedelta(days=1),
    )["quest_id"]
    grow(service, alice, 1, "wheat")
    clock.advance(61)
    service.in_group_context(a).harvest_crop(alice, 1)
    progress_a = service.quest_status(a, alice, quest_a)["progress"]
    assert progress_a > 0

    service.transfer_home_group(alice, b)
    for group in (a, b):  # 在舊群組操作不再計分；在新群組操作計入新群組
        grow(service, alice, 1, "wheat")
        clock.advance(61)
        service.in_group_context(group).harvest_crop(alice, 1)
    assert service.quest_status(a, alice, quest_a)["progress"] == progress_a  # 已有進度不改寫、不增加
    assert service.quest_status(b, alice, quest_b)["progress"] == progress_a


def test_admin_transfer_requires_reason_and_records_bypass(session_factory, clock, groups, service):
    admin = AdminService(session_factory, clock)
    a, b, c = groups
    alice = make_farmer(service, "Alice", a)
    with pytest.raises(ValidationError, match="原因"):
        admin.admin_transfer_farm("root", alice, b, "  ")
    result = admin.admin_transfer_farm("root", alice, b, "玩家申請")  # 管理員轉移會自動加入目標群組
    assert result["changed"] and not result["cooldown_bypassed"]
    with pytest.raises(HomeGroupLockedError):
        admin.admin_transfer_farm("root", alice, c, "再轉一次")
    result = admin.admin_transfer_farm("root", alice, c, "客服處理", skip_cooldown=True)
    assert result["cooldown_bypassed"]
    assert service.get_home_group(alice) == (c, "C 群")
    # 略過冷卻只在該交易有效：之後玩家自己轉移仍受冷卻限制
    service.add_member(a, alice)
    with pytest.raises(HomeGroupLockedError):
        service.transfer_home_group(alice, a)
    with session_factory() as s:
        actions = s.scalars(
            select(AdminAction).where(AdminAction.action == "transfer_farm_group").order_by(AdminAction.id)
        ).all()
        assert [x.details["reason"] for x in actions] == ["玩家申請", "客服處理"]
        assert actions[1].details["cooldown_bypassed"] is True
        assert actions[1].details["before"]["home_group_id"] == b
        assert s.execute(text("SELECT current_setting('farmgm.admin_group_transfer', true)")).scalar() in (None, "")
    gold_invariant(session_factory)
