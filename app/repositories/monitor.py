"""監控儀表板使用的唯讀彙總查詢。"""

from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import CropType, Farm, FarmPlot, GameTransaction, Group, InventoryItem, Player, StealRecord
from app.repositories import inventory as inventory_repo


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _plot_state_counts(now: datetime, wither_cutoff: datetime):
    """依時間即時結算土地狀態的計數欄位（與 rules.plot_state 相同的規則）。"""
    has_crop = FarmPlot.crop_type_id.is_not(None)
    watered = FarmPlot.watered_at.is_not(None)
    return (
        func.count().filter(~has_crop).label("empty"),
        func.count().filter(has_crop, ~watered).label("waiting_water"),
        func.count().filter(watered, FarmPlot.ready_at > now).label("growing"),
        func.count().filter(watered, FarmPlot.ready_at <= now, FarmPlot.ready_at > wither_cutoff).label("ready"),
        func.count().filter(watered, FarmPlot.ready_at <= wither_cutoff).label("withered"),
    )


def summary(session: Session, now: datetime, today_start: datetime, wither_cutoff: datetime) -> dict:
    plots = session.execute(
        select(func.count(), *_plot_state_counts(now, wither_cutoff)).select_from(FarmPlot)
    ).one()
    return {
        "players": session.scalar(select(func.count()).select_from(Player)) or 0,
        "farms": session.scalar(select(func.count()).select_from(Farm)) or 0,
        "groups": session.scalar(select(func.count()).select_from(Group)) or 0,
        "total_gold": total_gold(session),
        "plots": plots[0],
        "waiting_water": plots[2],
        "growing": plots[3],
        "ready": plots[4],
        "withered": plots[5],
        "steals_today": session.scalar(
            select(func.count()).where(StealRecord.created_at >= today_start, StealRecord.outcome == "success")
        )
        or 0,
        "blocks_today": session.scalar(
            select(func.count()).where(StealRecord.created_at >= today_start, StealRecord.outcome == "blocked")
        )
        or 0,
        "inventory_items": int(
            session.scalar(select(func.coalesce(func.sum(InventoryItem.quantity), 0))) or 0
        ),
        "transactions_today": session.scalar(
            select(func.count()).where(GameTransaction.created_at >= today_start)
        )
        or 0,
    }


