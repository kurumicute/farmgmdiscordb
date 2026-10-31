from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.db.models import CropType, Farm, FarmPlot, Group, GroupMember, Player, StealRecord


def add(session: Session, **values) -> StealRecord:
    record = StealRecord(**values)
    session.add(record)
    session.flush()
    return record


def already_stolen(session: Session, plot_id: int, planting_no: int, thief_player_id: int) -> bool:
    stmt = select(StealRecord.id).where(
        StealRecord.plot_id == plot_id,
        StealRecord.planting_no == planting_no,
        StealRecord.thief_player_id == thief_player_id,
    )
    return session.scalar(stmt) is not None


def plots_stolen_since(session: Session, thief_player_id: int, victim_player_id: int, since: datetime) -> int:
    """偷菜者自 since 起從同一位玩家成功偷過的土地數（被阻止的不算）。"""
    stmt = select(func.count(func.distinct(StealRecord.plot_number))).where(
        StealRecord.thief_player_id == thief_player_id,
        StealRecord.victim_player_id == victim_player_id,
        StealRecord.outcome == "success",
        StealRecord.created_at >= since,
    )
    return int(session.scalar(stmt) or 0)


def victims_at_limit(session: Session, thief_player_id: int, since: datetime, limit: int):
    """自 since 起偷菜者已達每日土地上限的被偷者 ID（子查詢）。"""
    return (
        select(StealRecord.victim_player_id)
        .where(
            StealRecord.thief_player_id == thief_player_id,
            StealRecord.outcome == "success",
            StealRecord.created_at >= since,
        )
        .group_by(StealRecord.victim_player_id)
        .having(func.count(func.distinct(StealRecord.plot_number)) >= limit)
    )


def stolen_quantity(session: Session, plot_id: int, planting_no: int) -> int:
    """同一株作物（土地＋種植次數）被成功偷走的總數。"""
    stmt = select(func.coalesce(func.sum(StealRecord.quantity), 0)).where(
        StealRecord.plot_id == plot_id, StealRecord.planting_no == planting_no, StealRecord.outcome == "success"
    )
    return int(session.scalar(stmt) or 0)


def candidate_plots(
    session: Session,
    group_id: int,
    thief_player_id: int,
    now: datetime,
    wither_cutoff: datetime,
    day_start: datetime,
    plots_per_victim: int,
) -> list[tuple]:
    """目前群組中其他成員、已成熟未乾枯、仍有剩餘、偷菜者尚未偷過且今天還沒偷滿該玩家的土地。"""
    stmt = (
        select(
            Player.id, Player.display_name, Farm.name, FarmPlot.plot_number, CropType.name, FarmPlot.remaining_quantity
        )
        .select_from(GroupMember)
        .join(Player, Player.id == GroupMember.player_id)
        .join(Farm, Farm.player_id == Player.id)
        .join(FarmPlot, FarmPlot.farm_id == Farm.id)
        .join(CropType, CropType.id == FarmPlot.crop_type_id)
        .where(
            GroupMember.group_id == group_id,
            GroupMember.player_id != thief_player_id,
            GroupMember.player_id.not_in(victims_at_limit(session, thief_player_id, day_start, plots_per_victim)),
            FarmPlot.watered_at.is_not(None),
            FarmPlot.ready_at <= now,
            FarmPlot.ready_at > wither_cutoff,
            FarmPlot.remaining_quantity > 0,
            ~exists().where(
                StealRecord.plot_id == FarmPlot.id,
                StealRecord.planting_no == FarmPlot.planting_no,
                StealRecord.thief_player_id == thief_player_id,
            ),
        )
        .order_by(Player.id, FarmPlot.plot_number)
    )
    return list(session.execute(stmt))


def search(
    session: Session,
    *,
    limit: int,
    group_id: int | None = None,
    player_id: int | None = None,
    victim_player_id: int | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """偷菜嘗試紀錄（成功與被阻止）。player_id 比對偷菜者或被偷者；victim_player_id 只看自己的農場。"""
    thief = aliased(Player)
    victim = aliased(Player)
    stmt = (
        select(StealRecord, Group.name, thief.display_name, victim.display_name, CropType.name, CropType.code)
        .join(Group, Group.id == StealRecord.group_id)
        .join(thief, thief.id == StealRecord.thief_player_id)
        .join(victim, victim.id == StealRecord.victim_player_id)
        .join(CropType, CropType.id == StealRecord.crop_type_id)
        .order_by(StealRecord.created_at.desc(), StealRecord.id.desc())
        .limit(limit)
    )
    conditions = []
    if group_id is not None:
        conditions.append(StealRecord.group_id == group_id)
    if player_id is not None:
        conditions.append(or_(StealRecord.thief_player_id == player_id, StealRecord.victim_player_id == player_id))
    if victim_player_id is not None:
        conditions.append(StealRecord.victim_player_id == victim_player_id)
    if outcome is not None:
        conditions.append(StealRecord.outcome == outcome)
    if since is not None:
        conditions.append(StealRecord.created_at >= since)
    if until is not None:
        conditions.append(StealRecord.created_at < until)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "group_id": r.group_id,
            "group_name": group_name,
            "thief_player_id": r.thief_player_id,
            "thief_name": thief_name,
            "victim_player_id": r.victim_player_id,
            "victim_name": victim_name,
            "crop_name": crop_name,
            "crop_code": crop_code,
            "quantity": r.quantity,
            "plot_number": r.plot_number,
            "outcome": r.outcome,
            "blocked_by": r.blocked_by,
            "block_chance": r.block_chance,
        }
        for r, group_name, thief_name, victim_name, crop_name, crop_code in session.execute(stmt)
    ]
