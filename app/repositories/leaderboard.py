"""排行榜查詢：金幣為目前真實餘額（或期間淨賺金幣）；販售統計取自統計表（farm_stats／farm_daily_stats）。

- 全部期間：farm_stats（每座農場一列，隨販售在同一筆交易中更新），不再加總全部交易紀錄。
- 本日／本週／本月與排名獎勵結算：期間以台灣日期切分，加總 farm_daily_stats（每座農場每天最多一列）。
- 期間邊界不是台灣午夜時（非標準期間）才回退為直接加總 game_transactions。

每位玩家最多一座農場（farms.player_id 唯一），總排行榜中每位玩家只計算一次。
群組總排行：把「主要所屬群組為該群組且仍是成員」的農場加總，每座農場只計入一個群組。
同分時依固定的次要條件與 ID 排序，名次穩定。
"""

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import Farm, FarmStats, GameTransaction, Group, GroupMember, Player
from app.game.ranking import NET_GOLD_EXCLUDED
from app.repositories import crops as crops_repo
from app.repositories import farm_stats as farm_stats_repo

# money：目前金幣（指定期間時為期間淨賺金幣）；sales：農產品（作物）販售；goods：商品總販售（作物＋動物產品＋加工品）
KINDS = ("money", "sales", "goods")


def _tx_stats(kind: str, since: datetime | None, until: datetime | None):
    """直接由交易紀錄加總（只用於非標準期間）。"""
    sold = GameTransaction.event_type == "sell"
    if kind != "goods":
        # 原有販售排行只統計作物（含珍稀變種；不同品質／突變仍是同一筆販售，只依實際數量與收入計算）
        sold = sold & GameTransaction.item_code.in_(crops_repo.produce_codes_select())
    stmt = select(
        GameTransaction.farm_id,
        func.sum(case((sold, GameTransaction.quantity), else_=0)).label("qty"),
        func.sum(case((sold, GameTransaction.gold_delta), else_=0)).label("income"),
        func.sum(
            case((GameTransaction.event_type.not_in(NET_GOLD_EXCLUDED), GameTransaction.gold_delta), else_=0)
        ).label("net"),
    )
    if since is not None:
        stmt = stmt.where(GameTransaction.created_at >= since)
    if until is not None:
        stmt = stmt.where(GameTransaction.created_at < until)
    return stmt.group_by(GameTransaction.farm_id).subquery()


def _stats(kind: str, since: datetime | None, until: datetime | None):
    """各農場的販售數量、販售收入與淨賺金幣（子查詢，欄位 farm_id／qty／income／net）。"""
    prefix = "goods" if kind == "goods" else "crop"
    if since is None and until is None:
        s = FarmStats
        return select(
            s.farm_id,
            getattr(s, f"{prefix}_sold_quantity").label("qty"),
            getattr(s, f"{prefix}_sales_income").label("income"),
            # 全部期間淨賺金幣 = 目前金幣 - 非經營金幣（交易紀錄 gold_delta 加總 = 金幣）
            (Farm.gold - s.non_operating_gold).label("net"),
        ).join(Farm, Farm.id == s.farm_id).subquery()
    if since is not None and until is not None and farm_stats_repo.is_day_aligned(since) and (
        farm_stats_repo.is_day_aligned(until)
    ):
        d = farm_stats_repo.period_rows(since, until)
        return select(
            d.c.farm_id,
            getattr(d.c, f"{prefix}_qty").label("qty"),
            getattr(d.c, f"{prefix}_income").label("income"),
            d.c.net,
        ).subquery()
    return _tx_stats(kind, since, until)


