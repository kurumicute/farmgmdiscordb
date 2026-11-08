"""每日簽到：以台灣日期為準每天一次，開啟農場面板時自動簽到並發放獎勵。

- 昨天有簽到時連續天數 +1，中斷一天就從 1 重新計算。
- 獎勵預設 7 天一輪（第 7 天最多），之後重新循環；以道具為主、金幣少量，避免通膨。
  每天的獎勵與循環天數可在管理網站調整（feature_config 的 "checkin"），一律經 rewards()／reward_for() 讀取。
- 道具放不進倉庫時改發等值金幣（道具商店價格），自動簽到不會因倉庫滿而失敗。
"""

from datetime import date, timedelta

from app.game import balance, feature_config
from app.game.errors import ValidationError

CONFIG_KEY = "checkin"
MAX_CYCLE_DAYS = 31


def _defaults() -> dict:
    # 第 1–7 天的獎勵：{"gold": 金幣, "items": {道具代碼: 數量}}
    return {"rewards": [
        {"gold": 100, "items": {}},
        {"gold": 0, "items": {balance.ITEM_FERTILIZER: 3}},
        {"gold": 200, "items": {}},
        {"gold": 0, "items": {balance.ITEM_PATROL_TICKET: 2}},
        {"gold": 300, "items": {}},
        {"gold": 0, "items": {balance.ITEM_FERTILIZER: 5}},
        {"gold": 500, "items": {balance.ITEM_EARTHWORM: 1}},
    ]}


def validate_config(raw: dict) -> dict:
    days = raw.get("rewards")
    if not isinstance(days, list) or not 1 <= len(days) <= MAX_CYCLE_DAYS:
        raise ValidationError(f"簽到獎勵必須有 1–{MAX_CYCLE_DAYS} 天。")
    out = []
    for i, day in enumerate(days, 1):
        if not isinstance(day, dict):
            raise ValidationError(f"第 {i} 天的獎勵格式不正確。")
        gold = feature_config.int_value(day.get("gold", 0), f"第 {i} 天的金幣", 0, 1_000_000)
        items = feature_config.items_value(day.get("items"), f"第 {i} 天", max_kinds=3, max_qty=100)
        out.append({"gold": gold, "items": items})
    return {"rewards": out}


feature_config.register(CONFIG_KEY, "每日簽到", _defaults, validate_config)


def rewards() -> list[dict]:
    return feature_config.get(CONFIG_KEY)["rewards"]


def cycle_days() -> int:
    return len(rewards())


def next_streak(last_day: date | None, last_streak: int, today: date) -> int:
    """今天簽到後的連續天數：上一次簽到是昨天就接續，否則重新從 1 開始。"""
    if last_day is not None and last_day == today - timedelta(days=1):
        return last_streak + 1
    return 1


def reward_for(streak: int) -> dict:
    """連續第 streak 天的獎勵（cycle_days() 天一輪）。"""
    days = rewards()
    return days[(max(1, streak) - 1) % len(days)]


def item_gold_value(item_code: str) -> int:
    """道具放不進倉庫時改發的金幣（道具商店價格）。"""
    return balance.ITEMS[item_code][1]


def item_name(item_code: str) -> str:
    return balance.ITEMS[item_code][0]


def reward_text(reward: dict) -> str:
    parts = [f"{reward['gold']:,} 金幣"] if reward.get("gold") else []
    parts += [f"{item_name(code)} ×{qty}" for code, qty in (reward.get("items") or {}).items()]
    return "、".join(parts) or "無"
