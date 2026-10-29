"""時間來源抽象：正式環境用系統 UTC 時間，測試可注入可控制的時鐘。"""

from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

# 台灣無日光節約時間，固定 UTC+8 即可，不需額外的時區資料套件。
TAIPEI_TZ = timezone(timedelta(hours=8), name="Asia/Taipei")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """測試用時鐘，可手動前進，避免真實等待作物生長。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
