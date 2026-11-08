"""新功能規則設定（feature_settings）的讀寫與程序內快取。

各程序（Bot、網站）每隔 REFRESH_SECONDS 秒在第一筆交易時重新讀取一次（4 列以內），因此管理員在網站修改後，
Bot 最晚 REFRESH_SECONDS 秒內套用；網站本身在儲存後立即套用（feature_config.set_current）。
"""

import threading
import time
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import FeatureSetting
from app.game import feature_config

REFRESH_SECONDS = 10.0
_lock = threading.Lock()
_loaded_at: float | None = None


def refresh(session: Session, *, force: bool = False) -> None:
    """距離上次讀取超過 REFRESH_SECONDS 秒時，從資料庫重新載入目前設定。"""
    global _loaded_at
    now = time.monotonic()
    if not force and _loaded_at is not None and now - _loaded_at < REFRESH_SECONDS:
        return
    with _lock:
        if not force and _loaded_at is not None and now - _loaded_at < REFRESH_SECONDS:
            return
        _loaded_at = now  # 先記時間：讀取失敗時其他執行緒不會同時重試
    rows = session.execute(select(FeatureSetting.key, FeatureSetting.value)).all()
    feature_config.apply({k: v for k, v in rows})


def invalidate() -> None:
    """下一筆交易重新讀取（測試清空資料表後使用）。"""
    global _loaded_at
    _loaded_at = None


def rows(session: Session) -> dict[str, FeatureSetting]:
    return {r.key: r for r in session.scalars(select(FeatureSetting))}


def save(session: Session, key: str, value: dict, actor: str, now: datetime) -> None:
    stmt = insert(FeatureSetting).values(key=key, value=value, updated_at=now, updated_by=actor)
    session.execute(stmt.on_conflict_do_update(
        index_elements=[FeatureSetting.key], set_={"value": value, "updated_at": now, "updated_by": actor},
    ))


def remove(session: Session, key: str) -> None:
    session.execute(delete(FeatureSetting).where(FeatureSetting.key == key))
