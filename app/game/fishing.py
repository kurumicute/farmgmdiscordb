"""釣魚小遊戲的純規則：稀有度、設定驗證、釣具效果、魚種抽選與拉扯博弈。不依賴資料庫或 Discord。

重要設計（需求：稀有度機率與釣魚成功率是兩個獨立設定）：
- 稀有度只決定「出現哪一種魚」與「拉扯有多難」（體力、起始距離、掙扎機率），不會降低成功率。
- 基礎成功率（預設 90%）在拋竿時獨立判定：沒通過時，魚會在拉扯開始後「脫鉤」（與稀有度、裝備無關）。
- 拉扯中魚的下一個動作會事先顯示（💬 提示），依提示操作（衝刺前放線、平靜時收線）一定能把魚拉上來；
  因此依提示操作的玩家，各稀有度的整體成功率都接近基礎成功率。不看提示亂按才會斷線或讓魚逃走。
- 魚竿（收線力、穩定度、反應時間）與魚線（最大張力）提高操作容錯；魚餌提高稀有魚的出現機率並縮短等待。
- 裝備再好也不能只靠一直收線：連續收線的額外張力依魚線上限按比例增加（好魚線一樣會斷）、
  魚還有力氣時拉到水邊也拉不上來（LAND_STAMINA_RATIO）、魚會隨機往左或右竄，要按同方向壓竿（DODGE_*）。
- 防腳本（GUARD_*）：定期與操作時間異常時要通過人機確認，答錯暫停釣魚；只影響能不能拋竿，不扣任何資產。

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

STEER_ACTIONS = ("left", "right")
ACTIONS = ("reel", "release", "wait", *STEER_ACTIONS)
ACTION_NAMES = {"reel": "收線", "release": "放線", "wait": "等待", "left": "左壓", "right": "右壓"}

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
    "reel": {"tension": 14, "stamina": -5, "distance": 0, "spike_mul": 1.0, "dash_mul": 1.0},  # 距離由魚竿收線力決定
    "release": {"tension": -30, "stamina": 0, "distance": 6, "spike_mul": 0.5, "dash_mul": 1.0},
    "wait": {"tension": -12, "stamina": 3, "distance": 0, "spike_mul": 0.8, "dash_mul": 0.5},
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

# 連續收線：每多連續一次，額外增加「魚線最大張力的 N%」（依上限按比例，好魚線也撐不住一直收線）。
# 放線或壓中方向歸零；等待只會讓連續次數減半次（REEL_STREAK_WAIT_DECAY）。
REEL_STREAK_TENSION_PCT = 14
REEL_STREAK_WAIT_DECAY = 0.5
# 魚的體力要降到這個比例以下才拉得上岸；還有力氣時最多拉到 LAND_GATE_DISTANCE，收線力再高也一樣
LAND_STAMINA_RATIO = 0.25
LAND_GATE_DISTANCE = 8
# 魚竄向一邊：要按同方向壓竿（方向只在畫面文字上）
DODGE_BASE_PCT = 15  # 機率 = 基礎 + 魚的強力掙扎機率的一半（稀有度越高越常出現）
DODGE_HIT = {"tension": -15, "stamina": -10, "distance": -3}  # 壓中：張力下降、魚耗力、拉近一點
DODGE_MISS = {"tension": 18, "distance": 8}  # 沒壓中（按錯方向或其他操作）：張力暴增、魚被拉遠
STEER_IDLE_TENSION = 8  # 魚沒有竄動時亂壓竿
DODGE_TEXT = {"left": "⬅️ 魚猛然往左邊竄！快往同方向壓竿！", "right": "➡️ 魚猛然往右邊竄！快往同方向壓竿！"}

# 防腳本：只影響能不能拋竿，不扣金幣或魚。測試預設關閉（conftest），防腳本測試自行開啟
GUARD_ENABLED = True
GUARD_CHECK_EVERY_CASTS = (25, 45)  # 每隔這麼多次拋竿（隨機）要通過一次人機確認
GUARD_CHALLENGE_SECONDS = 180  # 題目有效時間；過期換一題（不算答錯）
GUARD_LOCK_MINUTES = 5  # 答錯暫停釣魚；連續答錯加倍
GUARD_LOCK_MAX_MINUTES = 60
GUARD_SUSPICION_LIMIT = 6  # 可疑分數到這裡，下一次拋竿就要確認（通過後歸零）
GUARD_FAST_ACTION_SECONDS = 0.5  # 畫面更新後到按下一步的最短合理時間（含 Discord 來回與人的反應）
GUARD_FAST_HOOK_SECONDS = 0.6  # 上鉤後多久內拉竿不合理（面板本身就要 0.4 秒後才更新畫面）
GUARD_REGULAR_MIN_SAMPLES = 6  # 一場至少這麼多次操作才判斷節奏
GUARD_REGULAR_STDEV_SECONDS = 0.08  # 操作間隔的標準差低於這個值：節奏規律得不像人
GUARD_GAPS_KEPT = 30  # 一場最多保留幾個操作間隔
GUARD_SCORE = {"fast_action": 1, "fast_hook": 2, "regular": 3}
# 題目：只寫中文名稱、按鈕只有 emoji，單純比對文字的腳本答不出來
GUARD_CREATURES = {
    "puffer": ("🐡", "河豚"), "tropical": ("🐠", "熱帶魚"), "shrimp": ("🦐", "蝦子"), "crab": ("🦀", "螃蟹"),
    "octopus": ("🐙", "章魚"), "squid": ("🦑", "魷魚"), "whale": ("🐳", "鯨魚"), "shark": ("🦈", "鯊魚"),
    "turtle": ("🐢", "烏龜"), "lobster": ("🦞", "龍蝦"),
}
GUARD_OPTIONS = 5

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
    ("pale_chub", "溪哥", "🐟", "common", 3, 18, 20, 120, 1, "溪流裡成群游動的小魚。"),
    ("loach", "泥鰍", "🐟", "common", 5, 23, 30, 200, 1, "喜歡躲在泥巴裡，滑溜溜的。"),
    ("crucian_carp", "鯽魚", "🐟", "common", 3, 27, 100, 800, 2, "池塘裡最常見的魚。"),
    ("tilapia", "吳郭魚", "🐟", "common", 8, 35, 300, 1500, 2, "適應力超強，到處都釣得到。"),
    ("common_carp", "鯉魚", "🐟", "common", 18, 45, 1000, 8000, 3, "力氣不小的大型淡水魚。"),
    ("sweetfish", "香魚", "🐠", "rare", 68, 113, 100, 400, 2, "只住在清澈溪流，帶有淡淡香氣。"),
    ("sea_bass", "鱸魚", "🐠", "rare", 48, 90, 1000, 6000, 3, "會突然衝刺的掠食魚。"),
    ("rainbow_trout", "虹鱒", "🐠", "rare", 57, 102, 800, 4000, 3, "身上有彩虹般的光澤。"),
    ("catfish", "鯰魚", "🐠", "rare", 48, 86, 1500, 12000, 4, "夜行性，咬餌後會往深處鑽。"),
    ("salmon", "鮭魚", "🐡", "epic", 116, 180, 2000, 10000, 3, "逆流而上的勇者。"),
    ("mahi_mahi", "鬼頭刀", "🐡", "epic", 125, 203, 2000, 15000, 4, "速度飛快，會連續跳出水面。"),
    ("grouper", "石斑魚", "🐡", "epic", 135, 215, 3000, 25000, 4, "被釣到會拚命往礁石裡躲。"),
    ("formosan_salmon", "櫻花鉤吻鮭", "🌸", "legendary", 270, 428, 200, 1500, 3, "冰河時期留下的國寶魚。"),
    ("golden_koi", "黃金錦鯉", "🎏", "legendary", 315, 450, 3000, 12000, 4, "傳說釣到就會好運連連。"),
    ("oarfish", "皇帶魚", "🐉", "legendary", 228, 405, 20000, 150000, 5, "深海中的神秘巨魚。"),
    # 以下 20 種由 Migration 0033 新增（普通 6、稀有 6、史詩 5、傳說 3；部分史詩／傳說魚要用指定的高級魚餌）
    ("goby", "鰕虎", "🐟", "common", 5, 21, 10, 80, 1, "趴在溪底石頭上的小魚，一點也不怕人。"),
    ("pond_smelt", "公魚", "🐟", "common", 3, 14, 10, 60, 1, "冬天湖面上最受歡迎的小魚。"),
    ("bluegill", "藍鰓太陽魚", "🐟", "common", 5, 23, 50, 400, 1, "鰓蓋上有一抹藍色，貪吃又好釣。"),
    ("milkfish", "虱目魚", "🐟", "common", 9, 32, 300, 2000, 2, "魚塭裡的國民魚，刺很多但很好吃。"),
    ("mullet", "烏魚", "🐟", "common", 12, 36, 500, 3000, 2, "冬至前後成群洄游，烏魚子就是牠的。"),
    ("snakehead", "鱧魚", "🐟", "common", 18, 45, 1000, 6000, 3, "俗稱雷魚，咬餌兇猛，力氣比外表大。"),
    ("mackerel", "鯖魚", "🐠", "rare", 48, 80, 200, 1200, 2, "背上有波浪花紋，成群在海面附近游動。"),
    ("flounder", "比目魚", "🐠", "rare", 48, 86, 300, 3000, 2, "兩隻眼睛長在同一邊，平貼著海底。"),
    ("yellowfin_seabream", "黃鰭鯛", "🐠", "rare", 50, 90, 300, 2000, 2, "河口常見的鯛魚，鰭帶著亮黃色。"),
    ("black_seabream", "黑鯛", "🐠", "rare", 54, 95, 400, 3000, 3, "警戒心很強，磯釣客的夢幻對手。"),
    ("amberjack", "紅魽", "🐠", "rare", 57, 102, 1500, 9000, 3, "會繞著礁石狂奔的大型掠食魚。"),
    ("eel", "鰻魚", "🐍", "rare", 68, 113, 500, 3000, 4, "滑溜又有力，咬到餌就往石縫裡鑽。"),
    ("lionfish", "獅子魚", "🦁", "epic", 125, 192, 300, 1200, 3, "華麗的鰭條藏著毒刺，小心別被扎到。"),
    ("napoleon_fish", "曲紋唇魚", "🐡", "epic", 135, 215, 5000, 40000, 4, "俗稱蘇眉，額頭隆起的珊瑚礁巨魚。"),
    ("arowana", "金龍魚", "🐉", "epic", 147, 215, 1000, 6000, 4, "鱗片閃著金光，被視為招財的象徵。"),
    ("yellowfin_tuna", "黃鰭鮪", "🐟", "epic", 270, 405, 10000, 80000, 5, "深海的速度之王，只吃深海誘餌以上的魚餌。"),
    ("swordfish", "旗魚", "🗡️", "epic", 293, 450, 30000, 200000, 5, "用長劍般的上顎突擊獵物，只吃深海誘餌以上的魚餌。"),
    ("moon_ray", "月光魟", "🌙", "legendary", 495, 788, 10000, 80000, 5,
     "月圓時才浮上海面的發光魟魚，要用深海誘餌以上的魚餌。"),
    ("coelacanth", "腔棘魚", "🦴", "legendary", 563, 900, 30000, 90000, 5, "活化石，只會被龍涎香餌吸引。"),
    ("dragon_king_carp", "龍王鯉", "🐲", "legendary", 675, 1125, 5000, 30000, 5, "躍過龍門的鯉魚王，只吃龍涎香餌。"),
    # 以下 10 種特殊魚由 Migration 0035 新增：都有季節或時段限定（game/seasons.FISH_LIMITS），部分還要指定魚餌
    ("dawn_trout", "黎明鱒", "🌅", "rare", 80, 135, 300, 2500, 3, "只在天剛亮（04–07 點）時出現的鱒魚。"),
    ("snow_icefish", "雪精靈冰魚", "❄️", "rare", 90, 147, 50, 300, 2, "冬天清晨才會游到淺水的透明小魚。"),
    ("firefly_squid", "螢火魷", "✨", "epic", 225, 360, 50, 200, 3,
     "春天夜裡發出藍光的魷魚，要用深海誘餌以上的魚餌。"),
    ("lanternfish", "燈籠魚", "🏮", "epic", 203, 338, 100, 600, 3, "夏夜提著小燈籠浮上海面的深海魚。"),
    ("maple_salmon", "楓紅鮭", "🍁", "epic", 248, 383, 2000, 12000, 4, "秋天黃昏逆流而上，身上像楓葉一樣紅。"),
    ("rainbow_wrasse", "虹彩隆頭魚", "🌈", "epic", 215, 350, 200, 2000, 3,
     "夏天正午的珊瑚礁上閃著七彩光芒。"),
    ("midnight_angler", "午夜鮟鱇", "🌑", "legendary", 720, 1170, 5000, 40000, 5,
     "只在午夜 0–3 點提著燈出沒，要用深海誘餌以上的魚餌。"),
    ("spring_koi", "春神錦鯉", "🌸", "legendary", 788, 1350, 3000, 15000, 5,
     "春天早晨才會現身的神之錦鯉，只吃龍涎香餌。"),
    ("ghost_shark", "幽靈鯊", "👻", "legendary", 900, 1463, 20000, 150000, 5,
     "秋天深夜的海面下有一道半透明的影子，要用深海誘餌以上的魚餌。"),
    ("frost_dragon_fish", "霜龍魚", "🧊", "legendary", 1013, 1575, 10000, 60000, 5,
     "冬天深夜冰海中的龍形巨魚，只吃龍涎香餌。"),
)
# 要用指定魚餌才釣得到的魚（code → 可用的魚餌代碼）；沒列在這裡的魚任何魚餌都能釣到。
# 拋竿時只從「目前裝備的魚餌釣得到」的魚種中挑；該稀有度都釣不到時往較低的稀有度找（pick_fish）
FISH_REQUIRED_BAITS = {
    "yellowfin_tuna": ("deep_sea_bait", "dragon_bait"),
    "swordfish": ("deep_sea_bait", "dragon_bait"),
    "moon_ray": ("deep_sea_bait", "dragon_bait"),
    "coelacanth": ("dragon_bait",),
    "dragon_king_carp": ("dragon_bait",),
    "firefly_squid": ("deep_sea_bait", "dragon_bait"),
    "midnight_angler": ("deep_sea_bait", "dragon_bait"),
    "spring_koi": ("dragon_bait",),
    "ghost_shark": ("deep_sea_bait", "dragon_bait"),
    "frost_dragon_fish": ("dragon_bait",),
}
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
    ("premium_bait", "bait", "高級魚餌", "🦐", 6, 1, "稀有魚比較愛吃，上鉤也比較快。",
     {"rare_bp": 300, "epic_bp": 100, "legendary_bp": 30, "bite_speed_pct": 20}),
    ("special_bait", "bait", "特殊魚餌", "🌟", 12, 4, "特製香餌，吸引史詩與傳說魚。",
     {"rare_bp": 500, "epic_bp": 300, "legendary_bp": 150, "bite_speed_pct": 40}),
    # 以下由 Migration 0033 新增：部分史詩／傳說魚只吃這些魚餌（FISH_REQUIRED_BAITS）。
    # 所有魚餌的需要等級已由 Migration 0034 調低 2 級（最低 Lv.1），這裡是調整後的值
    ("deep_sea_bait", "bait", "深海誘餌", "🦑", 40, 6, "深海魚才會咬的誘餌：黃鰭鮪、旗魚、月光魟要用它（或龍涎香餌）。",
     {"rare_bp": 400, "epic_bp": 500, "legendary_bp": 200, "bite_speed_pct": 30}),
    ("dragon_bait", "bait", "龍涎香餌", "🐉", 120, 8, "傳說中的頂級香餌：腔棘魚、龍王鯉只吃它，深海魚也愛吃。",
     {"rare_bp": 300, "epic_bp": 600, "legendary_bp": 400, "bite_speed_pct": 20}),
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


def bait_allows(required_baits, bait_code: str | None) -> bool:
    """這條魚能不能用目前的魚餌釣到（required_baits 為空 = 任何魚餌都可以）。"""
    return not required_baits or bait_code in required_baits


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
        "intent": "calm", "last": None, "reel_streak": 0, "dodge": None,
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


def reel_tension(state: dict) -> float:
    """這次收線會增加多少張力：連續收線越多次越高（依魚線上限按比例）。"""
    extra = state.get("reel_streak", 0) * state["max_tension"] * REEL_STREAK_TENSION_PCT / 100
    return PLAYER_ACTIONS["reel"]["tension"] + extra


def land_stamina(state: dict) -> int:
    """魚的體力要降到多少以下才拉得上岸。"""
    return int(state["max_stamina"] * LAND_STAMINA_RATIO)


def dodge_pct(diff: dict) -> float:
    return DODGE_BASE_PCT + diff.get("strong_pct", 0) / 2


def next_dodge(state: dict, diff: dict, previous: str | None, rng: Callable[[], float]) -> str | None:
    """下一回合魚會不會竄向一邊：不連續出現；魚累垮時不會竄。"""
    if previous or state["stamina"] <= 0:
        return None
    if rng() * 100 >= dodge_pct(diff):
        return None
    return STEER_ACTIONS[0] if rng() < 0.5 else STEER_ACTIONS[1]


def step(state: dict, action: str, diff: dict, rod: dict, rng: Callable[[], float]) -> tuple[dict, str | None]:
    """執行一次拉扯操作，回傳 (新狀態, 結果)；結果 None 表示繼續，否則為 landed／escaped／line_broken／slipped。

    順序：脫鉤判定 → 壓中方向（這回合結束）或玩家操作（魚累了才拉得上岸）→ 魚依事先顯示的動作反應
    → 沒壓中方向的懲罰 → 斷線／逃走判定 → 決定魚的下一個動作。"""
    if action not in ACTIONS:
        raise ValidationError("沒有這個操作。")
    s = dict(state)
    s["turn"] += 1
    intent = s["intent"]
    dodge, s["dodge"] = s.get("dodge"), None
    streak = s.get("reel_streak", 0)
    if s.get("slip_turn") and s["turn"] >= s["slip_turn"]:
        s["last"] = {"action": action, "intent": intent, "text": "🪝 魚鉤沒有咬牢，魚掙脫逃走了！"}
        return s, "slipped"
    if action in STEER_ACTIONS and action == dodge:  # 壓中：魚這回合的動作被化解
        s["reel_streak"] = 0
        s["tension"] = max(0, s["tension"] + DODGE_HIT["tension"])
        s["stamina"] = _clamp(s["stamina"] + DODGE_HIT["stamina"], 0, s["max_stamina"])
        s["distance"] = max(1, s["distance"] + DODGE_HIT["distance"])
        s["last"] = {"action": action, "intent": intent, "text": "🎯 壓對方向！魚被你帶回來，耗了不少力氣。"}
        s["intent"] = next_intent(s, diff, intent, rng)
        return s, None
    if action in STEER_ACTIONS:
        s["reel_streak"] = 0
        p = {"tension": 0 if dodge else STEER_IDLE_TENSION, "stamina": 0, "distance": 0, "spike_mul": 1.0,
             "dash_mul": 1.0}
    else:
        p = PLAYER_ACTIONS[action]
    ratio = s["stamina"] / s["max_stamina"] if s["max_stamina"] else 0
    if action == "reel":
        s["tension"] = max(0, s["tension"] + reel_tension(s))
        s["reel_streak"] = streak + 1
        s["distance"] -= rod.get("reel_power", 10)
    else:
        if action == "wait":
            s["reel_streak"] = max(0, streak - REEL_STREAK_WAIT_DECAY)
        elif action == "release":
            s["reel_streak"] = 0
        s["tension"] = max(0, s["tension"] + p["tension"])
        s["distance"] += p["distance"]
    s["stamina"] = _clamp(s["stamina"] + p["stamina"], 0, s["max_stamina"])
    if s["tension"] > s["max_tension"]:  # 最後一下也要撐得住，不能硬拉上岸
        s["tension"] = _clamp(s["tension"], 0, 999)
        s["last"] = {"action": action, "intent": intent, "text": "💥 連續硬拉，張力太大，魚線斷了！"}
        return s, "line_broken"
    gate_note = ""
    if s["distance"] <= 0:
        if s["stamina"] <= land_stamina(s):
            s["distance"] = 0
            s["tension"] = _clamp(s["tension"], 0, 999)
            s["last"] = {"action": action, "intent": intent, "text": "🎉 收線成功，把魚拉出水面了！"}
            return s, "landed"
        s["distance"] = LAND_GATE_DISTANCE  # 魚還有力氣：拉到水邊又被甩開
        gate_note = "　🐟 魚還有力氣，到水邊又甩開了！"
    f = FISH_INTENTS[intent]
    spike = fish_spike(intent, rod)
    if spike > 0:
        spike *= p["spike_mul"]
    s["tension"] = _clamp(s["tension"] + spike, 0, 999)
    s["distance"] = _clamp(s["distance"] + f["distance"] * p["dash_mul"] * (0.5 + 0.5 * ratio), 1, ESCAPE_DISTANCE)
    s["stamina"] = _clamp(s["stamina"] + f["stamina"], 0, s["max_stamina"])
    result_text = STEER_MISS_TEXT if action in STEER_ACTIONS else ACTION_RESULT_TEXT[action]
    s["last"] = {"action": action, "intent": intent, "text": result_text + "　" + INTENT_RESULT[intent] + gate_note}
    if dodge:
        s["tension"] = _clamp(s["tension"] + DODGE_MISS["tension"], 0, 999)
        s["distance"] = _clamp(s["distance"] + DODGE_MISS["distance"], 1, ESCAPE_DISTANCE)
        s["last"]["text"] += "　⚠️ 沒壓對方向，魚線被猛扯了一下！"
    if s["tension"] > s["max_tension"]:
        s["last"]["text"] = "💥 張力太大，魚線斷了！"
        return s, "line_broken"
    if s["distance"] >= ESCAPE_DISTANCE:
        s["last"]["text"] = "🌊 魚游得太遠，掙脫逃走了！"
        return s, "escaped"
    s["intent"] = next_intent(s, diff, intent, rng)
    s["dodge"] = next_dodge(s, diff, dodge, rng)
    return s, None


def new_guard_challenge(pick: Callable, shuffle: Callable) -> dict:
    """出一題人機確認；pick(序列, 數量) 取不重複的選項、shuffle 打亂順序（由服務層傳入）。"""
    options = list(pick(list(GUARD_CREATURES), GUARD_OPTIONS))
    answer = options[0]
    shuffle(options)
    return {"answer": answer, "options": options}


def guard_lock_minutes(consecutive_failures: int) -> int:
    return min(GUARD_LOCK_MAX_MINUTES, GUARD_LOCK_MINUTES * 2 ** max(0, consecutive_failures - 1))


def regular_rhythm(gaps: list[float]) -> bool:
    """一場的操作間隔是否規律得不像人（真人的間隔會隨畫面內容明顯變動）。"""
    if len(gaps) < GUARD_REGULAR_MIN_SAMPLES:
        return False
    mean = sum(gaps) / len(gaps)
    return math.sqrt(sum((g - mean) ** 2 for g in gaps) / len(gaps)) < GUARD_REGULAR_STDEV_SECONDS


ACTION_RESULT_TEXT = {"reel": "🎣 你用力收線。", "release": "↩️ 你放出一些線。", "wait": "⏳ 你穩住魚竿觀察。"}
STEER_MISS_TEXT = "🎣 你壓了一下竿。"
INTENT_RESULT = {
    "calm": "魚游得很平穩。", "struggle": "魚左右掙扎！", "surge": "魚突然衝刺！", "strong": "魚強力衝刺！",
    "tired": "魚累得游不動了。",
}


# ---------- 模擬（測試與調整數值用） ----------

POLICY_KEYS = ("intent", "tension", "distance", "stamina", "max_stamina", "max_tension", "dodge", "reel_streak", "turn")


def smart_action(state: dict, diff: dict, rod: dict, margin: int = 4) -> str:
    """依提示操作的玩家：魚竄動時壓同方向；收線不會斷線或讓魚逃走時收線，否則等待，最後才放線。"""
    if state.get("dodge"):
        return state["dodge"]
    intent, t, d, mt = state["intent"], state["tension"], state["distance"], state["max_tension"]
    spike = max(0.0, fish_spike(intent, rod))
    ratio = state["stamina"] / state["max_stamina"] if state["max_stamina"] else 0
    dash = FISH_INTENTS[intent]["distance"] * (0.5 + 0.5 * ratio)
    reel = rod.get("reel_power", 10)
    pull = reel_tension(state)
    tired_enough = state["stamina"] + PLAYER_ACTIONS["reel"]["stamina"] <= land_stamina(state)
    if d - reel <= 0 and tired_enough and t + pull <= mt:
        return "reel"
    if t + pull + spike <= mt - margin and d - reel + dash < ESCAPE_DISTANCE:
        return "reel"
    if t - 12 + spike * 0.8 <= mt - margin and d + dash * 0.5 < ESCAPE_DISTANCE:
        return "wait"
    return "release"


def naive_action(state: dict, diff: dict, rod: dict) -> str:
    """不看提示的玩家：一直收線，張力超過七成才放線（不看魚往哪邊竄）。"""
    return "release" if state["tension"] > state["max_tension"] * 0.7 else "reel"


def reel_only_action(state: dict, diff: dict, rod: dict) -> str:
    """最簡單的連點腳本：只按收線。"""
    return "reel"


def alternate_action(state: dict, diff: dict, rod: dict) -> str:
    """固定節奏的腳本：收線、等待輪流按，不看畫面。"""
    return "reel" if state["turn"] % 2 == 0 else "wait"


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
