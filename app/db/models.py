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
    SmallInteger,
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
    "fishing_buy",
    "fish_sell",
    "release_animal",
    "boss_donate",
    "boss_reward",
    "rename_farm",
    "checkin_reward",
    "achievement_reward",
    "ruins_donate",
    "ruins_guardian",
    "rumor_treasure",
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
        CheckConstraint(
            "(hour_start IS NULL) = (hour_end IS NULL) AND (hour_start IS NULL OR "
            "(hour_start BETWEEN 0 AND 23 AND hour_end BETWEEN 0 AND 23 AND hour_start <> hour_end))",
            name="hours_valid",
        ),
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
    # 季節與時段限定（game/seasons.py；NULL = 不限，Migration 0034）
    seasons: Mapped[list | None] = mapped_column(JSON)
    hour_start: Mapped[int | None] = mapped_column(SmallInteger)
    hour_end: Mapped[int | None] = mapped_column(SmallInteger)


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
        CheckConstraint("greenhouse_level BETWEEN 0 AND 10", name="greenhouse_level_valid"),
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
    title: Mapped[str | None] = mapped_column(String(20))  # 目前選用的稱號（必須在 farm_titles 中），NULL 不顯示
    # 溫室等級（0 = 沒有溫室）：每一級讓之後澆水的作物生長時間減少 special_shop.greenhouse_pct_per_level()%
    greenhouse_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    player: Mapped[Player] = relationship(back_populates="farm")


