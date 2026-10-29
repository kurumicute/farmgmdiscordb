"""群組共同任務與群組競賽。"""

import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.auth import AdminAuth, hash_password
from app.cli.commands import CommandHandler
from app.db.models import AdminAction, CompetitionAward, Farm, GameTransaction, InventoryItem, QuestContribution
from app.db.session import make_engine, make_session_factory
from app.game import balance
from app.game.errors import (
    AlreadyExistsError,
    NotFoundError,
    NotGroupMemberError,
    PlotNotReadyError,
    ValidationError,
    WarehouseFullError,
)
from app.main import create_app
from app.repositories import community as community_repo
from app.repositories import inventory as inventory_repo
from app.services import activity
from app.services.community_service import CommunityService
from tests.conftest import grow, make_farmer

DAY = timedelta(days=1)
GRACE = timedelta(seconds=balance.COMPETITION_SETTLE_GRACE_SECONDS)


@pytest.fixture
def world(service, session_factory):
    """A 群：alice、carol（主要群組 A）；B 群：bob（主要群組 B）。alice 也加入 B，dave 主要群組 B 也加入 A。"""
    a = service.create_group("A 群").id
    b = service.create_group("B 群").id
    ids = {
        "alice": make_farmer(service, "alice", a),
        "carol": make_farmer(service, "carol", a),
        "bob": make_farmer(service, "bob", b),
        "dave": make_farmer(service, "dave", b),
    }
    service.add_member(b, ids["alice"])
    service.add_member(a, ids["dave"])
    with session_factory.begin() as s:
        s.execute(update(Farm).values(gold=10_000))
    return {**ids, "A": a, "B": b}


def quest(service, clock, group_id, **kw):
    params = dict(
        group_id=group_id, name="收成任務", quest_type="harvest", target=5, reward_mode="fixed",
        reward_type="gold", reward_amount=100, starts_at=clock.now(), ends_at=clock.now() + DAY,
    )
    params.update(kw)
    return service.create_quest("admin", **params)["quest_id"]


def competition(service, clock, **kw):
    params = dict(
        name="收成大賽", comp_type="harvest", rewards=[500, 300], starts_at=clock.now(), ends_at=clock.now() + DAY
    )
    params.update(kw)
    return service.create_competition("admin", **params)["competition_id"]


def harvest(service, clock, pid, group_id, plot=1, crop="wheat"):
    """在指定群組上下文中種植並收成（小麥收成 2 個）。"""
    ctx = service.in_group_context(group_id)
    grow(ctx, pid, plot, crop)
    clock.advance(600)
    return ctx.harvest_crop(pid, plot)


def progress(service, group_id, pid, quest_id):
    return service.quest_status(group_id, pid, quest_id)["progress"]


def gold(service, pid):
    return service.get_farm_status(pid).gold


def concurrently(n, fn):
    barrier = threading.Barrier(n)
    results, lock = [], threading.Lock()

    def run():
        barrier.wait()
        try:
            r = ("ok", fn())
        except Exception as exc:  # noqa: BLE001
            r = ("err", exc)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---------- 任務進度與跨群組計分 ----------


def test_only_home_group_context_counts(service, world, clock):
    A, B, alice, bob = world["A"], world["B"], world["alice"], world["bob"]
    qa = quest(service, clock, A, target=100)
    qb = quest(service, clock, B, target=100)
    harvest(service, clock, alice, A)  # 主要群組 A、在 A 操作 → 計入 A
    harvest(service, clock, alice, B)  # 也是 B 成員，但主要群組是 A → 不計入任何群組
    ctx_none = service  # CMD 未選群組
    grow(ctx_none, alice, 1, "wheat")
    clock.advance(600)
    ctx_none.harvest_crop(alice, 1)
    harvest(service, clock, bob, B)  # bob 主要群組 B → 計入 B
    assert progress(service, A, alice, qa) == 2
    assert progress(service, B, bob, qb) == 2
    # 同一筆操作不會同時計入兩個群組
    assert service.quest_status(B, alice, qb)["my_contribution"] == 0


