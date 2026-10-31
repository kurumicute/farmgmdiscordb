"""在專用執行緒池中執行同步的遊戲服務，並處理可安全重試的資料庫錯誤。

- 遊戲服務使用同步 SQLAlchemy Session；Bot 是 asyncio，因此每次呼叫都送到有限大小的 ThreadPoolExecutor，
  不在事件迴圈中等待資料庫（否則 Gateway 心跳會逾時）。
- 每個公開服務方法是一筆完整交易。死結（40P01）與序列化失敗（40001）代表整筆交易已回滾，
  因此只對這兩種錯誤重試同一個服務方法，最多 RETRY_ATTEMPTS 次。
- 連線中斷、連線池耗盡等錯誤不重試：交易可能未開始、也可能已提交，回覆玩家稍後確認。
"""

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.game.errors import GameError

logger = logging.getLogger(__name__)

RETRYABLE_SQLSTATES = {"40P01", "40001"}  # deadlock_detected、serialization_failure
RETRY_ATTEMPTS = 3

MSG_BUSY = "伺服器忙碌中，請稍後再試。"
MSG_DB_DOWN = "資料庫暫時無法連線，請稍後再試；如果剛才是買賣等操作，可用 /farm log 確認是否已完成。"
MSG_UNEXPECTED = "發生未預期的錯誤，已記錄下來，請稍後再試。"


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) in RETRYABLE_SQLSTATES


def player_message(exc: BaseException) -> str:
    """把例外轉成可以給玩家看的訊息；不包含堆疊、SQL 或連線資訊。"""
    if isinstance(exc, GameError):
        return str(exc)
    if isinstance(exc, PoolTimeoutError) or is_retryable(exc):
        return MSG_BUSY
    if isinstance(exc, OperationalError | InterfaceError):
        return MSG_DB_DOWN
    return MSG_UNEXPECTED


class RetryingService:
    """包裝遊戲服務：每個公開方法遇到死結／序列化失敗時重試（每次呼叫本身就是一筆完整交易）。"""

    def __init__(self, service, attempts: int = RETRY_ATTEMPTS, sleep: Callable[[float], None] = time.sleep):
        self._service = service
        self._attempts = attempts
        self._sleep = sleep

    def in_group_context(self, group_id: int | None) -> "RetryingService":
        return RetryingService(self._service.in_group_context(group_id), self._attempts, self._sleep)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._service, name)
        if name.startswith("_") or not callable(attr):
            return attr

        @functools.wraps(attr)
        def call(*args, **kwargs):
            in_batch = getattr(self._service, "in_batch", None)
            if in_batch is not None and in_batch():  # 共用交易中失敗代表整筆已回滾，只能由外層的 run_batch 整批重試
                return attr(*args, **kwargs)
            for attempt in range(1, self._attempts + 1):
                try:
                    return attr(*args, **kwargs)
                except DBAPIError as exc:
                    if not is_retryable(exc) or attempt == self._attempts:
                        raise
                    logger.warning("服務 %s 遇到 %s，第 %d 次重試", name, exc.orig.sqlstate, attempt)
                    self._sleep(0.05 * attempt)
            raise AssertionError("unreachable")

        return call


class ServiceRunner:
    """把同步函式送到專用執行緒池執行。執行緒數 = 同時處理中的指令上限，應與連線池大小相符。"""

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="farm-db")

    async def run(self, fn: Callable, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    def shutdown(self) -> None:
        # 不取消已開始的工作：讓進行中的交易完成提交或回滾，避免中途中斷
        self._executor.shutdown(wait=True, cancel_futures=True)
