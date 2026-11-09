"""農場日報：產生群組前一天的日報內容，顯示在 /farm 總覽頁最下方（只讀取，Bot 不主動發訊息）。

規則與文字在 game/news.py。過去某一天的交易紀錄不會再變，所以同一個 (群組, 日期) 的結果快取在程序內，
不必每次打開總覽都重新彙總一天的交易紀錄（農場改名最多晚一天才反映在日報上）。
"""

import threading
from datetime import date

from app.game import fishing, news, ruins
from app.repositories import catalog as catalog_repo
from app.repositories import news as news_repo
from app.repositories import ruins as ruins_repo
from app.services.world_boss_service import WorldBossService

CACHE_MAX = 1024  # 超過時整個清空（每個群組每天一筆，很少達到）
_cache: dict[tuple[int, date], dict] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    """測試重設資料庫後呼叫。"""
    with _cache_lock:
        _cache.clear()


class NewsService(WorldBossService):
    def daily_news(self, group_id: int, day: date | None = None) -> dict:
        """群組某一天（預設：台灣時間昨天）的日報：{"day", "empty", "text"}。"""
        day = day or news.news_day(self._clock.now())
        key = (group_id, day)
        with _cache_lock:
            if key in _cache:
                return _cache[key]
        start, end = news.day_range(day)
        with self._transaction() as session:
            self._settle_guardian(session, group_id)  # 先結算守護者，崩壞的消息才不會被快取漏掉
            data = news.DayData(
                events=news_repo.farm_events(session, group_id, start, end),
                special=news_repo.special_harvests(session, group_id, start, end),
            )
            fish = news_repo.best_fish(session, group_id, start, end)
            if fish:
                data.fish = (fish[0], fish[1], fishing.RARITY_NAMES.get(fish[2], fish[2]), fish[3])
            farm_ids = {r[0] for r in data.events} | {r[0] for r in data.special} | ({fish[0]} if fish else set())
            data.rumor = news.rumor(group_id, day, sorted({r[0] for r in data.events}))
            if data.rumor:
                farm_ids.add(data.rumor.target)
            data.names = news_repo.farm_names(session, farm_ids)
            data.item_names = catalog_repo.item_names(session)
            row = ruins_repo.get(session, group_id)
            donated = sum(q for _f, event, _n, q, _g in data.events if event == "ruins_donate")
            if row is not None:
                guardian = ruins_repo.get_guardian(session, group_id, ruins.week_start(start))

                def within(at) -> bool:
                    return at is not None and start <= at < end

                data.ruins = {"donated": donated, "percent": ruins.percent(row.stage, row.progress),
                              "completed_today": within(row.completed_at),
                              "collapsed_today": within(row.collapsed_at),
                              "guardian_defeated_today": guardian is not None and within(guardian.defeated_at)}
        result = news.build(day, data, seed=str(group_id))
        out = {"day": day, "empty": result.empty, "text": news.render(result)}
        if day < self._clock.now().astimezone(news.TAIPEI_TZ).date():  # 只快取已經結束的日子
            with _cache_lock:
                if len(_cache) >= CACHE_MAX:
                    _cache.clear()
                _cache[key] = out
        return out
