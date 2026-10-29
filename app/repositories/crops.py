from sqlalchemy import func, or_, select, union
from sqlalchemy.orm import Session

from app.db.models import CropType


def list_all(session: Session, *, active_only: bool = False) -> list[CropType]:
    stmt = select(CropType).order_by(CropType.seed_price, CropType.id)
    if active_only:
        stmt = stmt.where(CropType.active.is_(True))
    return list(session.scalars(stmt))


def find(session: Session, key: str) -> CropType | None:
    """依代碼（不分大小寫）或中文名稱尋找作物（不含珍稀變種）。"""
    key = key.strip()
    stmt = select(CropType).where(or_(func.lower(CropType.code) == key.lower(), CropType.name == key))
    return session.scalars(stmt).first()


def find_variant(session: Session, key: str) -> CropType | None:
    """依珍稀變種代碼（不分大小寫）或名稱找到原作物。"""
    key = key.strip()
    stmt = select(CropType).where(
        or_(func.lower(CropType.variant_code) == key.lower(), CropType.variant_name == key)
    )
    return session.scalars(stmt).first()


def by_code(session: Session, code: str, *, for_update: bool = False) -> CropType | None:
    stmt = select(CropType).where(CropType.code == code)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def produce_map(session: Session) -> dict[str, tuple[CropType, bool]]:
    """農產品代碼 → (作物, 是否為珍稀變種)。"""
    result = {}
    for crop in list_all(session):
        result[crop.code] = (crop, False)
        result[crop.variant_code] = (crop, True)
    return result


def produce_codes_select():
    """所有屬於「作物」的農產品代碼（含珍稀變種）的子查詢，供販售排行等判斷。"""
    return union(select(CropType.code), select(CropType.variant_code))


def set_grow_seconds(session: Session, code: str, seconds: int) -> CropType | None:
    crop = by_code(session, code, for_update=True)
    if crop is not None:
        crop.grow_seconds = seconds
    return crop


def get(session: Session, crop_type_id: int) -> CropType | None:
    return session.get(CropType, crop_type_id)
