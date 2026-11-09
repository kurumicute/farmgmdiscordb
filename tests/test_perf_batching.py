"""效能改動的正確性：一鍵操作的延後寫入、遊戲目錄快取、操作後不必要的圖鑑／成就標記。"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, text

from app.db import catalog_cache, txcache
from app.db.models import CropType, FarmCodexEntry, GameTransaction, InventoryItem, QuestContribution
from app.game.errors import GameError
from app.repositories import codex as codex_repo
from app.repositories import crops as crops_repo
from app.repositories import farm_stats as farm_stats_repo
from app.repositories import genetics as genetics_repo
from app.repositories import inventory as inventory_repo
from app.services.admin_service import AdminService
from tests.conftest import make_farmer


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


def plots_ready(admin, clock, pid, plots=16, crop="wheat"):
    admin.set_farm_values("test", pid, gold=1_000_000)
    missing = plots - len(admin.get_farm_status(pid).plots)
    if missing > 0:
        admin.add_plots("test", pid, missing)
    admin.plant_all(pid, crop)
    admin.water_all(pid)
    clock.advance(600)


class SqlCounter:
    def __init__(self, engine):
        self.engine, self.statements = engine, []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._on)

    def _on(self, conn, cur, stmt, params, ctx, many):
        self.statements.append(" ".join(stmt.split()))

    def count(self, prefix):
        return sum(s.startswith(prefix) for s in self.statements)


# ---------- 延後寫入 ----------


def test_harvest_all_merges_writes_and_keeps_every_record(admin, clock, player_id, engine, session_factory):
    plots_ready(admin, clock, player_id)
    with SqlCounter(engine) as sql:
        result = admin.harvest_all(player_id)
    assert result.quantity > 0
    # 16 格同一種作物，原本三種寫入各 16 條；合併後最多 2 條（第一格之後的第一次查詢會先送出已累積的部分）
    assert sql.count("INSERT INTO inventory_items") <= 2
    assert sql.count("INSERT INTO farm_codex") <= 2
    assert sql.count("INSERT INTO game_transactions") <= 2
    with session_factory() as s:
        rows = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "harvest")
                         .order_by(GameTransaction.id)).all()
        assert len(rows) == 16
        assert sum(r.quantity for r in rows) == result.quantity
        assert [r.description.split("，")[0] for r in rows] == [f"收成第 {i} 格" for i in range(1, 17)]
        stack = s.scalars(select(InventoryItem).where(InventoryItem.item_code == "wheat")).one()
        assert stack.quantity == result.quantity
        codex = s.get(FarmCodexEntry, (1, "wheat"))
        assert codex.quantity == result.quantity
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0


def test_deferred_writes_are_visible_to_later_queries_in_the_same_transaction(session_factory, player_id):
    with session_factory.begin() as s:
        farm_id = s.execute(text("SELECT id FROM farms")).scalar()
        with txcache.bulk(s):
            inventory_repo.add(s, farm_id, "wheat", 3)
            inventory_repo.add(s, farm_id, "wheat", 4)
            assert inventory_repo.get_quantity(s, farm_id, "wheat") == 7  # 查詢前先送出
            inventory_repo.add(s, farm_id, "wheat", 1)
        assert inventory_repo.get_quantity(s, farm_id, "wheat") == 8


def test_codex_merge_keeps_best_quality(session_factory, clock, player_id):
    now = clock.now()
    with session_factory.begin() as s:
        farm_id = s.execute(text("SELECT id FROM farms")).scalar()
        with txcache.bulk(s):
            codex_repo.register(s, farm_id, "wheat", 2, "legendary", now)
            codex_repo.register(s, farm_id, "wheat", 3, "normal", now)
    with session_factory() as s:
        row = s.get(FarmCodexEntry, (farm_id, "wheat"))
        assert row.quantity == 5 and row.best_quality == codex_repo.QUALITY_RANK["legendary"]


def test_failed_bulk_discards_deferred_writes(session_factory, player_id):
    with session_factory.begin() as s:
        farm_id = s.execute(text("SELECT id FROM farms")).scalar()
        savepoint = s.begin_nested()
        with pytest.raises(RuntimeError), txcache.bulk(s):
            inventory_repo.add(s, farm_id, "wheat", 3)
            raise RuntimeError("boom")
        savepoint.rollback()
        assert inventory_repo.get_quantity(s, farm_id, "wheat") == 0
    with session_factory() as s:
        assert inventory_repo.get_quantity(s, farm_id, "wheat") == 0


def test_quest_scoring_still_gets_transaction_ids(admin, clock, player_id, home_group, session_factory):
    """有進行中的任務時交易紀錄要逐筆寫（計分需要 ID）；任務進度與貢獻都要正確。"""
    admin.create_quest("root", group_id=home_group, name="收成任務", quest_type="harvest", target=100000,
                       reward_mode="fixed", reward_type="gold", reward_amount=10,
                       starts_at=clock.now(), ends_at=clock.now() + timedelta(days=1))
    scoped = admin.in_group_context(home_group)
    plots_ready(scoped, clock, player_id, plots=4)
    result = scoped.harvest_all(player_id)
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(QuestContribution)) == 4  # 每格一筆（各自的交易紀錄 ID）
        tx_ids = set(s.scalars(select(QuestContribution.transaction_id)))
        harvests = set(s.scalars(select(GameTransaction.id).where(GameTransaction.event_type == "harvest")))
        assert tx_ids == harvests and len(harvests) == 4
    assert admin.group_quests(home_group, player_id)[0]["progress"] == result.quantity


def test_auto_seller_sees_harvest_in_inventory(admin, clock, player_id, session_factory):
    """自動販售在同一個一鍵操作中先收成再賣：背包要先寫入，才賣得掉。"""
    plots_ready(admin, clock, player_id, plots=4)
    admin.set_farm_values("test", player_id, gold=1_000_000)
    with session_factory.begin() as s:
        s.execute(text("INSERT INTO farm_tools (farm_id, tool_code, enabled, acquired_at) "
                       "SELECT id, 'auto_seller', true, now() FROM farms"))
    gold = admin.get_farm_status(player_id).gold
    result = admin.harvest_all(player_id)
    assert admin.get_farm_status(player_id).gold > gold and result.quantity > 0
    with session_factory() as s:
        assert (s.scalar(select(func.coalesce(func.sum(InventoryItem.quantity), 0))
                         .where(InventoryItem.item_code == "wheat"))) == 0
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0


def test_concurrent_harvest_all_and_steals_conserve_crops(session_factory, clock, home_group, player_id):
    """一鍵收成（延後寫入）同時被多人偷菜：農場列的鎖讓兩者依序執行，作物不會多也不會少，紀錄與背包一致。"""
    admin = AdminService(session_factory, clock, rng=lambda: 0.99)  # 不觸發防守判定
    thieves = [make_farmer(admin, f"小偷{i}", home_group) for i in range(8)]
    plots_ready(admin, clock, player_id)
    planted = 16 * 2  # 小麥每格 2 個（conftest：100% 普通、不突變）
    barrier = threading.Barrier(len(thieves) + 1)
    errors: list[Exception] = []

    def steal(thief, plot):
        barrier.wait()
        for number in (plot, plot + 1):
            try:
                admin.steal_crop(home_group, thief, player_id, number, 1)
            except GameError:
                pass  # 土地已被收成、偷光或達到上限都是合法結果
            except Exception as exc:
                errors.append(exc)

    def harvest():
        barrier.wait()
        try:
            admin.harvest_all(player_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=steal, args=(t, 1 + i)) for i, t in enumerate(thieves)]
    threads.append(threading.Thread(target=harvest))
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    with session_factory() as s:
        def total(event_type):
            return s.scalar(select(func.coalesce(func.sum(GameTransaction.quantity), 0))
                            .where(GameTransaction.event_type == event_type))

        left = s.execute(text("SELECT coalesce(sum(remaining_quantity), 0) FROM farm_plots")).scalar()
        stock = s.scalar(select(func.coalesce(func.sum(InventoryItem.quantity), 0))
                         .where(InventoryItem.item_code == "wheat"))
        assert total("harvest") + total("steal") + left == planted
        assert stock == total("harvest") + total("steal")
        assert farm_stats_repo.check(s)["farm_stats_mismatches"] == 0
    with session_factory() as s:
        gold = s.execute(text("SELECT (SELECT sum(gold) FROM farms), (SELECT sum(gold_delta) FROM game_transactions)"))
        assert len(set(gold.one())) == 1


# ---------- 目錄快取 ----------


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(catalog_cache, "TTL_SECONDS", 10.0)
    catalog_cache.clear()


def test_catalog_reads_are_cached_across_transactions(cache_on, engine, session_factory):
    with session_factory() as s:
        crops_repo.list_all(s)
    with SqlCounter(engine) as sql, session_factory() as s:
        crops = crops_repo.list_all(s)
        genetics_repo.current(s)
        genetics_repo.current(s)
    assert crops and sql.count("SELECT crop_types") == 0
    assert sql.count("SELECT crop_genetics_settings") == 1  # 第一次讀，之後快取


def test_catalog_snapshots_are_not_attached_to_a_session(cache_on, session_factory):
    with session_factory() as s:
        first = crops_repo.list_all(s)
    with session_factory() as s:
        second = crops_repo.list_all(s)
        assert second[0] is first[0] and second[0] not in s
        assert second[0].code and second[0].sell_price is not None
        second.pop()  # 呼叫端可以改自己的 list，不影響快取
    with session_factory() as s:
        assert len(crops_repo.list_all(s)) == len(first)


def test_snapshot_does_not_detach_object_being_edited(cache_on, session_factory):
    """同一筆交易正在修改的物件不能被快取影響（這是用快照而不是 expunge 的原因）。"""
    with session_factory.begin() as s:
        crop = crops_repo.by_code(s, "wheat", for_update=True)
        crops_repo.list_all(s)
        crop.sell_price = 77
    with session_factory() as s:
        assert crops_repo.by_code(s, "wheat").sell_price == 77


def test_write_in_same_process_invalidates_cache_and_sees_own_changes(cache_on, admin, session_factory):
    with session_factory() as s:
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat")
    with session_factory.begin() as s:
        crop = crops_repo.by_code(s, "wheat", for_update=True)
        crop.sell_price = 123
        s.flush()
        # 交易內看得到自己未提交的修改
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat").sell_price == 123
    with session_factory() as s:
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat").sell_price == 123  # 提交後立即生效


def test_rolled_back_write_does_not_stay_in_cache(cache_on, session_factory):
    with pytest.raises(RuntimeError), session_factory.begin() as s:
        crop = crops_repo.by_code(s, "wheat", for_update=True)
        original = crop.sell_price
        crop.sell_price = original + 999
        s.flush()
        crops_repo.list_all(s)
        raise RuntimeError("rollback")
    with session_factory() as s:
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat").sell_price == original


def test_other_process_changes_appear_after_ttl(cache_on, monkeypatch, session_factory):
    """管理網站與 Bot 是不同程序：以原生 SQL 模擬另一個程序的寫入，TTL 過後才看得到。"""
    with session_factory() as s:
        crops_repo.list_all(s)
    with session_factory.begin() as s:
        s.execute(text("UPDATE crop_types SET sell_price = 456 WHERE code = 'wheat'"))
    with session_factory() as s:
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat").sell_price != 456
    now = catalog_cache.time.monotonic()
    monkeypatch.setattr(catalog_cache.time, "monotonic", lambda: now + catalog_cache.TTL_SECONDS + 1)
    with session_factory() as s:
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat").sell_price == 456


def test_admin_crop_update_is_visible_immediately_with_cache(cache_on, admin, session_factory):
    with session_factory() as s:
        crops_repo.list_all(s)
    admin.update_crop_grow_seconds("test", "wheat", 777)
    with session_factory() as s:
        assert next(c for c in crops_repo.list_all(s) if c.code == "wheat").grow_seconds == 777
        assert s.scalar(select(CropType.grow_seconds).where(CropType.code == "wheat")) == 777
