"""每日簽到：以台灣日期為準每天一次，開啟農場面板時自動簽到並發放獎勵。

- 昨天有簽到時連續天數 +1，中斷一天就從 1 重新計算。
- 獎勵 7 天一輪（第 7 天最多），之後重新循環；以道具為主、金幣少量，避免通膨。
- 道具放不進倉庫時改發等值金幣（道具商店價格），自動簽到不會因倉庫滿而失敗。
"""

from datetime import date, timedelta

from app.game import balance

CYCLE_DAYS = 7
# 第 1–7 天的獎勵：{"gold": 金幣, "items": {道具代碼: 數量}}
REWARDS: tuple[dict, ...] = (
    {"gold": 100, "items": {}},
    {"gold": 0, "items": {balance.ITEM_FERTILIZER: 3}},
    {"gold": 200, "items": {}},
    {"gold": 0, "items": {balance.ITEM_PATROL_TICKET: 2}},
    {"gold": 300, "items": {}},
    {"gold": 0, "items": {balance.ITEM_FERTILIZER: 5}},
    {"gold": 500, "items": {balance.ITEM_EARTHWORM: 1}},
)


def next_streak(last_day: date | None, last_streak: int, today: date) -> int:
    """今天簽到後的連續天數：上一次簽到是昨天就接續，否則重新從 1 開始。"""
    if last_day is not None and last_day == today - timedelta(days=1):
        return last_streak + 1
    return 1


def reward_for(streak: int) -> dict:
    """連續第 streak 天的獎勵（7 天一輪）。"""
    return REWARDS[(max(1, streak) - 1) % CYCLE_DAYS]


def item_gold_value(item_code: str) -> int:
    """道具放不進倉庫時改發的金幣（道具商店價格）。"""
    return balance.ITEMS[item_code][1]


def item_name(item_code: str) -> str:
    return balance.ITEMS[item_code][0]


def reward_text(reward: dict) -> str:
    parts = [f"{reward['gold']:,} 金幣"] if reward.get("gold") else []
    parts += [f"{item_name(code)} ×{qty}" for code, qty in (reward.get("items") or {}).items()]
    return "、".join(parts) or "無"