def test_non_matching_events_and_failed_operations_do_not_count(service, world, clock, session_factory, monkeypatch):
    A, alice = world["A"], world["alice"]
    qa = quest(service, clock, A, target=100)
    ctx = service.in_group_context(A)
    ctx.buy_seeds(alice, "wheat", 3)  # 購買不是收成
    ctx.plant_crop(alice, 1, "wheat")
    with pytest.raises(PlotNotReadyError):  # 失敗的操作
        ctx.water_crop(alice, 1)
        ctx.harvest_crop(alice, 1)
    assert progress(service, A, alice, qa) == 0

    # 計分寫入失敗時，整個遊戲操作一起回滾
    clock.advance(60)

    def boom(*args, **kwargs):
        raise RuntimeError("模擬計分失敗")

    monkeypatch.setattr(community_repo, "add_quest_contribution", boom)
    with pytest.raises(RuntimeError):
        ctx.harvest_crop(alice, 1)
    monkeypatch.undo()
    assert service.get_farm_status(alice).plots[0].state == "ready"  # 作物仍在
    assert progress(service, A, alice, qa) == 0
    assert ctx.harvest_crop(alice, 1).quantity == 2
    assert progress(service, A, alice, qa) == 2


def test_same_transaction_counted_once(service, world, clock, session_factory):
    A, alice = world["A"], world["alice"]
    qa = quest(service, clock, A, target=100)
    cid = competition(service, clock)
    harvest(service, clock, alice, A)
    with session_factory.begin() as s:  # 重複處理同一筆交易紀錄（例如重試或重啟後重放）
        tx = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "harvest")).one()
        farm = s.get(Farm, tx.farm_id)
        activity.on_event(s, farm, tx)
        activity.on_event(s, farm, tx)
    assert progress(service, A, alice, qa) == 2
    assert service.competition_detail(cid)["ranking"][0]["score"] == 2


def test_quest_period_limits(service, world, clock):
    A, alice = world["A"], world["alice"]
    later = quest(service, clock, A, starts_at=clock.now() + DAY, ends_at=clock.now() + 2 * DAY)
    short = quest(service, clock, A, ends_at=clock.now() + timedelta(minutes=5))
    assert service.quest_status(A, alice, later)["state"] == "scheduled"
    harvest(service, clock, alice, A)  # 收成發生在第 10 分鐘：short 已截止，later 尚未開始
    assert progress(service, A, alice, short) == 0 and progress(service, A, alice, later) == 0
    assert service.quest_status(A, alice, short)["state"] == "expired"
    with pytest.raises(ValidationError, match="已截止且未達成"):
        service.claim_quest(A, alice, short)
    clock.advance(DAY.total_seconds())
    harvest(service, clock, alice, A, plot=2)
    assert progress(service, A, alice, later) == 2


def test_completion_stops_progress_and_fixed_rewards(service, world, clock, session_factory):
    A, alice, carol, dave = world["A"], world["alice"], world["carol"], world["dave"]
    qa = quest(service, clock, A, target=3, reward_amount=100)
    harvest(service, clock, alice, A)  # 2
    with pytest.raises(ValidationError, match="尚未完成"):
        service.claim_quest(A, alice, qa)
    harvest(service, clock, carol, A)  # 4 ≥ 3 → 完成
    status = service.quest_status(A, alice, qa)
    assert status["state"] == "completed" and status["progress"] == 4
    harvest(service, clock, carol, A, plot=2)  # 完成後不再累積
    assert progress(service, A, alice, qa) == 4

    before = gold(service, alice)
    assert "100 金幣" in service.claim_quest(A, alice, qa).message
    assert gold(service, alice) == before + 100
    with pytest.raises(AlreadyExistsError):
        service.claim_quest(A, alice, qa)
    assert gold(service, alice) == before + 100
    with pytest.raises(ValidationError, match="沒有參與"):
        service.claim_quest(A, dave, qa)  # A 成員但沒有貢獻
    with pytest.raises(NotGroupMemberError):
        service.claim_quest(A, world["bob"], qa)
    with pytest.raises(NotFoundError):
        service.claim_quest(world["B"], alice, qa)  # 任務不屬於目前群組


