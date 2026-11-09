"""農場傳聞的挖寶紀錄（rumor_digs）：每座農場每天挖一次，寶藏每個群組每天只會被挖到一次。"""

from datetime import date, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.models import RumorDig


def dug(session: Session, farm_id: int, day: date) -> bool:
    return session.get(RumorDig, (farm_id, day)) is not None


def found(session: Session, group_id: int, day: date) -> bool:
    return bool(session.scalar(select(exists().where(RumorDig.group_id == group_id, RumorDig.day == day,
                                                     RumorDig.found))))


def add(session: Session, *, farm_id: int, day: date, group_id: int, target_farm_id: int, found: bool, gold: int,
        now: datetime) -> None:
    session.add(RumorDig(farm_id=farm_id, day=day, group_id=group_id, target_farm_id=target_farm_id, found=found,
                         gold=gold, created_at=now))
    session.flush()  # 主鍵與 (group_id, day) WHERE found 唯一是重複挖寶的最後防線
