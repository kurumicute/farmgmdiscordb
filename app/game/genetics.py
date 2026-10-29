"""作物品質與突變的純規則：設定驗證、機率加成、抽取、售價與生長時間。

所有公式只在這裡定義，其他模組一律呼叫這些函式，避免重複套用倍率。
- 機率以萬分比（bp）整數表示，總和固定為 10000。
- 倍率以百分比整數表示；售價計算全程使用整數，最後無條件捨去（floor）到整數金幣。
"""

from app.game import balance, rules
from app.game.errors import ValidationError

QUALITY_ORDER = tuple(balance.QUALITIES)  # normal, good, rare, legendary
MUTATION_ORDER = ("giant", "golden", "fast", "variant")
TOTAL_BP = 10_000

# 設定的合法範圍
QUALITY_PRICE_RANGE = (50, 1000)
MUTATION_BP_RANGE = (0, 2000)
MUTATION_TOTAL_MAX_BP = 5000
GIANT_RANGE = (1, 5)
GOLDEN_PCT_RANGE = (100, 1000)
FAST_REDUCTION_RANGE = (0, 7 * 24 * 3600)
VARIANT_PCT_RANGE = (100, 2000)
LEVEL_BONUS_RANGE = (0, 200)
LEVEL_BONUS_MAX_RANGE = (0, 2000)


def _int(value, label: str, low: int, high: int, errors: list[str]) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label}必須是整數。")
        return 0
    if not low <= value <= high:
        errors.append(f"{label}必須在 {low}–{high} 之間（目前 {value}）。")
    return value