def test_concurrent_claims_and_restart(service, world, clock, database_url):
    A, alice = world["A"], world["alice"]
    qa = quest(service, clock, A, target=2, reward_amount=250)
    harvest(service, clock, alice, A)
    before = gold(service, alice)
    engine = make_engine(database_url)
    try:
        restarted = CommunityService(make_session_factory(engine), clock)  # 重啟後完成狀態仍在
        assert restarted.quest_status(A, alice, qa)["state"] == "completed"
        results = concurrently(5, lambda: restarted.claim_quest(A, alice, qa))
        assert sum(1 for r in results if r[0] == "ok") == 1
        assert all(isinstance(r[1], AlreadyExistsError) for r in results if r[0] == "err")
    finally:
        engine.dispose()
    assert gold(service, alice) == before + 250
    assert service.quest_status(A, alice, qa)["my_claim"]["amount"] == 250


def test_proportional_rewards_and_item_rewards(service, world, clock, session_factory):
    A, alice, carol = world["A"], world["alice"], world["carol"]
    qa = quest(service, clock, A, target=8, reward_mode="proportional", reward_amount=100)
    harvest(service, clock, alice, A)  # 2
    harvest(service, clock, alice, A, plot=2)  # 4
    harvest(service, clock, alice, A, plot=3)  # 6
    harvest(service, clock, carol, A)  # 8 → 完成；alice 6/8、carol 2/8
    assert service.quest_status(A, alice, qa)["my_reward"] == 75
    assert service.quest_status(A, carol, qa)["my_reward"] == 25

    qi = quest(service, clock, A, target=2, reward_type="fertilizer", reward_amount=3)
    harvest(service, clock, carol, A, plot=2)
    with session_factory.begin() as s:
        farm_id = s.scalar(select(Farm.id).where(Farm.player_id == carol))
        used = s.scalar(select(func.sum(InventoryItem.quantity)).where(InventoryItem.farm_id == farm_id)) or 0
        inventory_repo.add(s, farm_id, "carrot", 48 - used)  # 背包 48/50
    with pytest.raises(WarehouseFullError, match="保留"):
        service.claim_quest(A, carol, qi)  # 背包不足：獎勵保留
    service.sell(carol, "carrot", 10)
    assert "3 個化肥" in service.claim_quest(A, carol, qi).message
    assert service.get_backpack(carol).consumables[0].quantity == 3


def test_cancel_records_reason_and_keeps_history(service, world, clock, session_factory):
    A, alice = world["A"], world["alice"]
    qa = quest(service, clock, A, target=100)
    harvest(service, clock, alice, A)
    with pytest.raises(ValidationError, match="取消原因"):
        service.cancel_quest("admin", qa, " ")
    view = service.cancel_quest("admin", qa, "目標設定錯誤")
    assert view["state"] == "cancelled" and view["cancel_reason"] == "目標設定錯誤"
    harvest(service, clock, alice, A, plot=2)
    assert progress(service, A, alice, qa) == 2  # 取消後不再累積，既有貢獻保留
    with pytest.raises(ValidationError, match="已取消"):
        service.claim_quest(A, alice, qa)
    with pytest.raises(ValidationError):
        service.cancel_quest("admin", qa, "再取消一次")
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(QuestContribution)) == 1
        reasons = [a.details.get("reason") for a in s.scalars(select(AdminAction))]
        assert "目標設定錯誤" in reasons


def test_reward_terms_are_fixed(service, world, clock, session_factory):
    qa = quest(service, clock, world["A"])
    cid = competition(service, clock)
    for sql in (
        f"UPDATE quests SET reward_amount = 999999 WHERE id = {qa}",
        f"UPDATE quests SET target = 1 WHERE id = {qa}",
        f"UPDATE competitions SET rewards = '[99999]' WHERE id = {cid}",
        f"UPDATE competitions SET ends_at = ends_at + interval '1 day' WHERE id = {cid}",
    ):
        with pytest.raises(IntegrityError, match="fixed after creation"):
            with session_factory.begin() as s:
                s.execute(text(sql))


