"""釣魚小遊戲的純規則：稀有度、設定驗證、釣具效果、魚種抽選與拉扯博弈。不依賴資料庫或 Discord。

重要設計（需求：稀有度機率與釣魚成功率是兩個獨立設定）：
- 稀有度只決定「出現哪一種魚」與「拉扯有多難」（體力、起始距離、掙扎機率），不會降低成功率。
- 基礎成功率（預設 90%）在拋竿時獨立判定：沒通過時，魚會在拉扯開始後「脫鉤」（與稀有度、裝備無關）。
- 拉扯中魚的下一個動作會事先顯示（💬 提示），依提示操作（衝刺前放線、平靜時收線）一定能把魚拉上來；
  因此依提示操作的玩家，各稀有度的整體成功率都接近基礎成功率。不看提示亂按才會斷線或讓魚逃走。
- 魚竿（收線力、穩定度、反應時間）與魚線（最大張力）提高操作容錯；魚餌提高稀有魚的出現機率並縮短等待。

所有數值可由管理員調整（fishing_settings 版本紀錄）；拋竿時把設定與裝備效果存進釣魚 Session，
之後修改設定不影響已經開始的釣魚。
"""

import math
from collections.abc import Callable

from app.game.errors import ValidationError

RARITIES = ("common", "rare", "epic", "legendary")
RARITY_NAMES = {"common": "普通", "rare": "稀有", "epic": "史詩", "legendary": "傳說"}
RARITY_EMOJI = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legendary": "🟡"}
# 每種魚的售價由管理員自由設定，不受稀有度限制；上限只是避免超出資料庫整數欄位
PRICE_MIN, PRICE_MAX = 0, 1_000_000_000


def price_span(fish_list, rarity: str) -> tuple[int, int] | None:
    """某稀有度目前魚種的售價範圍（最低售價的最小值～最高售價的最大值），沒有魚時回傳 None。"""
    rows = [f for f in fish_list if _get(f, "rarity") == rarity]
    if not rows:
        return None
    return min(_get(f, "min_price") for f in rows), max(_get(f, "max_price") for f in rows)


def _get(obj, key):
    return obj[key] if isinstance(obj, dict) else getattr(obj, key)

GEAR_KINDS = ("rod", "line", "bait")
GEAR_KIND_NAMES = {"rod": "魚竿", "line": "魚線", "bait": "魚餌"}
GEAR_KIND_EMOJI = {"rod": "🎋", "line": "🪢", "bait": "🪱"}

ACTIONS = ("reel", "release", "wait")
ACTION_NAMES = {"reel": "收線", "release": "放線", "wait": "等待"}

# Session 狀態：casting（等魚上鉤，含咬鉤反應時間）、fighting（拉扯中）；其餘為結束狀態
ACTIVE_STATUSES = ("casting", "fighting")
END_STATUSES = ("landed", "escaped", "line_broken", "slipped", "missed", "timeout", "abandoned")
STATUS_NAMES = {
    "casting": "等待上鉤", "fighting": "拉扯中", "landed": "成功釣起", "escaped": "魚逃走了", "line_broken": "斷線",
    "slipped": "脫鉤", "missed": "沒有及時拉竿", "timeout": "操作逾時", "abandoned": "放棄／中斷",
}
FAIL_STATUSES = ("escaped", "line_broken", "slipped", "missed", "timeout", "abandoned")

ESCAPE_DISTANCE = 100  # 距離達到上限：魚逃走
BASE_MAX_TENSION = 100  # 沒有魚線加成時的最大張力
START_TENSION = 20
REACTION_GRACE_SECONDS = 1.5  # 網路延遲的寬限（不顯示給玩家）
SESSION_MAX_SECONDS = 15 * 60  # 單次釣魚最長時間（避免殘留）
SLIP_TURNS = (1, 2)  # 基礎成功率沒通過時，在第幾次拉扯操作脫鉤
FISH_DIFFICULTY_STAMINA = {1: 0.8, 2: 0.9, 3: 1.0, 4: 1.15, 5: 1.3}  # 魚種掙扎難度 → 體力倍率
TIRED_RATIO = 0.3  # 體力低於 30% 時，掙扎與衝刺機率減半

