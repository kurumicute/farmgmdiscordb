"""永久工具與限時效果。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FarmEffect, FarmTool


def get_tool(session: Session, farm_id: int, tool_code: str) -> FarmTool | None:
    stmt = select(FarmTool).where(FarmTool.farm_id == farm_id, FarmTool.tool_code == tool_code)
    return session.scalars(stmt).first()


def add_tool(session: Session, farm_id: int, tool_code: str, now: datetime) -> FarmTool:
    tool = FarmTool(farm_id=farm_id, tool_code=tool_code, enabled=True, acquired_at=now)
    session.add(tool)
    session.flush()
    return tool


def list_tools(session: Session, farm_id: int) -> list[FarmTool]:
    return list(session.scalars(select(FarmTool).where(FarmTool.farm_id == farm_id).order_by(FarmTool.id)))


def tool_enabled(session: Session, farm_id: int, tool_code: str) -> bool:
    tool = get_tool(session, farm_id, tool_code)
    return bool(tool and tool.enabled)


def get_effect(session: Session, farm_id: int, effect_code: str) -> FarmEffect | None:
    stmt = select(FarmEffect).where(FarmEffect.farm_id == farm_id, FarmEffect.effect_code == effect_code)
    return session.scalars(stmt).first()


def effect_active(session: Session, farm_id: int, effect_code: str, now: datetime) -> bool:
    effect = get_effect(session, farm_id, effect_code)
    return bool(effect and effect.started_at <= now < effect.expires_at)


def list_effects(session: Session, farm_id: int) -> list[FarmEffect]:
    return list(session.scalars(select(FarmEffect).where(FarmEffect.farm_id == farm_id).order_by(FarmEffect.id)))
