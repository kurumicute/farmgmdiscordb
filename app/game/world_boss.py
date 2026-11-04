"""世界 Boss（群組後勤活動）：補給類型、可捐獻物品、積分、戰鬥模擬公式、獎勵與設定驗證（純規則，不碰資料庫）。

玩家不直接攻擊 Boss，而是捐獻農作物、漁獲、素材與加工品給前線；前線依四種補給的達成率自動作戰：
- 糧食補給 → 持續作戰能力（出勤率）
- 醫療補給 → 恢復能力（把缺勤時間救回來）
- 戰鬥補給 → 傷害效率
- 裝備補給 → 戰鬥能力（另一個傷害倍率）

所有數值集中在本檔案的預設值；每場活動建立時把設定與獎勵快照存進 world_boss_events（管理網站可修改），
Migration 只寫死資料表結構，不引用這裡的數值。
"""

import copy
import math
from datetime import date, datetime, time, timedelta

from app.clock import TAIPEI_TZ
from app.game.errors import ValidationError

# ---------- 補給類型與捐獻類別 ----------

SUPPLIES = ("food", "medical", "combat", "equipment")
# code: (emoji, 名稱, 效果名稱, 說明)
SUPPLY_INFO = {
    "food": ("🌾", "糧食補給", "持續作戰", "農作物；讓前線維持出勤，不足時大部分時間在休息"),
    "medical": ("🩹", "醫療補給", "恢復能力", "藥草、雞蛋、牛奶、果醬等指定食物與素材；把受傷缺勤的時間救回來"),
    "combat": ("⚔️", "戰鬥補給", "傷害效率", "漁獲與麵粉、麵包等加工物品；提高每次攻擊的傷害"),
    "equipment": ("🛡️", "裝備補給", "戰鬥能力", "洞窟探索與機器製作素材；強化前線裝備"),
}
CATEGORIES = ("crop", "fish", "material", "goods", "special")
# code: (emoji, 名稱)
CATEGORY_INFO = {
    "crop": ("🌾", "農作物"),
    "fish": ("🐟", "漁獲"),
    "material": ("⚙️", "農場素材"),
    "goods": ("🍲", "指定加工物品"),
    "special": ("🧪", "指定特殊材料"),
}
# 山洞素材中屬於「特殊材料」的代碼（其餘普通素材歸在農場素材）；假幣是貨幣，不能捐
SPECIAL_MATERIALS = ("iron_ore", "gear", "crystal", "ancient_core", "machine_core")
NOT_DONATABLE = ("fake_coin",)

STATUSES = ("active", "paused", "ended")
STATE_LABELS = {
    "scheduled": "尚未開始", "active": "進行中", "paused": "已暫停", "ended": "已結束", "defeated": "已擊敗",
}
END_REASONS = ("defeated", "timeout", "admin")

# ---------- 預設值（建立活動時複製一份到活動設定） ----------

DEFAULT_NAME = "深淵魔龍"
DEFAULT_LEVEL = 50
DEFAULT_MAX_HP = 100_000
DEFAULT_DURATION_HOURS = 72

DEFAULT_SETTINGS = {
    # 前線基礎傷害（每小時）；實際傷害 = 基礎 × 攻擊倍率 × 出勤率
    "base_damage_per_hour": 1500,
    # 每 1 積分捐獻時立即造成的傷害（再乘攻擊倍率），讓捐獻馬上看得到效果
    "instant_damage_per_point": 2,
    # 作物與魚依「單價 ÷ 此值」換算積分（無條件進位，至少 1 分）；指定物品用 items 的固定積分
    "gold_per_point": 10,
    "crop_supply": "food",
    "fish_supply": "combat",
    # 各補給的目標積分
    "supply_targets": {"food": 2000, "medical": 800, "combat": 1200, "equipment": 800},
    "effects": {
        "ratio_cap_pct": 150,  # 補給最多算到目標的 150%（超過不再加成，避免單一補給灌爆）
        "base_uptime_pct": 30,  # 完全沒有糧食時的前線出勤率
        "medical_recovery_pct": 50,  # 醫療滿額時，缺勤時間被救回的比例
        "combat_attack_pct": 100,  # 戰鬥補給滿額時傷害 +100%
        "equipment_attack_pct": 50,  # 裝備補給滿額時傷害 +50%
    },
    "limits": {
        "per_item_max": 200,  # 每位玩家每種物品在一場活動最多捐獻的數量
        "per_action_max": 99,  # 單次捐獻上限
    },
    # 指定物品：代碼 → {補給, 每個積分}；積分 0 表示不可捐獻。作物代碼寫在這裡時改用固定積分
    "items": {
        "herb": {"supply": "medical", "points": 6},
        "egg": {"supply": "medical", "points": 3},
        "milk": {"supply": "medical", "points": 6},
        "strawberry_jam": {"supply": "medical", "points": 30},
        "flour": {"supply": "combat", "points": 5},
        "bread": {"supply": "combat", "points": 20},
        "wool": {"supply": "equipment", "points": 12},
        "wood": {"supply": "equipment", "points": 2},
        "stone": {"supply": "equipment", "points": 2},
        "copper_ore": {"supply": "equipment", "points": 3},
        "resin": {"supply": "equipment", "points": 2},
        "iron_ore": {"supply": "equipment", "points": 6},
        "gear": {"supply": "equipment", "points": 12},
        "crystal": {"supply": "equipment", "points": 15},
        "ancient_core": {"supply": "equipment", "points": 40},
        "machine_core": {"supply": "equipment", "points": 40},
    },
}