def ranking(
    session: Session,
    kind: str,
    group_id: int | None,
    limit: int,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    if kind not in KINDS:
        raise ValueError(f"unknown leaderboard kind: {kind}")
    stats = _stats(kind, since, until)
    sold_qty = func.coalesce(stats.c.qty, 0)
    sold_income = func.coalesce(stats.c.income, 0)
    net = func.coalesce(stats.c.net, 0)
    period = since is not None
    # 固定的次要排序條件，資料不變時排名穩定
    if kind == "money":
        score = net if period else Farm.gold
        order = (score.desc(), Farm.gold.desc(), sold_income.desc(), Player.id.asc())
    elif kind == "goods":
        # 商品單價差異大（雞蛋 vs 麵包），總販售依收入排名，再比數量
        score = sold_income
        order = (sold_income.desc(), sold_qty.desc(), Player.id.asc())
    else:
        score = sold_qty
        order = (sold_qty.desc(), sold_income.desc(), Player.id.asc())

    stmt = (
        select(
            Player.id, Player.display_name, Farm.name, Farm.level, Farm.gold, sold_qty, sold_income, net, score,
            Farm.home_group_id,
        )
        .join(Farm, Farm.player_id == Player.id)
        .outerjoin(stats, stats.c.farm_id == Farm.id)
        .order_by(*order)
        .limit(limit)
    )
    if group_id is not None:
        # 群組排行：必須是目前成員，且農場的主要所屬群組就是此群組。
        # 每座農場只有一個 home_group_id，因此同一座農場不會出現在多個群組排行；未綁定的舊農場不列入任何群組。
        stmt = stmt.join(GroupMember, GroupMember.player_id == Player.id).where(
            GroupMember.group_id == group_id, Farm.home_group_id == group_id
        )
    return [
        {
            "rank": i,
            "player_id": pid,
            "player_name": name,
            "farm_name": farm_name,
            "level": level,
            "gold": gold,
            "sold_quantity": int(qty),
            "sales_income": int(income),
            "net_gold": int(net_gold),
            "score": int(score_value),
            "home_group_id": home_group,
        }
        for i, (pid, name, farm_name, level, gold, qty, income, net_gold, score_value, home_group) in enumerate(
            session.execute(stmt), start=1
        )
    ]


def group_ranking(
    session: Session, kind: str, limit: int, since: datetime | None = None, until: datetime | None = None
) -> list[dict]:
    """群組總排行：各群組成員（主要所屬群組為此群組且仍是成員）的金幣、販售加總。沒有成員的群組不列入。"""
    if kind not in KINDS:
        raise ValueError(f"unknown leaderboard kind: {kind}")
    stats = _stats(kind, since, until)
    members = func.count(Farm.id)
    gold = func.coalesce(func.sum(Farm.gold), 0)
    qty = func.coalesce(func.sum(stats.c.qty), 0)
    income = func.coalesce(func.sum(stats.c.income), 0)
    net = func.coalesce(func.sum(stats.c.net), 0)
    if kind == "money":
        score = net if since is not None else gold
        order = (score.desc(), gold.desc(), Group.id.asc())
    elif kind == "goods":
        score = income
        order = (income.desc(), qty.desc(), Group.id.asc())
    else:
        score = qty
        order = (qty.desc(), income.desc(), Group.id.asc())
    stmt = (
        select(Group.id, Group.name, members, gold, qty, income, net, score)
        .join(Farm, Farm.home_group_id == Group.id)
        .join(GroupMember, (GroupMember.player_id == Farm.player_id) & (GroupMember.group_id == Group.id))
        .outerjoin(stats, stats.c.farm_id == Farm.id)
        .group_by(Group.id, Group.name)
        .order_by(*order)
        .limit(limit)
    )
    return [
        {
            "rank": i,
            "group_id": gid,
            "group_name": name,
            "member_count": int(count),
            "gold": int(gold_sum),
            "sold_quantity": int(qty_sum),
            "sales_income": int(income_sum),
            "net_gold": int(net_sum),
            "score": int(score_value),
        }
        for i, (gid, name, count, gold_sum, qty_sum, income_sum, net_sum, score_value) in enumerate(
            session.execute(stmt), start=1
        )
    ]