# 玩家操作的效果（距離為負 = 拉近）；spike_mul：這回合魚的動作造成的張力增加倍率（放線時魚線鬆，衝刺不易繃緊）；
# dash_mul：魚衝刺拉開的距離倍率（等待＝穩住魚竿，魚只能拉走一半距離）
PLAYER_ACTIONS = {
    "reel": {"tension": 14, "stamina": -8, "distance": 0, "spike_mul": 1.0, "dash_mul": 1.0},  # 距離由魚竿收線力決定
    "release": {"tension": -30, "stamina": 0, "distance": 6, "spike_mul": 0.5, "dash_mul": 1.0},
    "wait": {"tension": -12, "stamina": 2, "distance": 0, "spike_mul": 0.8, "dash_mul": 0.5},
}
# 魚的動作：張力、距離（依剩餘體力比例縮放 50%–100%）、體力消耗
FISH_INTENTS = {
    "calm": {"tension": -6, "distance": 0, "stamina": 0},
    "struggle": {"tension": 12, "distance": 4, "stamina": -4},
    "surge": {"tension": 22, "distance": 9, "stamina": -12},
    "strong": {"tension": 32, "distance": 13, "stamina": -18},
    "tired": {"tension": -8, "distance": 0, "stamina": 0},
}
INTENT_TEXT = {
    "calm": "🌊 魚很平靜，現在收線正好！",
    "struggle": "💦 魚在掙扎，小心張力。",
    "surge": "⚡ 魚準備衝刺！",
    "strong": "💥 魚正在猛烈掙扎，就要強力衝刺了！",
    "tired": "😮‍💨 魚累了，快收線！",
}

DEFAULT_DIFFICULTY = {
    "common": {"stamina": 40, "distance": 40, "struggle_pct": 30, "surge_pct": 10, "strong_pct": 0, "chain_pct": 0},
    "rare": {"stamina": 60, "distance": 50, "struggle_pct": 30, "surge_pct": 20, "strong_pct": 5, "chain_pct": 0},
    "epic": {"stamina": 80, "distance": 55, "struggle_pct": 25, "surge_pct": 25, "strong_pct": 15, "chain_pct": 20},
    "legendary": {"stamina": 100, "distance": 55, "struggle_pct": 20, "surge_pct": 25, "strong_pct": 25,
                  "chain_pct": 40},
}
DEFAULT_SETTINGS = {
    "rarity_bp": {"common": 8000, "rare": 1500, "epic": 400, "legendary": 100},  # 萬分比，合計 10000
    "base_success_pct": 90,
    "bite_wait_min_seconds": 5,
    "bite_wait_max_seconds": 12,
    "reaction_seconds": 5,
    "action_timeout_seconds": 20,
    "basket_capacity": 20,
    "difficulty": DEFAULT_DIFFICULTY,
}
DIFFICULTY_LIMITS = {
    "stamina": (10, 300), "distance": (20, 90), "struggle_pct": (0, 100), "surge_pct": (0, 100),
    "strong_pct": (0, 100), "chain_pct": (0, 90),
}
SETTING_LIMITS = {
    "base_success_pct": (50, 100),
    "bite_wait_min_seconds": (1, 30),
    "bite_wait_max_seconds": (1, 60),
    "reaction_seconds": (2, 15),
    "action_timeout_seconds": (5, 60),
    "basket_capacity": (5, 200),
}

# 釣具效果欄位與範圍（管理員可調整）
GEAR_EFFECT_LIMITS = {
    "rod": {"reel_power": (8, 30), "control_pct": (0, 60), "reaction_bonus_ms": (0, 5000)},
    "line": {"max_tension": (80, 250)},
    "bait": {"rare_bp": (0, 3000), "epic_bp": (0, 2000), "legendary_bp": (0, 1000), "bite_speed_pct": (0, 80)},
}
GEAR_EFFECT_NAMES = {
    "reel_power": "收線力", "control_pct": "穩定度（減少張力 %）", "reaction_bonus_ms": "反應時間加成（毫秒）",
    "max_tension": "最大張力", "rare_bp": "稀有機率加成（萬分比）", "epic_bp": "史詩機率加成（萬分比）",
    "legendary_bp": "傳說機率加成（萬分比）", "bite_speed_pct": "上鉤速度加快 %",
}
GEAR_PRICE_RANGE = (1, 1_000_000)
GEAR_LEVEL_RANGE = (1, 100)
GEAR_STOCK_RANGE = (0, 1_000_000)
BAIT_MAX_HOLD = 999
BAIT_MAX_BUY = 99

