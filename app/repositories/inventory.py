from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import GamblePrize, InventoryItem
from app.game import exploration

# 山洞素材與假幣放在獨立的素材背包，不計入倉庫空間
MATERIAL_CODES = tuple(exploration.MATERIALS)


def is_material(item_code: str) -> bool:
    return item_code in exploration.MATERIALS


def in_warehouse():
    """SQL 條件：佔倉庫空間的庫存（排除素材背包的物品）。"""
    return InventoryItem.item_code.not_in(MATERIAL_CODES)


def list_items(session: Session, farm_id: int) -> list[InventoryItem]:
    stmt = (
        select(InventoryItem)
        .where(InventoryItem.farm_id == farm_id, InventoryItem.quantity > 0)
        .order_by(InventoryItem.item_code, InventoryItem.quality, InventoryItem.mutation)
    )
    return list(session.scalars(stmt))


def _stack(farm_id: int, item_code: str, quality: str, mutation: str):
    return (
        InventoryItem.farm_id == farm_id,
        InventoryItem.item_code == item_code,
        InventoryItem.quality == quality,
        InventoryItem.mutation == mutation,
    )


def get_quantity(
    session: Session, farm_id: int, item_code: str, quality: str = "normal", mutation: str = "none"
) -> int:
    """同一物品不同品質／突變是不同的庫存；未指定時為普通、無突變。"""
    stmt = select(InventoryItem.quantity).where(*_stack(farm_id, item_code, quality, mutation))
    return session.scalar(stmt) or 0


def total_quantity(session: Session, farm_id: int) -> int:
    """倉庫已使用空間：一般庫存加上賭場獎品（素材背包的物品不計入）。"""
    items = select(func.coalesce(func.sum(InventoryItem.quantity), 0)).where(
        InventoryItem.farm_id == farm_id, in_warehouse()
    )
    prizes = select(func.coalesce(func.sum(GamblePrize.quantity), 0)).where(GamblePrize.farm_id == farm_id)
    return int(session.scalar(items) or 0) + int(session.scalar(prizes) or 0)


def material_quantity(session: Session, farm_id: int) -> int:
    """素材背包已使用空間（山洞素材與假幣的總數）。"""
    stmt = select(func.coalesce(func.sum(InventoryItem.quantity), 0)).where(
        InventoryItem.farm_id == farm_id, InventoryItem.item_code.in_(MATERIAL_CODES)
    )
    return int(session.scalar(stmt) or 0)


def list_prizes(session: Session, farm_id: int, *, for_update: bool = False) -> list[GamblePrize]:
    stmt = (
        select(GamblePrize)
        .where(GamblePrize.farm_id == farm_id, GamblePrize.quantity > 0)
        .order_by(GamblePrize.multiplier.desc(), GamblePrize.item_code, GamblePrize.id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(session.scalars(stmt))


def add_prize(session: Session, farm_id: int, item_code: str, quality: str, mutation: str, multiplier: int,
              quantity: int) -> None:
    stmt = insert(GamblePrize).values(
        farm_id=farm_id, item_code=item_code, quality=quality, mutation=mutation, multiplier=multiplier,
        quantity=quantity,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_gamble_prizes_farm_stack",
        set_={"quantity": GamblePrize.quantity + quantity, "updated_at": func.now()},
    )
    session.execute(stmt)


def add(
    session: Session, farm_id: int, item_code: str, quantity: int, quality: str = "normal", mutation: str = "none"
) -> None:
    # 以唯一限制 (farm_id, item_code, quality, mutation) 做 upsert，避免重複建立庫存紀錄
    stmt = insert(InventoryItem).values(
        farm_id=farm_id, item_code=item_code, quality=quality, mutation=mutation, quantity=quantity
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_inventory_items_farm_item",
        set_={"quantity": InventoryItem.quantity + quantity, "updated_at": func.now()},
    )
    session.execute(stmt)


def remove(
    session: Session, farm_id: int, item_code: str, quantity: int, quality: str = "normal", mutation: str = "none"
) -> bool:
    """扣除指定品質／突變的庫存；數量不足時不做任何修改並回傳 False。"""
    stmt = (
        update(InventoryItem)
        .where(*_stack(farm_id, item_code, quality, mutation), InventoryItem.quantity >= quantity)
        .values(quantity=InventoryItem.quantity - quantity, updated_at=func.now())
    )
    return session.execute(stmt).rowcount == 1
