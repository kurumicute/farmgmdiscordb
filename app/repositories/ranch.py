"""建築、動物與加工訂單的資料存取。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CraftOrder, Farm, FarmAnimal, FarmBuilding, Player

# ---------- 建築 ----------


def list_buildings(session: Session, farm_id: int) -> list[FarmBuilding]:
    stmt = (
        select(FarmBuilding)
        .where(FarmBuilding.farm_id == farm_id)
        .order_by(FarmBuilding.building_code, FarmBuilding.slot)
    )
    return list(session.scalars(stmt))


def count_buildings(session: Session, farm_id: int, building_code: str) -> int:
    stmt = select(func.count()).where(FarmBuilding.farm_id == farm_id, FarmBuilding.building_code == building_code)
    return session.scalar(stmt) or 0


def add_building(session: Session, farm_id: int, building_code: str, slot: int, now: datetime) -> FarmBuilding:
    building = FarmBuilding(farm_id=farm_id, building_code=building_code, slot=slot, built_at=now)
    session.add(building)
    session.flush()
    return building


# ---------- 動物 ----------


def list_animals(session: Session, farm_id: int, *, for_update: bool = False) -> list[FarmAnimal]:
    stmt = select(FarmAnimal).where(FarmAnimal.farm_id == farm_id).order_by(FarmAnimal.animal_no)
    if for_update:
        stmt = stmt.with_for_update()
    return list(session.scalars(stmt))


def get_animal(session: Session, farm_id: int, animal_no: int, *, for_update: bool = False) -> FarmAnimal | None:
    stmt = select(FarmAnimal).where(FarmAnimal.farm_id == farm_id, FarmAnimal.animal_no == animal_no)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def count_animals(session: Session, farm_id: int) -> int:
    return session.scalar(select(func.count()).where(FarmAnimal.farm_id == farm_id)) or 0


def add_animals(session: Session, farm_id: int, animal_code: str, quantity: int, now: datetime) -> list[int]:
    start = (session.scalar(select(func.max(FarmAnimal.animal_no)).where(FarmAnimal.farm_id == farm_id)) or 0) + 1
    numbers = list(range(start, start + quantity))
    session.add_all(FarmAnimal(farm_id=farm_id, animal_no=n, animal_code=animal_code, acquired_at=now) for n in numbers)
    session.flush()
    return numbers


# ---------- 加工訂單 ----------


def add_order(session: Session, **values) -> CraftOrder:
    order = CraftOrder(**values)
    session.add(order)
    session.flush()
    return order


def get_order(session: Session, farm_id: int, order_id: int, *, for_update: bool = False) -> CraftOrder | None:
    """只回傳屬於該農場的訂單，避免以訂單 ID 存取他人的訂單。"""
    stmt = select(CraftOrder).where(CraftOrder.id == order_id, CraftOrder.farm_id == farm_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def busy_order(session: Session, farm_id: int, building_code: str, now: datetime) -> CraftOrder | None:
    """該設施目前加工中（尚未完成）的訂單。"""
    stmt = select(CraftOrder).where(
        CraftOrder.farm_id == farm_id,
        CraftOrder.building_code == building_code,
        CraftOrder.status == "in_progress",
        CraftOrder.ready_at > now,
    )
    return session.scalars(stmt).first()


def list_orders(
    session: Session, farm_id: int | None = None, *, include_collected: bool = True, limit: int = 50
) -> list[tuple[CraftOrder, str, int, str]]:
    stmt = (
        select(CraftOrder, Farm.name, Player.id, Player.display_name)
        .join(Farm, Farm.id == CraftOrder.farm_id)
        .join(Player, Player.id == Farm.player_id)
        .order_by(CraftOrder.started_at.desc(), CraftOrder.id.desc())
        .limit(limit)
    )
    if farm_id is not None:
        stmt = stmt.where(CraftOrder.farm_id == farm_id)
    if not include_collected:
        stmt = stmt.where(CraftOrder.status == "in_progress")
    return [(o, farm_name, pid, pname) for o, farm_name, pid, pname in session.execute(stmt)]


# ---------- 監控 ----------


def farms_with_ranch(session: Session) -> list[tuple[Farm, str]]:
    """有建築或動物的農場。"""
    has_building = select(FarmBuilding.farm_id)
    has_animal = select(FarmAnimal.farm_id)
    stmt = (
        select(Farm, Player.display_name)
        .join(Player, Player.id == Farm.player_id)
        .where(Farm.id.in_(has_building) | Farm.id.in_(has_animal))
        .order_by(Farm.id)
    )
    return [(f, name) for f, name in session.execute(stmt)]