FISH_WEIGHT_RANGE_G = (10, 500_000)  # 10 公克 – 500 公斤
FISH_DIFFICULTY_RANGE = (1, 5)
SELL_MAX_PER_ACTION = 200
SHARE_COOLDOWN_SECONDS = 10  # 同一隻魚連續分享的最短間隔（避免洗版）


# 初始魚種與釣具（與 Migration 0016 相同；測試用來重設目錄。正式資料以資料庫為準，可由管理網站修改）
# code, 名稱, emoji, 稀有度, 最低價, 最高價, 最小重量 g, 最大重量 g, 掙扎難度, 說明
INITIAL_FISH = (
    ("pale_chub", "溪哥", "🐟", "common", 1, 8, 20, 120, 1, "溪流裡成群游動的小魚。"),
    ("loach", "泥鰍", "🐟", "common", 2, 10, 30, 200, 1, "喜歡躲在泥巴裡，滑溜溜的。"),
    ("crucian_carp", "鯽魚", "🐟", "common", 1, 12, 100, 800, 2, "池塘裡最常見的魚。"),
    ("tilapia", "吳郭魚", "🐟", "common", 3, 15, 300, 1500, 2, "適應力超強，到處都釣得到。"),
    ("common_carp", "鯉魚", "🐟", "common", 8, 20, 1000, 8000, 3, "力氣不小的大型淡水魚。"),
    ("sweetfish", "香魚", "🐠", "rare", 30, 50, 100, 400, 2, "只住在清澈溪流，帶有淡淡香氣。"),
    ("sea_bass", "鱸魚", "🐠", "rare", 21, 40, 1000, 6000, 3, "會突然衝刺的掠食魚。"),
    ("rainbow_trout", "虹鱒", "🐠", "rare", 25, 45, 800, 4000, 3, "身上有彩虹般的光澤。"),
    ("catfish", "鯰魚", "🐠", "rare", 21, 38, 1500, 12000, 4, "夜行性，咬餌後會往深處鑽。"),
    ("salmon", "鮭魚", "🐡", "epic", 51, 80, 2000, 10000, 3, "逆流而上的勇者。"),
    ("mahi_mahi", "鬼頭刀", "🐡", "epic", 55, 90, 2000, 15000, 4, "速度飛快，會連續跳出水面。"),
    ("grouper", "石斑魚", "🐡", "epic", 60, 95, 3000, 25000, 4, "被釣到會拚命往礁石裡躲。"),
    ("formosan_salmon", "櫻花鉤吻鮭", "🌸", "legendary", 120, 190, 200, 1500, 3, "冰河時期留下的國寶魚。"),
    ("golden_koi", "黃金錦鯉", "🎏", "legendary", 140, 200, 3000, 12000, 4, "傳說釣到就會好運連連。"),
    ("oarfish", "皇帶魚", "🐉", "legendary", 101, 180, 20000, 150000, 5, "深海中的神秘巨魚。"),
)
# code, 類型, 名稱, emoji, 價格, 需要等級, 說明, 效果
INITIAL_GEAR = (
    ("bamboo_rod", "rod", "竹製魚竿", "🎋", 100, 1, "新手入門的魚竿。",
     {"reel_power": 10, "control_pct": 0, "reaction_bonus_ms": 0}),
    ("wooden_rod", "rod", "木製魚竿", "🎋", 600, 2, "比竹竿穩一點。",
     {"reel_power": 11, "control_pct": 5, "reaction_bonus_ms": 500}),
    ("iron_rod", "rod", "鐵製魚竿", "🎣", 2000, 4, "收線更有力，魚衝刺時比較穩。",
     {"reel_power": 13, "control_pct": 10, "reaction_bonus_ms": 1000}),
    ("pro_rod", "rod", "高級魚竿", "🎣", 6000, 7, "專業釣手愛用。",
     {"reel_power": 15, "control_pct": 18, "reaction_bonus_ms": 1500}),
    ("legend_rod", "rod", "傳說魚竿", "✨", 20000, 10, "傳說魚也拉得動的頂級魚竿。",
     {"reel_power": 18, "control_pct": 25, "reaction_bonus_ms": 2000}),
    ("basic_line", "line", "普通魚線", "🪢", 50, 1, "一般的尼龍線。", {"max_tension": 100}),
    ("strong_line", "line", "強化魚線", "🪢", 800, 3, "比較不容易斷。", {"max_tension": 115}),
    ("heavy_line", "line", "高強度魚線", "🪢", 3000, 6, "大魚的衝擊也撐得住。", {"max_tension": 130}),
    ("worm_bait", "bait", "普通蚯蚓", "🪱", 3, 1, "最普通的魚餌（每次拋竿用掉 1 個）。",
     {"rare_bp": 0, "epic_bp": 0, "legendary_bp": 0, "bite_speed_pct": 0}),
    ("premium_bait", "bait", "高級魚餌", "🦐", 6, 3, "稀有魚比較愛吃，上鉤也比較快。",
     {"rare_bp": 300, "epic_bp": 100, "legendary_bp": 30, "bite_speed_pct": 20}),
    ("special_bait", "bait", "特殊魚餌", "🌟", 12, 6, "特製香餌，吸引史詩與傳說魚。",
     {"rare_bp": 500, "epic_bp": 300, "legendary_bp": 150, "bite_speed_pct": 40}),
)