DEFAULT_REWARDS = {
    # 參與門檻：個人積分未達此值的玩家沒有任何獎勵（含勝利、參與、達標與排名），避免只捐 1 個物品白拿獎勵
    "min_points": 30,
    "victory": {"gold": 800, "items": {"crystal": 1}},  # Boss 被擊敗：所有貢獻者
    "participation": {"gold": 200, "items": {}},  # Boss 未被擊敗：所有貢獻者
    "bonus_points": 300,  # 個人積分達標（>= 此值）另發
    "bonus": {"gold": 300, "items": {"seed_amber_melon": 1}},
    # 個人排名獎勵（第 1、2、3… 名）
    "rank": [
        {"gold": 1500, "items": {"seed_moonflower": 1}},
        {"gold": 1000, "items": {"ancient_core": 1}},
        {"gold": 600, "items": {"gear": 2}},
    ],
    "title": "屠龍後勤英雄",  # 特殊稱號：擊敗 Boss 時前 title_top 名獲得（空字串表示不發）
    "title_top": 3,
}

# 驗證範圍
MAX_HP_RANGE = (1, 1_000_000_000)
LEVEL_RANGE = (1, 999)
NAME_MAX_LEN = 40
DURATION_RANGE_SECONDS = (60, 60 * 86400)
TARGET_RANGE = (1, 100_000_000)
POINTS_RANGE = (0, 100_000)
GOLD_REWARD_RANGE = (0, 1_000_000)
ITEM_REWARD_RANGE = (1, 1000)
MAX_REWARD_ITEMS = 5
MAX_RANK_REWARDS = 10
MAX_ITEMS = 60
TITLE_MAX_LEN = 20
LEADERBOARD_SIZE = 20

# ---------- 固定週期出現的世界 Boss（world_boss_schedules） ----------

SCHEDULE_INTERVAL_RANGE = (1, 30)  # 每幾天出現一次
SCHEDULE_DURATION_MINUTES_RANGE = (10, 7 * 24 * 60)  # 每次持續多久（分鐘）
SCHEDULE_PREVIEW = 5  # 管理網站顯示接下來幾場的時間

# 預設排程「深淵魔龍」：全服（所有伺服器的玩家一起打），每 2 天晚上 9 點（台灣時間）出現、持續 3 小時。
# 數值以 10 位玩家同時支援可以擊敗為目標：每人一次捐約 310 積分（作物 100、醫療 60、戰鬥 70、裝備 80），
# 10 人剛好補滿四種補給；捐獻的立即傷害約 1 萬，補滿後每小時 3,750，約 1 小時內擊敗 15,000 HP。
# 只有 1 人時 3 小時約 2,700 傷害、5 人約 1 萬，都打不倒（tests/test_world_boss.py 驗證）。
# Migration 0020 寫入相同數值（Migration 不引用本檔案）。
ABYSS_DRAGON = {
    "name": "深淵魔龍",
    "interval_days": 2,
    "spawn_hour": 21,
    "spawn_minute": 0,
    "anchor_date": date(2026, 11, 4),
    "duration_minutes": 180,
    "max_hp": 15_000,
    "settings": {
        **DEFAULT_SETTINGS,
        "supply_targets": {"food": 1000, "medical": 600, "combat": 700, "equipment": 800},
    },
    "rewards": {
        "min_points": 30,
        "victory": {"gold": 1200, "items": {"crystal": 2}},
        "participation": {"gold": 300, "items": {}},
        "bonus_points": 200,
        "bonus": {"gold": 500, "items": {"seed_amber_melon": 2}},
        "rank": [
            {"gold": 3000, "items": {"seed_moonflower": 2, "ancient_core": 1}},
            {"gold": 2000, "items": {"seed_moonflower": 1, "machine_core": 1}},
            {"gold": 1500, "items": {"ancient_core": 1}},
            {"gold": 800, "items": {"gear": 2}},
            {"gold": 800, "items": {"gear": 2}},
            {"gold": 400, "items": {"crystal": 1}},
            {"gold": 400, "items": {"crystal": 1}},
            {"gold": 400, "items": {"crystal": 1}},
            {"gold": 400, "items": {"crystal": 1}},
            {"gold": 400, "items": {"crystal": 1}},
        ],
        "title": "降龍勇士",
        "title_top": 3,
    },
}


