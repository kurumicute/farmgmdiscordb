"""農場累積統計（farm_stats）與每日統計（farm_daily_stats）。

- on_transaction：由 GameService._record 在寫入交易紀錄的同一筆資料庫交易中呼叫，以原子遞增更新。
- rebuild／check：由 game_transactions 重算，或比對目前統計與重算結果（scripts/farm_stats.py）。

日期以台灣時間（UTC+8）切分，與排行榜的本日／本週／本月期間一致。
"""

from datetime import date, datetime

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clock import TAIPEI_TZ
from app.db.models import CropType, Farm, FarmDailyStats, FarmStats, GameTransaction
from app.game.ranking import NET_GOLD_EXCLUDED

STAT_COLUMNS = ("crop_sold_quantity", "crop_sales_income", "goods_sold_quantity", "goods_sales_income")


def taipei_day(at: datetime) -> date:
    return at.astimezone(TAIPEI_TZ).date()


def is_crop_produce(session: Session, item_code: str | None) -> bool:
    if item_code is None:
        return False
    stmt = select(CropType.id).where((CropType.code == item_code) | (CropType.variant_code == item_code)).limit(1)
    return session.scalar(stmt) is not None


def _upsert(session: Session, model, keys: dict, values: dict) -> None:
    values = {k: v for k, v in values.items() if v}
    if not values:
        return
    table = model.__table__
    stmt = insert(model).values(**keys, **values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c[k] for k in keys if k != "player_id"],
        set_={k: table.c[k] + stmt.excluded[k] for k in values},
    )
    session.execute(stmt)


def on_transaction(session: Session, farm: Farm, tx: GameTransaction) -> None:
    """依一筆交易紀錄遞增統計；與交易紀錄在同一筆資料庫交易中提交或回滾。"""
    is_sell = tx.event_type == "sell"
    non_operating = tx.event_type in NET_GOLD_EXCLUDED
    if not is_sell and not tx.gold_delta:
        return
    sales = {}
    if is_sell:
        sales = {"goods_sold_quantity": tx.quantity, "goods_sales_income": tx.gold_delta}
        if is_crop_produce(session, tx.item_code):
            sales |= {"crop_sold_quantity": tx.quantity, "crop_sales_income": tx.gold_delta}
    totals = dict(sales)
    if non_operating:
        totals["non_operating_gold"] = tx.gold_delta
    _upsert(session, FarmStats, {"farm_id": farm.id, "player_id": farm.player_id}, totals)
    daily = dict(sales)
    if not non_operating:
        daily["net_gold"] = tx.gold_delta
    _upsert(session, FarmDailyStats, {"farm_id": farm.id, "day": taipei_day(tx.created_at)}, daily)


def ensure_row(session: Session, farm: Farm) -> None:
    """建立農場時先建立累積統計列，讓排行查詢可直接以內部連結取得。"""
    stmt = insert(FarmStats).values(farm_id=farm.id, player_id=farm.player_id).on_conflict_do_nothing()
    session.execute(stmt)


# ---------- 由交易紀錄重算 ----------

_CROP_CODES = "(SELECT code FROM crop_types UNION SELECT variant_code FROM crop_types)"
_EXCLUDED = "(" + ", ".join(f"'{e}'" for e in NET_GOLD_EXCLUDED) + ")"
_CROP_SELL = f"t.event_type = 'sell' AND t.item_code IN {_CROP_CODES}"

EXPECTED_TOTALS_SQL = f"""
SELECT f.id AS farm_id, f.player_id,
       coalesce(sum(t.quantity) FILTER (WHERE {_CROP_SELL}), 0) AS crop_sold_quantity,
       coalesce(sum(t.gold_delta) FILTER (WHERE {_CROP_SELL}), 0) AS crop_sales_income,
       coalesce(sum(t.quantity) FILTER (WHERE t.event_type = 'sell'), 0) AS goods_sold_quantity,
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type = 'sell'), 0) AS goods_sales_income,
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type IN {_EXCLUDED}), 0) AS non_operating_gold
FROM farms f
LEFT JOIN game_transactions t ON t.farm_id = f.id
GROUP BY f.id, f.player_id
"""

