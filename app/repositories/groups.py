from datetime import datetime

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Farm, FarmPlot, Group, GroupMember, InventoryItem, Player


def create(session: Session, name: str, discord_guild_id: str | None = None) -> Group:
    group = Group(name=name, discord_guild_id=discord_guild_id)
    session.add(group)
    session.flush()
    return group


def get(session: Session, group_id: int) -> Group | None:
    return session.get(Group, group_id)


def get_by_guild(session: Session, guild_id: str) -> Group | None:
    return session.scalars(select(Group).where(Group.discord_guild_id == guild_id)).first()


def list_with_counts(session: Session, player_id: int | None = None) -> list[tuple[Group, int, bool]]:
    """回傳 (群組, 成員數, 指定玩家是否為成員)。"""
    count = func.count(GroupMember.player_id)
    is_member = (
        func.bool_or(GroupMember.player_id == player_id) if player_id is not None else func.bool_or(False)
    )
    stmt = (
        select(Group, count, func.coalesce(is_member, False))
        .outerjoin(GroupMember, GroupMember.group_id == Group.id)
        .group_by(Group.id)
        .order_by(Group.id)
    )
    return [(g, c, bool(m)) for g, c, m in session.execute(stmt)]


def is_member(session: Session, group_id: int, player_id: int) -> bool:
    stmt = select(GroupMember.player_id).where(
        GroupMember.group_id == group_id, GroupMember.player_id == player_id
    )
    return session.scalar(stmt) is not None


def add_member(session: Session, group_id: int, player_id: int) -> bool:
    """加入成員；已是成員時不變並回傳 False。"""
    stmt = (
        insert(GroupMember)
        .values(group_id=group_id, player_id=player_id)
        .on_conflict_do_nothing(index_elements=["group_id", "player_id"])
        .returning(GroupMember.player_id)
    )
    # ORM 執行 INSERT ... ON CONFLICT 時 rowcount 不可靠，以 RETURNING 判斷是否真的新增
    return session.execute(stmt).first() is not None


def remove_member(session: Session, group_id: int, player_id: int) -> bool:
    """只移除成員關係，不影響玩家、農場或任何紀錄。"""
    stmt = delete(GroupMember).where(GroupMember.group_id == group_id, GroupMember.player_id == player_id)
    return session.execute(stmt).rowcount == 1


def members_with_farms(session: Session, group_id: int, now: datetime, wither_cutoff: datetime) -> list[dict]:
    has_crop = FarmPlot.crop_type_id.is_not(None)
    watered = FarmPlot.watered_at.is_not(None)
    plot_stats = (
        select(
            FarmPlot.farm_id,
            func.count().label("plots"),
            func.count().filter(~has_crop).label("empty"),
            func.count().filter(has_crop, ~watered).label("waiting"),
            func.count().filter(watered, FarmPlot.ready_at > now).label("growing"),
            func.count().filter(watered, FarmPlot.ready_at <= now, FarmPlot.ready_at > wither_cutoff).label("ready"),
            func.count().filter(watered, FarmPlot.ready_at <= wither_cutoff).label("withered"),
        )
        .group_by(FarmPlot.farm_id)
        .subquery()
    )
    inv = (
        select(InventoryItem.farm_id, func.sum(InventoryItem.quantity).label("used"))
        .group_by(InventoryItem.farm_id)
        .subquery()
    )
    stmt = (
        select(
            Player.id,
            Player.display_name,
            GroupMember.joined_at,
            Farm.id,
            Farm.name,
            Farm.level,
            Farm.gold,
            Farm.warehouse_capacity,
            func.coalesce(inv.c.used, 0),
            plot_stats.c.plots,
            plot_stats.c.empty,
            plot_stats.c.waiting,
            plot_stats.c.growing,
            plot_stats.c.ready,
            plot_stats.c.withered,
            Farm.home_group_id,
        )
        .join(Player, Player.id == GroupMember.player_id)
        .outerjoin(Farm, Farm.player_id == Player.id)
        .outerjoin(plot_stats, plot_stats.c.farm_id == Farm.id)
        .outerjoin(inv, inv.c.farm_id == Farm.id)
        .where(GroupMember.group_id == group_id)
        .order_by(case((Farm.id.is_(None), 1), else_=0), Player.id)
    )
    rows = []
    for r in session.execute(stmt):
        rows.append(
            {
                "player_id": r[0],
                "player_name": r[1],
                "joined_at": r[2],
                "farm": None
                if r[3] is None
                else {
                    "farm_id": r[3],
                    "farm_name": r[4],
                    "level": r[5],
                    "gold": r[6],
                    "warehouse_capacity": r[7],
                    "warehouse_used": int(r[8]),
                    "plots": r[9] or 0,
                    "empty": r[10] or 0,
                    "waiting_water": r[11] or 0,
                    "growing": r[12] or 0,
                    "ready": r[13] or 0,
                    "withered": r[14] or 0,
                    "home_group_id": r[15],
                    # 主要所屬群組就是此群組時才列入群組排行
                    "ranked_in_group": r[15] == group_id,
                },
            }
        )
    return rows
