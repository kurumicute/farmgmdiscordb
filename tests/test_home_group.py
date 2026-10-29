"""農場主要所屬群組（home_group_id）與群組排行規則。"""

import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.cli.commands import CommandHandler
from app.db.models import Farm, GameTransaction, InventoryItem
from app.db.session import make_engine, make_session_factory
from app.game import balance
from app.game.errors import HomeGroupLockedError, NotFoundError, ValidationError
from app.repositories import farms as farms_repo
from app.services.social_service import SocialService
from tests.conftest import grow, make_farmer, set_test_crop_times

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def groups(service):
    return service.create_group("A 群").id, service.create_group("B 群").id


def names(rows):
    return [r["player_name"] for r in rows]


def make_legacy_farm(session_factory, service, name: str) -> int:
    """模擬 Migration 前就存在、尚未綁定群組的舊農場。"""
    pid = service.create_player(name).id
    with session_factory.begin() as s:
        farm = Farm(player_id=pid, name=f"{name}的舊農場", level=1, gold=300, warehouse_capacity=50)
        s.add(farm)
        s.flush()
        farms_repo.add_plots(s, farm.id, balance.INITIAL_PLOTS)
    return pid


# ---------- 驗收 1–5：建立、加入其他群組、排行 ----------


def test_farm_belongs_to_creation_group_only(service, groups, clock):
    a, b = groups
    alice = service.create_player("Alice").id
    status = service.create_farm(alice, home_group_id=a)  # 1. 在群組 A 建立農場
    assert status.home_group_id == a
    assert service.is_member(a, alice)  # 建立時自動成為群組 A 成員
    other_b = make_farmer(service, "Bob", b)
    service.add_member(b, alice)  # 2. 同一玩家加入群組 B
    assert service.get_home_group(alice) == (a, "A 群")  # 加入 B 不改變歸屬

    grow(service, alice, 1, "wheat")
    clock.advance(60)
    service.harvest_crop(alice, 1)
    service.sell_crop(alice, "wheat", 2)

    for kind in ("money", "sales"):
        # 3. 出現在群組 A 排行
        assert names(service.leaderboard(kind, group_id=a, player_id=alice)) == ["Alice"]
        # 4. 不會因加入 B 而出現在 B 的排行（從 B 的成員視角查看）
        assert names(service.leaderboard(kind, group_id=b, player_id=other_b)) == ["Bob"]
        # 5. 總排行榜每位玩家只計算一次
        assert sorted(names(service.leaderboard(kind))) == ["Alice", "Bob"]
    assert service.leaderboard("sales")[0]["sold_quantity"] == 2


def test_create_farm_requires_existing_group(service):
    pid = service.create_player("x").id
    with pytest.raises(ValidationError):
        service.create_farm(pid, home_group_id=None)
    with pytest.raises(NotFoundError):
        service.create_farm(pid, home_group_id=999)
    assert not service.get_player(pid).has_farm


def test_leaving_and_rejoining_home_group(service, groups):
    a, _b = groups
    alice = make_farmer(service, "Alice", a)
    carol = make_farmer(service, "Carol", a)
    service.remove_member(a, alice)
    assert names(service.leaderboard("money", group_id=a, player_id=carol)) == ["Carol"]
    assert "Alice" in names(service.leaderboard("money"))  # 總排行不受影響
    assert service.get_farm_status(alice).gold == balance.INITIAL_GOLD  # 農場與金幣保留
    service.add_member(a, alice)
    assert sorted(names(service.leaderboard("money", group_id=a, player_id=carol))) == ["Alice", "Carol"]


# ---------- 驗收 6：舊農場一次性設定 ----------


def test_legacy_farm_one_time_binding(service, groups, session_factory):
    a, b = groups
    legacy = make_legacy_farm(session_factory, service, "Old")
    service.add_member(a, legacy)
    member_a = make_farmer(service, "Anna", a)
    assert service.get_home_group(legacy) is None
    # 未綁定：可出現在總排行，但不列入任何群組排行（即使是成員）
    assert "Old" in names(service.leaderboard("money"))
    assert names(service.leaderboard("money", group_id=a, player_id=member_a)) == ["Anna"]

    result = service.set_home_group(legacy, a)
    assert "群組轉移" in result.message
    assert service.get_home_group(legacy) == (a, "A 群")
    assert sorted(names(service.leaderboard("money", group_id=a, player_id=member_a))) == ["Anna", "Old"]
    with pytest.raises(HomeGroupLockedError) as info:
        service.set_home_group(legacy, b)
    assert "操縱排行" in str(info.value)
    assert service.get_home_group(legacy) == (a, "A 群")
    with session_factory() as s:
        assert s.scalar(select(func.count()).where(GameTransaction.event_type == "bind_group")) == 1


def test_concurrent_one_time_binding_only_once(service, groups, session_factory):
    a, b = groups
    legacy = make_legacy_farm(session_factory, service, "Old")
    barrier = threading.Barrier(2)
    results = []

    def bind(group_id):
        barrier.wait()
        try:
            service.set_home_group(legacy, group_id)
            results.append(("ok", group_id))
        except HomeGroupLockedError:
            results.append(("locked", group_id))

    threads = [threading.Thread(target=bind, args=(g,)) for g in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r[0] for r in results) == ["locked", "ok"]
    winner = next(g for r, g in results if r == "ok")
    assert service.get_home_group(legacy)[0] == winner


