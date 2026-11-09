"""遊戲目錄的程序內快取：作物、動物、魚種與品質設定幾乎不變，每筆交易卻都要查，占面板 SQL 的一成半。

- 最多 TTL_SECONDS 秒讀一次；Bot 與網站是不同程序，管理網站改完後 Bot 最晚 TTL_SECONDS 秒內套用
  （與 feature_settings 相同）。
- 回傳的是不屬於任何 Session 的快照物件（只有欄位值）：可跨執行緒共用，唯讀。要修改目錄一律用加鎖的查詢
  （crops_repo.by_code(for_update=True) 等），不要改這裡拿到的物件。
- 同一程序內的寫入：這筆交易寫過目錄後，到交易結束前都不用快取（看得到自己未提交的修改），
  結束時（不論提交或回滾）清空。服務層不用原生 SQL 寫目錄；測試或腳本用原生 SQL 寫入時要呼叫 clear()。
"""

import copy
import time
from collections.abc import Callable

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import AnimalType, CropGeneticsSetting, CropType, FishType

TTL_SECONDS = 10.0  # 0 = 不快取（測試預設，見 conftest）
WATCHED = (CropType, AnimalType, FishType, CropGeneticsSetting)
_WATCHED_TABLES = frozenset(m.__tablename__ for m in WATCHED)
_DIRTY = "catalog_dirty"
_cache: dict = {}  # key → (讀取時間, 值)；單一 dict 操作在 GIL 下是原子的，同時過期時最多多查幾次


def clear() -> None:
    _cache.clear()


def _snapshot(obj):
    """複製成沒有 Session 的物件：不能 expunge 原物件（同一筆交易可能正在修改它）。"""
    cls = type(obj)
    clone = cls()
    for attr in inspect(cls).column_attrs:
        set_committed_value(clone, attr.key, copy.deepcopy(getattr(obj, attr.key)))
    return clone


def _lookup(session: Session, key, compute: Callable[[], object], freeze: Callable[[object], object]):
    if TTL_SECONDS <= 0 or session.info.get(_DIRTY):
        return compute()
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < TTL_SECONDS:
        return hit[1]
    value = freeze(compute())
    _cache[key] = (now, value)
    return value


def rows(session: Session, key, compute: Callable[[], list]) -> list:
    """快取一串目錄資料列；回傳新的 list（呼叫端可排序或過濾，資料列本身唯讀）。"""
    return list(_lookup(session, key, compute, lambda value: [_snapshot(o) for o in value]))


def value(session: Session, key, compute: Callable[[], object]):
    """快取純資料（例如設定 dict）；唯讀。"""
    return _lookup(session, key, compute, copy.deepcopy)


# ---------- 同一程序內的寫入：標記 → 交易結束時清空 ----------


@event.listens_for(Session, "after_flush")
def _after_flush(session, _flush_context) -> None:
    if any(isinstance(o, WATCHED) for o in (*session.new, *session.dirty, *session.deleted)):
        session.info[_DIRTY] = True


@event.listens_for(Session, "do_orm_execute")
def _on_execute(state) -> None:
    if state.is_insert or state.is_update or state.is_delete:
        table = getattr(state.statement, "table", None)
        if getattr(table, "name", None) in _WATCHED_TABLES:
            state.session.info[_DIRTY] = True


@event.listens_for(Session, "after_transaction_end")
def _after_transaction_end(session, transaction) -> None:
    if transaction.parent is None and session.info.pop(_DIRTY, None):
        clear()
