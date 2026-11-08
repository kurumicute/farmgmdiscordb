"""特賣與升級（商店的第三個分頁）：溫室、每週特賣、稱號商店。只放規則與數值，不碰資料庫。

- 數值（溫室價格與加成、每週特賣、稱號商店）可在管理網站調整（feature_config 的 "special_shop"），一律經函式讀取。
- 溫室：分級永久升級，每一級讓之後澆水的作物生長時間再少 greenhouse_pct_per_level()%（與限時活動的加速相加）。
- 每週特賣：台灣時間每週一 00:00 換一批，用現有的種子；每人每週每項限購，購買量存在 weekly_shop_purchases。
  商品由週次決定（不存資料庫），所有玩家同一週看到的一樣。
- 稱號商店：用金幣買稱號（放進稱號清單，可在「🎖️ 稱號」選用）；每個稱號只能買一次。
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.clock import TAIPEI_TZ
from app.game import feature_config
from app.game.errors import ValidationError

CONFIG_KEY = "special_shop"
GREENHOUSE_CODE = "greenhouse"
GREENHOUSE_LEVEL_LIMIT = 10  # farms.greenhouse_level 的 CHECK 上限
TITLE_SHOP_CODE = "title"
TITLE_MAX_LEN = 20  # farm_titles.title 的長度
TITLE_SHOP_MAX = 20
_EPOCH = date(2026, 1, 5)  # 第 0 週的週一（台灣日期）


def _defaults() -> dict:
    return {
        "greenhouse_prices": [5_000, 15_000, 40_000, 100_000, 250_000],  # 升到第 1、2…級的價格
        "greenhouse_pct_per_level": 5,
        "normal_deals": 2,  # 每週打折的一般種子數
        "normal_discount_pct": 30,  # 一般種子打 7 折
        "normal_limit": 50,  # 每人每週每項限購
        "rare_price_pct": 120,  # 稀有種子（商店平常不賣）= 種子價 × 1.2
        "rare_limit": 3,
        "titles": [
            {"title": "黃金地主", "price": 100_000},
            {"title": "農場貴族", "price": 250_000},
            {"title": "傳奇農場主", "price": 500_000},
            {"title": "億萬富農", "price": 1_000_000},
        ],
    }


def validate_config(raw: dict) -> dict:
    v = feature_config
    out = {
        "greenhouse_prices": v.int_list(raw.get("greenhouse_prices"), "溫室升級價格", 1, 1_000_000_000, 1,
                                        GREENHOUSE_LEVEL_LIMIT),
        "greenhouse_pct_per_level": v.int_value(raw.get("greenhouse_pct_per_level"), "溫室每級加速", 0, 50),
        "normal_deals": v.int_value(raw.get("normal_deals"), "每週打折種子數", 0, 10),
        "normal_discount_pct": v.int_value(raw.get("normal_discount_pct"), "一般種子折扣", 0, 90),
        "normal_limit": v.int_value(raw.get("normal_limit"), "一般種子每週限購", 1, 9_999),
        "rare_price_pct": v.int_value(raw.get("rare_price_pct"), "稀有種子價格", 1, 10_000),
        "rare_limit": v.int_value(raw.get("rare_limit"), "稀有種子每週限購", 1, 9_999),
    }
    top = len(out["greenhouse_prices"]) * out["greenhouse_pct_per_level"]
    if top > 90:
        raise ValidationError(f"溫室最高等級會讓生長時間減少 {top}%，不可超過 90%。")
    titles = raw.get("titles")
    if not isinstance(titles, list) or len(titles) > TITLE_SHOP_MAX:
        raise ValidationError(f"稱號商店最多 {TITLE_SHOP_MAX} 個稱號。")
    out["titles"] = []
    for i, t in enumerate(titles, 1):
        if not isinstance(t, dict):
            raise ValidationError(f"第 {i} 個商店稱號格式不正確。")
        out["titles"].append({
            "title": v.title_value(t.get("title"), f"第 {i} 個商店稱號", TITLE_MAX_LEN, required=True),
            "price": v.int_value(t.get("price"), f"稱號「{t.get('title')}」的價格", 1, 1_000_000_000),
        })
    names = [t["title"] for t in out["titles"]]
    if len(names) != len(set(names)):
        raise ValidationError("稱號商店的稱號不可重複。")
    return out


feature_config.register(CONFIG_KEY, "特賣與升級", _defaults, validate_config)


def config() -> dict:
    return feature_config.get(CONFIG_KEY)


# ---------- 溫室 ----------


def greenhouse_prices() -> list[int]:
    return config()["greenhouse_prices"]


def greenhouse_max_level() -> int:
    return len(greenhouse_prices())


def greenhouse_pct_per_level() -> int:
    return config()["greenhouse_pct_per_level"]


def greenhouse_pct(level: int) -> int:
    """溫室讓生長時間減少的百分比（等級超過目前上限時以上限計算）。"""
    return max(0, min(level, greenhouse_max_level())) * greenhouse_pct_per_level()


def greenhouse_price(level: int) -> int | None:
    """從目前等級升到下一級的價格；已滿級回傳 None。"""
    prices = greenhouse_prices()
    return prices[level] if 0 <= level < len(prices) else None


# ---------- 稱號商店 ----------


def title_shop() -> list[tuple[str, int]]:
    """(稱號, 價格)，依商店顯示順序。"""
    return [(t["title"], t["price"]) for t in config()["titles"]]


def title_price(title: str) -> int | None:
    return dict(title_shop()).get(title)


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
    cfg = config()
    deals, discount = cfg["normal_deals"], cfg["normal_discount_pct"]
    week_no = (start - _EPOCH).days // 7
    normal = sorted((c for c in crops if c.shop_buyable), key=lambda c: (c.seed_price, c.code))
    rare = sorted((c for c in crops if not c.shop_buyable), key=lambda c: (c.seed_price, c.code))
    offers = []
    if normal:
        picks = []
        for i in range(min(deals, len(normal))):
            c = normal[(week_no * deals + i) % len(normal)]
            if c not in picks:
                picks.append(c)
        for c in picks:
            price = max(1, math.ceil(c.seed_price * (100 - discount) / 100))
            offers.append(Offer(c.code, c.name, price, c.seed_price, cfg["normal_limit"], False))
    if rare:
        c = rare[week_no % len(rare)]
        offers.append(Offer(c.code, c.name, math.ceil(c.seed_price * cfg["rare_price_pct"] / 100), c.seed_price,
                            cfg["rare_limit"], True))
    return offers
