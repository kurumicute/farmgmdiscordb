from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.db import txcache
from app.db.models import GameTransaction


@dataclass(frozen=True)
class TxRecord:
    """剛寫入的交易紀錄（排行統計、任務計分與圖鑑登記需要的欄位）。"""

    id: int | None  # defer=True 時尚未寫入，沒有 ID（任務／競賽計分需要 ID 時不會延後）
    farm_id: int
    event_type: str
    item_code: str | None
    quantity: int
    gold_delta: int
    context_group_id: int | None
    quality: str | None
    mutation: str | None
    created_at: datetime


def _flush_deferred(session: Session, rows: list[dict]) -> None:
    """一次送出（executemany），依累積順序寫入，ID 順序與逐筆寫入相同。"""
    session.connection().execute(insert(GameTransaction.__table__), rows)


def add(
    session: Session,
    *,
    defer: bool = False,
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
    """寫入一筆交易紀錄（INSERT … RETURNING id；不觸發 ORM flush，見 txcache.core_execute）。

    defer=True：一鍵操作中累積，離開 bulk 或下一次查詢前整批寫入（txcache.deferred），回傳的 id 是 None。"""
    table = GameTransaction.__table__
    values = dict(
        farm_id=farm_id, context_group_id=context_group_id, quality=quality, mutation=mutation,
        unit_price=unit_price, applied_events=applied_events, event_type=event_type, item_code=item_code,
        quantity=quantity, gold_delta=gold_delta, description=description, created_at=created_at,
    )
    pending = txcache.deferred(session, _flush_deferred, list) if defer else None
    if pending is not None:
        pending.append(values)
        tx_id = None
    else:
        txcache.flush_writes(session)  # 維持寫入順序：之前累積的先寫
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
