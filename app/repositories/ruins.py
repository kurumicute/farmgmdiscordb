"""古代遺跡：群組的修復進度（group_ruins）、成員的捐獻統計（ruins_contributions）與每週的守護者。"""

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Farm, GroupRuins, RuinsContribution, RuinsGuardian, RuinsGuardianHit


def get(session: Session, group_id: int, *, for_update: bool = False) -> GroupRuins | None:
    stmt = select(GroupRuins).where(GroupRuins.group_id == group_id)
    if for_update:
        # 同一筆交易先前不加鎖讀過時，要用鎖定後的最新值覆蓋記憶體中的物件
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalars(stmt).first()


def get_or_create(session: Session, group_id: int, now: datetime) -> GroupRuins:
    """取得並鎖定群組的遺跡列（第一次捐獻時建立；並行建立只會成功一筆）。"""
    stmt = insert(GroupRuins).values(group_id=group_id, stage=0, progress={}, updated_at=now)
    session.execute(stmt.on_conflict_do_nothing(index_elements=[GroupRuins.group_id]))
    return get(session, group_id, for_update=True)


def is_completed(session: Session, group_id: int | None) -> bool:
    if group_id is None:
        return False
    return session.scalar(select(GroupRuins.completed_at).where(GroupRuins.group_id == group_id)) is not None


def add_contribution(session: Session, group_id: int, farm_id: int, quantity: int, now: datetime) -> None:
    t = RuinsContribution.__table__
    stmt = insert(t).values(group_id=group_id, farm_id=farm_id, quantity=quantity, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[t.c.group_id, t.c.farm_id],
        set_={"quantity": t.c.quantity + stmt.excluded.quantity, "updated_at": stmt.excluded.updated_at},
    )
    session.execute(stmt)


def contribution(session: Session, group_id: int, farm_id: int) -> int:
    stmt = select(RuinsContribution.quantity).where(RuinsContribution.group_id == group_id,
                                                     RuinsContribution.farm_id == farm_id)
    return session.scalar(stmt) or 0


def top_contributors(session: Session, group_id: int, limit: int = 10) -> list[tuple[str, str | None, int]]:
    """(農場名稱, 稱號, 捐獻數量)，依數量排序。"""
    stmt = (
        select(Farm.name, Farm.title, RuinsContribution.quantity)
        .join(Farm, Farm.id == RuinsContribution.farm_id)
        .where(RuinsContribution.group_id == group_id, RuinsContribution.quantity > 0)
        .order_by(RuinsContribution.quantity.desc(), Farm.id)
        .limit(limit)
    )
    return [tuple(row) for row in session.execute(stmt).all()]


def contributor_farms(session: Session, group_id: int) -> list[int]:
    stmt = select(RuinsContribution.farm_id).where(RuinsContribution.group_id == group_id,
                                                    RuinsContribution.quantity > 0)
    return list(session.scalars(stmt))


# ---------- 守護者 ----------


def member_farms(session: Session, group_id: int) -> int:
    """所屬群組是 group_id 的農場數（守護者 HP 用）。"""
    return session.scalar(select(func.count()).select_from(Farm).where(Farm.home_group_id == group_id)) or 0


def get_guardian(session: Session, group_id: int, week: date, *, for_update: bool = False) -> RuinsGuardian | None:
    stmt = select(RuinsGuardian).where(RuinsGuardian.group_id == group_id, RuinsGuardian.week_start == week)
    if for_update:
        # 同一筆交易先前不加鎖讀過時，要用鎖定後的最新值覆蓋記憶體中的物件
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.scalars(stmt).first()


def ensure_guardian(session: Session, group_id: int, week: date, hp_max: int) -> RuinsGuardian:
    """取得並鎖定這週的守護者（第一次攻擊時建立，HP 以當下的成員數決定；並行建立只會成功一筆）。"""
    stmt = insert(RuinsGuardian).values(group_id=group_id, week_start=week, hp_max=hp_max, damage=0)
    session.execute(stmt.on_conflict_do_nothing(index_elements=[RuinsGuardian.group_id, RuinsGuardian.week_start]))
    return get_guardian(session, group_id, week, for_update=True)


def add_hit(session: Session, group_id: int, week: date, farm_id: int, damage: int) -> None:
    t = RuinsGuardianHit.__table__
    stmt = insert(t).values(group_id=group_id, week_start=week, farm_id=farm_id, damage=damage)
    session.execute(stmt.on_conflict_do_update(index_elements=[t.c.group_id, t.c.week_start, t.c.farm_id],
                                               set_={"damage": t.c.damage + stmt.excluded.damage}))


def my_hits(session: Session, group_id: int, week: date, farm_id: int) -> int:
    stmt = select(RuinsGuardianHit.damage).where(RuinsGuardianHit.group_id == group_id,
                                                 RuinsGuardianHit.week_start == week,
                                                 RuinsGuardianHit.farm_id == farm_id)
    return session.scalar(stmt) or 0


def top_hits(session: Session, group_id: int, week: date, limit: int = 5) -> list[tuple[str, int]]:
    stmt = (
        select(Farm.name, RuinsGuardianHit.damage)
        .join(Farm, Farm.id == RuinsGuardianHit.farm_id)
        .where(RuinsGuardianHit.group_id == group_id, RuinsGuardianHit.week_start == week)
        .order_by(RuinsGuardianHit.damage.desc(), Farm.id)
        .limit(limit)
    )
    return [tuple(r) for r in session.execute(stmt).all()]
