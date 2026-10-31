"""山洞探險：各區域的探險次數、進行中的探險與探險紀錄。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ExplorationLog, FarmExploration


def list_cooldowns(session: Session, farm_id: int) -> dict[str, FarmExploration]:
    stmt = select(FarmExploration).where(FarmExploration.farm_id == farm_id)
    return {row.area_code: row for row in session.scalars(stmt)}


def get_cooldown(session: Session, farm_id: int, area_code: str, *, for_update: bool = False) -> FarmExploration | None:
    stmt = select(FarmExploration).where(FarmExploration.farm_id == farm_id, FarmExploration.area_code == area_code)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def set_cooldown(
    session: Session, row: FarmExploration | None, farm_id: int, area_code: str, now: datetime, until: datetime
) -> FarmExploration:
    if row is None:
        row = FarmExploration(farm_id=farm_id, area_code=area_code, last_explored_at=now, next_available_at=until,
                              explore_count=1)
        session.add(row)
    else:
        row.last_explored_at = now
        row.next_available_at = until
        row.explore_count += 1
    session.flush()  # (farm_id, area_code) 唯一：並行時第二筆在這裡失敗並回滾
    return row


def add_log(session: Session, **values) -> ExplorationLog:
    log = ExplorationLog(**values)
    session.add(log)
    session.flush()
    return log


def get_active(session: Session, farm_id: int, *, for_update: bool = False) -> ExplorationLog | None:
    """進行中或已回來但還沒領取的探險（每座農場最多一筆）。"""
    stmt = select(ExplorationLog).where(ExplorationLog.farm_id == farm_id, ExplorationLog.claimed_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def list_logs(session: Session, farm_id: int, limit: int) -> list[ExplorationLog]:
    """已領取的探險紀錄（未領取的獎勵還沒揭曉，不列出）。"""
    stmt = (
        select(ExplorationLog)
        .where(ExplorationLog.farm_id == farm_id, ExplorationLog.claimed_at.is_not(None))
        .order_by(ExplorationLog.explored_at.desc(), ExplorationLog.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))
