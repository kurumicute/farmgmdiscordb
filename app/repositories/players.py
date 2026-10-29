from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Farm, Player


def create(
    session: Session,
    display_name: str,
    external_platform: str | None = None,
    external_user_id: str | None = None,
) -> Player:
    player = Player(
        display_name=display_name,
        external_platform=external_platform,
        external_user_id=external_user_id,
    )
    session.add(player)
    session.flush()
    return player


def get(session: Session, player_id: int, *, for_update: bool = False) -> Player | None:
    stmt = select(Player).where(Player.id == player_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def get_by_external(session: Session, platform: str, user_id: str) -> Player | None:
    stmt = select(Player).where(Player.external_platform == platform, Player.external_user_id == user_id)
    return session.scalars(stmt).first()


def list_all(session: Session, limit: int = 100) -> list[Player]:
    return list(session.scalars(select(Player).order_by(Player.id).limit(limit)))


def search(session: Session, keyword: str | None, limit: int, offset: int) -> tuple[list[tuple[Player, bool]], int]:
    """依名稱關鍵字或 ID 搜尋玩家（分頁）；回傳 ([(玩家, 是否有農場)], 總數)。"""
    stmt = select(Player)
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cond = Player.display_name.ilike(f"%{escaped}%", escape="\\")
        if keyword.isdigit():
            cond = cond | (Player.id == int(keyword))
        stmt = stmt.where(cond)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.execute(
        stmt.add_columns(Farm.id.is_not(None))
        .outerjoin(Farm, Farm.player_id == Player.id)
        .order_by(Player.id)
        .limit(limit)
        .offset(offset)
    )
    return [(p, bool(has)) for p, has in rows], total
