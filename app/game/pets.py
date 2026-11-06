"""寵物（貓、狗）：規則與數值，不碰資料庫。

- 購買後是 Lv0（出現在農場圖的左上／右上，還沒有加成），之後升級到 Lv1–Lv4，每級加成 PCT_PER_LEVEL%。
- 貓：趕走偷吃作物的老鼠，主人收成時收成數量 +X%（小數部分依機率多 1 個，見 bonus_quantity）。
- 狗：趕走偷菜的小偷。防守設施（稻草人、警戒犬、防盜圍欄）都沒擋住時，狗再判定一次 X%；
  不受防守設施 60% 整體上限限制。擋下時偷菜紀錄的 blocked_by = DOG_BLOCKER。
"""

from dataclasses import dataclass

from app.game import balance

CAT, DOG = "cat", "dog"
BUY_PRICE = 5_000
UPGRADE_PRICES = (10_000, 20_000, 30_000, 40_000)  # Lv0→1、1→2、2→3、3→4
PCT_PER_LEVEL = 5
MAX_LEVEL = len(UPGRADE_PRICES)
DOG_BLOCKER = "pet_dog"  # steal_records.blocked_by（狗擋下時）


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
    return max(0, min(level, MAX_LEVEL)) * PCT_PER_LEVEL


def upgrade_price(level: int) -> int | None:
    """從目前等級升到下一級的價格；已滿級回傳 None。"""
    return UPGRADE_PRICES[level] if 0 <= level < MAX_LEVEL else None


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
