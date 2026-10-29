"""動物種類與產品（animal_types）的資料存取。"""

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import AnimalType


def list_all(session: Session, *, active_only: bool = False) -> list[AnimalType]:
    stmt = select(AnimalType).order_by(AnimalType.price, AnimalType.id)
    if active_only:
        stmt = stmt.where(AnimalType.active.is_(True))
    return list(session.scalars(stmt))


def by_code(session: Session) -> dict[str, AnimalType]:
    return {a.code: a for a in list_all(session)}


def get(session: Session, code: str, *, for_update: bool = False) -> AnimalType | None:
    stmt = select(AnimalType).where(AnimalType.code == code)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def find(session: Session, key: str) -> AnimalType | None:
    """依代碼（不分大小寫）或名稱尋找動物。"""
    key = key.strip()
    stmt = select(AnimalType).where(or_(func.lower(AnimalType.code) == key.lower(), AnimalType.name == key))
    return session.scalars(stmt).first()


def find_product(session: Session, key: str) -> AnimalType | None:
    """依產品代碼（不分大小寫）或產品名稱找到動物種類。"""
    key = key.strip()
    stmt = select(AnimalType).where(
        or_(func.lower(AnimalType.product_code) == key.lower(), AnimalType.product_name == key)
    )
    return session.scalars(stmt).first()
