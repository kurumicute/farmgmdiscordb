"""寵物（貓、狗）：規則與數值，不碰資料庫。

- 購買後是 Lv0（出現在農場圖的左上／右上），加成 base_pct%；之後升級，每級再 +pct_per_level%（預設 Lv4 = 25%）。
- 價格與加成可在管理網站調整（feature_config 的 "pets"），一律經 buy_price()／upgrade_prices()／bonus_pct() 讀取。
- 貓：趕走偷吃作物的老鼠，主人收成時收成數量 +X%（小數部分依機率多 1 個，見 bonus_quantity）。
- 狗：趕走偷菜的小偷。防守設施（稻草人、警戒犬、防盜圍欄）都沒擋住時，狗再判定一次 X%；
  不受防守設施 60% 整體上限限制。擋下時偷菜紀錄的 blocked_by = DOG_BLOCKER。
"""

from dataclasses import dataclass

from app.game import balance, feature_config
from app.game.errors import ValidationError

CAT, DOG = "cat", "dog"
DOG_BLOCKER = "pet_dog"  # steal_records.blocked_by（狗擋下時）
LEVEL_LIMIT = 10  # farm_pets.level 的 CHECK 上限：升級價格最多 10 個
CONFIG_KEY = "pets"


def _defaults() -> dict:
    return {
        "buy_price": 5_000,
        "upgrade_prices": [10_000, 20_000, 30_000, 40_000],  # Lv0→1、1→2、2→3、3→4
        "base_pct": 5,  # Lv0 的加成
        "pct_per_level": 5,  # 每升一級再加
    }


def validate_config(raw: dict) -> dict:
    v = feature_config
    out = {
        "buy_price": v.int_value(raw.get("buy_price"), "領養價格", 1, 100_000_000),
        "upgrade_prices": v.int_list(raw.get("upgrade_prices"), "升級價格", 1, 100_000_000, 1, LEVEL_LIMIT),
        "base_pct": v.int_value(raw.get("base_pct"), "Lv0 加成", 0, 100),
        "pct_per_level": v.int_value(raw.get("pct_per_level"), "每級加成", 0, 100),
    }
    top = out["base_pct"] + len(out["upgrade_prices"]) * out["pct_per_level"]
    if top > 100:
        raise ValidationError(f"最高等級的加成為 {top}%，不可超過 100%。")
    return out


feature_config.register(CONFIG_KEY, "寵物", _defaults, validate_config)


def config() -> dict:
    return feature_config.get(CONFIG_KEY)


def buy_price() -> int:
    return config()["buy_price"]


def upgrade_prices() -> list[int]:
    return config()["upgrade_prices"]


def max_level() -> int:
    return len(upgrade_prices())


def base_pct() -> int:
    return config()["base_pct"]


def pct_per_level() -> int:
    return config()["pct_per_level"]


@dataclass(frozen=True)
class Pet:
    code: str
    name: str
    emoji: str
    role: str  # 簡介
    effect: str  # 效果說明（{pct} 代入目前加成）


PETS = {
    CAT: Pet(CAT, "貓咪", "🐱", "抓老鼠：趕走偷吃農作物的老鼠", "收成數量 +{pct}%"),
    DOG: Pet(DOG, "狗狗", "🐶", "保衛農田：趕走來偷菜的小偷", "防守設施沒擋住時，再有 {pct}% 機率擋下偷菜（所有農田）"),
}


def item_code(pet_code: str) -> str:
    """交易紀錄的 item_code（購買與升級都記 buy_item）。"""
    return f"pet_{pet_code}"


def bonus_pct(level: int | None) -> int:
    """level 為 None 表示沒有這隻寵物。"""
    if level is None:
        return 0
    return base_pct() + max(0, min(level, max_level())) * pct_per_level()


def upgrade_price(level: int) -> int | None:
    """從目前等級升到下一級的價格；已滿級回傳 None。"""
    prices = upgrade_prices()
    return prices[level] if 0 <= level < len(prices) else None


def effect_text(code: str, level: int | None) -> str:
    return PETS[code].effect.format(pct=bonus_pct(level))


def bonus_quantity(quantity: int, pct: int, u: float) -> int:
    """貓的收成加成：quantity × pct%，整數部分一定給，小數部分以機率 u ∈ [0, 1) 決定是否多 1 個。"""
    if quantity <= 0 or pct <= 0:
        return 0
    extra = quantity * pct / 100
    whole = int(extra)
    return whole + (1 if u < extra - whole else 0)


def odds_text(quantity: int, pct: int) -> str:
    """貓咪對「每格收成 quantity 個」的實際效果（與 bonus_quantity 相同算法）。"""
    extra = quantity * pct / 100
    sure = int(extra)
    chance = round((extra - sure) * 100, 1)
    chance_text = f"{chance:g}%"
    if sure and chance:
        return f"固定 +{sure}，另有 {chance_text} 機率再 +1"
    if sure:
        return f"固定 +{sure}"
    if chance:
        return f"{chance_text} 機率 +1"
    return "不會多"


def blocker_name(code: str) -> str:
    """偷菜被阻止時的阻止者名稱（防守設施或狗狗）。"""
    if code == DOG_BLOCKER:
        return PETS[DOG].name
    return balance.DEFENSES[code][0]