EXPECTED_DAILY_SQL = f"""
SELECT t.farm_id, ((t.created_at AT TIME ZONE 'UTC') + interval '8 hours')::date AS day,
       coalesce(sum(t.quantity) FILTER (WHERE {_CROP_SELL}), 0) AS crop_sold_quantity,
       coalesce(sum(t.gold_delta) FILTER (WHERE {_CROP_SELL}), 0) AS crop_sales_income,
       coalesce(sum(t.quantity) FILTER (WHERE t.event_type = 'sell'), 0) AS goods_sold_quantity,
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type = 'sell'), 0) AS goods_sales_income,
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type NOT IN {_EXCLUDED}), 0) AS net_gold
FROM game_transactions t
WHERE t.event_type = 'sell' OR (t.gold_delta <> 0 AND t.event_type NOT IN {_EXCLUDED})
GROUP BY 1, 2
"""

TOTAL_COLUMNS = STAT_COLUMNS + ("non_operating_gold",)
DAILY_COLUMNS = STAT_COLUMNS + ("net_gold",)


def rebuild(session: Session) -> dict:
    """以 game_transactions 重算全部統計（呼叫端負責交易；會鎖定兩張統計表避免同時更新）。"""
    session.execute(text("LOCK TABLE farm_stats, farm_daily_stats IN EXCLUSIVE MODE"))
    session.execute(delete(FarmDailyStats))
    session.execute(delete(FarmStats))
    cols = ", ".join(("farm_id", "player_id") + TOTAL_COLUMNS)
    totals = session.execute(text(f"INSERT INTO farm_stats ({cols}) {EXPECTED_TOTALS_SQL}")).rowcount
    cols = ", ".join(("farm_id", "day") + DAILY_COLUMNS)
    daily = session.execute(text(f"INSERT INTO farm_daily_stats ({cols}) {EXPECTED_DAILY_SQL}")).rowcount
    return {"farm_stats": totals, "farm_daily_stats": daily}


def check(session: Session, limit: int = 20) -> dict:
    """比對統計表與交易紀錄重算結果；回傳不一致的筆數與前幾筆差異。"""
    diff_totals = " OR ".join(f"e.{c} IS DISTINCT FROM s.{c}" for c in TOTAL_COLUMNS)
    totals = session.execute(
        text(
            f"SELECT e.farm_id, {', '.join(f'e.{c}, s.{c}' for c in TOTAL_COLUMNS)} "
            f"FROM ({EXPECTED_TOTALS_SQL}) e LEFT JOIN farm_stats s ON s.farm_id = e.farm_id "
            f"WHERE s.farm_id IS NULL OR {diff_totals}"
        )
    ).all()
    diff_daily = " OR ".join(f"coalesce(e.{c}, 0) <> coalesce(s.{c}, 0)" for c in DAILY_COLUMNS)
    daily = session.execute(
        text(
            f"SELECT coalesce(e.farm_id, s.farm_id), coalesce(e.day, s.day) "
            f"FROM ({EXPECTED_DAILY_SQL}) e FULL JOIN farm_daily_stats s ON s.farm_id = e.farm_id AND s.day = e.day "
            f"WHERE {diff_daily}"
        )
    ).all()
    return {
        "farm_stats_mismatches": len(totals),
        "farm_daily_stats_mismatches": len(daily),
        "examples": [tuple(r) for r in totals[:limit]] + [tuple(r) for r in daily[:limit]],
    }


def period_rows(since: datetime, until: datetime):
    """[since, until) 的台灣日期範圍內，各農場的統計加總（子查詢）。"""
    first, last = taipei_day(since), taipei_day(until)
    d = FarmDailyStats
    return (
        select(
            d.farm_id,
            func.sum(d.crop_sold_quantity).label("crop_qty"),
            func.sum(d.crop_sales_income).label("crop_income"),
            func.sum(d.goods_sold_quantity).label("goods_qty"),
            func.sum(d.goods_sales_income).label("goods_income"),
            func.sum(d.net_gold).label("net"),
        )
        .where(d.day >= first, d.day < last)
        .group_by(d.farm_id)
        .subquery()
    )


def is_day_aligned(at: datetime | None) -> bool:
    if at is None:
        return True
    local = at.astimezone(TAIPEI_TZ)
    return (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0)

