"""面板延遲統計：每次開啟面板、換頁、操作從 Bot 收到互動到畫面送出的耗時，定期彙總寫入日誌。

只存在記憶體（不寫資料庫），Bot 重啟即歸零；只在事件迴圈中呼叫，不需要鎖。
日誌查詢：`docker compose logs bot | grep 面板延遲`。
"""

import logging
import time

logger = logging.getLogger(__name__)

SLOW_MS = 3000  # 單次超過就立刻記一筆警告（玩家明顯感覺卡）
MAX_SAMPLES = 5000  # 每個項目每段期間最多保留的樣本；超過只累計次數、總和與最大值
TOP = 15  # 彙總時列出次數最多的項目數
LOG_INTERVAL_SECONDS = 600  # 每 10 分鐘寫一次彙總（期間沒有操作就不寫）


def percentile(values: list[float], q: float) -> float:
    """最近排名法；values 需已排序且不可為空。"""
    k = max(0, min(len(values) - 1, int(-(-q * len(values) // 1)) - 1))
    return values[k]


class LatencyStats:
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self.since = time.monotonic()

    def record(self, name: str, ms: float, deferred: bool = False) -> None:
        d = self._data.setdefault(name, {"samples": [], "count": 0, "deferred": 0, "total": 0.0, "max": 0.0})
        d["count"] += 1
        d["total"] += ms
        d["max"] = max(d["max"], ms)
        d["deferred"] += 1 if deferred else 0
        if len(d["samples"]) < MAX_SAMPLES:
            d["samples"].append(ms)
        if ms >= SLOW_MS:
            logger.warning("面板延遲偏高 %.0f ms（%s%s）", ms, name, "，已先 defer" if deferred else "")

    def summary(self) -> list[dict]:
        """各項目的次數、平均、P50、P95、最大與 defer 次數，依次數由多到少；最前面一列是全部合計。"""
        rows = []
        everything: list[float] = []
        total = {"name": "全部", "count": 0, "deferred": 0, "total": 0.0, "max": 0.0}
        for name, d in self._data.items():
            values = sorted(d["samples"])
            everything.extend(values)
            rows.append({"name": name, "count": d["count"], "deferred": d["deferred"],
                         "avg": d["total"] / d["count"], "p50": percentile(values, 0.5),
                         "p95": percentile(values, 0.95), "max": d["max"]})
            for k in ("count", "deferred", "total"):
                total[k] += d[k]
            total["max"] = max(total["max"], d["max"])
        if not rows:
            return []
        everything.sort()
        rows.sort(key=lambda r: (-r["count"], r["name"]))
        return [{"name": "全部", "count": total["count"], "deferred": total["deferred"],
                 "avg": total["total"] / total["count"], "p50": percentile(everything, 0.5),
                 "p95": percentile(everything, 0.95), "max": total["max"]}, *rows]

    def log_and_reset(self) -> None:
        rows = self.summary()
        minutes = (time.monotonic() - self.since) / 60
        self._data, self.since = {}, time.monotonic()
        if not rows:
            return
        lines = [f"面板延遲（最近 {minutes:.0f} 分鐘，單位 ms；次數 平均 P50 P95 最大 先defer）："]
        for r in rows[:TOP + 1]:
            lines.append(f"  {r['name']}：{r['count']} 次 {r['avg']:.0f} {r['p50']:.0f} {r['p95']:.0f} "
                         f"{r['max']:.0f} defer {r['deferred']}")
        if len(rows) > TOP + 1:
            lines.append(f"  （另有 {len(rows) - TOP - 1} 項較少的）")
        logger.info("\n".join(lines))


STATS = LatencyStats()
