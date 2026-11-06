"""農場寵物（farm_pets）。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FarmPet


def levels(session: Session, farm_id: int) -> dict[str, int]:
    """寵物代碼 → 等級（沒有的寵物不列出）。"""
    rows = session.execute(select(FarmPet.pet_code, FarmPet.level).where(FarmPet.farm_id == farm_id))
    return dict(rows.all())


def level(session: Session, farm_id: int, pet_code: str) -> int | None:
    """這隻寵物的等級；沒有時回傳 None。"""
    return session.scalar(select(FarmPet.level).where(FarmPet.farm_id == farm_id, FarmPet.pet_code == pet_code))


def get(session: Session, farm_id: int, pet_code: str) -> FarmPet | None:
    return session.get(FarmPet, (farm_id, pet_code))