def farms(
    session: Session, now: datetime, wither_cutoff: datetime, q: str | None, limit: int, offset: int
) -> tuple[list[dict], int]:
    plot_stats = (
        select(FarmPlot.farm_id, func.count().label("plots"), *_plot_state_counts(now, wither_cutoff))
        .group_by(FarmPlot.farm_id)
        .subquery()
    )
    inv = inventory_repo.warehouse_used_by_farm()  # 與遊戲內相同：含賭場獎品、不含素材背包
    base = select(Farm, Player.display_name).join(Player, Player.id == Farm.player_id)
    if q:
        pattern = f"%{_escape_like(q)}%"
        base = base.where(
            or_(Player.display_name.ilike(pattern, escape="\\"), Farm.name.ilike(pattern, escape="\\"))
        )
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0

    stmt = (
        base.add_columns(
            func.coalesce(plot_stats.c.plots, 0),
            func.coalesce(plot_stats.c.empty, 0),
            func.coalesce(plot_stats.c.growing, 0),
            func.coalesce(plot_stats.c.ready, 0),
            func.coalesce(inv.c.used, 0),
            func.coalesce(plot_stats.c.waiting_water, 0),
            func.coalesce(plot_stats.c.withered, 0),
            Group.name,
        )
        .outerjoin(plot_stats, plot_stats.c.farm_id == Farm.id)
        .outerjoin(inv, inv.c.farm_id == Farm.id)
        .outerjoin(Group, Group.id == Farm.home_group_id)
        .order_by(Farm.updated_at.desc(), Farm.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = []
    for farm, player_name, plots, empty, growing, ready, used, waiting, withered, home_name in session.execute(stmt):
        rows.append(
            {
                "farm_id": farm.id,
                "player_id": farm.player_id,
                "player_name": player_name,
                "home_group_id": farm.home_group_id,
                "home_group_name": home_name,
                "farm_name": farm.name,
                "level": farm.level,
                "gold": farm.gold,
                "plots": plots,
                "empty": empty,
                "growing": growing,
                "ready": ready,
                "waiting_water": waiting,
                "withered": withered,
                "warehouse_used": int(used),
                "warehouse_capacity": farm.warehouse_capacity,
                "updated_at": farm.updated_at,
            }
        )
    return rows, total


def farm_detail(session: Session, farm_id: int) -> tuple[Farm, str] | None:
    row = session.execute(
        select(Farm, Player.display_name).join(Player, Player.id == Farm.player_id).where(Farm.id == farm_id)
    ).first()
    return (row[0], row[1]) if row else None


def plots(session: Session, farm_id: int) -> list[tuple[FarmPlot, CropType | None]]:
    stmt = (
        select(FarmPlot, CropType)
        .outerjoin(CropType, CropType.id == FarmPlot.crop_type_id)
        .where(FarmPlot.farm_id == farm_id)
        .order_by(FarmPlot.plot_number)
    )
    return [(p, c) for p, c in session.execute(stmt)]


def inventory(session: Session, farm_id: int) -> list[InventoryItem]:
    stmt = (
        select(InventoryItem)
        .where(InventoryItem.farm_id == farm_id, InventoryItem.quantity > 0)
        .order_by(InventoryItem.item_code)
    )
    return list(session.scalars(stmt))


def transactions(session: Session, limit: int, farm_id: int | None = None) -> list[dict]:
    stmt = (
        select(GameTransaction, Farm.name, Player.display_name)
        .join(Farm, Farm.id == GameTransaction.farm_id)
        .join(Player, Player.id == Farm.player_id)
        .order_by(GameTransaction.created_at.desc(), GameTransaction.id.desc())
        .limit(limit)
    )
    if farm_id is not None:
        stmt = stmt.where(GameTransaction.farm_id == farm_id)
    return [
        {
            "id": t.id,
            "created_at": t.created_at,
            "farm_id": t.farm_id,
            "farm_name": farm_name,
            "player_name": player_name,
            "event_type": t.event_type,
            "item_code": t.item_code,
            "quantity": t.quantity,
            "gold_delta": t.gold_delta,
            "description": t.description,
        }
        for t, farm_name, player_name in session.execute(stmt)
    ]


def crop_distribution(session: Session, now: datetime, wither_cutoff: datetime) -> list[dict]:
    stmt = (
        select(
            CropType.code,
            CropType.name,
            func.count(FarmPlot.id).filter(FarmPlot.ready_at > now),
            func.count(FarmPlot.id).filter(FarmPlot.ready_at <= now, FarmPlot.ready_at > wither_cutoff),
        )
        .outerjoin(FarmPlot, FarmPlot.crop_type_id == CropType.id)
        .group_by(CropType.id, CropType.code, CropType.name, CropType.seed_price)
        .order_by(CropType.seed_price, CropType.id)
    )
    return [
        {"code": code, "name": name, "growing": growing, "ready": ready}
        for code, name, growing, ready in session.execute(stmt)
    ]


def hourly_activity(session: Session, since: datetime) -> list[tuple[datetime, int, int]]:
    """回傳 (小時, 操作數, 金幣變化總和)，只包含有紀錄的小時。"""
    hour = func.date_trunc("hour", GameTransaction.created_at)
    stmt = (
        select(hour, func.count(), func.coalesce(func.sum(GameTransaction.gold_delta), 0))
        .where(GameTransaction.created_at >= since)
        .group_by(hour)
        .order_by(hour)
    )
    return [(h, c, int(g)) for h, c, g in session.execute(stmt)]


def total_gold(session: Session) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(Farm.gold), 0))) or 0)


def has_transactions(session: Session) -> bool:
    return session.scalar(select(case((select(GameTransaction.id).limit(1).exists(), True), else_=False)))


def players(session: Session, limit: int = 500) -> list[dict]:
    stmt = (
        select(Player.id, Player.display_name, Farm.id)
        .outerjoin(Farm, Farm.player_id == Player.id)
        .order_by(Player.id)
        .limit(limit)
    )
    return [{"player_id": pid, "player_name": name, "farm_id": fid} for pid, name, fid in session.execute(stmt)]
