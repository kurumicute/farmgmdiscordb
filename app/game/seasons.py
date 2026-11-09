"""季節與時段限定（純規則）：部分作物只能在特定季節種植，部分魚只在特定季節或時段釣得到。

季節依台灣時間的真實月份（使用者選擇）：春 3–5 月、夏 6–8 月、秋 9–11 月、冬 12–2 月。
時段也是台灣時間的整點區間 [開始, 結束)，可以跨午夜（例如 18–6 = 晚上 6 點到早上 6 點）。
沒有設定（None）就是不限制。作物只在「種下」的當下檢查，種下後過了季節照樣會長大、收成；
魚在「拋竿」的當下檢查。
"""

from datetime import datetime

from app.clock import TAIPEI_TZ
from app.game.errors import ValidationError

SEASONS = ("spring", "summer", "autumn", "winter")
SEASON_NAMES = {"spring": "春", "summer": "夏", "autumn": "秋", "winter": "冬"}
SEASON_EMOJI = {"spring": "🌸", "summer": "☀️", "autumn": "🍂", "winter": "❄️"}
_MONTH_SEASON = {3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer",
                 9: "autumn", 10: "autumn", 11: "autumn", 12: "winter", 1: "winter", 2: "winter"}

# 預設限定（作物與 0033 的魚由 Migration 0034 寫入，0035 的特殊魚由 0035 寫入，數值相同）
CROP_SEASONS = {
    # 小麥四季都能種（不列出）；其他每種兩個季節，每季都有 4–5 種作物可以種（2026-11-09 使用者要求）
    "carrot": ("autumn", "winter"),
    "tomato": ("winter", "spring"),
    "strawberry": ("winter", "spring"),
    "corn": ("spring", "summer"),
    "watermelon": ("spring", "summer"),
    "pumpkin": ("summer", "autumn"),
    "grape": ("summer", "autumn"),
    "amber_melon": ("autumn", "winter"),
    "moonflower": ("winter", "spring"),
}
# 魚：code → (季節或 None, (開始時, 結束時) 或 None)
FISH_LIMITS = {
    "pond_smelt": (("winter",), None),
    "mullet": (("winter",), None),
    "mackerel": (("autumn",), None),
    "yellowfin_tuna": (("summer",), None),
    "swordfish": (("autumn",), None),
    "dragon_king_carp": (("spring",), None),
    "bluegill": (None, (6, 18)),
    "snakehead": (None, (18, 6)),
    "eel": (None, (18, 6)),
    "moon_ray": (None, (20, 4)),
    # Migration 0035 的特殊魚：季節＋時段（部分還要指定魚餌，見 fishing.FISH_REQUIRED_BAITS）
    "dawn_trout": (None, (4, 7)),
    "snow_icefish": (("winter",), (5, 9)),
    "firefly_squid": (("spring",), (20, 4)),
    "lanternfish": (("summer",), (20, 4)),
    "maple_salmon": (("autumn",), (16, 19)),
    "rainbow_wrasse": (("summer",), (10, 16)),
    "midnight_angler": (None, (0, 3)),
    "spring_koi": (("spring",), (6, 10)),
    "ghost_shark": (("autumn",), (21, 3)),
    "frost_dragon_fish": (("winter",), (22, 2)),
}


def season_of(now: datetime) -> str:
    return _MONTH_SEASON[now.astimezone(TAIPEI_TZ).month]


def in_hours(now: datetime, start: int | None, end: int | None) -> bool:
    if start is None or end is None or start == end:
        return True
    hour = now.astimezone(TAIPEI_TZ).hour
    return start <= hour < end if start < end else hour >= start or hour < end


def available(seasons, start: int | None, end: int | None, now: datetime) -> bool:
    return (not seasons or season_of(now) in seasons) and in_hours(now, start, end)


def describe(seasons, start: int | None = None, end: int | None = None) -> str:
    """顯示用：「🍂秋季限定」「🕒 18:00–06:00」，沒有限制時回傳空字串。"""
    parts = []
    if seasons:
        parts.append("".join(SEASON_EMOJI[s] for s in seasons) + "、".join(SEASON_NAMES[s] for s in seasons)
                     + "季限定")
    if start is not None and end is not None and start != end:
        parts.append(f"🕒 {start:02d}:00–{end:02d}:00")
    return "　".join(parts)


def now_text(now: datetime) -> str:
    s = season_of(now)
    return f"{SEASON_EMOJI[s]} 現在是{SEASON_NAMES[s]}季　🕒 台灣時間 {now.astimezone(TAIPEI_TZ):%H:%M}"


def validate(seasons, start, end) -> tuple[list[str] | None, int | None, int | None]:
    """管理網站送來的設定：季節清單（空 = 不限）、開始與結束整點（0–23，兩個都填或都不填）。"""
    if seasons is not None:
        if not isinstance(seasons, list) or any(s not in SEASONS for s in seasons):
            raise ValidationError("季節只能是 spring、summer、autumn、winter。")
        seasons = [s for s in SEASONS if s in seasons] or None
    if (start is None) != (end is None):
        raise ValidationError("時段的開始與結束要一起設定（或都不設定）。")
    for v in (start, end):
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 23):
            raise ValidationError("時段必須是 0–23 的整點。")
    if start is not None and start == end:
        raise ValidationError("時段的開始與結束不能相同。")
    return seasons, start, end