def _int(value, label: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValidationError(f"{label}必須是 {low}–{high} 之間的整數。")
    return value


# ---------- 設定 ----------


def validate_settings(raw: dict) -> dict:
    """檢查並整理管理員送出的釣魚設定；稀有度機率合計必須是 100%（10000 萬分比）。"""
    if not isinstance(raw, dict):
        raise ValidationError("設定格式不正確。")
    bp = raw.get("rarity_bp")
    if not isinstance(bp, dict) or set(bp) != set(RARITIES):
        raise ValidationError("稀有度機率必須包含普通、稀有、史詩、傳說四種。")
    rarity_bp = {r: _int(bp[r], f"{RARITY_NAMES[r]}出現機率", 0, 10000) for r in RARITIES}
    total = sum(rarity_bp.values())
    if total != 10000:
        raise ValidationError(f"稀有度出現機率合計必須是 100%（目前 {total / 100:g}%）。")
    out = {"rarity_bp": rarity_bp}
    for key, (low, high) in SETTING_LIMITS.items():
        out[key] = _int(raw.get(key), SETTING_LABELS[key], low, high)
    if out["bite_wait_max_seconds"] < out["bite_wait_min_seconds"]:
        raise ValidationError("上鉤等待時間的上限不可小於下限。")
    diff = raw.get("difficulty")
    if not isinstance(diff, dict) or set(diff) != set(RARITIES):
        raise ValidationError("拉扯難度必須包含四種稀有度。")
    out["difficulty"] = {}
    for r in RARITIES:
        d = diff[r]
        if not isinstance(d, dict):
            raise ValidationError(f"{RARITY_NAMES[r]}的拉扯難度格式不正確。")
        values = {k: _int(d.get(k), f"{RARITY_NAMES[r]}{DIFFICULTY_LABELS[k]}", low, high)
                  for k, (low, high) in DIFFICULTY_LIMITS.items()}
        if values["struggle_pct"] + values["surge_pct"] + values["strong_pct"] > 100:
            raise ValidationError(f"{RARITY_NAMES[r]}的掙扎、衝刺、強力掙扎機率合計不可超過 100%。")
        out["difficulty"][r] = values
    return out


SETTING_LABELS = {
    "base_success_pct": "基礎釣魚成功率（%）",
    "bite_wait_min_seconds": "上鉤等待時間下限（秒）",
    "bite_wait_max_seconds": "上鉤等待時間上限（秒）",
    "reaction_seconds": "拉竿反應時間（秒）",
    "action_timeout_seconds": "拉扯操作逾時（秒）",
    "basket_capacity": "魚簍容量",
}
DIFFICULTY_LABELS = {
    "stamina": "體力", "distance": "起始距離", "struggle_pct": "掙扎機率（%）", "surge_pct": "衝刺機率（%）",
    "strong_pct": "強力掙扎機率（%）", "chain_pct": "連續衝刺機率（%）",
}


# ---------- 釣具 ----------


def validate_gear_effects(kind: str, raw) -> dict:
    if kind not in GEAR_KINDS:
        raise ValidationError("釣具類型必須是魚竿、魚線或魚餌。")
    if not isinstance(raw, dict):
        raise ValidationError("釣具效果格式不正確。")
    limits = GEAR_EFFECT_LIMITS[kind]
    unknown = set(raw) - set(limits)
    if unknown:
        raise ValidationError(f"{GEAR_KIND_NAMES[kind]}沒有這些效果欄位：{'、'.join(sorted(unknown))}。")
    return {k: _int(raw.get(k), GEAR_EFFECT_NAMES[k], low, high) for k, (low, high) in limits.items()}


def gear_effects_text(kind: str, effects: dict) -> str:
    if kind == "rod":
        parts = [f"收線力 {effects.get('reel_power', 0)}"]
        if effects.get("control_pct"):
            parts.append(f"魚的衝擊張力 -{effects['control_pct']}%")
        if effects.get("reaction_bonus_ms"):
            parts.append(f"拉竿時間 +{effects['reaction_bonus_ms'] / 1000:g} 秒")
        return "、".join(parts)
    if kind == "line":
        return f"最大張力 {effects.get('max_tension', BASE_MAX_TENSION)}"
    parts = []
    for r in ("rare", "epic", "legendary"):
        if effects.get(f"{r}_bp"):
            parts.append(f"{RARITY_EMOJI[r]}{RARITY_NAMES[r]} +{effects[f'{r}_bp'] / 100:g}%")
    if effects.get("bite_speed_pct"):
        parts.append(f"上鉤快 {effects['bite_speed_pct']}%")
    return "、".join(parts) or "一般魚餌，沒有額外效果"


# ---------- 魚種 ----------


def validate_fish(raw: dict) -> dict:
    """魚種數值：售價由管理員自由設定（不受稀有度限制，只要求最低不高於最高）；重量以公克儲存。"""
    rarity = raw.get("rarity")
    if rarity not in RARITIES:
        raise ValidationError("稀有度必須是普通、稀有、史詩或傳說。")
    min_price = _int(raw.get("min_price"), "最低售價", PRICE_MIN, PRICE_MAX)
    max_price = _int(raw.get("max_price"), "最高售價", PRICE_MIN, PRICE_MAX)
    if min_price > max_price:
        raise ValidationError("最低售價不可高於最高售價。")
    min_w = _int(raw.get("min_weight_g"), "最小重量（公克）", *FISH_WEIGHT_RANGE_G)
    max_w = _int(raw.get("max_weight_g"), "最大重量（公克）", *FISH_WEIGHT_RANGE_G)
    if min_w > max_w:
        raise ValidationError("最小重量不可大於最大重量。")
    return {
        "rarity": rarity, "min_price": min_price, "max_price": max_price, "min_weight_g": min_w,
        "max_weight_g": max_w, "difficulty": _int(raw.get("difficulty"), "掙扎難度", *FISH_DIFFICULTY_RANGE),
    }


def roll_rarity(rarity_bp: dict, bait: dict, rng: Callable[[], float]) -> str:
    """依設定的稀有度機率（加上魚餌加成，從普通扣除）抽稀有度。"""
    weights = {r: rarity_bp[r] + (bait.get(f"{r}_bp", 0) if r != "common" else 0) for r in RARITIES}
    extra = sum(weights[r] - rarity_bp[r] for r in RARITIES)
    weights["common"] = max(0, rarity_bp["common"] - extra)
    total = sum(weights.values())
    point = rng() * total
    for r in RARITIES:
        point -= weights[r]
        if point < 0:
            return r
    return "common"


def rarity_chances(rarity_bp: dict, bait: dict) -> dict[str, float]:
    """顯示用：套用魚餌後各稀有度的實際機率（%）。"""
    weights = {r: rarity_bp[r] + (bait.get(f"{r}_bp", 0) if r != "common" else 0) for r in RARITIES}
    weights["common"] = max(0, rarity_bp["common"] - sum(weights[r] - rarity_bp[r] for r in RARITIES[1:]))
    total = sum(weights.values()) or 1
    return {r: weights[r] * 100 / total for r in RARITIES}


def pick_fish(fish_by_rarity: dict[str, list], rarity: str, rng: Callable[[], float]):
    """在抽到的稀有度中平均挑一種魚；該稀有度沒有啟用中的魚時，往較低的稀有度找。"""
    order = RARITIES[: RARITIES.index(rarity) + 1][::-1] + RARITIES[RARITIES.index(rarity) + 1:]
    for r in order:
        pool = fish_by_rarity.get(r) or []
        if pool:
            return pool[min(len(pool) - 1, int(rng() * len(pool)))]
    return None


def roll_weight(min_g: int, max_g: int, rng: Callable[[], float]) -> int:
    """重量偏向較輕（大魚比較少見）。"""
    return min_g + round((max_g - min_g) * rng() ** 1.6)


def fish_price(min_price: int, max_price: int, min_g: int, max_g: int, weight_g: int) -> int:
    """售價依重量在魚種的價格區間內線性換算（越重越值錢），不會超出區間。"""
    if max_g <= min_g:
        return max_price
    ratio = min(1.0, max(0.0, (weight_g - min_g) / (max_g - min_g)))
    return min_price + round((max_price - min_price) * ratio)


def weight_text(weight_g: int) -> str:
    return f"{weight_g / 1000:.2f} kg" if weight_g >= 1000 else f"{weight_g} g"


# ---------- 拉扯博弈 ----------


def bite_wait_seconds(settings: dict, bait: dict, rng: Callable[[], float]) -> float:
    low, high = settings["bite_wait_min_seconds"], settings["bite_wait_max_seconds"]
    wait = low + (high - low) * rng()
    return max(1.0, wait * (100 - bait.get("bite_speed_pct", 0)) / 100)


def reaction_seconds(settings: dict, rod: dict) -> float:
    return settings["reaction_seconds"] + rod.get("reaction_bonus_ms", 0) / 1000


def new_fight(settings: dict, rarity: str, fish_difficulty: int, line: dict, rng: Callable[[], float]) -> dict:
    """拋竿時建立拉扯初始狀態；基礎成功率沒通過時記下脫鉤的回合（與稀有度、裝備無關）。"""
    diff = settings["difficulty"][rarity]
    stamina = max(1, round(diff["stamina"] * FISH_DIFFICULTY_STAMINA.get(fish_difficulty, 1.0)))
    slip_turn = None
    if rng() * 100 >= settings["base_success_pct"]:
        slip_turn = SLIP_TURNS[min(len(SLIP_TURNS) - 1, int(rng() * len(SLIP_TURNS)))]
    state = {
        "stamina": stamina, "max_stamina": stamina, "distance": diff["distance"], "tension": START_TENSION,
        "max_tension": line.get("max_tension", BASE_MAX_TENSION), "turn": 0, "slip_turn": slip_turn,
        "intent": "calm", "last": None,
    }
    state["intent"] = next_intent(state, diff, None, rng)
    return state


def next_intent(state: dict, diff: dict, previous: str | None, rng: Callable[[], float]) -> str:
    if state["stamina"] <= 0:
        return "tired"
    if previous in ("surge", "strong") and rng() * 100 < diff["chain_pct"]:
        return previous  # 連續衝刺
    scale = 0.5 if state["stamina"] < state["max_stamina"] * TIRED_RATIO else 1.0
    point = rng() * 100
    for intent, key in (("strong", "strong_pct"), ("surge", "surge_pct"), ("struggle", "struggle_pct")):
        point -= diff[key] * scale
        if point < 0:
            return intent
    return "calm"


def _clamp(value: float, low: float, high: float) -> int:
    return int(round(min(high, max(low, value))))


def fish_spike(intent: str, rod: dict) -> float:
    """魚的動作造成的張力增加（魚竿穩定度降低衝擊）。"""
    t = FISH_INTENTS[intent]["tension"]
    return t * (100 - rod.get("control_pct", 0)) / 100 if t > 0 else t


def step(state: dict, action: str, diff: dict, rod: dict, rng: Callable[[], float]) -> tuple[dict, str | None]:
    """執行一次拉扯操作，回傳 (新狀態, 結果)；結果 None 表示繼續，否則為 landed／escaped／line_broken／slipped。

    順序：脫鉤判定 → 玩家操作（距離歸零即釣起）→ 魚依事先顯示的動作反應 → 斷線／逃走判定 → 決定魚的下一個動作。"""
    if action not in ACTIONS:
        raise ValidationError("沒有這個操作。")
    s = dict(state)
    s["turn"] += 1
    intent = s["intent"]
    if s.get("slip_turn") and s["turn"] >= s["slip_turn"]:
        s["last"] = {"action": action, "intent": intent, "text": "🪝 魚鉤沒有咬牢，魚掙脫逃走了！"}
        return s, "slipped"
    p = PLAYER_ACTIONS[action]
    ratio = s["stamina"] / s["max_stamina"] if s["max_stamina"] else 0
    pulled = rod.get("reel_power", 10) if action == "reel" else 0
    s["distance"] = s["distance"] - pulled + p["distance"]
    s["tension"] = max(0, s["tension"] + p["tension"])
    s["stamina"] = _clamp(s["stamina"] + p["stamina"], 0, s["max_stamina"])
    if s["distance"] <= 0:
        s["distance"] = 0
        s["last"] = {"action": action, "intent": intent, "text": "🎉 收線成功，把魚拉出水面了！"}
        return s, "landed"
    f = FISH_INTENTS[intent]
    spike = fish_spike(intent, rod)
    if spike > 0:
        spike *= p["spike_mul"]
    s["tension"] = _clamp(s["tension"] + spike, 0, 999)
    s["distance"] = _clamp(s["distance"] + f["distance"] * p["dash_mul"] * (0.5 + 0.5 * ratio), 0, ESCAPE_DISTANCE)
    s["stamina"] = _clamp(s["stamina"] + f["stamina"], 0, s["max_stamina"])
    s["last"] = {"action": action, "intent": intent, "text": ACTION_RESULT_TEXT[action] + "　" + INTENT_RESULT[intent]}
    if s["tension"] > s["max_tension"]:
        s["last"]["text"] = "💥 張力太大，魚線斷了！"
        return s, "line_broken"
    if s["distance"] >= ESCAPE_DISTANCE:
        s["last"]["text"] = "🌊 魚游得太遠，掙脫逃走了！"
        return s, "escaped"
    s["intent"] = next_intent(s, diff, intent, rng)
    return s, None


ACTION_RESULT_TEXT = {"reel": "🎣 你用力收線。", "release": "↩️ 你放出一些線。", "wait": "⏳ 你穩住魚竿觀察。"}
INTENT_RESULT = {
    "calm": "魚游得很平穩。", "struggle": "魚左右掙扎！", "surge": "魚突然衝刺！", "strong": "魚強力衝刺！",
    "tired": "魚累得游不動了。",
}


# ---------- 模擬（測試與調整數值用） ----------


def smart_action(state: dict, diff: dict, rod: dict, margin: int = 4) -> str:
    """依提示操作的玩家：收線不會斷線或讓魚逃走時收線，否則等待，最後才放線。"""
    intent, t, d, mt = state["intent"], state["tension"], state["distance"], state["max_tension"]
    spike = max(0.0, fish_spike(intent, rod))
    ratio = state["stamina"] / state["max_stamina"] if state["max_stamina"] else 0
    dash = FISH_INTENTS[intent]["distance"] * (0.5 + 0.5 * ratio)
    reel = rod.get("reel_power", 10)
    if d - reel <= 0:
        return "reel"
    if t + 14 + spike <= mt - margin and d - reel + dash < ESCAPE_DISTANCE:
        return "reel"
    if t - 12 + spike * 0.8 <= mt - margin and d + dash * 0.5 < ESCAPE_DISTANCE:
        return "wait"
    return "release"


def naive_action(state: dict, diff: dict, rod: dict) -> str:
    """不看提示的玩家：一直收線，張力超過七成才放線。"""
    return "release" if state["tension"] > state["max_tension"] * 0.7 else "reel"


def simulate(settings: dict, rarity: str, rod: dict, line: dict, policy: Callable, rng: Callable[[], float],
             fish_difficulty: int = 3, max_turns: int = 200) -> tuple[str, int]:
    """模擬一次拉扯（假設玩家及時拉竿），回傳 (結果, 操作次數)。"""
    diff = settings["difficulty"][rarity]
    state = new_fight(settings, rarity, fish_difficulty, line, rng)
    for _ in range(max_turns):
        state, result = step(state, policy(state, diff, rod), diff, rod, rng)
        if result:
            return result, state["turn"]
    return "timeout", state["turn"]


def success_rate(settings: dict, rarity: str, rod: dict, line: dict, policy: Callable, rng: Callable[[], float],
                 trials: int = 2000, fish_difficulty: int = 3) -> tuple[float, float]:
    """回傳 (成功率, 平均操作次數)。"""
    wins, turns = 0, 0
    for _ in range(trials):
        result, n = simulate(settings, rarity, rod, line, policy, rng, fish_difficulty)
        wins += result == "landed"
        turns += n
    return wins / trials, turns / trials


def estimated_seconds(settings: dict, turns: float, seconds_per_turn: float = 2.0) -> float:
    """估計一次釣魚的總時間（等待上鉤 + 反應 + 拉扯）。"""
    wait = (settings["bite_wait_min_seconds"] + settings["bite_wait_max_seconds"]) / 2
    return wait + 1.5 + math.ceil(turns) * seconds_per_turn
