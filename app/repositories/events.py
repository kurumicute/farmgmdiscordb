"""限時全服事件的資料存取。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GameEvent
from app.game import events as event_rules


def list_all(session: Session) -> list[GameEvent]:
    return list(session.scalars(select(GameEvent).order_by(GameEvent.id.desc())))


def get(session: Session, event_id: int, *, for_update: bool = False) -> GameEvent | None:
    stmt = select(GameEvent).where(GameEvent.id == event_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def active(session: Session, now: datetime) -> list[GameEvent]:
    """目前生效中的事件（事件數量少，先取出啟用的事件再判斷時段）。"""
    stmt = select(GameEvent).where(GameEvent.enabled.is_(True)).order_by(GameEvent.id)
    return [e for e in session.scalars(stmt) if event_rules.is_active(e, now)]
