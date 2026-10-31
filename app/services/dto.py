"""服務層回傳的資料物件：與 ORM、CMD、Web、Discord 均無耦合。"""

from dataclasses import dataclass, field
from datetime import datetime

from app.game.rules import PlotState


@dataclass(frozen=True)
class PlayerInfo:
    id: int
    display_name: str
    has_farm: bool
    external_platform: str | None = None
    external_user_id: str | None = None


@dataclass(frozen=True)
class CropInfo:
    code: str
    name: str
    seed_price: int
    grow_seconds: int
    harvest_quantity: int
    sell_price: int
    shop_buyable: bool = True  # false：稀有作物，商店不販售種子


@dataclass(frozen=True)
class PlotInfo:
    plot_number: int
    state: PlotState
    crop_code: str | None
    crop_name: str | None
    planted_at: datetime | None
    ready_at: datetime | None
    remaining_seconds: int
    watered_at: datetime | None = None
    remaining_quantity: int | None = None
    fertilized: bool = False
    quality: str | None = None
    mutation: str | None = None


@dataclass(frozen=True)
class InventoryEntry:
    item_code: str
    display_name: str
    quantity: int


@dataclass(frozen=True)
class Inventory:
    items: list[InventoryEntry]
    used: int
    capacity: int


@dataclass(frozen=True)
class FarmStatus:
    farm_id: int
    player_id: int
    home_group_id: int | None
    player_name: str
    name: str
    level: int
    gold: int
    warehouse_used: int
    warehouse_capacity: int
    plots: list[PlotInfo]
    now: datetime

    @property
    def empty_plots(self) -> int:
        return sum(1 for p in self.plots if p.state == PlotState.EMPTY)

    @property
    def growing_plots(self) -> int:
        return sum(1 for p in self.plots if p.state == PlotState.GROWING)

    @property
    def ready_plots(self) -> int:
        return sum(1 for p in self.plots if p.state == PlotState.READY)


@dataclass(frozen=True)
class ActionResult:
    """一次成功的遊戲操作結果。"""

    message: str
    gold: int
    gold_delta: int = 0
    item_code: str | None = None
    quantity: int = 0
    success: bool = True  # 例如偷菜被防守阻止時為 False（操作本身已完成並記錄）


@dataclass(frozen=True)
class TransactionInfo:
    created_at: datetime
    event_type: str
    item_code: str | None
    quantity: int
    gold_delta: int
    description: str


# ---------- 背包 ----------


@dataclass(frozen=True)
class BackpackLine:
    code: str
    name: str
    quantity: int
    price: int  # 種子：商店售價；農產品：依品質與突變計算的單價；道具：商店售價
    description: str = ""
    quality: str = "normal"
    mutation: str = "none"


@dataclass(frozen=True)
class ToolStatus:
    code: str
    name: str
    owned: bool
    enabled: bool
    acquired_at: datetime | None
    description: str


@dataclass(frozen=True)
class EffectStatus:
    code: str
    name: str
    status: str  # inactive / active / expired
    started_at: datetime | None
    expires_at: datetime | None
    remaining_seconds: int
    multiplier: float | None
    description: str


EFFECT_STATUS_LABELS = {"inactive": "未啟用", "active": "有效中", "expired": "已過期"}


@dataclass(frozen=True)
class Backpack:
    player_id: int
    seeds: list[BackpackLine]
    produce: list[BackpackLine]
    consumables: list[BackpackLine]
    tools: list[ToolStatus]
    effects: list[EffectStatus]
    used: int
    capacity: int
    now: datetime
    goods: list[BackpackLine] = field(default_factory=list)  # 動物產品與加工品
    prizes: list[dict] = field(default_factory=list)  # 賭場獎品（價值倍率 ×2／×5）
    materials: list[BackpackLine] = field(default_factory=list)  # 山洞探索素材與假幣（price 為假幣兌換價，素材為 0）
    material_used: int = 0  # 素材背包（不佔倉庫）
    material_capacity: int = 0

    @property
    def is_empty(self) -> bool:
        return (
            not (self.seeds or self.produce or self.consumables or self.goods or self.prizes or self.materials)
            and not any(t.owned for t in self.tools)
            and all(e.status == "inactive" for e in self.effects)
        )


# ---------- 群組與偷菜 ----------


@dataclass(frozen=True)
class GroupInfo:
    id: int
    name: str
    discord_guild_id: str | None
    member_count: int
    created_at: datetime
    is_member: bool = False


@dataclass(frozen=True)
class StealTarget:
    player_id: int
    player_name: str
    farm_name: str
    plot_number: int
    crop_name: str
    remaining_quantity: int
    max_steal: int


@dataclass(frozen=True)
class StealFarmView:
    """偷菜時查看同群組成員的農田：只有土地狀態，不含金幣、倉庫等資訊。"""

    player_id: int
    player_name: str
    farm_name: str
    plots: list[PlotInfo]
    now: datetime
    targets: list[StealTarget]  # 這次可以偷的土地
    limit_reached: bool  # 今天已經偷過這位玩家（每日土地上限）
    warehouse_full: bool  # 偷菜者的倉庫已滿


@dataclass(frozen=True)
class ExternalContext:
    """外部平台（Discord）一次互動對應的群組與玩家；ID 皆由平台互動來源取得，不接受玩家輸入。"""

    group_id: int | None
    group_name: str | None
    player_id: int | None
    player_name: str | None
    has_farm: bool
