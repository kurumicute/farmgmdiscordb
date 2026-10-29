from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.db.models import Farm, FarmPlot


def create(
    session: Session,
    player_id: int,
    name: str,
    level: int,
    gold: int,
    warehouse_capacity: int,
    home_group_id: int,
) -> Farm:
    farm = Farm(
        player_id=player_id,
        home_group_id=home_group_id,
        name=name,
        level=level,
        gold=gold,
        warehouse_capacity=warehouse_capacity,
    )
    session.add(farm)
    session.flush()
    return farm


def get_by_player(session: Session, player_id: int, *, for_update: bool = False) -> Farm | None:
    stmt = select(Farm).where(Farm.player_id == player_id)
    if for_update:
        # 所有會改變資產的操作都先鎖定農場資料列，使同一農場的操作序列化
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def list_plots(session: Session, farm_id: int) -> list[FarmPlot]:
    stmt = (
        select(FarmPlot)
        .options(joinedload(FarmPlot.crop_type))
        .where(FarmPlot.farm_id == farm_id)
        .order_by(FarmPlot.plot_number)
    )
    return list(session.scalars(stmt))


def get_plot(
    session: Session, farm_id: int, plot_number: int, *, for_update: bool = False
) -> FarmPlot | None:
    stmt = select(FarmPlot).where(
        FarmPlot.farm_id == farm_id, FarmPlot.plot_number == plot_number
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def count_plots(session: Session, farm_id: int) -> int:
    return session.scalar(select(func.count()).where(FarmPlot.farm_id == farm_id)) or 0


def add_plots(session: Session, farm_id: int, count: int) -> list[int]:
    current_max = (
        session.scalar(select(func.max(FarmPlot.plot_number)).where(FarmPlot.farm_id == farm_id))
        or 0
    )
    numbers = list(range(current_max + 1, current_max + count + 1))
    session.add_all(FarmPlot(farm_id=farm_id, plot_number=n) for n in numbers)
    session.flush()
    return numbers


def plant(
    plot: FarmPlot, crop_type_id: int, quantity: int, now: datetime, quality: str = "normal", mutation: str = "none"
) -> None:
    plot.crop_type_id = crop_type_id
    plot.quality = quality
    plot.mutation = mutation
    plot.planted_at = now
    plot.watered_at = None
    plot.ready_at = None
    plot.remaining_quantity = quantity
    plot.fertilized = False
    plot.planting_no += 1


def water(plot: FarmPlot, now: datetime, ready_at: datetime) -> None:
    plot.watered_at = now
    plot.ready_at = ready_at


def clear(plot: FarmPlot) -> None:
    plot.crop_type_id = None
    plot.quality = None
    plot.mutation = None
    plot.planted_at = None
    plot.watered_at = None
    plot.ready_at = None
    plot.remaining_quantity = None
    plot.fertilized = False


def get_by_id(session: Session, farm_id: int, *, for_update: bool = False) -> Farm | None:
    stmt = select(Farm).where(Farm.id == farm_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()
