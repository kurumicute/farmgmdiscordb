"""成就與稱號：成就定義、進度指標與獎勵（只放規則，不碰資料庫）。

- 每個成就看一個指標（metric）是否達到 target；指標由服務層從既有資料計算，因此歷史進度也會算進去。
- 達成後在「成就」頁按領取才發獎（金幣、道具、稱號）；每個成就每座農場只能領一次。
- 獎勵以道具與稱號為主、金幣少量，避免通膨；道具放不進倉庫時改發等值金幣（同每日簽到）。
- 代碼一旦上線就不要改（已領取紀錄以代碼保存）。門檻、金幣、道具與稱號可在管理網站調整
  （feature_config 的 "achievements"，只存有修改的成就），一律經 active()／by_code() 讀取套用設定後的成就。
- 改稱號名稱只影響之後領取的人，已經拿到的稱號維持原本的名稱。
"""

from dataclasses import dataclass, field, replace

from app.game import balance, feature_config
from app.game.errors import ValidationError

CONFIG_KEY = "achievements"
PCT_METRICS = frozenset({"codex_crop_pct", "codex_fish_pct", "codex_pct"})

TITLE_MAX_LEN = 20

# 指標：(名稱, 單位)；值由 ProgressService._achievement_metrics 計算
METRICS = {
    "crops": ("取得作物", "個"),
    "crop_sales": ("作物販售收入", "金"),
    "level": ("農場等級", "級"),
    "plots": ("農田", "格"),
    "gold": ("持有金幣", "金"),
    "legendary_crop": ("傳說品質作物", "次"),
    "variant_crop": ("珍稀變種作物", "種"),
    "animal_products": ("收集動物產品", "個"),
    "goods": ("領取加工品", "份"),
    "explorations": ("完成山洞探險", "次"),
    "fish": ("釣到的魚", "隻"),
    "legendary_fish": ("傳說魚", "隻"),
    "steals": ("偷菜成功", "次"),
    "defended": ("防守成功", "次"),
    "gambles": ("賭場遊玩", "次"),
    "boss_joined": ("領取世界 Boss 獎勵", "場"),
    "boss_won": ("擊敗世界 Boss", "場"),
    "best_streak": ("最長連續簽到", "天"),
    "codex_crop_pct": ("作物圖鑑", "%"),
    "codex_fish_pct": ("魚類圖鑑", "%"),
    "codex_pct": ("全部圖鑑", "%"),
}

# 成就分類：(代碼, 圖示, 名稱)
CATEGORIES = (
    ("farm", "🌾", "農耕"),
    ("wealth", "💰", "財富"),
    ("ranch", "🐔", "牧場與加工"),
    ("adventure", "⛰️", "冒險與釣魚"),
    ("social", "🥷", "社交與娛樂"),
    ("boss", "🐉", "世界 Boss"),
    ("collect", "📖", "簽到與圖鑑"),
)


@dataclass(frozen=True)
class Achievement:
    code: str
    category: str
    emoji: str
    name: str
    metric: str
    target: int
    gold: int = 0
    items: dict = field(default_factory=dict)
    title: str | None = None

    @property
    def description(self) -> str:
        label, unit = METRICS[self.metric]
        if self.metric in ("legendary_crop", "variant_crop", "legendary_fish") and self.target == 1:
            return f"取得{label}"
        return f"{label}達到 {self.target:,} {unit}"

    @property
    def reward(self) -> dict:
        return {"gold": self.gold, "items": dict(self.items)}


F, E, P = balance.ITEM_FERTILIZER, balance.ITEM_EARTHWORM, balance.ITEM_PATROL_TICKET