def test_database_trigger_blocks_changing_home_group(service, groups, session_factory):
    a, b = groups
    pid = make_farmer(service, "Alice", a)
    # 0010 起：不可清空；變更時必須同時記錄轉移時間（冷卻檢查見 test_group_transfer.py）
    for value, message in ((b, "must record last_group_transfer_at"), (None, "cannot be cleared")):
        with pytest.raises(IntegrityError, match=message):
            with session_factory.begin() as s:
                s.execute(update(Farm).where(Farm.player_id == pid).values(home_group_id=value))
    with session_factory.begin() as s:  # 其他欄位照常可更新
        s.execute(update(Farm).where(Farm.player_id == pid).values(gold=777))
    assert service.get_home_group(pid) == (a, "A 群")


# ---------- 驗收 7：群組切換、重啟、操作上下文 ----------


def test_group_switch_and_restart_do_not_change_home(service, groups, clock, session_factory, database_url):
    a, b = groups
    alice = make_farmer(service, "Alice", a)
    service.add_member(b, alice)
    bob = make_farmer(service, "Bob", b)

    cli = CommandHandler(service)
    cli.handle(f"player use {alice}")
    cli.handle(f"group use {b}")  # 切換到群組 B 操作
    cli.handle("buy wheat 1")
    assert "目前可轉移" in cli.handle("farm group")
    assert "不可更改" in cli.handle(f"farm group set {b}")

    with session_factory() as s:
        tx = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "buy_seed")).one()
        assert tx.context_group_id == b  # 記錄實際發生的群組
        assert s.scalar(select(Farm.home_group_id).where(Farm.player_id == alice)) == a

    # 程式重新啟動：新的 Engine 與服務
    engine = make_engine(database_url)
    try:
        restarted = SocialService(make_session_factory(engine), clock)
        assert restarted.get_home_group(alice) == (a, "A 群")
        assert names(restarted.leaderboard("money", group_id=b, player_id=bob)) == ["Bob"]
        assert names(restarted.leaderboard("money", group_id=a, player_id=alice)) == ["Alice"]
    finally:
        engine.dispose()


def test_context_only_recorded_for_member_groups(service, groups, session_factory):
    a, b = groups
    alice = make_farmer(service, "Alice", a)
    service.in_group_context(b).buy_seeds(alice, "wheat", 1)  # 不是 B 成員
    service.in_group_context(a).buy_seeds(alice, "wheat", 1)
    with session_factory() as s:
        ctx = list(s.scalars(
            select(GameTransaction.context_group_id)
            .where(GameTransaction.event_type == "buy_seed")
            .order_by(GameTransaction.id)
        ))
    assert ctx == [None, a]


# ---------- CMD 指令 ----------


def test_cli_farm_group_commands(service, groups, session_factory):
    a, b = groups
    cli = CommandHandler(service)
    cli.handle("player create 新手")
    assert "尚未建立農場，也尚未指定所屬群組" in cli.handle("farm group")
    assert "找不到群組 #99" in cli.handle("farm group set 99")
    assert "必須指定所屬群組" in cli.handle("farm start")  # 沒有任何群組可用
    assert "建立農場時將綁定群組 #2" in cli.handle(f"farm group set {b}")  # 建立前指定
    cli.handle(f"group use {a}")
    out = cli.handle("farm start 我的農場")  # 預先指定的優先於目前群組
    assert "所屬群組：#2「B 群」（預先設定" in out
    assert "#2「B 群」（目前可轉移" in cli.handle("farm group")
    assert "不可更改" in cli.handle(f"farm group set {a}")

    # 目前群組作為預設
    cli.handle("player create 第二人")
    assert "所屬群組：#1「A 群」（目前選擇" in cli.handle("farm start")
    # 指定群組 ID
    cli.handle("player create 第三人")
    assert "所屬群組：#2「B 群」（指定" in cli.handle(f"farm start {b} 第三農場")
    assert "不可更改" in cli.handle("farm group set 99")  # 已綁定：先因不可更改而拒絕

    # 舊農場的一次性設定
    legacy = make_legacy_farm(session_factory, service, "老玩家")
    cli.handle(f"player use {legacy}")
    assert "尚未設定所屬群組" in cli.handle("farm group")
    assert "群組轉移" in cli.handle(f"farm group set {a}")
    assert "不可更改" in cli.handle(f"farm group set {b}")
    assert "#1「A 群」（目前可轉移" in cli.handle("farm group")


# ---------- 驗收 8：Migration 不遺失既有資料 ----------


def test_migration_0003_roundtrip_preserves_game_data(engine, database_url, service, groups, clock):
    a, _b = groups
    alice = make_farmer(service, "Alice", a)
    grow(service, alice, 1, "wheat")
    service.buy_seeds(alice, "carrot", 3)

    def snapshot():
        with engine.connect() as conn:
            return {
                "gold": conn.scalar(select(Farm.gold).where(Farm.player_id == alice)),
                "tx": conn.scalar(select(func.count()).select_from(GameTransaction)),
                "items": conn.scalar(select(func.sum(InventoryItem.quantity))),
                "plots": conn.scalar(text("SELECT count(*) FROM farm_plots WHERE crop_type_id IS NOT NULL")),
                "members": conn.scalar(text("SELECT count(*) FROM group_members")),
            }

    before = snapshot()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    engine.dispose()  # 釋放連線，避免 DDL 等待鎖
    command.downgrade(cfg, "0002")
    try:
        assert snapshot() == before  # 降版只移除新欄位，不刪遊戲資料（無 bind_group 紀錄）
    finally:
        command.upgrade(cfg, "head")
        set_test_crop_times(engine)  # 重新升級會套用 0007 的 2 小時生長時間，還原測試用時間
    assert snapshot() == before
    with engine.connect() as conn:
        # 升級後既有農場成為「尚未綁定」的舊農場，等待玩家一次性設定
        assert conn.scalar(select(Farm.home_group_id).where(Farm.player_id == alice)) is None
