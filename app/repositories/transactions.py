from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.db import txcache
from app.db.models import GameTransaction


@dataclass(frozen=True)
class TxRecord:
    """剛寫入的交易紀錄（排行統計、任務計分與圖鑑登記需要的欄位）。"""

    id: int
    farm_id: int
    event_type: str
    item_code: str | None
    quantity: int
    gold_delta: int
    context_group_id: int | None
    quality: str | None
    mutation: str | None
    created_at: datetime


def add(
    session: Session,
    *,
    farm_id: int,
    event_type: str,
    description: str,
    created_at: datetime,
    item_code: str | None = None,
    quantity: int = 0,
    gold_delta: int = 0,
    context_group_id: int | None = None,
    quality: str | None = None,
    mutation: str | None = None,
    unit_price: int | None = None,
    applied_events: list[dict] | None = None,
) -> TxRecord:
    """寫入一筆交易紀錄（INSERT … RETURNING id；不觸發 ORM flush，見 txcache.core_execute）。"""
    table = GameTransaction.__table__
    values = dict(
        farm_id=farm_id, context_group_id=context_group_id, quality=quality, mutation=mutation,
        unit_price=unit_price, applied_events=applied_events, event_type=event_type, item_code=item_code,
        quantity=quantity, gold_delta=gold_delta, description=description, created_at=created_at,
    )
    tx_id = txcache.core_execute(session, insert(table).values(**values).returning(table.c.id)).scalar_one()
    return TxRecord(id=tx_id, farm_id=farm_id, event_type=event_type, item_code=item_code, quantity=quantity,
                    gold_delta=gold_delta, context_group_id=context_group_id, quality=quality, mutation=mutation,
                    created_at=created_at)


def recent_for_farm(session: Session, farm_id: int, limit: int = 20) -> list[GameTransaction]:
    stmt = (
        select(GameTransaction)
        .where(GameTransaction.farm_id == farm_id)
        .order_by(GameTransaction.created_at.desc(), GameTransaction.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))
