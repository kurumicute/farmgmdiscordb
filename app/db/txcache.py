"""交易內快取：一鍵操作的逐格快取（bulk／memo）與讀取整頁的讀取快取（read_scope／read_memo）。

種滿、全部澆水／收成／清除與自動播種會逐格處理，每格都重查同樣的設定（品質設定、限時活動、工具、蚯蚓、群組成員、
寵物等級、進行中的任務與競賽、倉庫用量…）。bulk() 期間這些「同一筆交易中不會被迴圈改變」的查詢只做一次；
只包住逐格迴圈，離開就清掉，其他方法照常每次查詢。快取放在 Session.info（每筆交易一個 Session）。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.orm import Session

_KEY = "bulk_cache"
_READ = "read_cache"


@contextmanager
def bulk(session: Session) -> Iterator[None]:
    if _KEY in session.info:  # 已在外層的 bulk 中
        yield
        return
    session.info[_KEY] = {}
    try:
        yield
    finally:
        session.info.pop(_KEY, None)


def memo(session: Session, key, compute: Callable[[], object]):
    """bulk() 期間同一個 key 只計算一次；不在 bulk() 中時每次都計算。"""
    cache = session.info.get(_KEY)
    if cache is None:
        return compute()
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def adjust(session: Session, key, delta: int) -> None:
    """快取中的數值跟著寫入一起變動（例如倉庫用量）；沒有快取時不做任何事。"""
    cache = session.info.get(_KEY)
    if cache is not None and key in cache:
        cache[key] += delta


# ---------- 讀取快取：面板讀取整頁（run_batch）時，同一筆交易中重複的讀取只查一次 ----------
# 例如一頁裡好幾個服務方法都各自查一次「玩家的農場」「作物種類」「工具」「加工訂單」。
# 安全規則：只要這筆交易有任何寫入（ORM flush，或 session.execute 送出的 INSERT／UPDATE／DELETE），
# 整個讀取快取立刻清空；
# 加鎖的查詢（FOR UPDATE）不使用快取。回傳 list 時呼叫端拿到的是複本。


@contextmanager
def read_scope(session: Session) -> Iterator[None]:
    if _READ in session.info:
        yield
        return
    session.info[_READ] = {}
    try:
        yield
    finally:
        session.info.pop(_READ, None)


def read_memo(session: Session, key, compute: Callable[[], object]):
    cache = session.info.get(_READ)
    if cache is None:
        return compute()
    if key not in cache:
        cache[key] = compute()
    value = cache[key]
    return list(value) if isinstance(value, list) else value


def _clear_reads(session: Session) -> None:
    cache = session.info.get(_READ)
    if cache:
        cache.clear()


@event.listens_for(Session, "after_flush")
def _after_flush(session, _flush_context) -> None:
    _clear_reads(session)


@event.listens_for(Session, "do_orm_execute")
def _on_execute(state) -> None:
    if state.is_insert or state.is_update or state.is_delete:
        _clear_reads(state.session)


# ---------- 直接在連線上執行的寫入（不觸發 ORM autoflush） ----------


def core_execute(session: Session, stmt):
    """在這筆交易的連線上直接執行 Core 語句，不觸發 ORM autoflush。

    Session.execute 會先把所有改過但還沒寫入的 ORM 物件（土地、農場金幣…）整批寫入；逐格操作每格都寫交易紀錄，
    原本每格都觸發一次。只用於「不依賴尚未寫入的 ORM 變更」的寫入（交易紀錄、排行統計、圖鑑、倉庫數量的 upsert）；
    ORM 物件仍會在下一次 ORM 查詢或提交時一起寫入。寫入時一併清空讀取快取（do_orm_execute 收不到這裡的語句）。"""
    _clear_reads(session)
    return session.connection().execute(stmt)
