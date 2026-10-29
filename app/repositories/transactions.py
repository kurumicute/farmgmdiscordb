from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GameTransaction


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
) -> GameTransaction:
    tx = GameTransaction(
        farm_id=farm_id,
        context_group_id=context_group_id,
        quality=quality,
        mutation=mutation,
        unit_price=unit_price,
        applied_events=applied_events,
        event_type=event_type,
        item_code=item_code,
        quantity=quantity,
        gold_delta=gold_delta,
        description=description,
        created_at=created_at,
    )
    session.add(tx)
    session.flush()
    return tx


def recent_for_farm(session: Session, farm_id: int, limit: int = 20) -> list[GameTransaction]:
    stmt = (
        select(GameTransaction)
        .where(GameTransaction.farm_id == farm_id)
        .order_by(GameTransaction.created_at.desc(), GameTransaction.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))