def abyss_dragon() -> dict:
    return copy.deepcopy(ABYSS_DRAGON)


def validate_schedule(raw: dict) -> dict:
    """驗證排程的時間欄位（設定與獎勵另外以 validate_settings／validate_rewards 驗證）。"""
    anchor = raw.get("anchor_date")
    if isinstance(anchor, str):
        try:
            anchor = date.fromisoformat(anchor)
        except ValueError:
            raise ValidationError("起始日期格式不正確。") from None
    if not isinstance(anchor, date):
        raise ValidationError("請設定起始日期。")
    return {
        "anchor_date": anchor,
        "interval_days": int_value(raw.get("interval_days"), "出現間隔（天）", *SCHEDULE_INTERVAL_RANGE),
        "spawn_hour": int_value(raw.get("spawn_hour"), "出現時間（時）", 0, 23),
        "spawn_minute": int_value(raw.get("spawn_minute"), "出現時間（分）", 0, 59),
        "duration_minutes": int_value(raw.get("duration_minutes"), "持續時間（分鐘）",
                                      *SCHEDULE_DURATION_MINUTES_RANGE),
        "max_hp": int_value(raw.get("max_hp"), "Boss HP", *MAX_HP_RANGE),
    }


def slot_after(anchor_date: date, hour: int, minute: int, interval_days: int, duration_minutes: int,
               now: datetime, after: datetime | None = None) -> datetime:
    """下一個出現時間（台灣時間對齊）：結束時間晚於 now，且晚於 after（上一場的排定時間）。"""
    base = datetime.combine(anchor_date, time(hour, minute), TAIPEI_TZ)
    step = interval_days * 86400
    earliest = now - timedelta(minutes=duration_minutes)  # 出現時間要 > earliest，才會在 now 之後結束
    k = max(0, math.floor((earliest - base).total_seconds() / step) + 1)
    slot = base + timedelta(seconds=k * step)
    while after is not None and slot <= after:
        slot += timedelta(seconds=step)
    return slot


def upcoming_slots(schedule: dict, now: datetime, after: datetime | None, count: int = SCHEDULE_PREVIEW) -> list:
    slots, last = [], after
    for _ in range(count):
        last = slot_after(schedule["anchor_date"], schedule["spawn_hour"], schedule["spawn_minute"],
                          schedule["interval_days"], schedule["duration_minutes"], now, last)
        slots.append(last)
    return slots


# ---------- 設定驗證 ----------


def int_value(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label}必須是整數。")
    if not low <= value <= high:
        raise ValidationError(f"{label}必須介於 {low} 到 {high}。")
    return value


