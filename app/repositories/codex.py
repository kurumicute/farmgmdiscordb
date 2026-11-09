"""圖鑑登記：第一次取得物品的時間、累計數量與最高品質。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db import txcache
from app.db.models import QUALITY_CODES, FarmCodexEntry

# 會登記到圖鑑的交易類型（取得物品的那一方）；山洞素材另外在領取探險時登記
CODEX_EVENTS = frozenset({"harvest", "steal", "collect_animal", "craft_collect"})
QUALITY_RANK = {q: i for i, q in enumerate(QUALITY_CODES)}


def _flush_deferred(session: Session, pending: dict) -> None:
    for (farm_id, item_code), (quantity, rank, first_at) in pending.items():
        _upsert(session, farm_id, item_code, quantity, rank, first_at)


def register(session: Session, farm_id: int, item_code: str, quantity: int, quality: str | None,
             now: datetime) -> None:
    """一鍵操作中相同的物品合併成一次寫入（數量相加、品質取最高，txcache.deferred）。"""
    if quantity <= 0:
        return
    rank = QUALITY_RANK.get(quality or "normal", 0)
    pending = txcache.deferred(session, _flush_deferred, dict)
    if pending is None:
        _upsert(session, farm_id, item_code, quantity, rank, now)
        return
    old = pending.get((farm_id, item_code))
    pending[(farm_id, item_code)] = (quantity, rank, now) if old is None else (
        old[0] + quantity, max(old[1], rank), min(old[2], now))


def _upsert(session: Session, farm_id: int, item_code: str, quantity: int, rank: int, now: datetime) -> None:
    t = FarmCodexEntry.__table__
    stmt = insert(t).values(farm_id=farm_id, item_code=item_code, quantity=quantity, best_quality=rank, first_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[t.c.farm_id, t.c.item_code],
        set_={
            "quantity": t.c.quantity + stmt.excluded.quantity,
            "best_quality": func.greatest(t.c.best_quality, stmt.excluded.best_quality),
        },
    )
    txcache.core_execute(session, stmt)  # 不觸發 ORM flush


def entries(session: Session, farm_id: int) -> dict[str, FarmCodexEntry]:
    rows = session.scalars(select(FarmCodexEntry).where(FarmCodexEntry.farm_id == farm_id))
    return {r.item_code: r for r in rows}