def test_create_validation(service, world, clock):
    A = world["A"]
    with pytest.raises(ValidationError):
        quest(service, clock, A, quest_type="harvest", metric="amount")  # 只有販售能依金額
    with pytest.raises(ValidationError):
        quest(service, clock, A, quest_type="harvest", item_filter="egg")
    with pytest.raises(ValidationError):
        quest(service, clock, A, ends_at=clock.now() - DAY, starts_at=clock.now() - 2 * DAY)
    with pytest.raises(ValidationError):
        quest(service, clock, A, ends_at=clock.now() + 100 * DAY)
    with pytest.raises(NotFoundError):
        quest(service, clock, 999)
    with pytest.raises(ValidationError):
        competition(service, clock, rewards=[1, 2, 3, 4])


# ---------- 各任務類型 ----------


def test_quest_types_sell_build_animal_defense(service, world, clock, session_factory):
    A, alice, carol, bob = world["A"], world["alice"], world["carol"], world["bob"]
    week = clock.now() + 7 * DAY  # 動物生產需要 24 小時，期限要涵蓋
    q_amount = quest(service, clock, A, quest_type="sell", metric="amount", target=1000, ends_at=week)
    q_build = quest(service, clock, A, quest_type="build", target=10, ends_at=week)
    q_egg = quest(service, clock, A, quest_type="animal", item_filter="egg", target=10, ends_at=week)
    q_def = quest(service, clock, A, quest_type="defense", target=10, ends_at=week)
    ctx = service.in_group_context(A)
    harvest(service, clock, alice, A)
    ctx.sell(alice, "wheat", 2)  # +30 金幣
    ctx.upgrade_farm(alice)
    ctx.build(alice, "畜舍")
    ctx.buy_animals(alice, "雞", 1)
    ctx.buy_animals(alice, "牛", 1)
    ctx.feed_all(alice)
    clock.advance(DAY.total_seconds())
    ctx.collect_animal(alice, 1)  # 雞蛋 2
    ctx.collect_animal(alice, 2)  # 牛奶不計入「雞蛋」任務
    assert progress(service, A, alice, q_amount) == 30
    assert progress(service, A, alice, q_build) == 2
    assert progress(service, A, alice, q_egg) == 2

    # 防守任務：被偷者 carol（主要群組 A）在 A 的偷菜中防守成功
    service.buy_defense(carol, "警戒犬")
    grow(service, carol, 1, "strawberry")
    clock.advance(600)
    service.add_member(A, bob)
    blocker = CommunityService(session_factory, clock, rng=lambda: 0.0)
    assert blocker.steal_crop(A, bob, carol, 1, 1).success is False
    assert progress(service, A, carol, q_def) == 1


# ---------- 競賽 ----------


