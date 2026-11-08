"""操作回饋用的玩家資料：上次打開面板的時間（「你不在的時候」摘要）與個人紀錄。"""

from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import FarmFeedback, Player, StealRecord

_TOUCH = text(
    "WITH old AS (SELECT last_seen_at FROM farm_feedback WHERE farm_id = :farm_id) "
    "INSERT INTO farm_feedback (farm_id, last_seen_at, records) VALUES (:farm_id, :now, '{}') "
    "ON CONFLICT (farm_id) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at "
    "RETURNING (SELECT last_seen_at FROM old)"
)


def touch_seen(session: Session, farm_id: int, now: datetime) -> datetime | None:
    """記錄這次打開面板的時間，回傳上一次的時間（第一次為 None）。"""
    return session.execute(_TOUCH, {"farm_id": farm_id, "now": now}).scalar()


def bump_record(session: Session, farm_id: int, key: str, value: int) -> int | None:
    """value 超過目前紀錄時更新並回傳舊紀錄（第一次記錄回傳 None）；沒有破紀錄回傳 None。

    呼叫端必須已鎖定這座農場（同一座農場的紀錄更新依序執行）。"""
    row = session.get(FarmFeedback, farm_id, with_for_update=True)
    if row is None:
        session.add(FarmFeedback(farm_id=farm_id, records={key: value}))
        session.flush()
        return None
    old = (row.records or {}).get(key)
    if old is not None and value <= old:
        return None
    row.records = {**(row.records or {}), key: value}  # 重新指定才會寫入 JSON 欄位
    return old


def steals_against(session: Session, victim_player_id: int, since: datetime) -> dict:
    """since 之後這位玩家被偷的情況：成功次數與數量、偷菜者名稱、被擋下的次數（狗狗與防守設施分開）。"""
    rows = session.execute(
        select(StealRecord.outcome, StealRecord.blocked_by, func.count(), func.sum(StealRecord.quantity))
        .where(StealRecord.victim_player_id == victim_player_id, StealRecord.created_at > since)
        .group_by(StealRecord.outcome, StealRecord.blocked_by)
    ).all()
    out = {"stolen_times": 0, "stolen_quantity": 0, "blocked": {}, "thieves": []}
    for outcome, blocked_by, count, quantity in rows:
        if outcome == "success":
            out["stolen_times"] += count
            out["stolen_quantity"] += int(quantity or 0)
        else:
            out["blocked"][blocked_by] = out["blocked"].get(blocked_by, 0) + count
    if out["stolen_times"]:
        out["thieves"] = list(session.scalars(
            select(Player.display_name).join(StealRecord, StealRecord.thief_player_id == Player.id)
            .where(StealRecord.victim_player_id == victim_player_id, StealRecord.created_at > since,
                   StealRecord.outcome == "success")
            .group_by(Player.display_name).order_by(func.sum(StealRecord.quantity).desc()).limit(3)
        ))
    return out
