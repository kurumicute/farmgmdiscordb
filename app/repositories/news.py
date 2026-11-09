"""農場日報：產生日報用的當日彙總查詢（只讀取交易紀錄與魚獲，不寫入）。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Farm, FarmFish, FishType, GameTransaction

# 日報挑選的突變／品質（快速生長不算稀奇，不上頭條）
NEWS_MUTATIONS = ("giant", "golden", "variant")
NEWS_QUALITIES = ("legendary",)
NEWS_FISH_RARITIES = ("epic", "legendary")


# ---------- 當日彙總 ----------


def _in_group_day(group_id: int, start: datetime, end: datetime):
    return (Farm.home_group_id == group_id, GameTransaction.created_at >= start, GameTransaction.created_at < end)


def farm_events(session: Session, group_id: int, start: datetime, end: datetime) -> list[tuple]:
    """(farm_id, event_type, 筆數, 數量合計, 金幣合計)：所屬群組為 group_id 的農場在期間內的交易紀錄。"""
    stmt = (
        select(GameTransaction.farm_id, GameTransaction.event_type, func.count(),
               func.coalesce(func.sum(GameTransaction.quantity), 0),
               func.coalesce(func.sum(GameTransaction.gold_delta), 0))
        .join(Farm, Farm.id == GameTransaction.farm_id)
        .where(*_in_group_day(group_id, start, end))
        .group_by(GameTransaction.farm_id, GameTransaction.event_type)
    )
    return [tuple(r) for r in session.execute(stmt).all()]


def special_harvests(session: Session, group_id: int, start: datetime, end: datetime) -> list[tuple]:
    """(farm_id, item_code, quality, mutation, 數量)：期間內收成的巨型／黃金／珍稀變種或傳說品質作物。"""
    stmt = (
        select(GameTransaction.farm_id, GameTransaction.item_code, GameTransaction.quality,
               GameTransaction.mutation, func.sum(GameTransaction.quantity))
        .join(Farm, Farm.id == GameTransaction.farm_id)
        .where(*_in_group_day(group_id, start, end), GameTransaction.event_type == "harvest",
               GameTransaction.mutation.in_(NEWS_MUTATIONS) | GameTransaction.quality.in_(NEWS_QUALITIES))
        .group_by(GameTransaction.farm_id, GameTransaction.item_code, GameTransaction.quality,
                  GameTransaction.mutation)
        .order_by(func.sum(GameTransaction.quantity).desc())
    )
    return [tuple(r) for r in session.execute(stmt).all()]


def best_fish(session: Session, group_id: int, start: datetime, end: datetime) -> tuple | None:
    """(farm_id, 魚名, 稀有度, 重量 g)：期間內釣到最有價值的史詩／傳說魚。"""
    stmt = (
        select(FarmFish.farm_id, FishType.name, FarmFish.rarity, FarmFish.weight_g)
        .join(Farm, Farm.id == FarmFish.farm_id)
        .join(FishType, FishType.code == FarmFish.fish_code)
        .where(Farm.home_group_id == group_id, FarmFish.caught_at >= start, FarmFish.caught_at < end,
               FarmFish.rarity.in_(NEWS_FISH_RARITIES))
        .order_by(FarmFish.price.desc(), FarmFish.weight_g.desc(), FarmFish.id)
        .limit(1)
    )
    row = session.execute(stmt).first()
    return tuple(row) if row else None


def farm_names(session: Session, farm_ids: set[int]) -> dict[int, str]:
    if not farm_ids:
        return {}
    return dict(session.execute(select(Farm.id, Farm.name).where(Farm.id.in_(farm_ids))).all())
