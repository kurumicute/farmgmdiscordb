"""特賣與升級（商店的第三個分頁）：溫室、每週特賣、稱號商店。只放規則與數值，不碰資料庫。

- 溫室：分級永久升級，每一級讓之後澆水的作物生長時間再少 GREENHOUSE_PCT_PER_LEVEL%（與限時活動的加速相加）。
- 每週特賣：台灣時間每週一 00:00 換一批，用現有的種子；每人每週每項限購，購買量存在 weekly_shop_purchases。
  商品由週次決定（不存資料庫），所有玩家同一週看到的一樣。
- 稱號商店：用金幣買稱號（放進稱號清單，可在「🎖️ 稱號」選用）；每個稱號只能買一次。
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.clock import TAIPEI_TZ

# ---------- 溫室 ----------
GREENHOUSE_CODE = "greenhouse"
GREENHOUSE_PCT_PER_LEVEL = 5
GREENHOUSE_PRICES = (5_000, 15_000, 40_000, 100_000, 250_000)  # 升到第 1、2…級的價格
GREENHOUSE_MAX_LEVEL = len(GREENHOUSE_PRICES)


def greenhouse_pct(level: int) -> int:
    """溫室讓生長時間減少的百分比。"""
    return max(0, min(level, GREENHOUSE_MAX_LEVEL)) * GREENHOUSE_PCT_PER_LEVEL


def greenhouse_price(level: int) -> int | None:
    """從目前等級升到下一級的價格；已滿級回傳 None。"""
    return GREENHOUSE_PRICES[level] if 0 <= level < GREENHOUSE_MAX_LEVEL else None


# ---------- 稱號商店 ----------
TITLE_SHOP_CODE = "title"
TITLE_SHOP = (
    ("黃金地主", 100_000),
    ("農場貴族", 250_000),
    ("傳奇農場主", 500_000),
    ("億萬富農", 1_000_000),
)
TITLE_PRICES = dict(TITLE_SHOP)

# ---------- 每週特賣 ----------
NORMAL_DEALS_PER_WEEK = 2
NORMAL_DISCOUNT_PCT = 30  # 一般種子打 7 折
NORMAL_LIMIT = 50  # 每人每週每項限購
RARE_PRICE_PCT = 120  # 稀有種子（商店平常不賣）= 種子價 × 1.2
RARE_LIMIT = 3
_EPOCH = date(2026, 1, 5)  # 第 0 週的週一（台灣日期）


@dataclass(frozen=True)
class CropRef:
    """選商品需要的作物資料（服務層由 crop_types 轉成這個）。"""

    code: str
    name: str
    seed_price: int
    shop_buyable: bool


@dataclass(frozen=True)
class Offer:
    crop_code: str
    crop_name: str
    price: int
    original_price: int
    limit: int
    rare: bool


def week_start(now: datetime) -> date:
    """包含 now 的那一週的週一（台灣日期）。"""
    today = now.astimezone(TAIPEI_TZ).date()
    return today - timedelta(days=today.weekday())


def week_ends_at(start: date) -> datetime:
    """下一次換商品的時間（下週一 00:00 台灣時間）。"""
    return datetime.combine(start + timedelta(days=7), datetime.min.time(), TAIPEI_TZ)


def weekly_offers(start: date, crops: list[CropRef]) -> list[Offer]:
    """這一週的特賣商品：依週次輪流挑一般種子（打折）與一種稀有種子。crops 只放啟用中的作物。"""
    week_no = (start - _EPOCH).days // 7
    normal = sorted((c for c in crops if c.shop_buyable), key=lambda c: (c.seed_price, c.code))
    rare = sorted((c for c in crops if not c.shop_buyable), key=lambda c: (c.seed_price, c.code))
    offers = []
    if normal:
        picks = []
        for i in range(min(NORMAL_DEALS_PER_WEEK, len(normal))):
            c = normal[(week_no * NORMAL_DEALS_PER_WEEK + i) % len(normal)]
            if c not in picks:
                picks.append(c)
        for c in picks:
            price = max(1, math.ceil(c.seed_price * (100 - NORMAL_DISCOUNT_PCT) / 100))
            offers.append(Offer(c.code, c.name, price, c.seed_price, NORMAL_LIMIT, False))
    if rare:
        c = rare[week_no % len(rare)]
        offers.append(Offer(c.code, c.name, math.ceil(c.seed_price * RARE_PRICE_PCT / 100), c.seed_price,
                            RARE_LIMIT, True))
    return offers
