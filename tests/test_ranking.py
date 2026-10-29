"""群組總排行與排名獎勵（每日／每週／每月結算）。

測試時鐘起點 2026-01-01 00:00 UTC = 台灣時間週四 08:00；
本週為 2025-12-29（一）00:00 ～ 2026-01-05（一）00:00（台灣時間），即 UTC 2026-01-04 16:00 結束。
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import AdminAuth, hash_password
from app.cli.commands import CommandHandler
from app.clock import TAIPEI_TZ
from app.db.models import GameTransaction, RankingAward
from app.game import balance
from app.game import ranking as ranking_rules
from app.game.errors import NotFoundError, ValidationError
from app.main import create_app
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer

ACTOR = "admin"
ADMIN = {"X-Farmgm-Admin": "1"}
PASSWORD = "test-password-123"
WEEK_END = 3 * 86400 + 16 * 3600  # 從測試時鐘起點到本週結束的秒數
DAY_END = 16 * 3600  # 到台灣時間 1/2 00:00
GRACE = balance.RANKING_SETTLE_GRACE_SECONDS + 1


@pytest.fixture
def admin(session_factory, clock) -> AdminService:
    return AdminService(session_factory, clock)


@pytest.fixture
def world(admin, clock):
    """兩個群組：北區（甲、乙）與南區（丙）；北區賣比較多草莓，丙個人賣最多小麥。"""
    north, south = admin.create_group("北區").id, admin.create_group("南區").id
    a, b = make_farmer(admin, "甲", north), make_farmer(admin, "乙", north)
    c = make_farmer(admin, "丙", south)
    for pid in (a, b):
        grow(admin, pid, 1, "strawberry")
    for plot in (1, 2, 3):
        grow(admin, c, plot, "wheat")
    clock.advance(600)
    for pid in (a, b):
        admin.harvest_crop(pid, 1)
        admin.sell_crop(pid, "strawberry", 5)  # 各 5 個、125 金幣
    for plot in (1, 2, 3):
        admin.harvest_crop(c, plot)
    admin.sell_crop(c, "wheat", 6)  # 6 個、90 金幣
    return {"north": north, "south": south, "a": a, "b": b, "c": c}


def rule(**kw) -> dict:
    return {
        "name": "每週群組金幣王",
        "scope": "group",
        "metric": "sales",
        "period": "weekly",
        "rewards": [{"reward_type": "gold", "amount": 2000}],
        **kw,
    }


def gold(admin, pid) -> int:
    return admin.get_farm_status(pid).gold


# ---------- 純規則 ----------


def test_period_bounds_taipei():
    at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)  # 台灣 1/1（四）08:00
    day = ranking_rules.period_bounds("daily", at)
    assert day[0] == datetime(2026, 1, 1, tzinfo=TAIPEI_TZ) and day[1] == datetime(2026, 1, 2, tzinfo=TAIPEI_TZ)
    week = ranking_rules.period_bounds("weekly", at)
    assert week == (datetime(2025, 12, 29, tzinfo=TAIPEI_TZ), datetime(2026, 1, 5, tzinfo=TAIPEI_TZ))
    dec = ranking_rules.period_bounds("monthly", datetime(2025, 12, 31, 20, tzinfo=UTC))  # 台灣已是 1/1 04:00
    assert dec == (datetime(2026, 1, 1, tzinfo=TAIPEI_TZ), datetime(2026, 2, 1, tzinfo=TAIPEI_TZ))
    assert ranking_rules.period_bounds("monthly", datetime(2025, 12, 15, tzinfo=UTC))[1] == datetime(
        2026, 1, 1, tzinfo=TAIPEI_TZ
    )


def test_rule_validation():
    with pytest.raises(ValidationError, match="獎勵名次"):
        ranking_rules.validate_rule(rule(rewards=[]))
    with pytest.raises(ValidationError, match="第 1 名的獎勵數量"):
        ranking_rules.validate_rule(rule(rewards=[{"reward_type": "fertilizer", "amount": 1000}]))
    with pytest.raises(ValidationError, match="結算週期"):
        ranking_rules.validate_rule(rule(period="yearly"))
    ok = ranking_rules.validate_rule(rule(rewards=[{"reward_type": "gold", "amount": 1}] * balance.RANKING_MAX_RANKS))
    assert len(ok["rewards"]) == balance.RANKING_MAX_RANKS


# ---------- 群組總排行 ----------


def test_group_leaderboard_sums_members(admin, world, clock):
    sales = admin.group_leaderboard("sales")
    assert [(r["group_name"], r["member_count"], r["score"]) for r in sales] == [("北區", 2, 10), ("南區", 1, 6)]
    goods = admin.group_leaderboard("goods")
    assert [(r["group_name"], r["score"]) for r in goods] == [("北區", 250), ("南區", 90)]
    money = admin.group_leaderboard("money")
    assert money[0]["score"] == gold(admin, world["a"]) + gold(admin, world["b"])
    # 期間：本週淨賺金幣不含建立農場的初始金幣（北區：賣 250 − 種子 120）
    week = admin.group_leaderboard("money", "week")
    assert {r["group_name"]: r["score"] for r in week} == {"北區": 250 - 120, "南區": 90 - 30}
    # 下週還沒有任何操作
    clock.advance(WEEK_END + GRACE)
    assert [r["score"] for r in admin.group_leaderboard("sales", "week")] == [0, 0]
    assert admin.group_leaderboard("sales", "all")[0]["score"] == 10
    with pytest.raises(ValidationError):
        admin.group_leaderboard("sales", "year")


# ---------- 結算 ----------


def test_weekly_group_and_individual_rewards(admin, world, clock, session_factory):
    a, b, c = world["a"], world["b"], world["c"]
    admin.create_ranking_rule(ACTOR, rule())
    admin.create_ranking_rule(
        ACTOR,
        rule(name="每週個人販售", scope="individual", metric="sales",
             rewards=[{"reward_type": "gold", "amount": 10000}, {"reward_type": "fertilizer", "amount": 3}]),
    )
    before = {pid: gold(admin, pid) for pid in (a, b, c)}
    assert admin.settle_due_rankings() == []  # 本週還沒結束
    clock.advance(WEEK_END - 600)  # world 已前進 600 秒：現在剛好是本週結束
    assert admin.settle_due_rankings() == []  # 仍在結算緩衝時間內
    clock.advance(GRACE)
    settled = admin.settle_due_rankings()
    assert len(settled) == 2
    # 群組第 1 名（北區）全體 +2000；個人第 1 名（丙，6 個）+10000；第 2 名（甲，5 個、ID 較小）3 個化肥
    assert gold(admin, a) == before[a] + 2000
    assert gold(admin, b) == before[b] + 2000
    assert gold(admin, c) == before[c] + 10000
    assert {i.item_code: i.quantity for i in admin.get_inventory(a).items}.get("fertilizer") == 3
    # 重複結算不會再發
    assert admin.settle_due_rankings() == []
    with session_factory() as s:
        assert s.query(RankingAward).count() == 4
        txs = list(s.scalars(select(GameTransaction).where(GameTransaction.event_type == "ranking_reward")))
    assert len(txs) == 4 and all("排名獎勵" in t.description for t in txs)
    detail = admin.ranking_settlement_detail(settled[0])
    assert [(x["player_name"], x["rank"]) for x in detail["awards"]] == [("甲", 1), ("乙", 1)]
    assert admin.player_ranking_awards(c)[0]["reward_text"] == "10000 金幣"
    # 下一週沒有任何販售：結算但沒有人得獎（分數必須 > 0）
    clock.advance(7 * 86400)
    next_week = admin.settle_due_rankings()
    assert len(next_week) == 2
    assert all(admin.ranking_settlement_detail(sid)["award_count"] == 0 for sid in next_week)


def test_reward_money_excludes_rewards_and_admin_adjust(admin, world, clock):
    admin.set_farm_values(ACTOR, world["c"], gold=999_999)  # 管理員調整不算淨賺
    rows = admin.period_leaderboard("money", "week")
    assert rows[0]["player_name"] in ("甲", "乙") and rows[0]["score"] == 125 - 60


def test_disabled_rule_not_backfilled_and_daily_period(admin, world, clock):
    weekly = admin.create_ranking_rule(ACTOR, rule())
    daily = admin.create_ranking_rule(ACTOR, rule(name="每日", period="daily"))
    admin.set_ranking_rule_enabled(ACTOR, weekly["id"], False)
    clock.advance(DAY_END + GRACE)
    assert len(admin.settle_due_rankings()) == 1  # 只有每日（1/1）
    clock.advance(WEEK_END)  # 跨過本週結束
    admin.set_ranking_rule_enabled(ACTOR, weekly["id"], True)
    settled_after = admin.settle_due_rankings()
    rules = {r["id"]: r for r in admin.list_ranking_rules()}
    # 每週規則在停用期間結束的本週不回溯；每日規則補結算錯過的天數
    assert all(admin.ranking_settlement_detail(s)["rule_id"] == daily["id"] for s in settled_after)
    assert rules[weekly["id"]]["enabled"] is True
    # 刪除規則後歷史保留
    admin.delete_ranking_rule(ACTOR, daily["id"])
    history = admin.ranking_settlements()
    assert history and history[0]["rule_id"] is None and history[0]["rule_name"] == "每日"
    with pytest.raises(NotFoundError):
        admin.update_ranking_rule(ACTOR, daily["id"], rule())


def test_item_reward_ignores_warehouse_capacity(admin, world, clock):
    a = world["a"]
    admin.set_inventory(ACTOR, a, "seed_wheat", "normal", "none", balance.INITIAL_WAREHOUSE_CAPACITY)
    admin.set_inventory(ACTOR, world["c"], "seed_wheat", "normal", "none", 0)
    admin.create_ranking_rule(
        ACTOR, rule(scope="individual", metric="goods", rewards=[{"reward_type": "earthworm", "amount": 2}])
    )
    clock.advance(WEEK_END + GRACE)
    admin.settle_due_rankings()
    inv = {i.item_code: i.quantity for i in admin.get_inventory(a).items}
    assert inv["earthworm"] == 2  # 倉庫已滿仍發放，不會遺失獎勵


def test_cli_group_leaderboard_and_rewards(admin, world, clock):
    admin.create_ranking_rule(ACTOR, rule())
    cli = CommandHandler(admin)
    cli.handle(f"player use {world['a']}")
    out = cli.handle("leaderboard groups sales week")
    assert "北區" in out and out.index("北區") < out.index("南區")
    clock.advance(WEEK_END + GRACE)
    out = cli.handle("leaderboard rewards")  # 查詢時也會結算到期的期間
    assert "每週群組金幣王" in out and "2000 金幣" in out
    assert "用法" in cli.handle("leaderboard groups sales year")


# ---------- API ----------


@pytest.fixture
def client(session_factory, clock):
    app = create_app(
        session_factory, clock=clock, refresh_seconds=0.2, allowed_hosts=["testserver"],
        admin_auth=AdminAuth("admin", hash_password(PASSWORD)),
    )
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers=ADMIN)
        yield c


def test_ranking_api(client, world, clock):
    res = client.get("/api/monitor/leaderboard", params={"scope": "groups", "kind": "sales", "period": "week"})
    assert [r["group_name"] for r in res.json()["items"]] == ["北區", "南區"]
    assert client.get("/api/monitor/leaderboard", params={"period": "year"}).status_code == 422
    body = rule(rewards=[{"reward_type": "gold", "amount": 2000}, {"reward_type": "patrol_ticket", "amount": 1}])
    created = client.post("/api/admin/ranking-rules", json=body, headers=ADMIN)
    assert created.status_code == 201
    rid = created.json()["id"]
    assert client.post("/api/admin/ranking-rules", json={**body, "rewards": []}, headers=ADMIN).status_code == 422
    bad = {**body, "rewards": [{"reward_type": "gold", "amount": 0}]}
    assert client.post("/api/admin/ranking-rules", json=bad, headers=ADMIN).status_code == 400
    assert client.put(f"/api/admin/ranking-rules/{rid}", json={**body, "period": "monthly"}, headers=ADMIN).json()[
        "period"
    ] == "monthly"
    client.put(f"/api/admin/ranking-rules/{rid}", json=body, headers=ADMIN)
    clock.advance(WEEK_END + GRACE)
    settled = client.post("/api/admin/ranking-rules/settle-due", headers=ADMIN).json()["settled"]
    # 改成每月又改回每週：從修改時間起算，本週在修改之後才結束，所以仍會結算
    assert len(settled) == 1
    data = client.get("/api/monitor/ranking-rewards").json()
    assert data["rules"][0]["rewards_text"] == "第 1 名 2000 金幣；第 2 名 1 個巡查券"
    assert data["settlements"][0]["award_count"] == 3  # 北區 2 人 + 南區 1 人
    detail = client.get(f"/api/monitor/ranking-settlements/{settled[0]}").json()
    assert [a["rank"] for a in detail["awards"]] == [1, 1, 2]
    assert client.get("/api/monitor/ranking-settlements/999").status_code == 404
    assert client.delete(f"/api/admin/ranking-rules/{rid}", headers=ADMIN).status_code == 200
