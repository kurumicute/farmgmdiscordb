"""排名獎勵的純規則：統計期間（台灣時間）與獎勵規則驗證。

- 每日：00:00 起算；每週：週一 00:00 起算；每月：1 日 00:00 起算。期間為 [開始, 結束)。
- 群組排名的分數是「主要所屬群組為該群組的成員」加總；個人排名為所有有農場的玩家。
- 期間內的項目：
  - money（金幣）：期間淨賺金幣 = 期間內交易紀錄的金幣變化加總，不含建立農場、管理員調整與各種獎勵；
    不指定期間時為目前金幣餘額。
  - sales（農產品販售）：期間內作物（含珍稀變種）販售數量。
  - goods（商品總販售）：期間內所有商品（作物＋動物產品＋加工品）販售收入。
- 只有分數大於 0 的群組／玩家可以得獎；同分時依次要條件與 ID 排序，名次不重複。
"""

from datetime import datetime, timedelta

from app.clock import TAIPEI_TZ
from app.game import balance
from app.game.errors import ValidationError

PERIODS = {"daily": "每日", "weekly": "每週", "monthly": "每月"}
VIEW_PERIODS = {"all": "全部期間", "day": "本日", "week": "本週", "month": "本月"}
VIEW_TO_PERIOD = {"day": "daily", "week": "weekly", "month": "monthly"}
SCOPES = {"group": "群組排名（獎勵發給群組全體成員）", "individual": "個人排名"}
METRICS = {"money": "金幣", "sales": "農產品販售", "goods": "商品總販售"}
# 計算「淨賺金幣」時排除的交易類型（非遊戲經營所得）
NET_GOLD_EXCLUDED = ("create_farm", "admin_adjust", "quest_reward", "competition_reward", "ranking_reward")


def period_bounds(period: str, at: datetime) -> tuple[datetime, datetime]:
    """回傳包含 at 的期間 [開始, 結束)，皆為有時區的 datetime（台灣時間）。"""
    local = at.astimezone(TAIPEI_TZ)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return day, day + timedelta(days=1)
    if period == "weekly":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    if period == "monthly":
        start = day.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start, end
    raise ValueError(f"unknown period: {period}")


def period_label(period: str, start: datetime) -> str:
    local = start.astimezone(TAIPEI_TZ)
    if period == "daily":
        return local.strftime("%Y-%m-%d")
    if period == "weekly":
        end = local + timedelta(days=6)
        return f"{local.strftime('%Y-%m-%d')}～{end.strftime('%m-%d')}（週）"
    return local.strftime("%Y-%m（月）")


def _int(value, label: str, low: int, high: int, errors: list[str]) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        errors.append(f"{label}必須是 {low}–{high} 之間的整數。")
        return low
    return value


def validate_rule(raw: dict) -> dict:
    """驗證排名獎勵規則；rewards 依名次排列，每項為 {"reward_type", "amount"}。錯誤一次列出。"""
    errors: list[str] = []
    name = (raw.get("name") or "").strip() if isinstance(raw.get("name"), str) else ""
    if not 1 <= len(name) <= balance.RANKING_NAME_MAX_LEN:
        errors.append(f"規則名稱長度必須為 1–{balance.RANKING_NAME_MAX_LEN} 字。")
    scope, metric, period = raw.get("scope"), raw.get("metric"), raw.get("period")
    if scope not in SCOPES:
        errors.append("排名對象只能是群組或個人。")
    if metric not in METRICS:
        errors.append("排名項目只能是金幣、農產品販售或商品總販售。")
    if period not in PERIODS:
        errors.append("結算週期只能是每日、每週或每月。")
    rewards_raw = raw.get("rewards")
    rewards = []
    if not isinstance(rewards_raw, list) or not 1 <= len(rewards_raw) <= balance.RANKING_MAX_RANKS:
        errors.append(f"獎勵名次必須設定 1–{balance.RANKING_MAX_RANKS} 名。")
    else:
        for i, r in enumerate(rewards_raw, start=1):
            if not isinstance(r, dict) or r.get("reward_type") not in balance.QUEST_REWARD_TYPES:
                errors.append(f"第 {i} 名的獎勵類型只能是金幣、化肥、蚯蚓或巡查券。")
                continue
            kind = r["reward_type"]
            high = balance.RANKING_MAX_GOLD if kind == "gold" else balance.RANKING_MAX_ITEMS
            amount = _int(r.get("amount"), f"第 {i} 名的獎勵數量", 1, high, errors)
            rewards.append({"reward_type": kind, "amount": amount})
    if errors:
        raise ValidationError("排名獎勵規則無效：" + "；".join(errors))
    return {
        "name": name,
        "scope": scope,
        "metric": metric,
        "period": period,
        "rewards": rewards,
        "enabled": bool(raw.get("enabled", True)),
    }


def reward_text(reward: dict) -> str:
    if reward["reward_type"] == "gold":
        return f"{reward['amount']} 金幣"
    return f"{reward['amount']} 個{balance.ITEMS[reward['reward_type']][0]}"
