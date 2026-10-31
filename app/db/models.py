"""SQLAlchemy 資料模型。資料表結構由 Alembic Migration 管理，不在啟動時自動建立。"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EVENT_TYPES = (
    "create_farm",
    "buy_seed",
    "plant",
    "harvest",
    "sell",
    "upgrade",
    "buy_item",
    "use_item",
    "water",
    "clear",
    "steal",
    "stolen",
    "bind_group",
    "steal_blocked",
    "defended",
    "defense_place",
    "build",
    "buy_animal",
    "feed_animal",
    "collect_animal",
    "craft_start",
    "craft_collect",
    "quest_reward",
    "competition_reward",
    "admin_adjust",
    "ranking_reward",
    "transfer_group",
    "gamble",
    "explore",
    "build_machine",
    "exchange",
    "make_seed",
)

QUALITY_CODES = ("normal", "good", "rare", "legendary")
MUTATION_CODES = ("none", "giant", "golden", "fast", "variant")
_QUALITY_SQL = ", ".join(f"'{q}'" for q in QUALITY_CODES)
_MUTATION_SQL = ", ".join(f"'{m}'" for m in MUTATION_CODES)

PLOT_CROP_CONSISTENCY = (
    "(crop_type_id IS NULL AND planted_at IS NULL AND watered_at IS NULL AND ready_at IS NULL "
    "AND remaining_quantity IS NULL AND NOT fertilized) OR "
    "(crop_type_id IS NOT NULL AND planted_at IS NOT NULL AND remaining_quantity IS NOT NULL "
    "AND (watered_at IS NULL) = (ready_at IS NULL) "
    "AND (watered_at IS NULL OR (watered_at >= planted_at AND ready_at > watered_at)))"
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (
        CheckConstraint("length(btrim(display_name)) BETWEEN 1 AND 32", name="display_name_len"),
        # 外部平台（例如 discord）與其使用者 ID 必須同時存在或同時為空
        CheckConstraint(
            "(external_platform IS NULL) = (external_user_id IS NULL)", name="external_pair"
        ),
        Index(
            "uq_players_external_identity",
            "external_platform",
            "external_user_id",
            unique=True,
            postgresql_where=text("external_platform IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(32), nullable=False)
    external_platform: Mapped[str | None] = mapped_column(String(32))
    external_user_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    farm: Mapped["Farm | None"] = relationship(back_populates="player", uselist=False)


class CropType(Base):
    __tablename__ = "crop_types"
    __table_args__ = (
        CheckConstraint("seed_price > 0", name="seed_price_positive"),
        CheckConstraint("grow_seconds > 0", name="grow_seconds_positive"),
        CheckConstraint("harvest_quantity > 0", name="harvest_quantity_positive"),
        CheckConstraint("sell_price >= 0", name="sell_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    seed_price: Mapped[int] = mapped_column(Integer, nullable=False)
    grow_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    harvest_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    sell_price: Mapped[int] = mapped_column(Integer, nullable=False)
    # 珍稀變種（收成後的獨立農產品代碼與名稱）；代碼建立後不可修改
    variant_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    variant_name: Mapped[str] = mapped_column(String(32), nullable=False)
    # 停用：不能購買種子或種植；已種植、已持有的仍可收成、販售
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # false：商店不販售種子（稀有作物，只能探索或培育取得），仍可種植已持有的種子
    shop_buyable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class AnimalType(Base):
    """動物種類與其產品（產品代碼即背包物品代碼）。停用後不能購買，已飼養的仍可餵食、收集與販售產品。"""

    __tablename__ = "animal_types"
    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("feed_cost >= 0", name="feed_cost_non_negative"),
        CheckConstraint("cycle_seconds > 0", name="cycle_seconds_positive"),
        CheckConstraint("product_quantity > 0", name="product_quantity_positive"),
        CheckConstraint("product_price >= 0", name="product_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    feed_cost: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    product_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    product_name: Mapped[str] = mapped_column(String(32), nullable=False)
    product_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    product_price: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Farm(Base):
    __tablename__ = "farms"
    __table_args__ = (
        CheckConstraint("gold >= 0", name="gold_non_negative"),
        CheckConstraint("level >= 1", name="level_positive"),
        CheckConstraint("warehouse_capacity > 0", name="warehouse_capacity_positive"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name="name_len"),
        # 金幣排行（全部期間）只取前幾名
        Index("ix_farms_gold_rank", text("gold DESC"), "player_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # MVP：一位玩家只擁有一座農場
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # 主要所屬群組：建立時設定；之後只能透過帶 7 天冷卻的轉移變更（資料庫觸發器保護）；舊資料可為空，允許一次性設定
    home_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="RESTRICT"), index=True
    )
    # 最近一次成功轉移所屬群組的時間；NULL 表示從未轉移（首次轉移不需等待）
    last_group_transfer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    gold: Mapped[int] = mapped_column(BigInteger, nullable=False)
    warehouse_capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    player: Mapped[Player] = relationship(back_populates="farm")


class FarmStats(Base):
    """每座農場的累積統計（全部期間排行用），在販售等交易的同一筆資料庫交易中更新。

    可由 game_transactions 重算（repositories/farm_stats.rebuild），並以 check 比對一致性。
    non_operating_gold：建立農場、管理員調整與各種獎勵的金幣加總；全部期間淨賺金幣 = 目前金幣 - 此值。
    """

    __tablename__ = "farm_stats"
    __table_args__ = (
        Index(
            "ix_farm_stats_crop_rank",
            text("crop_sold_quantity DESC"),
            text("crop_sales_income DESC"),
            "player_id",
        ),
        Index(
            "ix_farm_stats_goods_rank",
            text("goods_sales_income DESC"),
            text("goods_sold_quantity DESC"),
            "player_id",
        ),
    )

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    player_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    crop_sold_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    crop_sales_income: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    goods_sold_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    goods_sales_income: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    non_operating_gold: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class FarmDailyStats(Base):
    """每座農場每個台灣日期的販售與淨賺金幣，供本日／本週／本月排行與排名獎勵結算（避免掃描大量交易紀錄）。"""

    __tablename__ = "farm_daily_stats"
    __table_args__ = (Index("ix_farm_daily_stats_day", "day"),)

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    crop_sold_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    crop_sales_income: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    goods_sold_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    goods_sales_income: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    net_gold: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class FarmPlot(Base):
    __tablename__ = "farm_plots"
    __table_args__ = (
        UniqueConstraint("farm_id", "plot_number", name="uq_farm_plots_farm_plot_number"),
        CheckConstraint("plot_number >= 1", name="plot_number_positive"),
        CheckConstraint("remaining_quantity >= 1", name="remaining_quantity_positive"),
        CheckConstraint("planting_no >= 0", name="planting_no_non_negative"),
        # 空地時作物相關欄位全部為空；有作物時必有播種時間與剩餘數量，
        # 澆水時間與成熟時間同時存在（未澆水時同時為空）
        CheckConstraint(PLOT_CROP_CONSISTENCY, name="crop_consistency"),
        CheckConstraint(
            "(crop_type_id IS NULL) = (quality IS NULL) AND (crop_type_id IS NULL) = (mutation IS NULL)",
            name="genetics_consistency",
        ),
        CheckConstraint(f"quality IS NULL OR quality IN ({_QUALITY_SQL})", name="quality_valid"),
        CheckConstraint(f"mutation IS NULL OR mutation IN ({_MUTATION_SQL})", name="mutation_valid"),
        Index("ix_farm_plots_ready_at", "ready_at", postgresql_where=text("crop_type_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(
        ForeignKey("farms.id", ondelete="CASCADE"), nullable=False
    )
    plot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    crop_type_id: Mapped[int | None] = mapped_column(
        ForeignKey("crop_types.id", ondelete="RESTRICT"), index=True
    )
    planted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remaining_quantity: Mapped[int | None] = mapped_column(Integer)
    fertilized: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    # 播種時決定、之後固定不變的品質與主要突變
    quality: Mapped[str | None] = mapped_column(String(16))
    mutation: Mapped[str | None] = mapped_column(String(16))
    # 每次種植加 1，用來識別「同一株作物」（偷菜每人每株限一次）
    planting_no: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    crop_type: Mapped[CropType | None] = relationship()


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        # 同一種物品的不同品質或突變分開存放，不會混成同一筆庫存
        UniqueConstraint("farm_id", "item_code", "quality", "mutation", name="uq_inventory_items_farm_item"),
        CheckConstraint(f"quality IN ({_QUALITY_SQL})", name="quality_valid"),
        CheckConstraint(f"mutation IN ({_MUTATION_SQL})", name="mutation_valid"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(
        ForeignKey("farms.id", ondelete="CASCADE"), nullable=False
    )
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    quality: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    mutation: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class GamblePrize(Base):
    """賭場獎品：贏得的作物以「價值倍率」存放在倉庫（佔倉庫空間），可以賣出或保留展示。"""

    __tablename__ = "gamble_prizes"
    __table_args__ = (
        UniqueConstraint(
            "farm_id", "item_code", "quality", "mutation", "multiplier", name="uq_gamble_prizes_farm_stack"
        ),
        CheckConstraint(f"quality IN ({_QUALITY_SQL})", name="quality_valid"),
        CheckConstraint(f"mutation IN ({_MUTATION_SQL})", name="mutation_valid"),
        CheckConstraint("multiplier IN (2, 5)", name="multiplier_valid"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    quality: Mapped[str] = mapped_column(String(16), nullable=False)
    mutation: Mapped[str] = mapped_column(String(16), nullable=False)
    multiplier: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class GameTransaction(Base):
    __tablename__ = "game_transactions"
    __table_args__ = (
        CheckConstraint(
            "event_type IN (" + ", ".join(f"'{e}'" for e in EVENT_TYPES) + ")",
            name="event_type_valid",
        ),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        Index("ix_game_transactions_farm_created", "farm_id", "created_at"),
        Index("ix_game_transactions_created_at", "created_at"),
        # 販售排行榜依歷史販售紀錄加總
        Index("ix_game_transactions_sell_farm", "farm_id", postgresql_where=text("event_type = 'sell'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(
        ForeignKey("farms.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_code: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    gold_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 操作實際發生時所在的群組（供未來群組任務／競賽）；不影響農場的 home_group_id 與排行
    # 作物相關操作的品質與突變；販售時保存實際成交單價（總收入為 gold_delta），歷史紀錄不會被改寫
    quality: Mapped[str | None] = mapped_column(String(16))
    mutation: Mapped[str | None] = mapped_column(String(16))
    unit_price: Mapped[int | None] = mapped_column(BigInteger)
    context_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), index=True
    )
    # 這次操作套用的限時事件 [{"id", "name"}]；沒有套用時為 NULL
    applied_events: Mapped[list | None] = mapped_column(JSON)


class Group(Base):
    """CMD 模擬群組；未來可對應 Discord Guild。"""

    __tablename__ = "groups"
    __table_args__ = (CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name="name_len"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    discord_guild_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = _created_at()


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (Index("ix_group_members_player_id", "player_id"),)

    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), primary_key=True)
    joined_at: Mapped[datetime] = _created_at()


class FarmTool(Base):
    """永久工具（例如自動澆水器）。"""

    __tablename__ = "farm_tools"
    __table_args__ = (UniqueConstraint("farm_id", "tool_code", name="uq_farm_tools_farm_tool"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    tool_code: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 機器設定（例如自動加工機的加工項目 {"recipe": "flour"}）；一般工具為空
    settings: Mapped[dict | None] = mapped_column(JSON)


class FarmExploration(Base):
    """各區域的探索次數與最近一次探險（出發時間、回來時間）：每座農場每個區域一列。
    一次只能進行一趟探險，由 exploration_logs 的未領取紀錄判斷（Migration 0015）。"""

    __tablename__ = "farm_explorations"
    __table_args__ = (
        UniqueConstraint("farm_id", "area_code", name="uq_farm_explorations_farm_area"),
        CheckConstraint("area_code IN ('grassland', 'forest', 'mine')", name="area_code_valid"),
        CheckConstraint("next_available_at > last_explored_at", name="next_after_last"),
        CheckConstraint("explore_count >= 1", name="explore_count_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    area_code: Mapped[str] = mapped_column(String(16), nullable=False)
    last_explored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    explore_count: Mapped[int] = mapped_column(Integer, nullable=False)


class ExplorationLog(Base):
    """探險紀錄：出發時決定獎勵明細，回來（next_available_at）後領取才放進背包（claimed_at）。
    claimed_at 為 NULL 的紀錄就是進行中或待領取的探險，每座農場最多一筆（部分唯一索引）。"""

    __tablename__ = "exploration_logs"
    __table_args__ = (
        CheckConstraint("area_code IN ('grassland', 'forest', 'mine')", name="area_code_valid"),
        Index("ix_exploration_logs_farm_explored", "farm_id", "explored_at"),
        Index(
            "uq_exploration_logs_farm_unclaimed", "farm_id", unique=True, postgresql_where=text("claimed_at IS NULL")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    area_code: Mapped[str] = mapped_column(String(16), nullable=False)
    farm_level: Mapped[int] = mapped_column(Integer, nullable=False)
    advanced: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rewards: Mapped[dict] = mapped_column(JSON, nullable=False)  # {物品代碼: 數量}
    events: Mapped[list] = mapped_column(JSON, nullable=False)  # 觸發的特殊事件名稱
    explored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 出發時間
    next_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 回來時間
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 領取獎勵時間


class FarmEffect(Base):
    """限時效果（例如人工養殖蚯蚓），以到期時間判斷是否有效。"""

    __tablename__ = "farm_effects"
    __table_args__ = (
        UniqueConstraint("farm_id", "effect_code", name="uq_farm_effects_farm_effect"),
        CheckConstraint("expires_at > started_at", name="expires_after_start"),
        CheckConstraint("multiplier > 0", name="multiplier_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    effect_code: Mapped[str] = mapped_column(String(64), nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StealRecord(Base):
    __tablename__ = "steal_records"
    __table_args__ = (
        # 同一位偷菜者對同一株作物（同一土地的同一次種植）只能偷一次
        UniqueConstraint("plot_id", "planting_no", "thief_player_id", name="uq_steal_records_once_per_planting"),
        CheckConstraint("quantity BETWEEN 1 AND 2", name="quantity_range"),
        CheckConstraint("thief_player_id <> victim_player_id", name="not_self"),
        # 成功的紀錄沒有阻止設施；被阻止的紀錄必須記錄由哪個設施阻止
        CheckConstraint(
            "(outcome = 'success' AND blocked_by IS NULL) OR (outcome = 'blocked' AND blocked_by IS NOT NULL)",
            name="outcome_consistency",
        ),
        CheckConstraint("block_chance IS NULL OR (block_chance >= 0 AND block_chance <= 1)", name="block_chance_range"),
        Index("ix_steal_records_victim_created", "victim_player_id", "created_at"),
        Index("ix_steal_records_created_at", "created_at"),
        Index("ix_steal_records_group_created", "group_id", "created_at"),
        Index("ix_steal_records_thief", "thief_player_id"),
        Index("ix_steal_records_victim", "victim_player_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    thief_player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    victim_player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    # 土地被管理員刪除時保留偷菜紀錄（plot_id 設為 NULL，plot_number 仍保留）
    plot_id: Mapped[int | None] = mapped_column(ForeignKey("farm_plots.id", ondelete="SET NULL"))
    plot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    planting_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    crop_type_id: Mapped[int] = mapped_column(ForeignKey("crop_types.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # success：成功偷走 quantity 個；blocked：被 blocked_by 設施阻止，雙方物品都不變（quantity 為嘗試數量）
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, server_default="success")
    blocked_by: Mapped[str | None] = mapped_column(String(32))
    block_chance: Mapped[float | None] = mapped_column(Float)  # 當次整體阻止機率（無防守時為 NULL）
    roll: Mapped[float | None] = mapped_column(Float)  # 當次亂數值，供稽核


class FarmDefense(Base):
    """永久防守設施。slot 為同種設施的編號（1..上限），由服務層在鎖定農場後分配。"""

    __tablename__ = "farm_defenses"
    __table_args__ = (
        UniqueConstraint("farm_id", "device_code", "slot", name="uq_farm_defenses_farm_device_slot"),
        CheckConstraint("slot >= 1", name="slot_positive"),
        CheckConstraint("start_plot IS NULL OR start_plot >= 1", name="start_plot_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    device_code: Mapped[str] = mapped_column(String(32), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    # 守護範圍起點（連續 N 格）；守護全部土地的設施為 NULL
    start_plot: Mapped[int | None] = mapped_column(Integer)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FarmBuilding(Base):
    """畜舍與加工設施。slot 為同種建築編號（1..上限），由服務層在鎖定農場後分配。"""

    __tablename__ = "farm_buildings"
    __table_args__ = (
        UniqueConstraint("farm_id", "building_code", "slot", name="uq_farm_buildings_farm_building_slot"),
        CheckConstraint("slot >= 1", name="slot_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    building_code: Mapped[str] = mapped_column(String(32), nullable=False)
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FarmAnimal(Base):
    """動物：fed_at／ready_at 同時為空表示待餵食；ready_at 到了才能收集，收集後清空。"""

    __tablename__ = "farm_animals"
    __table_args__ = (
        UniqueConstraint("farm_id", "animal_no", name="uq_farm_animals_farm_animal_no"),
        CheckConstraint("animal_no >= 1", name="animal_no_positive"),
        CheckConstraint(
            "(fed_at IS NULL AND ready_at IS NULL) OR (fed_at IS NOT NULL AND ready_at > fed_at)",
            name="cycle_consistency",
        ),
        CheckConstraint("cycles_completed >= 0", name="cycles_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    animal_no: Mapped[int] = mapped_column(Integer, nullable=False)  # 農場內的動物編號
    animal_code: Mapped[str] = mapped_column(
        ForeignKey("animal_types.code", ondelete="RESTRICT"), nullable=False
    )
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cycles_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class CraftOrder(Base):
    """加工訂單：開單時已扣除原料（記錄在 inputs）；status 只有 in_progress／collected，完成與否依 ready_at 判斷。"""

    __tablename__ = "craft_orders"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress', 'collected')", name="status_valid"),
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint("output_quantity >= 1", name="output_quantity_positive"),
        CheckConstraint("ready_at > started_at", name="ready_after_start"),
        CheckConstraint(
            "(status = 'collected') = (collected_at IS NOT NULL)", name="collected_consistency"
        ),
        Index("ix_craft_orders_farm_status", "farm_id", "status"),
        Index("ix_craft_orders_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    recipe_code: Mapped[str] = mapped_column(String(32), nullable=False)
    building_code: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)  # 份數
    inputs: Mapped[dict] = mapped_column(JSON, nullable=False)  # 已扣除的原料 {代碼: 數量}
    output_code: Mapped[str] = mapped_column(String(64), nullable=False)
    output_quantity: Mapped[int] = mapped_column(Integer, nullable=False)  # 成品總數
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="in_progress")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminAction(Base):
    """管理員操作紀錄（建立、取消、結算任務與競賽等）。只新增，不修改或刪除。"""

    __tablename__ = "admin_actions"
    __table_args__ = (Index("ix_admin_actions_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Quest(Base):
    """群組共同任務。目標、期間與獎勵條件建立後不可修改（資料庫觸發器保護）。"""

    __tablename__ = "quests"
    __table_args__ = (
        CheckConstraint("quest_type IN ('harvest', 'sell', 'build', 'animal', 'defense')", name="type_valid"),
        CheckConstraint("metric IN ('quantity', 'amount')", name="metric_valid"),
        CheckConstraint("metric = 'quantity' OR quest_type = 'sell'", name="amount_only_for_sell"),
        CheckConstraint("item_filter IS NULL OR quest_type = 'animal'", name="filter_only_for_animal"),
        CheckConstraint("target > 0", name="target_positive"),
        CheckConstraint("reward_mode IN ('fixed', 'proportional')", name="reward_mode_valid"),
        CheckConstraint("reward_amount > 0", name="reward_amount_positive"),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        CheckConstraint("status IN ('active', 'completed', 'cancelled')", name="status_valid"),
        CheckConstraint("progress >= 0", name="progress_non_negative"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completed_consistency"),
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL)",
            name="cancelled_consistency",
        ),
        Index("ix_quests_group_status", "group_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    quest_type: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(16), nullable=False)
    item_filter: Mapped[str | None] = mapped_column(String(32))
    target: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reward_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reward_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    progress: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuestContribution(Base):
    __tablename__ = "quest_contributions"
    __table_args__ = (
        # 同一筆遊戲操作對同一任務只計算一次
        UniqueConstraint("quest_id", "transaction_id", name="uq_quest_contributions_once"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_quest_contributions_quest_player", "quest_id", "player_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("game_transactions.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QuestClaim(Base):
    __tablename__ = "quest_claims"
    __table_args__ = (
        UniqueConstraint("quest_id", "player_id", name="uq_quest_claims_once"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    quest_id: Mapped[int] = mapped_column(ForeignKey("quests.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    reward_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Competition(Base):
    """群組競賽。類型、期間與獎勵建立後不可修改（資料庫觸發器保護）。"""

    __tablename__ = "competitions"
    __table_args__ = (
        CheckConstraint("comp_type IN ('harvest', 'sales', 'development', 'defense')", name="type_valid"),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        CheckConstraint("status IN ('active', 'settled', 'cancelled')", name="status_valid"),
        CheckConstraint("(status = 'settled') = (settled_at IS NOT NULL)", name="settled_consistency"),
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL)",
            name="cancelled_consistency",
        ),
        Index("ix_competitions_status_ends", "status", "ends_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    comp_type: Mapped[str] = mapped_column(String(16), nullable=False)
    rewards: Mapped[list] = mapped_column(JSON, nullable=False)  # 各名次每位成員的金幣獎勵
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompetitionContribution(Base):
    __tablename__ = "competition_contributions"
    __table_args__ = (
        UniqueConstraint("competition_id", "transaction_id", name="uq_competition_contributions_once"),
        CheckConstraint("points > 0", name="points_positive"),
        Index("ix_competition_contributions_comp_group", "competition_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("game_transactions.id", ondelete="CASCADE"), nullable=False
    )
    points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CompetitionResult(Base):
    """結算時的群組排名快照（保存群組名稱，之後成員或名稱變動不影響歷史成績）。"""

    __tablename__ = "competition_results"

    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    group_name: Mapped[str] = mapped_column(String(40), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contributors: Mapped[int] = mapped_column(Integer, nullable=False)


class CompetitionAward(Base):
    """結算獎勵發放紀錄；每位玩家每場競賽最多一筆。"""

    __tablename__ = "competition_awards"
    __table_args__ = (CheckConstraint("gold > 0", name="gold_positive"),)

    competition_id: Mapped[int] = mapped_column(
        ForeignKey("competitions.id", ondelete="CASCADE"), primary_key=True
    )
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    player_name: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    gold: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CropGeneticsSetting(Base):
    """品質與突變設定的版本紀錄；最新版本生效，舊版本保留供稽核（只新增，不修改）。"""

    __tablename__ = "crop_genetics_settings"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


EVENT_SCHEDULES = ("once", "daily", "weekly")


class GameEvent(Base):
    """限時全服加成事件。時間視窗以台灣時間解讀；分鐘數 0–1440，不跨午夜。"""

    __tablename__ = "game_events"
    __table_args__ = (
        CheckConstraint("schedule_type IN ('once', 'daily', 'weekly')", name="schedule_type_valid"),
        CheckConstraint(
            "schedule_type <> 'once' OR (starts_at IS NOT NULL AND ends_at IS NOT NULL)", name="once_has_period"
        ),
        CheckConstraint("starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at", name="ends_after_start"),
        CheckConstraint(
            "schedule_type = 'once' OR (start_minute IS NOT NULL AND end_minute IS NOT NULL)", name="window_required"
        ),
        CheckConstraint(
            "start_minute IS NULL OR (start_minute >= 0 AND end_minute > start_minute AND end_minute <= 1440)",
            name="window_range",
        ),
        CheckConstraint(
            "schedule_type <> 'weekly' OR (weekdays BETWEEN 1 AND 127)", name="weekdays_valid"
        ),
        CheckConstraint("sell_price_pct BETWEEN 100 AND 300", name="sell_price_pct_range"),
        CheckConstraint("grow_reduction_pct BETWEEN 0 AND 90", name="grow_reduction_pct_range"),
        CheckConstraint("quality_bonus_bp BETWEEN 0 AND 3000", name="quality_bonus_bp_range"),
        CheckConstraint("mutation_bonus_bp BETWEEN 0 AND 3000", name="mutation_bonus_bp_range"),
        CheckConstraint("harvest_bonus_pct BETWEEN 0 AND 200", name="harvest_bonus_pct_range"),
        CheckConstraint("free_water BETWEEN 0 AND 1", name="free_water_range"),
        CheckConstraint("harvest_loss BETWEEN 0 AND 10", name="harvest_loss_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")
    # 只套用到指定作物（含其珍稀變種）；NULL 表示全部作物
    crop_type_id: Mapped[int | None] = mapped_column(ForeignKey("crop_types.id", ondelete="RESTRICT"), index=True)
    sell_price_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    grow_reduction_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    quality_bonus_bp: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    mutation_bonus_bp: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    harvest_bonus_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    free_water: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # 1 = 種下立即澆水
    harvest_loss: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # 收成時每格少 N 個
    schedule_type: Mapped[str] = mapped_column(String(8), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_minute: Mapped[int | None] = mapped_column(Integer)
    end_minute: Mapped[int | None] = mapped_column(Integer)
    weekdays: Mapped[int | None] = mapped_column(Integer)  # 位元遮罩：週一 = 1、週二 = 2 … 週日 = 64
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RankingRewardRule(Base):
    """排名獎勵規則。active_since：本次啟用（或變更週期）的時間，只結算在此之後結束的期間，不會回溯補發。"""

    __tablename__ = "ranking_reward_rules"
    __table_args__ = (
        CheckConstraint("scope IN ('group', 'individual')", name="scope_valid"),
        CheckConstraint("metric IN ('money', 'sales', 'goods')", name="metric_valid"),
        CheckConstraint("period IN ('daily', 'weekly', 'monthly')", name="period_valid"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 60", name="name_len"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(16), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    rewards: Mapped[list] = mapped_column(JSON, nullable=False)  # 依名次 [{"reward_type", "amount"}]
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    active_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RankingSettlement(Base):
    """一條規則在一個期間的結算結果（規則與排名快照）；(rule_id, period_start) 唯一，同一期間不會重複發獎。"""

    __tablename__ = "ranking_settlements"
    __table_args__ = (
        UniqueConstraint("rule_id", "period_start", name="uq_ranking_settlements_rule_period"),
        CheckConstraint("period_end > period_start", name="period_valid"),
        Index("ix_ranking_settlements_settled_at", "settled_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # 規則刪除後保留歷史結算（rule_id 設為 NULL，規則內容保存在 rule_snapshot）
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("ranking_reward_rules.id", ondelete="SET NULL"))
    rule_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ranking: Mapped[list] = mapped_column(JSON, nullable=False)
    settled_by: Mapped[str] = mapped_column(String(64), nullable=False)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RankingAward(Base):
    """排名獎勵發放紀錄；每位玩家在同一次結算最多一筆。"""

    __tablename__ = "ranking_awards"
    __table_args__ = (
        UniqueConstraint("settlement_id", "player_id", name="uq_ranking_awards_once"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        Index("ix_ranking_awards_player", "player_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    settlement_id: Mapped[int] = mapped_column(
        ForeignKey("ranking_settlements.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    player_name: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reward_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