def validate_settings(raw: dict, item_category) -> dict:
    """驗證並補齊活動設定。item_category(code) 回傳物品的捐獻類別，不能捐的物品回傳 None。"""
    if not isinstance(raw, dict):
        raise ValidationError("補給設定格式不正確。")
    base = DEFAULT_SETTINGS
    out = {
        "base_damage_per_hour": int_value(raw.get("base_damage_per_hour", base["base_damage_per_hour"]),
                                     "每小時基礎傷害", 0, 100_000_000),
        "instant_damage_per_point": int_value(raw.get("instant_damage_per_point", base["instant_damage_per_point"]),
                                         "每積分立即傷害", 0, 100_000),
        "gold_per_point": int_value(raw.get("gold_per_point", base["gold_per_point"]), "每積分金幣價值", 1, 100_000),
    }
    for key, label in (("crop_supply", "作物補給類型"), ("fish_supply", "漁獲補給類型")):
        value = raw.get(key, base[key])
        if value not in SUPPLIES:
            raise ValidationError(f"{label}不正確。")
        out[key] = value
    targets = raw.get("supply_targets", base["supply_targets"])
    if not isinstance(targets, dict):
        raise ValidationError("補給目標格式不正確。")
    out["supply_targets"] = {
        s: int_value(targets.get(s, base["supply_targets"][s]), f"{SUPPLY_INFO[s][1]}目標", *TARGET_RANGE)
        for s in SUPPLIES
    }
    effects = raw.get("effects", base["effects"])
    if not isinstance(effects, dict):
        raise ValidationError("補給效果格式不正確。")
    eff = {**base["effects"], **effects}
    out["effects"] = {
        "ratio_cap_pct": int_value(eff["ratio_cap_pct"], "補給加成上限", 100, 500),
        "base_uptime_pct": int_value(eff["base_uptime_pct"], "基礎出勤率", 0, 100),
        "medical_recovery_pct": int_value(eff["medical_recovery_pct"], "醫療恢復比例", 0, 100),
        "combat_attack_pct": int_value(eff["combat_attack_pct"], "戰鬥補給傷害加成", 0, 1000),
        "equipment_attack_pct": int_value(eff["equipment_attack_pct"], "裝備補給傷害加成", 0, 1000),
    }
    limits = raw.get("limits", base["limits"])
    if not isinstance(limits, dict):
        raise ValidationError("捐獻限制格式不正確。")
    lim = {**base["limits"], **limits}
    out["limits"] = {
        "per_item_max": int_value(lim["per_item_max"], "每人每種物品上限", 1, 100_000),
        "per_action_max": int_value(lim["per_action_max"], "單次捐獻上限", 1, 999),
    }
    items = raw.get("items", base["items"])
    if not isinstance(items, dict) or len(items) > MAX_ITEMS:
        raise ValidationError(f"指定物品格式不正確（最多 {MAX_ITEMS} 項）。")
    out["items"] = {}
    for code, entry in items.items():
        if not isinstance(code, str) or item_category(code) is None:
            raise ValidationError(f"「{code}」不是可以捐獻的物品（只能是作物、加工品、動物產品或山洞素材）。")
        if not isinstance(entry, dict) or entry.get("supply") not in SUPPLIES:
            raise ValidationError(f"「{code}」的補給類型不正確。")
        out["items"][code] = {
            "supply": entry["supply"], "points": int_value(entry.get("points"), f"「{code}」的積分", *POINTS_RANGE),
        }
    return out


def _reward(raw, label: str, valid_item) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError(f"{label}格式不正確。")
    items = raw.get("items") or {}
    if not isinstance(items, dict) or len(items) > MAX_REWARD_ITEMS:
        raise ValidationError(f"{label}的物品最多 {MAX_REWARD_ITEMS} 種。")
    for code in items:
        if not valid_item(code):
            raise ValidationError(f"{label}的「{code}」不是可以發放的物品。")
    return {
        "gold": int_value(raw.get("gold", 0), f"{label}金幣", *GOLD_REWARD_RANGE),
        "items": {code: int_value(q, f"{label}「{code}」數量", *ITEM_REWARD_RANGE) for code, q in items.items()},
    }


def validate_rewards(raw: dict, valid_item) -> dict:
    """驗證獎勵設定；valid_item(code) 判斷物品代碼是否可以放進背包。"""
    if not isinstance(raw, dict):
        raise ValidationError("獎勵設定格式不正確。")
    base = DEFAULT_REWARDS
    ranks = raw.get("rank", base["rank"])
    if not isinstance(ranks, list) or len(ranks) > MAX_RANK_REWARDS:
        raise ValidationError(f"排名獎勵最多 {MAX_RANK_REWARDS} 名。")
    title = raw.get("title", base["title"])
    if not isinstance(title, str) or len(title.strip()) > TITLE_MAX_LEN:
        raise ValidationError(f"稱號最多 {TITLE_MAX_LEN} 字。")
    return {
        "min_points": int_value(raw.get("min_points", base["min_points"]), "參與門檻", 0, 100_000_000),
        "victory": _reward(raw.get("victory", base["victory"]), "勝利獎勵", valid_item),
        "participation": _reward(raw.get("participation", base["participation"]), "參與獎勵", valid_item),
        "bonus_points": int_value(raw.get("bonus_points", base["bonus_points"]), "額外獎勵門檻", 0, 100_000_000),
        "bonus": _reward(raw.get("bonus", base["bonus"]), "貢獻達標獎勵", valid_item),
        "rank": [_reward(r, f"第 {i} 名獎勵", valid_item) for i, r in enumerate(ranks, start=1)],
        "title": title.strip(),
        "title_top": int_value(raw.get("title_top", base["title_top"]), "稱號名額", 0, 1000),
    }


# ---------- 積分 ----------