class FarmStats(Base):
    """每座農場的累積統計（全部期間排行用），在販售等交易的同一筆資料庫交易中更新。

    可由 game_transactions 重算（repositories/farm_stats.rebuild），並以 check 比對一致性。
    non_operating_gold：建立農場、管理員調整與各種獎勵的金幣加總；全部期間淨賺金幣 = 目前金幣 - 此值。
    steal_count／defended_count／gamble_count：偷菜成功、防守成功、賭場的次數（成就進度用，不必掃描交易紀錄）。
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
    steal_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    defended_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    gamble_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


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
        CheckConstraint("area_code IN ('grassland', 'forest', 'mine', 'ruins')", name="area_code_valid"),
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
        CheckConstraint("area_code IN ('grassland', 'forest', 'mine', 'ruins')", name="area_code_valid"),
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


# ---------- 釣魚（Migration 0016） ----------

FISH_RARITY_SQL = "'common', 'rare', 'epic', 'legendary'"
FISHING_STATUSES_SQL = (
    "'casting', 'fighting', 'landed', 'escaped', 'line_broken', 'slipped', 'missed', 'timeout', 'abandoned'"
)


class FishType(Base):
    """魚種（管理網站可新增、修改、停用）。代碼建立後不可修改；停用後不會再被釣到，已釣到的魚仍可販售。"""

    __tablename__ = "fish_types"
    __table_args__ = (
        CheckConstraint(f"rarity IN ({FISH_RARITY_SQL})", name="rarity_valid"),
        CheckConstraint("min_price <= max_price", name="price_order"),
        CheckConstraint("min_price >= 0", name="price_non_negative"),  # 售價不受稀有度限制（Migration 0017）
        CheckConstraint("min_weight_g > 0 AND min_weight_g <= max_weight_g", name="weight_order"),
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="difficulty_range"),
        CheckConstraint(
            "(hour_start IS NULL) = (hour_end IS NULL) AND (hour_start IS NULL OR "
            "(hour_start BETWEEN 0 AND 23 AND hour_end BETWEEN 0 AND 23 AND hour_start <> hour_end))",
            name="hours_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False, server_default="🐟")
    rarity: Mapped[str] = mapped_column(String(16), nullable=False)
    min_price: Mapped[int] = mapped_column(Integer, nullable=False)
    max_price: Mapped[int] = mapped_column(Integer, nullable=False)
    min_weight_g: Mapped[int] = mapped_column(Integer, nullable=False)
    max_weight_g: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")  # 掙扎難度（體力倍率）
    description: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # 只有裝備這些魚餌（fishing_gear.code 清單）時才釣得到；NULL = 任何魚餌都可以（Migration 0033）
    required_baits: Mapped[list | None] = mapped_column(JSON)
    # 季節與時段限定（game/seasons.py；NULL = 不限，Migration 0034）
    seasons: Mapped[list | None] = mapped_column(JSON)
    hour_start: Mapped[int | None] = mapped_column(SmallInteger)
    hour_end: Mapped[int | None] = mapped_column(SmallInteger)


class FishingGear(Base):
    """釣具商品：魚竿、魚線、魚餌。effects 依類型（app/game/fishing.GEAR_EFFECT_LIMITS）；stock NULL 表示不限量。"""

    __tablename__ = "fishing_gear"
    __table_args__ = (
        CheckConstraint("kind IN ('rod', 'line', 'bait')", name="kind_valid"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("required_level >= 1", name="required_level_positive"),
        CheckConstraint("stock IS NULL OR stock >= 0", name="stock_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False, server_default="🎣")
    description: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    required_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    stock: Mapped[int | None] = mapped_column(Integer)
    effects: Mapped[dict] = mapped_column(JSON, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class FarmFishingGear(Base):
    """玩家持有的釣具：魚竿、魚線數量固定 1；魚餌記數量。每種類型最多裝備一件（部分唯一索引）。"""

    __tablename__ = "farm_fishing_gear"
    __table_args__ = (
        UniqueConstraint("farm_id", "gear_code", name="uq_farm_fishing_gear_farm_gear"),
        CheckConstraint("kind IN ('rod', 'line', 'bait')", name="kind_valid"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        Index("uq_farm_fishing_gear_equipped", "farm_id", "kind", unique=True, postgresql_where=text("equipped")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    gear_code: Mapped[str] = mapped_column(ForeignKey("fishing_gear.code", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    equipped: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FishingSetting(Base):
    """釣魚設定的版本紀錄（最新版本生效，只新增不修改）；拋竿時把當時的設定存進 Session。"""

    __tablename__ = "fishing_settings"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FishingSession(Base):
    """一次釣魚：拋竿時扣魚餌並決定魚種、重量、價格與脫鉤回合，之後依時間與操作推進。
    casting／fighting 為進行中（每座農場最多一筆，部分唯一索引）；其餘為結束狀態。
    settings／gear 是拋竿當下的快照，管理員之後修改設定不影響進行中的釣魚。"""

    __tablename__ = "fishing_sessions"
    __table_args__ = (
        CheckConstraint(f"status IN ({FISHING_STATUSES_SQL})", name="status_valid"),
        CheckConstraint(f"rarity IN ({FISH_RARITY_SQL})", name="rarity_valid"),
        CheckConstraint("react_deadline > bite_at AND bite_at > started_at", name="time_order"),
        CheckConstraint("(status IN ('casting', 'fighting')) = (ended_at IS NULL)", name="ended_consistent"),
        CheckConstraint("turns >= 0", name="turns_non_negative"),
        Index(
            "uq_fishing_sessions_farm_active", "farm_id", unique=True,
            postgresql_where=text("status IN ('casting', 'fighting')"),
        ),
        Index("ix_fishing_sessions_farm_started", "farm_id", "started_at"),
        Index("ix_fishing_sessions_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    settings_version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False)
    gear: Mapped[dict] = mapped_column(JSON, nullable=False)  # {"rod": {...}, "line": {...}, "bait": {...}}
    fish_code: Mapped[str] = mapped_column(String(32), nullable=False)
    rarity: Mapped[str] = mapped_column(String(16), nullable=False)
    weight_g: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSON, nullable=False)  # 拉扯狀態（體力、距離、張力、下一個動作…）
    turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bite_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    react_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))


class FarmFish(Base):
    """釣到的魚（每隻一列，含重量與售價）。sold_at 有值表示已販售（保留作為捕獲紀錄與圖鑑）。
    session_id 唯一：同一次釣魚不會發放兩隻魚。"""

    __tablename__ = "farm_fish"
    __table_args__ = (
        CheckConstraint(f"rarity IN ({FISH_RARITY_SQL})", name="rarity_valid"),
        CheckConstraint("price >= 0", name="price_non_negative"),  # 售價跟隨 fish_types，不設上限（Migration 0021）
        CheckConstraint("weight_g > 0", name="weight_positive"),
        CheckConstraint("(sold_at IS NULL) = (sold_price IS NULL)", name="sold_consistent"),
        CheckConstraint("sold_at IS NULL OR donated_at IS NULL", name="sold_or_donated"),
        Index("ix_farm_fish_farm_unsold", "farm_id", postgresql_where=text("sold_at IS NULL")),
        Index("ix_farm_fish_farm_caught", "farm_id", "caught_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("fishing_sessions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    fish_code: Mapped[str] = mapped_column(ForeignKey("fish_types.code", ondelete="RESTRICT"), nullable=False)
    rarity: Mapped[str] = mapped_column(String(16), nullable=False)  # 捕獲當下的稀有度與價值（之後不變）
    weight_g: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    caught_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sold_price: Mapped[int | None] = mapped_column(Integer)
    # 捐獻給世界 Boss 的時間（Migration 0019）；有值時不在魚簍中，也不能販售或分享
    donated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FishShare(Base):
    """魚類分享紀錄（分享不轉移、不消耗魚，也沒有獎勵）。"""

    __tablename__ = "fish_shares"
    __table_args__ = (
        Index("ix_fish_shares_farm", "farm_id", "shared_at"),
        Index("ix_fish_shares_fish", "fish_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fish_id: Mapped[int] = mapped_column(ForeignKey("farm_fish.id", ondelete="CASCADE"), nullable=False)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    shared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------- 世界 Boss（Migration 0019） ----------


class WorldBossSchedule(Base):
    """固定週期出現的世界 Boss（例如每 2 天晚上 9 點的深淵魔龍）。group_id NULL 表示全服。
    系統在上一場結束後自動建立下一場（world_boss_events，含 slot_at），管理員可在出現前個別修改那一場的時間與獎勵；
    settings／rewards 是之後每一場的範本。"""

    __tablename__ = "world_boss_schedules"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name="name_len"),
        CheckConstraint("interval_days BETWEEN 1 AND 30", name="interval_range"),
        CheckConstraint("spawn_hour BETWEEN 0 AND 23 AND spawn_minute BETWEEN 0 AND 59", name="time_range"),
        CheckConstraint("duration_minutes BETWEEN 10 AND 10080", name="duration_range"),
        CheckConstraint("max_hp > 0", name="max_hp_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    spawn_hour: Mapped[int] = mapped_column(Integer, nullable=False)  # 台灣時間
    spawn_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_date: Mapped[date] = mapped_column(Date, nullable=False)  # 從這一天（台灣日期）起每 interval_days 天出現
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_hp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False)
    rewards: Mapped[dict] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldBossEvent(Base):
    """一場世界 Boss 活動：group_id 為所屬 Discord 群組，NULL 表示全服（所有玩家都能參加）。
    HP、時間、狀態與設定都存在這裡；
    status：active（未開始或進行中，依 starts_at 判斷）、paused、ended。
    手動建立的活動：同一範圍（全服或同一群組）最多一場未結束（部分唯一索引）。
    排程產生的活動：記錄 schedule_id 與排定時間 slot_at，(schedule_id, slot_at) 唯一，每個排程同時最多一場未結束。
    settings／rewards 是建立時的快照（app/game/world_boss 的預設值），管理員可在結束前修改；結束後保留作為歷史紀錄。"""

    __tablename__ = "world_boss_events"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused', 'ended')", name="status_valid"),
        CheckConstraint("max_hp > 0 AND hp >= 0 AND hp <= max_hp", name="hp_range"),
        CheckConstraint("level >= 1", name="level_positive"),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name="name_len"),
        CheckConstraint(
            "(status = 'ended') = (ended_at IS NOT NULL AND end_reason IS NOT NULL)", name="ended_consistent"
        ),
        CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('defeated', 'timeout', 'admin')", name="end_reason_valid"
        ),
        CheckConstraint("(defeated_at IS NOT NULL) = (end_reason = 'defeated')", name="defeated_consistent"),
        # 管理員手動建立的活動：同一範圍（全服或同一群組）最多一場未結束；排程產生的活動：每個排程最多一場未結束
        Index(
            "uq_world_boss_events_scope_open", text("COALESCE(group_id, 0)"), unique=True,
            postgresql_where=text("status IN ('active', 'paused') AND schedule_id IS NULL"),
        ),
        Index(
            "uq_world_boss_events_schedule_open", "schedule_id", unique=True,
            postgresql_where=text("status IN ('active', 'paused') AND schedule_id IS NOT NULL"),
        ),
        UniqueConstraint("schedule_id", "slot_at", name="uq_world_boss_events_schedule_slot"),
        Index("ix_world_boss_events_group_created", "group_id", "created_at"),
        Index("ix_world_boss_events_status_ends", "status", "ends_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"))
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("world_boss_schedules.id", ondelete="SET NULL"))
    slot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)  # 目前介面不顯示（保留欄位）
    max_hp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 戰鬥模擬已推進到的時間（之後的傷害依當下補給計算）
    last_tick_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    defeated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(16))
    # 系統確認活動結束（獎勵開放領取）的時間
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict] = mapped_column(JSON, nullable=False)
    rewards: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldBossSupply(Base):
    """各補給類型的進度（積分）與目標；建立活動時建立四列，捐獻時在同一筆交易中累加。"""

    __tablename__ = "world_boss_supplies"
    __table_args__ = (
        CheckConstraint("supply_code IN ('food', 'medical', 'combat', 'equipment')", name="supply_valid"),
        CheckConstraint("target > 0", name="target_positive"),
        CheckConstraint("progress >= 0", name="progress_non_negative"),
    )

    event_id: Mapped[int] = mapped_column(ForeignKey("world_boss_events.id", ondelete="CASCADE"), primary_key=True)
    supply_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    target: Mapped[int] = mapped_column(BigInteger, nullable=False)
    progress: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class WorldBossContribution(Base):
    """捐獻紀錄（只新增）。request_id 由面板產生，(event_id, player_id, request_id) 唯一：重複點擊不會重複扣物品。"""

    __tablename__ = "world_boss_contributions"
    __table_args__ = (
        UniqueConstraint("event_id", "player_id", "request_id", name="uq_world_boss_contributions_request"),
        CheckConstraint("category IN ('crop', 'fish', 'material', 'goods', 'special')", name="category_valid"),
        CheckConstraint("supply_code IN ('food', 'medical', 'combat', 'equipment')", name="supply_valid"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("points > 0", name="points_positive"),
        CheckConstraint("damage >= 0", name="damage_non_negative"),
        Index("ix_world_boss_contributions_event_player", "event_id", "player_id"),
        Index("ix_world_boss_contributions_event_created", "event_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("world_boss_events.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    supply_code: Mapped[str] = mapped_column(String(16), nullable=False)
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    quality: Mapped[str | None] = mapped_column(String(16))
    mutation: Mapped[str | None] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    damage: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")  # 捐獻時立即造成的傷害
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldBossRewardClaim(Base):
    """獎勵領取紀錄；(event_id, player_id) 唯一，每位玩家每場活動只能領一次。保存當時的名次、積分與發放內容。"""

    __tablename__ = "world_boss_reward_claims"
    __table_args__ = (
        UniqueConstraint("event_id", "player_id", name="uq_world_boss_reward_claims_once"),
        CheckConstraint("outcome IN ('victory', 'participation')", name="outcome_valid"),
        CheckConstraint("gold >= 0", name="gold_non_negative"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        Index("ix_world_boss_reward_claims_player", "player_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("world_boss_events.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    player_name: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    gold: Mapped[int] = mapped_column(BigInteger, nullable=False)
    items: Mapped[dict] = mapped_column(JSON, nullable=False)
    title: Mapped[str | None] = mapped_column(String(20))
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyCheckin(Base):
    """每日簽到紀錄（台灣日期）；(farm_id, day) 唯一，每座農場每天只能簽到一次。保存連續天數與發放內容。"""

    __tablename__ = "daily_checkins"
    __table_args__ = (
        UniqueConstraint("farm_id", "day", name="uq_daily_checkins_farm_day"),
        CheckConstraint("streak >= 1", name="streak_positive"),
        CheckConstraint("gold >= 0", name="gold_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    streak: Mapped[int] = mapped_column(Integer, nullable=False)
    gold: Mapped[int] = mapped_column(BigInteger, nullable=False)
    items: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FarmCodexEntry(Base):
    """圖鑑：每座農場第一次取得某物品的時間、累計取得數量與最高品質（0 普通～3 傳說）。

    收成、偷菜、收集動物產品、領取加工品時由 GameService._record 登記，山洞素材在領取探險時登記；
    魚類圖鑑用 farm_fish。"""

    __tablename__ = "farm_codex"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("best_quality BETWEEN 0 AND 3", name="best_quality_valid"),
    )

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    item_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    best_quality: Mapped[int] = mapped_column(Integer, nullable=False)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FarmAchievement(Base):
    """已領取的成就；(farm_id, code) 為主鍵，每個成就只能領一次。成就進度由既有資料即時計算，不另外存。"""

    __tablename__ = "farm_achievements"
    __table_args__ = (CheckConstraint("gold >= 0", name="gold_non_negative"),)

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    gold: Mapped[int] = mapped_column(BigInteger, nullable=False)
    items: Mapped[dict] = mapped_column(JSON, nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FarmTitle(Base):
    """玩家擁有的稱號；source 是來源（成就代碼 achievement:<code>、world_boss）。"""

    __tablename__ = "farm_titles"

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    title: Mapped[str] = mapped_column(String(20), primary_key=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    obtained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WeeklyShopPurchase(Base):
    """每週特賣的購買量（每人每週每項限購）；week_start 是台灣時間的週一。"""

    __tablename__ = "weekly_shop_purchases"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    item_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)


class FarmPet(Base):
    """農場的寵物（貓、狗）：購買後 Lv0，最高 Lv4；每種寵物每座農場一隻。數值在 game/pets.py。"""

    __tablename__ = "farm_pets"
    __table_args__ = (
        CheckConstraint("pet_code IN ('cat', 'dog')", name="pet_code_valid"),
        CheckConstraint("level BETWEEN 0 AND 10", name="level_valid"),
    )

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    pet_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    bought_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FeatureSetting(Base):
    """可在管理網站調整的新功能規則（每日簽到、寵物、特賣與升級、成就）：每個 key 一列 JSON。

    沒有資料列的 key 使用程式內的預設值；驗證與預設值在 game/feature_config.py 與各規則模組。"""

    __tablename__ = "feature_settings"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class FarmFeedback(Base):
    """操作回饋用的玩家資料（只用於顯示）：上次打開 Discord 面板的時間與個人紀錄（{紀錄代碼: 數值}）。"""

    __tablename__ = "farm_feedback"

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class FishingGuard(Base):
    """釣魚防腳本狀態（每座農場一列，第一次拋竿時建立）：距離上次人機確認的拋竿次數、可疑分數、題目與暫停時間。
    規則在 game/fishing.py（GUARD_*）；只影響能不能拋竿，不影響資產。"""

    __tablename__ = "fishing_guards"
    __table_args__ = (
        CheckConstraint("casts_since_check >= 0 AND check_after > 0 AND suspicion >= 0", name="counters_valid"),
    )

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    casts_since_check: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    check_after: Mapped[int] = mapped_column(Integer, nullable=False)
    suspicion: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    challenge: Mapped[dict | None] = mapped_column(JSON)  # {"answer": 代碼, "options": [代碼, …]}
    challenge_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class GroupRuins(Base):
    """群組的古代遺跡：修復階段（0–3，3 = 修復完成）與目前階段已捐的素材 {代碼: 數量}。規則在 game/ruins.py。"""

    __tablename__ = "group_ruins"
    __table_args__ = (
        CheckConstraint("stage BETWEEN 0 AND 3", name="stage_valid"),
        CheckConstraint("(stage = 3) = (completed_at IS NOT NULL)", name="completed_consistency"),
    )

    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    stage: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    guardian_settled: Mapped[date | None] = mapped_column(Date)  # 守護者已結算到哪一週（台灣週一）
    collapsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 最近一次崩壞的時間


class RuinsContribution(Base):
    """每座農場對群組遺跡累計捐獻的素材數量（修復貢獻排行、修復完成時發稱號）。"""

    __tablename__ = "ruins_contributions"
    __table_args__ = (CheckConstraint("quantity >= 0", name="quantity_non_negative"),)

    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuinsGuardian(Base):
    """群組每週一隻的遺跡守護者（week_start 是台灣時間週一）：HP、累計傷害與被打倒的時間。"""

    __tablename__ = "ruins_guardians"
    __table_args__ = (
        CheckConstraint("hp_max > 0", name="hp_positive"),
        CheckConstraint("damage >= 0", name="damage_non_negative"),
    )

    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False)
    damage: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    defeated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuinsGuardianHit(Base):
    """每座農場每週對守護者造成的傷害（排行用）。"""

    __tablename__ = "ruins_guardian_hits"
    __table_args__ = (CheckConstraint("damage >= 0", name="damage_non_negative"),)

    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    damage: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RumorDig(Base):
    """在日報傳聞的農場挖寶：每座農場每天（傳聞所屬的日報日期）只能挖一次；寶藏每個群組每天只會被挖到一次。"""

    __tablename__ = "rumor_digs"
    __table_args__ = (
        CheckConstraint("gold >= 0 AND (found OR gold = 0)", name="gold_valid"),
        Index("uq_rumor_digs_group_day_found", "group_id", "day", unique=True, postgresql_where=text("found")),
    )

    farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    target_farm_id: Mapped[int] = mapped_column(ForeignKey("farms.id", ondelete="CASCADE"), nullable=False)
    found: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gold: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