def validate_settings(raw: dict) -> dict:
    """驗證並回傳正規化的設定；任何錯誤都會一次列出並拒絕套用。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ValidationError("設定格式不正確。")
    quality_bp = raw.get("quality_bp") or {}
    quality_pct = raw.get("quality_price_pct") or {}
    mutation_bp = raw.get("mutation_bp") or {}
    settings = {
        "quality_bp": {
            q: _int(quality_bp.get(q), f"{balance.QUALITIES[q]}機率", 0, TOTAL_BP, errors) for q in QUALITY_ORDER
        },
        "quality_price_pct": {
            q: _int(quality_pct.get(q), f"{balance.QUALITIES[q]}售價倍率", *QUALITY_PRICE_RANGE, errors)
            for q in QUALITY_ORDER
        },
        "mutation_bp": {
            m: _int(mutation_bp.get(m), f"{balance.MUTATIONS[m]}機率", *MUTATION_BP_RANGE, errors)
            for m in MUTATION_ORDER
        },
        "giant_multiplier": _int(raw.get("giant_multiplier"), "巨型作物數量倍率", *GIANT_RANGE, errors),
        "golden_price_pct": _int(raw.get("golden_price_pct"), "黃金作物售價倍率", *GOLDEN_PCT_RANGE, errors),
        "fast_reduction_seconds": _int(
            raw.get("fast_reduction_seconds"), "快速生長減少秒數", *FAST_REDUCTION_RANGE, errors
        ),
        "variant_price_pct": _int(raw.get("variant_price_pct"), "珍稀變種售價倍率", *VARIANT_PCT_RANGE, errors),
        "level_bonus_bp": _int(raw.get("level_bonus_bp"), "每級品質加成", *LEVEL_BONUS_RANGE, errors),
        "level_bonus_max_bp": _int(raw.get("level_bonus_max_bp"), "品質加成上限", *LEVEL_BONUS_MAX_RANGE, errors),
    }
    if not errors:
        total = sum(settings["quality_bp"].values())
        if total != TOTAL_BP:
            errors.append(f"品質機率總和必須為 100%（目前 {total / 100:g}%）。")
        if settings["quality_bp"]["normal"] < settings["level_bonus_max_bp"]:
            errors.append("普通品質機率不可低於品質加成上限，否則加成後會出現負機率。")
        if sum(settings["quality_bp"][q] for q in QUALITY_ORDER[1:]) == 0 and settings["level_bonus_max_bp"] > 0:
            errors.append("優良／稀有／傳說機率全為 0 時，品質加成上限必須為 0。")
        mutation_total = sum(settings["mutation_bp"].values())
        if mutation_total > MUTATION_TOTAL_MAX_BP:
            errors.append(f"突變機率總和最多 {MUTATION_TOTAL_MAX_BP / 100:g}%（目前 {mutation_total / 100:g}%）。")
    if errors:
        raise ValidationError("設定無效：" + "；".join(errors))
    return settings


def level_bonus_bp(settings: dict, farm_level: int) -> int:
    """農場等級加成：每高於 1 級加 level_bonus_bp，上限 level_bonus_max_bp。"""
    return min(settings["level_bonus_max_bp"], settings["level_bonus_bp"] * max(0, farm_level - 1))


def effective_quality_bp(settings: dict, farm_level: int, event_bonus_bp: int = 0) -> dict[str, int]:
    """把加成（等級＋限時事件）從「普通」依其他品質的基礎機率比例重新分配；結果總和恆為 10000。

    加成總和不超過普通品質的基礎機率，因此不會出現負機率。
    """
    base = settings["quality_bp"]
    bonus = min(base["normal"], level_bonus_bp(settings, farm_level) + max(0, event_bonus_bp))
    others = QUALITY_ORDER[1:]
    others_total = sum(base[q] for q in others)
    result = dict(base)
    if bonus <= 0 or others_total == 0:
        return result
    result["normal"] = base["normal"] - bonus
    given = 0
    for q in others:
        share = bonus * base[q] // others_total
        result[q] = base[q] + share
        given += share
    result[others[0]] += bonus - given  # 捨去的餘數給第一個較高品質，維持總和
    return result


def roll_quality(quality_bp: dict[str, int], u: float) -> str:
    """u ∈ [0, 1)；依 QUALITY_ORDER 切分區間（普通在最前面）。"""
    point = int(u * TOTAL_BP)
    cumulative = 0
    for q in QUALITY_ORDER:
        cumulative += quality_bp[q]
        if point < cumulative:
            return q
    return QUALITY_ORDER[-1]


def effective_mutation_bp(settings: dict, event_bonus_bp: int = 0) -> dict[str, int]:
    """限時事件的突變加成依各突變的基礎機率比例分配；總和不超過 10000。基礎機率全為 0 時不加成。"""
    base = settings["mutation_bp"]
    total = sum(base.values())
    bonus = min(max(0, event_bonus_bp), TOTAL_BP - total)
    result = dict(base)
    if bonus <= 0 or total == 0:
        return result
    given = 0
    for m in MUTATION_ORDER:
        share = bonus * base[m] // total
        result[m] += share
        given += share
    first = next(m for m in MUTATION_ORDER if base[m] > 0)
    result[first] += bonus - given  # 捨去的餘數給第一個有機率的突變，維持總和
    return result


def roll_mutation(settings: dict, u: float, event_bonus_bp: int = 0) -> str:
    """u ∈ [0, 1)；突變區間放在最上方 [1 - 總機率, 1)，每批最多一個主要突變。"""
    mutation_bp = effective_mutation_bp(settings, event_bonus_bp)
    point = int(u * TOTAL_BP)
    start = TOTAL_BP - sum(mutation_bp.values())
    if point < start:
        return "none"
    cumulative = start
    for m in MUTATION_ORDER:
        cumulative += mutation_bp[m]
        if point < cumulative:
            return m
    return MUTATION_ORDER[-1]


def harvest_quantity(base_quantity: int, mutation: str, settings: dict, event_bonus_pct: int = 0) -> int:
    """收成數量：巨型作物先乘倍率，再套用限時事件加成（無條件捨去，至少為原數量）。"""
    quantity = base_quantity * settings["giant_multiplier"] if mutation == "giant" else base_quantity
    return quantity * (100 + max(0, event_bonus_pct)) // 100


def grow_seconds(
    base_seconds: int, mutation: str, earthworm_active: bool, settings: dict, event_reduction_pct: int = 0
) -> int:
    """生長時間依序套用：蚯蚓 → 限時事件（減少 %）→ 快速生長（減少固定秒數）→ 最短時間。不會變成 0 或負數。"""
    seconds = rules.grow_seconds_with_effects(base_seconds, earthworm_active)
    # 加速後的下限：最短生長時間；若蚯蚓後已低於最短時間，事件不會反而拉長時間
    floor = min(balance.MIN_GROW_SECONDS, seconds)
    if event_reduction_pct > 0:
        seconds = max(floor, seconds * (100 - min(event_reduction_pct, 100)) // 100)
    if mutation == "fast":
        seconds = max(balance.MIN_GROW_SECONDS, seconds - settings["fast_reduction_seconds"])
    return seconds


def base_price(crop_sell_price: int, mutation: str, settings: dict) -> int:
    """基本售價：珍稀變種是獨立作物，基本售價 = 原作物售價 × 變種倍率（捨去）。"""
    if mutation == "variant":
        return crop_sell_price * settings["variant_price_pct"] // 100
    return crop_sell_price


def mutation_price_pct(mutation: str, settings: dict) -> int:
    """突變售價倍率：只有黃金作物影響單價；巨型影響數量、快速生長影響時間、變種已反映在基本售價。"""
    return settings["golden_price_pct"] if mutation == "golden" else 100


def unit_price(crop_sell_price: int, quality: str, mutation: str, settings: dict) -> int:
    """最終單價 = 基本售價 × 品質倍率 × 突變售價倍率，整數運算後無條件捨去。"""
    base = base_price(crop_sell_price, mutation, settings)
    return base * settings["quality_price_pct"][quality] * mutation_price_pct(mutation, settings) // 10_000


def produce_code(crop, mutation: str) -> str:
    """收成後的農產品代碼：珍稀變種使用作物設定的獨立代碼（crop 需有 code、variant_code）。"""
    return crop.variant_code if mutation == "variant" else crop.code


def produce_name(crop, mutation: str | None) -> str:
    return crop.variant_name if mutation == "variant" else crop.name


def parse_quality(token: str | None) -> str | None:
    """接受代碼或中文名稱（例如 rare／稀有）；無法辨識時回傳 None。"""
    if token is None:
        return None
    token = token.strip().lower()
    for code, name in balance.QUALITIES.items():
        if token in (code, name):
            return code
    return None


def parse_mutation(token: str | None) -> str | None:
    """接受代碼、中文名稱或前兩字（例如 golden／黃金作物／黃金）；無法辨識時回傳 None。"""
    if token is None:
        return None
    token = token.strip().lower()
    for code, name in balance.MUTATIONS.items():
        if token in (code, name, name[:2]):
            return code
    return None
