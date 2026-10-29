from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Farm, FarmDefense, Player


def list_for_farm(session: Session, farm_id: int) -> list[FarmDefense]:
    stmt = select(FarmDefense).where(FarmDefense.farm_id == farm_id).order_by(FarmDefense.device_code, FarmDefense.slot)
    return list(session.scalars(stmt))


def get_slot(session: Session, farm_id: int, device_code: str, slot: int) -> FarmDefense | None:
    stmt = select(FarmDefense).where(
        FarmDefense.farm_id == farm_id, FarmDefense.device_code == device_code, FarmDefense.slot == slot
    )
    return session.scalars(stmt).first()


def add(
    session: Session, farm_id: int, device_code: str, slot: int, start_plot: int | None, now: datetime
) -> FarmDefense:
    device = FarmDefense(farm_id=farm_id, device_code=device_code, slot=slot, start_plot=start_plot, acquired_at=now)
    session.add(device)
    session.flush()
    return device


def all_with_owners(session: Session) -> list[tuple[FarmDefense, Farm, str]]:
    stmt = (
        select(FarmDefense, Farm, Player.display_name)
        .join(Farm, Farm.id == FarmDefense.farm_id)
        .join(Player, Player.id == Farm.player_id)
        .order_by(Farm.id, FarmDefense.device_code, FarmDefense.slot)
    )
    return [(d, f, name) for d, f, name in session.execute(stmt)]
