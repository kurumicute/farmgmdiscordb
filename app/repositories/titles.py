"""稱號：玩家擁有的稱號（farm_titles）與目前選用的稱號（farms.title）。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Farm, FarmTitle


def grant(session: Session, farm_id: int, title: str, source: str, now: datetime) -> None:
    """取得稱號；已擁有時不重複建立（保留第一次取得的時間與來源）。"""
    title = (title or "").strip()
    if not title:
        return
    stmt = insert(FarmTitle).values(farm_id=farm_id, title=title, source=source, obtained_at=now)
    session.execute(stmt.on_conflict_do_nothing(index_elements=[FarmTitle.farm_id, FarmTitle.title]))


def owned(session: Session, farm_id: int) -> list[FarmTitle]:
    stmt = select(FarmTitle).where(FarmTitle.farm_id == farm_id).order_by(FarmTitle.obtained_at, FarmTitle.title)
    return list(session.scalars(stmt))


def selected_by_player(session: Session, player_ids: list[int]) -> dict[int, str]:
    """玩家 → 目前選用的稱號（沒有選用的不列出）；排行榜顯示用。"""
    if not player_ids:
        return {}
    stmt = select(Farm.player_id, Farm.title).where(Farm.player_id.in_(player_ids), Farm.title.is_not(None))
    return dict(session.execute(stmt).all())