def price_points(unit_price: int, gold_per_point: int) -> int:
    """作物與魚：依單價換算每個的積分（無條件進位，至少 1 分）。"""
    return max(1, math.ceil(max(0, unit_price) / gold_per_point))


# ---------- 戰鬥模擬 ----------


def combat_stats(progress: dict, settings: dict) -> dict:
    """依補給進度計算前線狀態：各補給達成率、攻擊倍率、出勤率與每小時傷害。"""
    eff, targets = settings["effects"], settings["supply_targets"]
    cap = eff["ratio_cap_pct"] / 100
    ratios = {s: min(cap, progress.get(s, 0) / targets[s]) for s in SUPPLIES}
    attack = (1 + eff["combat_attack_pct"] / 100 * ratios["combat"]
              + eff["equipment_attack_pct"] / 100 * ratios["equipment"])
    base_up = eff["base_uptime_pct"] / 100
    stamina = base_up + (1 - base_up) * min(1.0, ratios["food"])
    uptime = stamina + (1 - stamina) * eff["medical_recovery_pct"] / 100 * min(1.0, ratios["medical"])
    return {
        "ratios": ratios,
        "attack_mult": attack,
        "uptime": uptime,
        "damage_per_hour": settings["base_damage_per_hour"] * attack * uptime,
    }


def instant_damage(points: int, settings: dict, attack_mult: float) -> int:
    return int(points * settings["instant_damage_per_point"] * attack_mult)


def simulate(
    hp: int, last_tick_at: datetime, starts_at: datetime, ends_at: datetime, damage_per_hour: float, now: datetime
) -> dict:
    """從 last_tick_at 推進到 now（不超過活動結束時間）：回傳新的 HP、新的 last_tick_at、擊敗時間與是否到期。

    傷害取整數；不足 1 點的部分不推進 last_tick_at，留到下次累積，頻繁讀取不會吃掉傷害。"""
    start = max(last_tick_at, starts_at)
    end = min(now, ends_at)
    result = {"hp": hp, "last_tick_at": last_tick_at, "defeated_at": None, "expired": now >= ends_at}
    if end <= start or hp <= 0:
        return result
    if damage_per_hour <= 0:
        result["last_tick_at"] = end
        return result
    seconds = (end - start).total_seconds()
    damage = int(damage_per_hour * seconds / 3600)
    if damage >= hp:
        defeated = start + timedelta(seconds=hp * 3600 / damage_per_hour)
        result.update(hp=0, last_tick_at=defeated, defeated_at=defeated, expired=False)
        return result
    if damage > 0:
        result["hp"] = hp - damage
        result["last_tick_at"] = start + timedelta(seconds=damage * 3600 / damage_per_hour)
    elif last_tick_at < starts_at:
        result["last_tick_at"] = starts_at
    return result


def display_state(status: str, defeated: bool, starts_at: datetime, now: datetime) -> str:
    if status == "ended":
        return "defeated" if defeated else "ended"
    if status == "paused":
        return "paused"
    return "scheduled" if now < starts_at else "active"


# ---------- 獎勵 ----------


def min_points(rewards: dict) -> int:
    """參與門檻（積分）。Migration 0022 以前結束、沒有這個欄位的活動視為 0（沒有門檻）。"""
    return int(rewards.get("min_points", 0))


def compose_rewards(rewards: dict, *, defeated: bool, points: int, rank: int) -> dict:
    """依活動結果、個人積分與名次組合獎勵：{gold, items, title, parts}。積分未達參與門檻時什麼都沒有。"""
    gold, items, parts = 0, {}, []
    if points < min_points(rewards):
        return {"gold": 0, "items": {}, "title": None, "parts": []}

    def add(part: dict, label: str) -> None:
        nonlocal gold
        if not part["gold"] and not part["items"]:
            return
        gold += part["gold"]
        for code, qty in part["items"].items():
            items[code] = items.get(code, 0) + qty
        parts.append(label)

    add(rewards["victory"] if defeated else rewards["participation"], "勝利獎勵" if defeated else "參與獎勵")
    if rewards["bonus_points"] and points >= rewards["bonus_points"]:
        add(rewards["bonus"], "貢獻達標獎勵")
    if 1 <= rank <= len(rewards["rank"]):
        add(rewards["rank"][rank - 1], f"第 {rank} 名獎勵")
    title = rewards["title"] if defeated and rewards["title"] and rank <= rewards["title_top"] else None
    return {"gold": gold, "items": items, "title": title, "parts": parts}


def bar(value: float, total: float, width: int = 10) -> str:
    filled = 0 if total <= 0 else min(width, max(0, round(width * value / total)))
    return "█" * filled + "░" * (width - filled)
