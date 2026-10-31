"""純遊戲規則：不依賴資料庫、CMD 或 Web，方便單元測試與重用。"""

from datetime import datetime, timedelta
from enum import StrEnum

from app.game import balance
from app.game.errors import ValidationError


class PlotState(StrEnum):
    EMPTY = "empty"
    WAITING_WATER = "waiting_water"
    GROWING = "growing"
    READY = "ready"
    WITHERED = "withered"


PLOT_STATE_LABELS = {
    PlotState.EMPTY: "空地",
    PlotState.WAITING_WATER: "等待澆水",
    PlotState.GROWING: "生長中",
    PlotState.READY: "可收成",
    PlotState.WITHERED: "已乾枯",
}


class EventType(StrEnum):
    CREATE_FARM = "create_farm"
    BUY_SEED = "buy_seed"
    PLANT = "plant"
    HARVEST = "harvest"
    SELL = "sell"
    UPGRADE = "upgrade"
    BUY_ITEM = "buy_item"
    USE_ITEM = "use_item"
    WATER = "water"
    CLEAR = "clear"
    STEAL = "steal"
    STOLEN = "stolen"
    BIND_GROUP = "bind_group"
    STEAL_BLOCKED = "steal_blocked"
    DEFENDED = "defended"
    DEFENSE_PLACE = "defense_place"
    BUILD = "build"
    BUY_ANIMAL = "buy_animal"
    FEED_ANIMAL = "feed_animal"
    COLLECT_ANIMAL = "collect_animal"
    CRAFT_START = "craft_start"
    CRAFT_COLLECT = "craft_collect"
    QUEST_REWARD = "quest_reward"
    COMPETITION_REWARD = "competition_reward"
    ADMIN_ADJUST = "admin_adjust"
    RANKING_REWARD = "ranking_reward"
    TRANSFER_GROUP = "transfer_group"
    GAMBLE = "gamble"
    EXPLORE = "explore"
    BUILD_MACHINE = "build_machine"
    EXCHANGE = "exchange"
    MAKE_SEED = "make_seed"


EVENT_TYPE_LABELS = {
    EventType.CREATE_FARM: "建立農場",
    EventType.BUY_SEED: "購買種子",
    EventType.PLANT: "種植",
    EventType.HARVEST: "收成",
    EventType.SELL: "販售",
    EventType.UPGRADE: "擴建土地",
    EventType.BUY_ITEM: "購買道具",
    EventType.USE_ITEM: "使用道具",
    EventType.WATER: "澆水",
    EventType.CLEAR: "清除乾枯",
    EventType.STEAL: "偷菜",
    EventType.STOLEN: "被偷",
    EventType.BIND_GROUP: "設定所屬群組",
    EventType.STEAL_BLOCKED: "偷菜被阻止",
    EventType.DEFENDED: "防守成功",
    EventType.DEFENSE_PLACE: "調整防守位置",
    EventType.BUILD: "建造設施",
    EventType.BUY_ANIMAL: "購買動物",
    EventType.FEED_ANIMAL: "餵食動物",
    EventType.COLLECT_ANIMAL: "收集動物產品",
    EventType.CRAFT_START: "開始加工",
    EventType.CRAFT_COLLECT: "領取加工品",
    EventType.QUEST_REWARD: "任務獎勵",
    EventType.COMPETITION_REWARD: "競賽獎勵",
    EventType.ADMIN_ADJUST: "管理員調整",
    EventType.RANKING_REWARD: "排名獎勵",
    EventType.TRANSFER_GROUP: "轉移所屬群組",
    EventType.GAMBLE: "賭場",
    EventType.EXPLORE: "山洞探索",
    EventType.BUILD_MACHINE: "製作機器",
    EventType.EXCHANGE: "假幣兌換",
    EventType.MAKE_SEED: "製造種子",
}


def seed_item_code(crop_code: str) -> str:
    return f"seed_{crop_code}"


def produce_item_code(crop_code: str) -> str:
    return crop_code


def plot_state(
    crop_type_id: int | None, watered_at: datetime | None, ready_at: datetime | None, now: datetime
) -> PlotState:
    """依時間即時結算土地狀態，不依賴任何儲存的狀態欄位。"""
    if crop_type_id is None:
        return PlotState.EMPTY
    if watered_at is None or ready_at is None:
        return PlotState.WAITING_WATER
    if now < ready_at:
        return PlotState.GROWING
    if now >= wither_time(ready_at):
        return PlotState.WITHERED
    return PlotState.READY


def wither_time(ready_at: datetime) -> datetime:
    return ready_at + timedelta(seconds=balance.WITHER_SECONDS)


def grow_seconds_with_effects(base_seconds: int, earthworm_active: bool) -> int:
    seconds = base_seconds * balance.EARTHWORM_GROW_MULTIPLIER if earthworm_active else base_seconds
    return max(1, round(seconds))


def remaining_seconds(ready_at: datetime | None, now: datetime) -> int:
    if ready_at is None:
        return 0
    return max(0, int((ready_at - now).total_seconds() + 0.999))


def ready_time(planted_at: datetime, grow_seconds: int) -> datetime:
    return planted_at + timedelta(seconds=grow_seconds)


