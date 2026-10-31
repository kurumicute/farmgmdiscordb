"""山洞冒險：探索區域、掉落表、特殊事件、機器配方與等級門檻（純規則，不碰資料庫）。

探索的機率、數量與解鎖等級全部集中在本檔案；調整數值只改這裡（Migration 只寫死區域代碼）。
機率單位為萬分比（bp，10000 = 100%），亂數由呼叫端注入，方便測試。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

TOTAL_BP = 10_000

# ---------- 素材與特殊道具（存在 inventory_items，但放在獨立的素材背包，不佔倉庫空間；不能在商店購買或販售） ----------
MATERIAL_BAG_CAPACITY = 500  # 素材背包上限（下列素材與假幣的總數）
# code: (名稱, 稀有度, 用途)
MATERIALS = {
    "wood": ("木材", "普通", "機器製作"),
    "stone": ("石塊", "普通", "機器製作"),
    "copper_ore": ("銅礦", "普通", "機器製作"),
    "iron_ore": ("鐵礦", "稀有", "機器製作"),
    "resin": ("樹脂", "普通", "機器製作"),
    "herb": ("藥草", "普通", "作物培育"),
    "gear": ("齒輪", "稀有", "自動化機器"),
    "crystal": ("水晶", "稀有", "稀有作物培育"),
    "ancient_core": ("古木核心", "極稀有", "高級機器製作"),
    "machine_core": ("機械核心", "極稀有", "高級機器製作"),
    "fake_coin": ("假幣", "特殊", "兌換金幣"),
}
FAKE_COIN = "fake_coin"
FAKE_COIN_VALUE = 100  # 1 個假幣 = 100 金幣
FAKE_COIN_MAX_EXCHANGE = 9999

# 稀有種子是作物種子（crop_types.shop_buyable = false，商店不販售，只能探索或培育取得）
# code, 名稱, 種子參考價, 生長秒數, 收成數量, 單位售價, 變種代碼, 變種名稱（Migration 0014 寫入相同數值）
RARE_CROPS = [
    ("amber_melon", "琥珀瓜", 200, 12 * 3600, 3, 120, "golden_amber_melon", "黃金琥珀瓜"),
    ("moonflower", "月光花", 500, 24 * 3600, 2, 450, "starflower", "星辰花"),
]
AMBER_SEED = "seed_amber_melon"
MOON_SEED = "seed_moonflower"

# ---------- 獎勵類型機率 ----------
REWARD_TYPES = ("common", "rare", "seed", "fake_coin", "event")
REWARD_TYPE_LABELS = {"common": "普通素材", "rare": "稀有素材", "seed": "稀有種子", "fake_coin": "假幣",
                      "event": "特殊事件"}
REWARD_TYPE_BP = {
    "normal": {"common": 6500, "rare": 2000, "seed": 800, "fake_coin": 500, "event": 200},
    "advanced": {"common": 4500, "rare": 3000, "seed": 1200, "fake_coin": 800, "event": 500},
}
# 抽到的類型在目前等級沒有可掉落的物品時，依序改用下列類型（例如礦坑沒有種子 → 稀有素材）
FALLBACK = {"seed": "rare", "rare": "common", "event": "common", "fake_coin": "common", "common": "common"}

FULL_POOL_LEVEL = 16  # Lv.16：三個區域都改用高級探索（完整獎勵池）


@dataclass(frozen=True)
class Drop:
    item: str
    min_level: int
    weight: int
    qty_min: int
    qty_max: int


@dataclass(frozen=True)
class Event:
    code: str
    name: str
    description: str
    min_level: int
    weight: int
    fake_coins: tuple[int, int] = (0, 0)
    bonus: tuple[str, ...] = ()  # 額外抽幾次的獎勵類型（例如 ("rare",) = 再抽 1 個稀有素材）


@dataclass(frozen=True)
class Area:
    code: str
    emoji: str
    name: str
    description: str
    unlock_level: int
    duration_seconds: int  # 探險時間：出發後經過這段時間才回來，期間不能去其他區域
    rolls: int  # 每次探索抽幾次獎勵
    advanced_level: int  # 達到此等級改用高級探索機率
    level_notes: tuple[tuple[int, str], ...]  # 介面顯示的等級解鎖說明
    pools: dict[str, tuple[Drop, ...]] = field(default_factory=dict)
    events: tuple[Event, ...] = ()


def _chest(fake: tuple[int, int]) -> Event:
    return Event("treasure_chest", "🎁 特殊寶箱", "發現一個上鎖的寶箱，裡面有假幣與稀有素材！", 1, 6, fake, ("rare",))


AREAS: dict[str, Area] = {
    "grassland": Area(
        "grassland", "🌿", "青青草原", "微風吹拂的草原，適合收集木材、石塊與藥草，偶爾能撿到琥珀種子。",
        unlock_level=1, duration_seconds=8 * 3600, rolls=3, advanced_level=FULL_POOL_LEVEL,
        level_notes=((2, "草原進階素材：銅礦"), (4, "草原稀有發現：鐵礦、稀有發現事件"),
                     (15, "極稀有探索事件"), (16, "完整獎勵池（高級探索）")),
        pools={
            "common": (Drop("wood", 1, 3, 2, 4), Drop("stone", 1, 3, 2, 4), Drop("herb", 1, 2, 1, 3),
                       Drop("copper_ore", 2, 1, 1, 2)),
            "rare": (Drop("iron_ore", 4, 1, 1, 2),),
            "seed": (Drop(AMBER_SEED, 1, 1, 1, 1),),
            "fake_coin": (Drop(FAKE_COIN, 1, 1, 1, 2),),
        },
        events=(
            _chest((2, 4)),
            Event("rare_find", "🔍 草原稀有發現", "在草叢深處發現一處隱密的寶地。", 4, 3, (1, 1), ("rare", "seed")),
            Event("ancient_ruins", "🏛️ 古代遺跡", "草原下竟然埋著古代遺跡！", 15, 1, (5, 10), ("rare", "rare")),
        ),
    ),
    "forest": Area(
        "forest", "🌲", "古老森林", "千年古樹林立的森林，盛產樹脂與齒輪，深處藏著古木核心與月光種子。",
        unlock_level=5, duration_seconds=16 * 3600, rolls=4, advanced_level=9,
        level_notes=((6, "森林進階素材：鐵礦"), (7, "森林稀有發現：古木核心、稀有發現事件"),
                     (9, "森林高級探索獎勵"), (14, "高級機器素材：古木核心機率提高"), (15, "極稀有探索事件")),
        pools={
            "common": (Drop("wood", 5, 3, 3, 5), Drop("resin", 5, 3, 2, 4), Drop("herb", 5, 2, 2, 3),
                       Drop("iron_ore", 6, 1, 1, 2)),
            "rare": (Drop("gear", 5, 3, 1, 2), Drop("ancient_core", 7, 1, 1, 1), Drop("ancient_core", 14, 1, 1, 1)),
            "seed": (Drop(MOON_SEED, 5, 1, 1, 1),),
            "fake_coin": (Drop(FAKE_COIN, 5, 1, 1, 3),),
        },
        events=(
            _chest((2, 5)),
            Event("rare_find", "🔍 森林稀有發現", "在古樹樹洞裡找到前人留下的寶藏。", 7, 3, (1, 2), ("rare", "seed")),
            Event("ancient_ruins", "🏛️ 森林古代遺跡", "藤蔓後方出現一座古代神殿！", 15, 1, (5, 10), ("rare", "rare")),
        ),
    ),
    "mine": Area(
        "mine", "⛏️", "神秘礦坑", "深不見底的礦坑，有銅礦、鐵礦與水晶，運氣好能挖到機械核心。",
        unlock_level=10, duration_seconds=24 * 3600, rolls=5, advanced_level=FULL_POOL_LEVEL,
        level_notes=((11, "礦坑進階素材：鐵礦"), (13, "礦坑稀有發現：機械核心機率提高、稀有發現事件"),
                     (14, "高級機器素材：機械核心機率再提高"), (15, "極稀有探索事件"),
                     (16, "完整獎勵池（高級探索）")),
        pools={
            "common": (Drop("stone", 10, 3, 3, 5), Drop("copper_ore", 10, 3, 2, 4), Drop("iron_ore", 11, 2, 1, 3)),
            "rare": (Drop("iron_ore", 10, 2, 1, 2), Drop("gear", 10, 2, 1, 2), Drop("crystal", 10, 2, 1, 2),
                     Drop("machine_core", 10, 1, 1, 1), Drop("machine_core", 13, 1, 1, 1),
                     Drop("machine_core", 14, 1, 1, 1)),
            "seed": (),
            "fake_coin": (Drop(FAKE_COIN, 10, 1, 2, 4),),
        },
        events=(
            _chest((3, 6)),
            Event("rare_find", "🔍 礦坑稀有發現", "鎬子敲開一條閃閃發光的礦脈！", 13, 3, (1, 2), ("rare", "rare")),
            Event("ancient_ruins", "🏛️ 地底遺跡", "礦坑最深處通往一座失落的地底城市！", 15, 1, (5, 10),
                  ("rare", "rare", "rare")),
        ),
    ),
}
AREA_ORDER = ("grassland", "forest", "mine")


# ---------- 機器（存在 farm_tools；製作一次永久持有，可啟用／停用） ----------
@dataclass(frozen=True)
class Machine:
    code: str
    emoji: str
    name: str
    unlock_level: int
    materials: dict[str, int]
    description: str


MACHINES: dict[str, Machine] = {
    "seed_maker": Machine("seed_maker", "🌱", "種子製造機", 3, {"wood": 10, "stone": 10, "copper_ore": 5, "gear": 3},
                          "把收成的作物做成種子：1 個作物 = 2 顆對應種子"),
    "auto_harvester": Machine("auto_harvester", "🚜", "自動收割機", 5, {"wood": 20, "iron_ore": 10, "gear": 5},
                              "開啟面板時自動收成成熟的作物（含成熟後才乾枯的作物）"),
    "auto_processor": Machine("auto_processor", "🏭", "自動加工機", 8,
                              {"stone": 15, "copper_ore": 15, "iron_ore": 10, "machine_core": 1},
                              "開啟面板時自動領取完成的加工品，並用背包原料開始設定的加工項目"),
    "rare_incubator": Machine("rare_incubator", "🧬", "稀有作物培育器", 12,
                              {"wood": 20, "herb": 15, "crystal": 5, "ancient_core": 1},
                              "用普通作物與探索素材培育稀有作物種子"),
}
MACHINE_ORDER = ("seed_maker", "auto_harvester", "auto_processor", "rare_incubator")

# 種子製造機：1 個作物 = 2 顆種子；只接受下列品質與突變（稀有作物是否可製造種子在此設定）
SEED_MAKER_RATIO = 2
SEED_MAKER_QUALITIES = ("normal",)
SEED_MAKER_MUTATIONS = ("none",)
SEED_MAKER_RARE_CROPS = False  # True：琥珀瓜、月光花等稀有作物也能製造種子
SEED_MAKER_MAX = 999

# 稀有作物培育器的配方（以加工訂單 craft_orders 執行，building_code = rare_incubator）
# code: (名稱, {原料: 每份數量}, 成品代碼, 每份產量, 每份秒數, 別名)
INCUBATOR_RECIPES = {
    "incubate_amber": ("琥珀瓜種子培育", {"wheat": 10, "herb": 3}, AMBER_SEED, 1, 4 * 3600, ("琥珀瓜", "琥珀")),
    "incubate_moon": ("月光花種子培育", {"strawberry": 5, "herb": 5, "crystal": 1}, MOON_SEED, 1, 8 * 3600,
                      ("月光花", "月光")),
}
INCUBATOR_MAX_QUANTITY = 5


# ---------- 等級解鎖總覽（介面顯示用；實際判斷以上面的 unlock_level／min_level 為準） ----------
LEVEL_UNLOCKS = (
    (1, "青青草原"), (2, "草原進階素材"), (3, "種子製造機"), (4, "草原稀有發現"), (5, "古老森林、自動收割機"),
    (6, "森林進階素材"), (7, "森林稀有發現"), (8, "自動加工機"), (9, "森林高級探索獎勵"), (10, "神秘礦坑"),
    (11, "礦坑進階素材"), (12, "稀有作物培育器"), (13, "礦坑稀有發現"), (14, "高級機器素材"),
    (15, "極稀有探索事件"), (16, "三個區域完整探索獎勵池"),
)


# ---------- 規則 ----------
def find_area(key: str) -> str | None:
    key = (key or "").strip()
    for code, area in AREAS.items():
        if key.lower() == code or key == area.name:
            return code
    return None


def is_advanced(area: Area, level: int) -> bool:
    return level >= area.advanced_level


def eligible(pool: tuple[Drop, ...], level: int) -> list[Drop]:
    return [d for d in pool if level >= d.min_level and d.weight > 0]


def possible_items(area: Area, level: int | None = None) -> set[str]:
    """這個區域可能掉落的所有物品代碼（level=None 表示所有等級）。"""
    lv = 10_000 if level is None else level
    items = {d.item for pool in area.pools.values() for d in eligible(pool, lv)}
    if any(e.fake_coins[1] > 0 for e in area.events if lv >= e.min_level):
        items.add(FAKE_COIN)
    return items


def _weighted(entries: list, u: float):
    total = sum(e.weight for e in entries)
    point = u * total
    cumulative = 0
    for e in entries:
        cumulative += e.weight
        if point < cumulative:
            return e
    return entries[-1]


def roll_type(bp: dict[str, int], u: float) -> str:
    point = int(u * TOTAL_BP)
    cumulative = 0
    for t in REWARD_TYPES:
        cumulative += bp[t]
        if point < cumulative:
            return t
    return REWARD_TYPES[-1]


@dataclass
class Result:
    area: str
    advanced: bool
    items: dict[str, int] = field(default_factory=dict)  # 物品代碼 → 數量（依取得順序）
    events: list[str] = field(default_factory=list)  # 觸發的特殊事件名稱
    types: list[str] = field(default_factory=list)  # 每次抽到（含改用）的獎勵類型

    def add(self, item: str, qty: int) -> None:
        if qty > 0:
            self.items[item] = self.items.get(item, 0) + qty

    @property
    def total(self) -> int:
        return sum(self.items.values())


def _qty(drop: Drop, rng: Callable[[], float]) -> int:
    return drop.qty_min + min(drop.qty_max - drop.qty_min, int(rng() * (drop.qty_max - drop.qty_min + 1)))


def _roll_item(area: Area, kind: str, level: int, rng: Callable[[], float], result: Result) -> None:
    """抽一個指定類型的物品；目前等級沒有可掉落的物品時依 FALLBACK 改用其他類型。"""
    seen = set()
    while kind not in seen:
        seen.add(kind)
        entries = eligible(area.pools.get(kind, ()), level)
        if entries:
            drop = _weighted(entries, rng())
            result.add(drop.item, _qty(drop, rng))
            result.types.append(kind)
            return
        kind = FALLBACK[kind]


def explore(area_code: str, level: int, rng: Callable[[], float]) -> Result:
    """產生一次探索的全部獎勵（不檢查解鎖與是否正在探險，由服務層驗證）。"""
    area = AREAS[area_code]
    advanced = is_advanced(area, level)
    bp = REWARD_TYPE_BP["advanced" if advanced else "normal"]
    result = Result(area_code, advanced)
    for _ in range(area.rolls):
        kind = roll_type(bp, rng())
        if kind == "event":
            events = [e for e in area.events if level >= e.min_level]
            if events:
                event = _weighted(events, rng())
                result.events.append(event.name)
                result.types.append("event")
                low, high = event.fake_coins
                if high > 0:
                    result.add(FAKE_COIN, low + min(high - low, int(rng() * (high - low + 1))))
                for bonus in event.bonus:
                    _roll_item(area, bonus, level, rng, result)
                continue
            kind = FALLBACK["event"]
        _roll_item(area, kind, level, rng, result)
    return result


def missing_materials(machine: Machine, have: dict[str, int]) -> dict[str, int]:
    """缺少的素材 → 還差幾個。"""
    return {code: need - have.get(code, 0) for code, need in machine.materials.items() if have.get(code, 0) < need}


def material_name(code: str) -> str:
    return MATERIALS[code][0] if code in MATERIALS else code