def test_competition_scoring_settlement_and_history(service, world, clock, session_factory):
    A, B = world["A"], world["B"]
    alice, carol, bob, dave = world["alice"], world["carol"], world["bob"], world["dave"]
    cid = competition(service, clock, rewards=[500, 300])
    harvest(service, clock, alice, A)  # A +2
    harvest(service, clock, alice, A, plot=2)  # A +2
    harvest(service, clock, alice, B, plot=3)  # alice 在 B 操作：不計分
    harvest(service, clock, bob, B)  # B +2
    detail = service.competition_detail(cid, alice)
    assert [(r["group_name"], r["score"]) for r in detail["ranking"]] == [("A 群", 4), ("B 群", 2)]
    assert detail["my_points"] == 4 and not detail["final"]
    with pytest.raises(ValidationError, match="尚未截止"):
        service.settle_competition(cid)

    clock.advance((detail["ends_at"] - clock.now()).total_seconds() - 600)
    grow(service.in_group_context(B), bob, 2, "strawberry")  # 600 秒後（剛好截止時）成熟
    clock.advance(600)
    service.in_group_context(B).harvest_crop(bob, 2)  # 截止當下的操作不計分
    assert service.competition_detail(cid)["ranking"][1]["score"] == 2
    with pytest.raises(ValidationError):
        service.settle_competition(cid)  # 仍在結算緩衝時間內
    clock.advance(GRACE.total_seconds())
    before = {p: gold(service, p) for p in (alice, carol, bob, dave)}
    results = concurrently(4, lambda: service.settle_competition(cid))  # 並行結算只會成功一次
    assert sorted(r[1] for r in results) == [False, False, False, True]
    after = {p: gold(service, p) for p in (alice, carol, bob, dave)}
    # 第 1 名 A：主要群組為 A 的全體成員（alice、carol，含沒貢獻的 carol）；dave 是 A 成員但主要群組 B
    assert after[alice] - before[alice] == 500 and after[carol] - before[carol] == 500
    assert after[bob] - before[bob] == 300 and after[dave] - before[dave] == 300
    assert service.settle_competition(cid) is False  # 重複結算不重複發獎
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(CompetitionAward)) == 4

    # 結算後成員變動、群組改名都不改寫歷史成績
    service.remove_member(A, alice)
    with session_factory.begin() as s:
        s.execute(text(f"UPDATE groups SET name = '改名' WHERE id = {A}"))
    final = service.competition_detail(cid, alice)
    assert final["final"] and [(r["rank"], r["group_name"], r["score"]) for r in final["ranking"]] == [
        (1, "A 群", 4),
        (2, "B 群", 2),
    ]
    assert final["my_award"] == {"rank": 1, "gold": 500}
    history = service.competition_history(alice)
    assert [h["competition_id"] for h in history] == [cid]


def test_competition_types_and_cancel(service, world, clock, session_factory):
    A, B, alice, bob = world["A"], world["B"], world["alice"], world["bob"]
    dev = competition(service, clock, name="發展", comp_type="development", rewards=[100])
    sales = competition(service, clock, name="銷售", comp_type="sales", rewards=[100])
    cancelled = competition(service, clock, name="取消", comp_type="harvest", rewards=[100])
    service.cancel_competition("admin", cancelled, "活動延期")
    ctx_a, ctx_b = service.in_group_context(A), service.in_group_context(B)
    ctx_a.upgrade_farm(alice)  # 10 分
    ctx_a.build(alice, "磨坊")  # 5 分
    ctx_b.build(bob, "畜舍")  # 5 分
    harvest(service, clock, alice, A)
    ctx_a.sell(alice, "wheat", 2)  # 收入 30
    assert [(r["group_name"], r["score"]) for r in service.competition_detail(dev)["ranking"]] == [
        ("A 群", 15), ("B 群", 5)
    ]
    assert service.competition_detail(sales)["ranking"][0]["score"] == 30
    assert service.competition_detail(cancelled)["ranking"] == []  # 取消後不再計分
    clock.advance((DAY + GRACE).total_seconds())
    assert sorted(service.settle_due_competitions("admin")) == sorted([dev, sales])
    assert service.competition_detail(cancelled)["state"] == "cancelled"


def test_competitions_survive_restart(service, world, clock, database_url):
    A, alice = world["A"], world["alice"]
    cid = competition(service, clock)
    harvest(service, clock, alice, A)
    clock.advance((DAY + GRACE).total_seconds())
    engine = make_engine(database_url)
    try:
        restarted = CommunityService(make_session_factory(engine), clock)
        assert restarted.competition_detail(cid)["final"] is True  # 重啟後查詢時結算
        assert restarted.competition_detail(cid, alice)["my_award"]["gold"] == 500
    finally:
        engine.dispose()


# ---------- CMD ----------