def validate_quantity(quantity: int) -> int:
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise ValidationError("數量必須是整數。")
    if quantity <= 0:
        raise ValidationError("數量必須大於 0。")
    if quantity > balance.MAX_TRADE_QUANTITY:
        raise ValidationError(f"單次數量不可超過 {balance.MAX_TRADE_QUANTITY}。")
    return quantity


def validate_name(value: str, *, label: str, max_len: int) -> str:
    name = (value or "").strip()
    if not name:
        raise ValidationError(f"{label}不可為空白。")
    if len(name) > max_len:
        raise ValidationError(f"{label}最多 {max_len} 個字元。")
    if any(ord(ch) < 32 for ch in name):
        raise ValidationError(f"{label}不可包含控制字元。")
    return name


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小時 {minutes} 分 {sec} 秒"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def defense_covers(range_size: int | None, start_plot: int | None, plot_number: int) -> bool:
    """range_size 為 None 表示守護全部土地（例如警戒犬）。"""
    if range_size is None:
        return True
    return start_plot is not None and start_plot <= plot_number < start_plot + range_size


def resolve_defense(
    active: list[tuple[str, float]], roll: float, cap: float = balance.DEFENSE_MAX_BLOCK_CHANCE
) -> tuple[str | None, float]:
    """一輪防守判定：只擲一次亂數 roll ∈ [0, 1)。

    active 依判定順序排列（同種設施已去重）。依序累加「前面都沒擋下時本設施擋下」的機率，
    整體上限為 cap；roll 落在哪個區間就由該設施阻止。回傳 (阻止的設施代碼或 None, 整體阻止機率)。
    """
    cumulative = 0.0
    not_blocked = 1.0
    blocked_by = None
    for code, chance in active:
        share = min(not_blocked * chance, cap - cumulative)
        if share <= 0:
            break
        if blocked_by is None and roll < cumulative + share:
            blocked_by = code
        cumulative += share
        not_blocked *= 1 - chance
    return blocked_by, cumulative


class AnimalState(StrEnum):
    HUNGRY = "hungry"
    PRODUCING = "producing"
    READY = "ready"


ANIMAL_STATE_LABELS = {
    AnimalState.HUNGRY: "待餵食",
    AnimalState.PRODUCING: "生產中",
    AnimalState.READY: "可收集",
}


def animal_state(fed_at: datetime | None, ready_at: datetime | None, now: datetime) -> AnimalState:
    """未餵食時停止生產；餵食後到 ready_at 才能收集，收集後回到待餵食。"""
    if fed_at is None or ready_at is None:
        return AnimalState.HUNGRY
    return AnimalState.PRODUCING if now < ready_at else AnimalState.READY


class CraftState(StrEnum):
    IN_PROGRESS = "in_progress"
    READY = "ready"
    COLLECTED = "collected"


CRAFT_STATE_LABELS = {
    CraftState.IN_PROGRESS: "加工中",
    CraftState.READY: "待領取",
    CraftState.COLLECTED: "已領取",
}


def craft_state(status: str, ready_at: datetime, now: datetime) -> CraftState:
    if status == "collected":
        return CraftState.COLLECTED
    return CraftState.IN_PROGRESS if now < ready_at else CraftState.READY


def quest_amount(
    quest_type: str,
    metric: str,
    item_filter: str | None,
    event_type: str,
    item_code: str | None,
    quantity: int,
    gold_delta: int,
) -> int:
    """一筆成功的遊戲操作對某類型任務貢獻的進度（0 表示不相關）。"""
    if quest_type == "harvest" and event_type == "harvest":
        return quantity
    if quest_type == "sell" and event_type == "sell":
        return gold_delta if metric == "amount" else quantity
    if quest_type == "build" and event_type in ("upgrade", "build"):
        return 1
    if quest_type == "animal" and event_type == "collect_animal":
        return quantity if item_filter in (None, item_code) else 0
    if quest_type == "defense" and event_type == "defended":
        return 1
    return 0


def competition_points(comp_type: str, event_type: str, quantity: int, gold_delta: int) -> int:
    """一筆成功的遊戲操作在某類型競賽中的得分（0 表示不相關）。"""
    if comp_type == "harvest" and event_type == "harvest":
        return quantity
    if comp_type == "sales" and event_type == "sell":
        return gold_delta
    if comp_type == "development" and event_type in balance.DEVELOPMENT_POINTS:
        return balance.DEVELOPMENT_POINTS[event_type]
    if comp_type == "defense" and event_type == "defended":
        return 1
    return 0


def proportional_share(pool: int, mine: int, total: int) -> int:
    """依貢獻比例分配獎池（無條件捨去）。"""
    if total <= 0 or mine <= 0:
        return 0
    return pool * mine // total


def event_state(status: str, starts_at: datetime, ends_at: datetime, now: datetime) -> str:
    """任務／競賽的顯示狀態：scheduled／active／expired，或資料庫中的 completed／settled／cancelled。"""
    if status != "active":
        return status
    if now < starts_at:
        return "scheduled"
    return "active" if now < ends_at else "expired"


EVENT_STATE_LABELS = {
    "scheduled": "尚未開始",
    "active": "進行中",
    "expired": "已截止",
    "completed": "已完成",
    "settled": "已結算",
    "cancelled": "已取消",
}
