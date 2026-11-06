"""一鍵操作的交易內快取。

種滿、全部澆水／收成／清除與自動播種會逐格處理，每格都重查同樣的設定（品質設定、限時活動、工具、蚯蚓、群組成員、
寵物等級、進行中的任務與競賽、倉庫用量…）。bulk() 期間這些「同一筆交易中不會被迴圈改變」的查詢只做一次；
只包住逐格迴圈，離開就清掉，其他方法照常每次查詢。快取放在 Session.info（每筆交易一個 Session）。
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

_KEY = "bulk_cache"


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