# 預設的成就（程式內建）；實際生效的是 active()（套用管理網站的設定）
BASE_ACHIEVEMENTS: tuple[Achievement, ...] = (
    # 農耕
    Achievement("crops_100", "farm", "🌱", "初出茅廬", "crops", 100, gold=200),
    Achievement("crops_1000", "farm", "🌾", "勤勞農夫", "crops", 1_000, items={F: 10}, title="勤勞農夫"),
    Achievement("crops_10000", "farm", "🚜", "農業大亨", "crops", 10_000, gold=2_000, items={E: 3}, title="農業大亨"),
    Achievement("level_5", "farm", "⭐", "小有規模", "level", 5, items={F: 5}),
    Achievement("level_10", "farm", "🌟", "資深農夫", "level", 10, items={E: 2}, title="資深農夫"),
    Achievement("plots_16", "farm", "🗺️", "大地主", "plots", balance.MAX_PLOTS, gold=1_000, title="大地主"),
    Achievement("legendary_crop", "farm", "👑", "傳說農夫", "legendary_crop", 1, items={P: 3}, title="傳說農夫"),
    Achievement("variant_crop", "farm", "💎", "變種獵人", "variant_crop", 1, items={E: 1}, title="變種獵人"),
    # 財富
    Achievement("sales_10k", "wealth", "🪙", "小本生意", "crop_sales", 10_000, gold=300),
    Achievement("sales_100k", "wealth", "💵", "生意興隆", "crop_sales", 100_000, items={E: 2}, title="生意興隆"),
    Achievement("sales_1m", "wealth", "💰", "富甲一方", "crop_sales", 1_000_000, gold=5_000, title="富甲一方"),
    Achievement("gold_500k", "wealth", "🏦", "大富翁", "gold", 500_000, title="大富翁"),
    # 牧場與加工
    Achievement("animal_50", "ranch", "🥚", "小小牧場", "animal_products", 50, items={F: 5}),
    Achievement("animal_500", "ranch", "🐄", "牧場主人", "animal_products", 500, items={E: 2}, title="牧場主人"),
    Achievement("goods_20", "ranch", "🍞", "學徒工匠", "goods", 20, gold=300),
    Achievement("goods_200", "ranch", "🏭", "工匠", "goods", 200, items={E: 2}, title="工匠"),
    # 冒險與釣魚
    Achievement("explore_10", "adventure", "🔦", "初探山洞", "explorations", 10, items={F: 5}),
    Achievement("explore_100", "adventure", "⛏️", "洞穴探險家", "explorations", 100, items={E: 2},
                title="洞穴探險家"),
    Achievement("fish_10", "adventure", "🎣", "第一次釣魚", "fish", 10, items={P: 2}),
    Achievement("fish_100", "adventure", "🐟", "釣魚高手", "fish", 100, gold=1_000, title="釣魚高手"),
    Achievement("fish_legendary", "adventure", "🐋", "傳說釣手", "legendary_fish", 1, items={E: 2}, title="傳說釣手"),
    # 社交與娛樂
    Achievement("steal_20", "social", "🥷", "神偷", "steals", 20, items={P: 3}, title="神偷"),
    Achievement("defend_10", "social", "🛡️", "守護者", "defended", 10, items={P: 3}, title="守護者"),
    Achievement("gamble_50", "social", "🎰", "幸運兒", "gambles", 50, gold=200, title="幸運兒"),
    # 世界 Boss
    Achievement("boss_join", "boss", "📦", "後勤新兵", "boss_joined", 1, gold=300),
    Achievement("boss_win_1", "boss", "⚔️", "屠龍者", "boss_won", 1, items={E: 1}, title="屠龍者"),
    Achievement("boss_win_10", "boss", "🐉", "龍之剋星", "boss_won", 10, gold=3_000, items={E: 3}, title="龍之剋星"),
    # 簽到與圖鑑
    Achievement("streak_7", "collect", "📅", "一週全勤", "best_streak", 7, items={F: 5}),
    Achievement("streak_30", "collect", "🗓️", "全勤農夫", "best_streak", 30, items={E: 3}, title="全勤農夫"),
    Achievement("codex_crop_all", "collect", "🌽", "作物學者", "codex_crop_pct", 100, gold=2_000, title="作物學者"),
    Achievement("codex_fish_all", "collect", "🐠", "垂釣大師", "codex_fish_pct", 100, gold=2_000, title="垂釣大師"),
    Achievement("codex_half", "collect", "📗", "收藏家", "codex_pct", 50, items={E: 2}),
    Achievement("codex_all", "collect", "📚", "博物學家", "codex_pct", 100, gold=5_000, title="博物學家"),
)
BASE_BY_CODE = {a.code: a for a in BASE_ACHIEVEMENTS}