def test_cli_quest_and_competition(service, world, clock):
    A, alice, bob = world["A"], world["alice"], world["bob"]
    qa = quest(service, clock, A, name="豐收週", target=2)
    cid = competition(service, clock, name="收成大賽")
    cli = CommandHandler(service)
    cli.handle(f"player use {alice}")
    assert "請先選擇群組" in cli.handle("quest group")
    assert "請先選擇群組" in cli.handle(f"quest claim {qa}")
    assert "豐收週" in cli.handle("quest list")
    cli.handle(f"group use {A}")
    cli.handle("buy wheat 1")
    cli.handle("plant 1 wheat")
    cli.handle("water 1")
    clock.advance(60)
    cli.handle("harvest 1")  # 在主要群組 A 中操作 → 計分
    status = cli.handle(f"quest status {qa}")
    assert "已完成" in status and "我的貢獻：2" in status and "可領取獎勵：100" in status
    assert "已領取任務" in cli.handle(f"quest claim {qa}")
    assert "已經領取過" in cli.handle(f"quest claim {qa}")
    assert "豐收週" in cli.handle("quest group")
    board = cli.handle(f"competition leaderboard {cid}")
    assert "即時排名" in board and "A 群" in board and "alice（A 群）：2 分" in board
    assert "我的貢獻：2 分" in cli.handle(f"competition status {cid}")
    assert "收成大賽" in cli.handle("competition list")
    assert "目前沒有已結算的競賽" in cli.handle("competition history")
    clock.advance((DAY + GRACE).total_seconds())
    assert "第 1 名 A 群" in cli.handle("competition history")
    cli.handle(f"player use {bob}")
    assert "不是目前群組的成員" in cli.handle(f"quest status {qa}")  # bob 不是 A 成員


# ---------- 網站管理 ----------


def test_admin_api_and_monitor(service, world, clock, session_factory):
    A, alice = world["A"], world["alice"]
    pw = "community-admin-pass"
    app = create_app(
        session_factory, clock=clock, allowed_hosts=["testserver"], admin_auth=AdminAuth("admin", hash_password(pw))
    )
    hdr = {"X-Farmgm-Admin": "1"}
    body = {
        "group_id": A, "name": "網頁任務", "quest_type": "harvest", "target": 2, "reward_mode": "fixed",
        "reward_type": "gold", "reward_amount": 50,
        "starts_at": clock.now().isoformat(), "ends_at": (clock.now() + DAY).isoformat(),
    }
    with TestClient(app) as c:
        assert c.post("/api/admin/quests", json=body, headers=hdr).status_code == 401  # 未登入
        c.post("/api/auth/login", json={"username": "admin", "password": pw}, headers=hdr)
        res = c.post("/api/admin/quests", json=body, headers=hdr)
        assert res.status_code == 201
        qid = res.json()["quest_id"]
        assert c.post("/api/admin/quests", json={**body, "quest_type": "magic"}, headers=hdr).status_code == 422
        assert c.post("/api/admin/quests", json={**body, "metric": "amount"}, headers=hdr).status_code == 400
        comp = {"name": "網頁競賽", "comp_type": "sales", "rewards": [300, 200, 100],
                "starts_at": clock.now().isoformat(), "ends_at": (clock.now() + DAY).isoformat()}
        cid = c.post("/api/admin/competitions", json=comp, headers=hdr).json()["competition_id"]

        harvest(service, clock, alice, A)
        detail = c.get(f"/api/monitor/quests/{qid}").json()
        assert detail["state"] == "completed" and detail["contributors"][0]["player_name"] == "alice"
        assert c.get(f"/api/monitor/competitions/{cid}").json()["final"] is False
        assert c.post(f"/api/admin/competitions/{cid}/cancel", json={}, headers=hdr).status_code == 422  # 須有原因
        cancel = c.post(f"/api/admin/competitions/{cid}/cancel", json={"reason": "測試取消"}, headers=hdr)
        assert cancel.status_code == 200
        actions = c.get("/api/monitor/admin-actions").json()
        assert [a["action"] for a in actions][:3] == ["cancel_competition", "create_competition", "create_quest"]
        assert actions[0]["actor"] == "admin" and actions[0]["details"]["reason"] == "測試取消"
        assert c.post("/api/admin/competitions/settle-due", headers=hdr).json() == {"settled": []}
        assert c.post("/api/monitor/quests", json={}).status_code == 405  # 監控端點唯讀
        assert c.get("/api/monitor/quests", params={"group_id": A}).json()[0]["name"] == "網頁任務"
