"""遊戲平衡數值集中管理。

作物的價格、生長時間與收成量存在資料庫 crop_types 表（由 Alembic 初始化），
調整作物數值請新增 Migration 或直接更新 crop_types；其餘農場層級的數值在此調整。
"""

INITIAL_GOLD = 500
INITIAL_PLOTS = 4
INITIAL_LEVEL = 1
INITIAL_WAREHOUSE_CAPACITY = 50
# 倉庫擴大卷：每次 +50 格，價格依次遞增；最大 300 格
WAREHOUSE_EXPANSION_CODE = "warehouse_expansion"
WAREHOUSE_EXPANSION_STEP = 50
WAREHOUSE_EXPANSION_PRICES = (4_000, 20_000, 50_000, 100_000, 200_000)
WAREHOUSE_MAX_CAPACITY = 300


def warehouse_expansion_price(capacity: int) -> int | None:
    """下一次擴大倉庫的價格；已達上限時回傳 None。"""
    if capacity >= WAREHOUSE_MAX_CAPACITY:
        return None
    tier = max(0, (capacity - INITIAL_WAREHOUSE_CAPACITY) // WAREHOUSE_EXPANSION_STEP)
    return WAREHOUSE_EXPANSION_PRICES[min(tier, len(WAREHOUSE_EXPANSION_PRICES) - 1)]

# 擴建土地：每次 +1 格，價格依目前格數遞增（第 5 格 500 … 第 16 格 180,000）
UPGRADE_PLOTS_ADDED = 1
UPGRADE_PRICES = (500, 1_000, 2_000, 4_000, 8_000, 20_000, 40_000, 80_000, 120_000, 160_000, 170_000, 180_000)
MAX_PLOTS = 16  # 每座農場最多土地數（Discord 農田圖片為 4×4）


def upgrade_price(plots: int) -> int | None:
    """目前有 plots 格時，下一次擴建的價格；已達上限時回傳 None。"""
    if plots >= MAX_PLOTS:
        return None
    tier = max(0, plots - INITIAL_PLOTS)
    return UPGRADE_PRICES[min(tier, len(UPGRADE_PRICES) - 1)]


# 單次購買／販售數量上限，避免誤輸入過大數字
MAX_TRADE_QUANTITY = 9999

PLAYER_NAME_MAX_LEN = 32
FARM_NAME_MAX_LEN = 40

# 初始作物設定（供測試參考；遊戲執行時以資料庫 crop_types 為準，可在監控網站新增／修改／停用）
# Migration 0013 已把資料庫價格調整為：小麥 5/11、胡蘿蔔 12/15、番茄 25/22、草莓 60/34（種子價格/單位售價）
INITIAL_CROPS = [
    # code, 名稱, 種子價格, 生長秒數, 收成數量, 單位售價（生長秒數由 Migration 0011 改為 1／2／4／8 小時，可在網站調整）
    ("wheat", "小麥", 10, 3600, 2, 15),
    ("carrot", "胡蘿蔔", 20, 7200, 3, 25),
    ("tomato", "番茄", 35, 14400, 4, 20),
    ("strawberry", "草莓", 60, 28800, 5, 25),
]
CROP_GROW_SECONDS_RANGE = (60, 7 * 24 * 3600)  # 網站可設定的生長時間範圍
CROP_SEED_PRICE_RANGE = (1, 1_000_000)
CROP_HARVEST_QUANTITY_RANGE = (1, 100)
CROP_SELL_PRICE_RANGE = (0, 1_000_000)
CATALOG_CODE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"  # 作物、變種、動物、產品代碼（建立後不可修改）
CATALOG_NAME_MAX_LEN = 32

# ---------- 澆水、乾枯 ----------
# 成熟後超過此秒數未收成即乾枯，乾枯作物不能收成或被偷，需用 clear 清除
WITHER_SECONDS = 16 * 3600

# ---------- 偷菜 ----------
STEAL_MAX_PER_ACTION = 2
# 每位偷菜者每天（台灣時間）對同一位玩家只能成功偷 1 格土地；被防守設施阻止的不算
STEAL_PLOTS_PER_VICTIM_PER_DAY = 1
GROUP_NAME_MAX_LEN = 40
# 農場轉移所屬群組的冷卻時間（首次建立農場後的第一次轉移不需等待）
GROUP_TRANSFER_COOLDOWN_SECONDS = 7 * 24 * 3600
GROUP_TRANSFER_REASON_MAX_LEN = 200

# ---------- 道具 ----------
FERTILIZER_TIME_FACTOR = 0.75  # 化肥：剩餘生長時間減少 25%
EARTHWORM_DURATION_SECONDS = 3 * 24 * 3600  # 蚯蚓效果持續 3 天；有效期間再次使用再延長 3 天
EARTHWORM_GROW_MULTIPLIER = 0.75  # 蚯蚓有效期間澆水的作物，生長時間 ×0.75

ITEM_FERTILIZER = "fertilizer"
ITEM_EARTHWORM = "earthworm"
TOOL_AUTO_WATERER = "auto_waterer"
TOOL_AUTO_PLANTER = "auto_planter"
TOOL_AUTO_SELLER = "auto_seller"
EFFECT_EARTHWORM = "earthworm"

# code: (名稱, 價格, 種類, 說明, 別名)
ITEMS = {
    ITEM_FERTILIZER: (
        "化肥", 30, "consumable", "用於生長中的農田，剩餘生長時間減少 25%（每次種植限用一次）", ("化肥",)
    ),
    ITEM_EARTHWORM: (
        "人工養殖蚯蚓",
        300,
        "consumable",
        "使用後 3 天內澆水的作物，生長時間 ×0.75（效果期間再次使用可再延長 3 天）",
        ("蚯蚓", "人工養殖蚯蚓"),
    ),
    TOOL_AUTO_WATERER: ("自動澆水器", 3500, "tool", "永久工具：啟用時種植後自動澆水", ("澆水器", "自動澆水器")),
    TOOL_AUTO_PLANTER: (
        "自動播種機",
        60_000,
        "tool",
        "奢侈品：啟用時，收成、清除乾枯、擴建或購買種子後，自動從背包隨機挑選種子把空著的農田種滿",
        ("播種機", "自動播種機"),
    ),
    TOOL_AUTO_SELLER: (
        "自動販售機",
        300_000,
        "tool",
        "奢侈品：啟用時，收成的農作物依當下價目表自動賣出（不佔倉庫）",
        ("販售機", "自動販售機"),
    ),
    "patrol_ticket": (
        "巡查券",
        60,
        "consumable",
        "使用後查看自己農場近 7 天所有偷菜嘗試（成功與被阻止，含偷菜者）",
        ("巡查券", "patrol"),
    ),
}
ITEM_PATROL_TICKET = "patrol_ticket"


# ---------- 賭場 ----------
# 系統從 1–10 擲一個數字：比大（6–10）／比小（1–5）贏了價值 ×2；猜中指定數字價值 ×5；輸了沒收下注的作物
GAMBLE_MIN, GAMBLE_MAX, GAMBLE_BIG_FROM = 1, 10, 6
GAMBLE_HILO_MULTIPLIER = 2
GAMBLE_EXACT_MULTIPLIER = 5
# 只有這些品質或突變的作物可以下注
GAMBLE_QUALITIES = ("rare", "legendary")
GAMBLE_MUTATIONS = ("giant", "golden", "variant")
GAMBLE_MODES = {"big": "比大（6–10）", "small": "比小（1–5）", "exact": "猜數字"}


# ---------- 防守設施 ----------
# code: (名稱, 價格, 每座農場上限, 阻止機率, 守護格數（None = 全部土地）, 別名)
DEFENSES = {
    "scarecrow": ("稻草人", 400, 2, 0.25, 3, ("稻草人",)),
    "guard_dog": ("警戒犬", 1200, 1, 0.30, None, ("警戒犬", "dog", "狗")),
    "fence": ("防盜圍欄", 800, 1, 0.20, 4, ("防盜圍欄", "圍欄")),
}
# 判定順序固定：稻草人 → 警戒犬 → 防盜圍欄；同種設施重疊守護同一格只計算一次
DEFENSE_ORDER = ("scarecrow", "guard_dog", "fence")
# 每次偷菜整體阻止機率上限
DEFENSE_MAX_BLOCK_CHANCE = 0.60
PATROL_DAYS = 7
PATROL_LIMIT = 20

# ---------- 動物養殖 ----------
# 動物種類與產品存在資料庫 animal_types（Migration 0008 寫入下列初始值，可在監控網站新增／修改／停用）
# code, 名稱, 購買價格, 每次餵食費用, 生產週期秒數, 產品代碼, 產品名稱, 每次產量, 產品售價
INITIAL_ANIMALS = [
    ("chicken", "雞", 500, 20, 24 * 3600, "egg", "雞蛋", 2, 25),
    ("cow", "牛", 2000, 50, 24 * 3600, "milk", "牛奶", 2, 60),
    ("sheep", "羊", 1500, 35, 48 * 3600, "wool", "羊毛", 1, 120),
]
ANIMAL_MAX_BUY = 12
ANIMAL_PRICE_RANGE = (1, 1_000_000)
ANIMAL_FEED_RANGE = (0, 100_000)
ANIMAL_CYCLE_RANGE = (60, 7 * 24 * 3600)
ANIMAL_PRODUCT_QUANTITY_RANGE = (1, 100)
ANIMAL_PRODUCT_PRICE_RANGE = (0, 1_000_000)

# ---------- 建築（畜舍與加工設施） ----------
# code: (名稱, 建造價格, 每座農場上限, 別名)
BUILDINGS = {
    "barn": ("畜舍", 1000, 3, ("畜舍",)),
    "mill": ("磨坊", 2000, 1, ("磨坊",)),
    "bakery": ("烘焙坊", 3500, 1, ("烘焙坊",)),
    "jam_workshop": ("果醬工坊", 2500, 1, ("果醬工坊",)),
}
BARN_SLOTS = 4  # 每座畜舍可安置的動物數（任一動物佔 1 欄位）

# ---------- 加工配方 ----------
# code: (名稱, 所需設施, {原料代碼: 每份數量}, 成品代碼, 每份產量, 每份加工秒數, 別名)
RECIPES = {
    "flour": ("麵粉", "mill", {"wheat": 3}, "flour", 1, 30 * 60, ("麵粉",)),
    "bread": ("麵包", "bakery", {"flour": 2}, "bread", 1, 60 * 60, ("麵包",)),
    "strawberry_jam": (
        "草莓果醬", "jam_workshop", {"strawberry": 3}, "strawberry_jam", 1, 60 * 60, ("草莓果醬", "果醬", "jam")
    ),
}
CRAFT_MAX_QUANTITY = 10  # 單張訂單最多份數；每座設施同時只能有 1 張加工中的訂單

# ---------- 加工品（可販售商品；動物產品在資料庫 animal_types） ----------
# code: (名稱, 單位售價, 別名)
GOODS = {
    "flour": ("麵粉", 45, ("麵粉",)),
    "bread": ("麵包", 130, ("麵包",)),
    "strawberry_jam": ("草莓果醬", 140, ("草莓果醬", "果醬", "jam")),
}

# ---------- 群組共同任務 ----------
# 任務類型：(名稱, 可用計量方式)；amount 只適用販售（金額）
QUEST_TYPES = {
    "harvest": ("共同收成", ("quantity",)),
    "sell": ("共同販售", ("quantity", "amount")),
    "build": ("農場建設", ("quantity",)),
    "animal": ("動物生產", ("quantity",)),
    "defense": ("防守任務", ("quantity",)),
}
QUEST_REWARD_TYPES = ("gold", "fertilizer", "earthworm", "patrol_ticket")
QUEST_REWARD_MODES = {"fixed": "每位貢獻者固定獎勵", "proportional": "獎池依貢獻比例分配"}
QUEST_MAX_TARGET = 1_000_000
QUEST_MAX_GOLD_REWARD = 1_000_000
QUEST_MAX_ITEM_REWARD = 100
EVENT_MAX_DAYS = 90  # 任務與競賽最長期間

# ---------- 群組競賽 ----------
COMPETITION_TYPES = {
    "harvest": ("群組收成大賽", "有效收成數量（每收成 1 個農產品 1 分）"),
    "sales": ("群組銷售大賽", "成功販售收入（每 1 金幣 1 分）"),
    "development": ("群組農場發展大賽", "土地擴建每次 10 分、建造設施每座 5 分"),
    "defense": ("群組防守大賽", "成功阻止偷菜每次 1 分"),
}
DEVELOPMENT_POINTS = {"upgrade": 10, "build": 5}
COMPETITION_REWARD_RANKS = 3  # 前幾名群組有獎勵
COMPETITION_MAX_REWARD = 100_000
# 截止後再等待此秒數才結算，確保截止前開始的操作都已提交
COMPETITION_SETTLE_GRACE_SECONDS = 60

# ---------- 作物品質與突變 ----------
# 依序排列（判定時依此順序切分機率區間）
QUALITIES = {"normal": "普通", "good": "優良", "rare": "稀有", "legendary": "傳說"}
MUTATIONS = {"none": "無", "giant": "巨型作物", "golden": "黃金作物", "fast": "快速生長", "variant": "珍稀變種"}
# 珍稀變種的代碼與名稱存在 crop_types.variant_code／variant_name；下列為初始作物的變種（供測試參考）
INITIAL_VARIANTS = {
    "wheat": ("amber_wheat", "琥珀小麥"),
    "carrot": ("amethyst_carrot", "紫晶胡蘿蔔"),
    "tomato": ("black_tomato", "黑鑽番茄"),
    "strawberry": ("snow_strawberry", "白雪草莓"),
}
MIN_GROW_SECONDS = 60  # 任何加速後的最短生長時間

# 預設設定（資料庫中沒有設定時使用；網站調整後以資料庫最新版本為準）
# 機率單位為萬分比（bp，10000 = 100%）；倍率單位為百分比（120 = 1.2 倍）
DEFAULT_GENETICS = {
    "quality_bp": {"normal": 7000, "good": 2000, "rare": 800, "legendary": 200},
    "quality_price_pct": {"normal": 100, "good": 120, "rare": 150, "legendary": 200},
    "mutation_bp": {"giant": 300, "golden": 100, "fast": 300, "variant": 100},
    "giant_multiplier": 2,
    "golden_price_pct": 300,
    "fast_reduction_seconds": 7200,
    "variant_price_pct": 400,
    "level_bonus_bp": 50,
    "level_bonus_max_bp": 500,
}

# ---------- 限時全服事件 ----------
EVENT_SCHEDULE_TYPES = {"once": "單次", "daily": "每日", "weekly": "每週"}
WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")  # 位元 0 = 週一
EVENT_NAME_MAX_LEN = 60
EVENT_DESC_MAX_LEN = 300
# 單一事件的設定範圍（與資料庫 CHECK 一致）
EVENT_SELL_PRICE_PCT_RANGE = (100, 300)  # 售價倍率（%）
EVENT_GROW_REDUCTION_PCT_RANGE = (0, 90)  # 生長時間減少（%）
EVENT_QUALITY_BONUS_BP_RANGE = (0, 3000)  # 品質加成（萬分比，從普通移到較高品質）
EVENT_MUTATION_BONUS_BP_RANGE = (0, 3000)  # 突變機率加成（萬分比，依各突變比例分配）
EVENT_HARVEST_BONUS_PCT_RANGE = (0, 200)  # 收成數量加成（%）
EVENT_FREE_WATER_RANGE = (0, 1)  # 1 = 免澆水：活動期間種下的作物立即開始生長
EVENT_HARVEST_LOSS_RANGE = (0, 10)  # 大風颳走：收成時每格少 N 個（至少保留 1 個）
# 多個事件同時生效時加總，但不超過下列上限
EVENT_MAX_SELL_BONUS_PCT = 200  # 最多 ×3
EVENT_MAX_GROW_REDUCTION_PCT = 75
EVENT_MAX_QUALITY_BONUS_BP = 3000
EVENT_MAX_MUTATION_BONUS_BP = 3000
EVENT_MAX_HARVEST_BONUS_PCT = 200
EVENT_MAX_FREE_WATER = 1
EVENT_MAX_HARVEST_LOSS = 10

# ---------- 管理員調整 ----------
ADMIN_MAX_PLOTS = MAX_PLOTS  # 管理員新增土地也不能超過上限
ADMIN_MAX_ITEM_QUANTITY = 100_000
ADMIN_MAX_GOLD = 1_000_000_000_000
ADMIN_MAX_LEVEL = 100  # 只影響品質加成，不會自動增減土地

# ---------- 排名獎勵（每日／每週／每月結算） ----------
RANKING_NAME_MAX_LEN = 60
RANKING_MAX_RANKS = 10  # 每條規則最多設定前幾名的獎勵
RANKING_MAX_GOLD = 10_000_000
RANKING_MAX_ITEMS = 100
RANKING_SETTLE_GRACE_SECONDS = 60  # 期間結束後再等待此秒數才結算，確保期間內的操作都已提交
RANKING_MAX_CATCHUP_PERIODS = 400  # 服務長時間停止後，一次最多補結算的期間數