def _validate() -> None:
    assert len(BASE_BY_CODE) == len(BASE_ACHIEVEMENTS), "成就代碼重複"
    cats = {c for c, _e, _n in CATEGORIES}
    titles = [a.title for a in BASE_ACHIEVEMENTS if a.title]
    assert len(titles) == len(set(titles)), "成就稱號重複"
    for a in BASE_ACHIEVEMENTS:
        assert a.category in cats and a.metric in METRICS and a.target > 0, a.code
        assert not a.title or len(a.title) <= TITLE_MAX_LEN, a.code
        assert all(code in balance.ITEMS for code in a.items), a.code


_validate()


# ---------- 可調整的設定：{"items": {成就代碼: {"target", "gold", "items", "title"}}}（只列有修改的成就） ----------


def editable(a: Achievement) -> dict:
    return {"target": a.target, "gold": a.gold, "items": dict(a.items), "title": a.title}


def _defaults() -> dict:
    return {"items": {}}


def validate_config(raw: dict) -> dict:
    items = raw.get("items")
    if not isinstance(items, dict):
        raise ValidationError("成就設定格式不正確。")
    out = {}
    for code, values in items.items():
        base = BASE_BY_CODE.get(code)
        if base is None:
            raise ValidationError(f"沒有「{code}」這個成就。")
        if not isinstance(values, dict):
            raise ValidationError(f"成就「{base.name}」的設定格式不正確。")
        v = feature_config
        high = 100 if base.metric in PCT_METRICS else 1_000_000_000_000
        entry = {
            "target": v.int_value(values.get("target", base.target), f"「{base.name}」的門檻", 1, high),
            "gold": v.int_value(values.get("gold", base.gold), f"「{base.name}」的金幣", 0, 10_000_000),
            "items": v.items_value(values.get("items", base.items), f"「{base.name}」的道具", max_kinds=3),
            "title": v.title_value(values.get("title", base.title), f"「{base.name}」的稱號", TITLE_MAX_LEN),
        }
        if entry != editable(base):  # 和預設一樣的不必存
            out[code] = entry
    config = {"items": out}
    titles = [a.title for a in apply_config(config) if a.title]
    dup = {t for t in titles if titles.count(t) > 1}
    if dup:
        raise ValidationError(f"成就稱號不可重複：{'、'.join(sorted(dup))}。")
    return config


def apply_config(config: dict) -> tuple[Achievement, ...]:
    """把設定套用到預設成就上（不改變目前生效的設定；存檔前檢查用）。"""
    over = config["items"]
    return tuple(replace(a, **{**over[a.code], "items": dict(over[a.code]["items"])}) if a.code in over else a
                 for a in BASE_ACHIEVEMENTS)


feature_config.register(CONFIG_KEY, "成就", _defaults, validate_config)

_cache: tuple[int, tuple[Achievement, ...], dict[str, Achievement]] | None = None


def _current() -> tuple[tuple[Achievement, ...], dict[str, Achievement]]:
    global _cache
    version = feature_config.version()
    if _cache is None or _cache[0] != version:
        rows = apply_config(feature_config.get(CONFIG_KEY))
        _cache = (version, rows, {a.code: a for a in rows})
    return _cache[1], _cache[2]


def active() -> tuple[Achievement, ...]:
    """目前生效的成就（套用管理網站的設定）。"""
    return _current()[0]


def by_code() -> dict[str, Achievement]:
    return _current()[1]


def titles() -> set[str]:
    return {a.title for a in active() if a.title}


def reached(a: Achievement, metrics: dict) -> bool:
    return metrics.get(a.metric, 0) >= a.target


def reward_text(a: Achievement) -> str:
    parts = [f"{a.gold:,} 金幣"] if a.gold else []
    parts += [f"{balance.ITEMS[code][0]} ×{qty}" for code, qty in a.items.items()]
    if a.title:
        parts.append(f"稱號「{a.title}」")
    return "、".join(parts) or "無"
