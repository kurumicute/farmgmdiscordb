"""限時全服事件的純規則：設定驗證、是否生效、加成加總與上限。

事件只在「操作當下」判斷是否生效：
- 播種時：品質加成、突變加成、收成數量加成（結果固定在土地上，之後事件結束也不變）。
- 澆水時：生長時間減少（成熟時間在澆水時決定）；免澆水活動期間種下的作物立即澆水。
- 收成時：大風颳走（每格少 N 個，至少保留 1 個）。
- 販售作物時：售價倍率（實際成交單價寫入交易紀錄）。
多個事件同時生效時加成相加，但各自有上限（balance.EVENT_MAX_*）。
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.clock import TAIPEI_TZ
from app.game import balance
from app.game.errors import ValidationError

# 欄位：(中文名稱, 範圍, 無效果時的值, 加總上限)
EFFECTS = {
    "sell_price_pct": ("售價倍率", balance.EVENT_SELL_PRICE_PCT_RANGE, 100, balance.EVENT_MAX_SELL_BONUS_PCT),
    "grow_reduction_pct": (
        "生長時間減少",
        balance.EVENT_GROW_REDUCTION_PCT_RANGE,
        0,
        balance.EVENT_MAX_GROW_REDUCTION_PCT,
    ),
    "quality_bonus_bp": ("品質加成", balance.EVENT_QUALITY_BONUS_BP_RANGE, 0, balance.EVENT_MAX_QUALITY_BONUS_BP),
    "mutation_bonus_bp": ("突變加成", balance.EVENT_MUTATION_BONUS_BP_RANGE, 0, balance.EVENT_MAX_MUTATION_BONUS_BP),
    "harvest_bonus_pct": ("收成加成", balance.EVENT_HARVEST_BONUS_PCT_RANGE, 0, balance.EVENT_MAX_HARVEST_BONUS_PCT),
    # 0／1：活動期間種下的作物立即澆水並開始生長
    "free_water": ("免澆水", balance.EVENT_FREE_WATER_RANGE, 0, balance.EVENT_MAX_FREE_WATER),
    # 收成時每格少 N 個（至少保留 1 個）；屬於負面效果，但同樣以「大於 0 才生效」處理
    "harvest_loss": ("大風颳走", balance.EVENT_HARVEST_LOSS_RANGE, 0, balance.EVENT_MAX_HARVEST_LOSS),
}
PLANT_EFFECTS = ("quality_bonus_bp", "mutation_bonus_bp", "harvest_bonus_pct")


@dataclass
class Bonus:
    """加總後（已套用上限）的加成值與實際貢獻的事件。"""

    value: int = 0
    applied: list[dict] = field(default_factory=list)


def _int(value, label: str, low: int, high: int, errors: list[str]) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label}必須是整數。")
        return low
    if not low <= value <= high:
        errors.append(f"{label}必須在 {low}–{high} 之間（目前 {value}）。")
    return value


def validate(raw: dict) -> dict:
    """驗證事件設定並回傳正規化的值；錯誤一次列出。時間需為有時區的 datetime。"""
    errors: list[str] = []
    name = (raw.get("name") or "").strip()
    if not 1 <= len(name) <= balance.EVENT_NAME_MAX_LEN:
        errors.append(f"事件名稱長度必須為 1–{balance.EVENT_NAME_MAX_LEN} 字。")
    description = (raw.get("description") or "").strip()
    if len(description) > balance.EVENT_DESC_MAX_LEN:
        errors.append(f"事件說明最多 {balance.EVENT_DESC_MAX_LEN} 字。")
    values = {
        key: _int(raw.get(key, neutral), label, low, high, errors)
        for key, (label, (low, high), neutral, _cap) in EFFECTS.items()
    }
    if not errors and all(values[k] == EFFECTS[k][2] for k in EFFECTS):
        errors.append("至少要設定一項加成效果。")

    schedule = raw.get("schedule_type")
    starts_at, ends_at = raw.get("starts_at"), raw.get("ends_at")
    start_minute, end_minute, weekdays = raw.get("start_minute"), raw.get("end_minute"), raw.get("weekdays")
    if schedule not in balance.EVENT_SCHEDULE_TYPES:
        errors.append("排程類型只能是單次、每日或每週。")
    for label, dt in (("開始時間", starts_at), ("結束時間", ends_at)):
        if dt is not None and (not isinstance(dt, datetime) or dt.tzinfo is None):
            errors.append(f"{label}格式不正確。")
    if schedule == "once" and (starts_at is None or ends_at is None):
        errors.append("單次事件必須設定開始與結束時間。")
    if isinstance(starts_at, datetime) and isinstance(ends_at, datetime) and ends_at <= starts_at:
        errors.append("結束時間必須晚於開始時間。")
    if schedule in ("daily", "weekly"):
        if start_minute is None or end_minute is None:
            errors.append("每日／每週事件必須設定每天的開始與結束時刻。")
        else:
            start_minute = _int(start_minute, "開始時刻", 0, 1439, errors)
            end_minute = _int(end_minute, "結束時刻", 1, 1440, errors)
            if isinstance(start_minute, int) and isinstance(end_minute, int) and end_minute <= start_minute:
                errors.append("結束時刻必須晚於開始時刻（不可跨越午夜，可設定到 24:00）。")
    else:
        start_minute = end_minute = None
    if schedule == "weekly":
        weekdays = _int(weekdays, "星期", 1, 127, errors) if weekdays is not None else None
        if weekdays is None:
            errors.append("每週事件至少要選擇一天。")
    else:
        weekdays = None
    crop_code = raw.get("crop_code") or None
    if errors:
        raise ValidationError("事件設定無效：" + "；".join(errors))
    return {
        "name": name,
        "description": description,
        "crop_code": crop_code,
        **values,
        "schedule_type": schedule,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "weekdays": weekdays,
        "enabled": bool(raw.get("enabled", True)),
    }


def is_active(event, now: datetime) -> bool:
    """事件在 now（任何時區）是否生效；每日／每週時段以台灣時間判斷，區間為 [開始, 結束)。"""
    if not event.enabled:
        return False
    if event.starts_at is not None and now < event.starts_at:
        return False
    if event.ends_at is not None and now >= event.ends_at:
        return False
    if event.schedule_type == "once":
        return True
    local = now.astimezone(TAIPEI_TZ)
    minute = local.hour * 60 + local.minute
    if not event.start_minute <= minute < event.end_minute:
        return False
    if event.schedule_type == "weekly":
        return bool(event.weekdays & (1 << local.weekday()))
    return True


def applies_to_crop(event, crop_type_id: int | None) -> bool:
    return event.crop_type_id is None or event.crop_type_id == crop_type_id


def bonus(events, effect: str, crop_type_id: int | None) -> Bonus:
    """加總已生效事件中某一項效果（已過濾作物），並套用上限。events 應只包含生效中的事件。"""
    _label, _range, neutral, cap = EFFECTS[effect]
    result = Bonus()
    total = 0
    for event in events:
        amount = getattr(event, effect) - neutral
        if amount > 0 and applies_to_crop(event, crop_type_id):
            total += amount
            result.applied.append({"id": event.id, "name": event.name})
    result.value = min(total, cap)
    return result


def merge_applied(*bonuses: Bonus) -> list[dict] | None:
    """合併多項加成的事件清單（依 ID 去重）；沒有任何事件時回傳 None。"""
    seen: dict[int, dict] = {}
    for b in bonuses:
        for item in b.applied:
            seen.setdefault(item["id"], item)
    return list(seen.values()) or None


def apply_sell_bonus(unit_price: int, bonus_pct: int) -> int:
    """售價加成：單價 × (100 + 加成%) / 100，無條件捨去。"""
    return unit_price * (100 + bonus_pct) // 100


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def schedule_text(event) -> str:
    def fmt(dt: datetime) -> str:
        return dt.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")

    period = ""
    if event.starts_at or event.ends_at:
        start = fmt(event.starts_at) if event.starts_at else "即日起"
        period = f"{start} ～ {fmt(event.ends_at) if event.ends_at else '不限'}"
    if event.schedule_type == "once":
        return period
    window = f"{_hhmm(event.start_minute)}–{_hhmm(event.end_minute)}"
    if event.schedule_type == "daily":
        text = f"每日 {window}"
    else:
        days = "、".join(n for i, n in enumerate(balance.WEEKDAY_NAMES) if event.weekdays & (1 << i))
        text = f"每週{days} {window}"
    return f"{text}（{period}）" if period else text


def effects_text(event) -> str:
    parts = []
    if event.sell_price_pct != 100:
        parts.append(f"售價 ×{event.sell_price_pct / 100:g}")
    if event.grow_reduction_pct:
        parts.append(f"生長時間 -{event.grow_reduction_pct}%")
    if event.quality_bonus_bp:
        parts.append(f"品質加成 +{event.quality_bonus_bp / 100:g}%")
    if event.mutation_bonus_bp:
        parts.append(f"突變機率 +{event.mutation_bonus_bp / 100:g}%")
    if event.harvest_bonus_pct:
        parts.append(f"收成 +{event.harvest_bonus_pct}%")
    if event.free_water:
        parts.append("免澆水（種下立即生長）")
    if event.harvest_loss:
        parts.append(f"收成時每格被吹走 {event.harvest_loss} 個")
    return "、".join(parts)


def view(e, now: datetime) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "description": e.description,
        "crop_type_id": e.crop_type_id,
        **{k: getattr(e, k) for k in EFFECTS},
        "schedule_type": e.schedule_type,
        "schedule_label": balance.EVENT_SCHEDULE_TYPES[e.schedule_type],
        "starts_at": e.starts_at,
        "ends_at": e.ends_at,
        "start_minute": e.start_minute,
        "end_minute": e.end_minute,
        "weekdays": e.weekdays,
        "enabled": e.enabled,
        "active_now": is_active(e, now),
        "schedule_text": schedule_text(e),
        "effects_text": effects_text(e),
        "created_by": e.created_by,
        "updated_at": e.updated_at,
    }
