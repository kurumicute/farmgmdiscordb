"""fishing mini game: fish types, fishing gear, player gear, settings, sessions, caught fish and shares

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-02

- fish_types：魚種（稀有度、售價區間（CHECK 限制在稀有度區間內）、重量範圍、掙扎難度、啟用旗標）。
- fishing_gear：釣具商品（魚竿／魚線／魚餌，價格、等級、庫存（NULL 不限量）、效果 JSON）。
- farm_fishing_gear：玩家持有的釣具與裝備狀態；(farm_id, kind) WHERE equipped 唯一，每種最多裝備一件。
- fishing_settings：釣魚設定版本（沒有資料時使用程式內預設值）。
- fishing_sessions：每次釣魚（設定與裝備快照）；(farm_id) WHERE status IN ('casting', 'fighting') 唯一。
- farm_fish：釣到的魚（session_id 唯一，同一次釣魚不會發兩隻魚；sold_at 標記已販售，保留捕獲紀錄）。
- fish_shares：分享紀錄。
- 新事件類型：fishing_buy（購買釣具）、fish_sell（販售魚類）。
- 初始魚種與釣具在這裡寫死（不引用 app 常數）；同代碼已存在時不重複建立。

降版會刪除上述資料表（含玩家的魚、釣具與釣魚紀錄）與新事件類型的交易紀錄，降版前請先備份。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

EVENTS_0015 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
)
NEW_EVENTS = ("fishing_buy", "fish_sell")
RARITY_SQL = "'common', 'rare', 'epic', 'legendary'"
PRICE_BAND = (
    "(rarity = 'common' AND min_price >= 1 AND max_price <= 20) OR "
    "(rarity = 'rare' AND min_price >= 21 AND max_price <= 50) OR "
    "(rarity = 'epic' AND min_price >= 51 AND max_price <= 100) OR "
    "(rarity = 'legendary' AND min_price >= 101 AND max_price <= 200)"
)
STATUSES = "'casting', 'fighting', 'landed', 'escaped', 'line_broken', 'slipped', 'missed', 'timeout', 'abandoned'"

# code, 名稱, emoji, 稀有度, 最低價, 最高價, 最小重量 g, 最大重量 g, 掙扎難度, 說明
FISH = (
    ("pale_chub", "溪哥", "🐟", "common", 1, 8, 20, 120, 1, "溪流裡成群游動的小魚。"),
    ("loach", "泥鰍", "🐟", "common", 2, 10, 30, 200, 1, "喜歡躲在泥巴裡，滑溜溜的。"),
    ("crucian_carp", "鯽魚", "🐟", "common", 1, 12, 100, 800, 2, "池塘裡最常見的魚。"),
    ("tilapia", "吳郭魚", "🐟", "common", 3, 15, 300, 1500, 2, "適應力超強，到處都釣得到。"),
    ("common_carp", "鯉魚", "🐟", "common", 8, 20, 1000, 8000, 3, "力氣不小的大型淡水魚。"),
    ("sweetfish", "香魚", "🐠", "rare", 30, 50, 100, 400, 2, "只住在清澈溪流，帶有淡淡香氣。"),
    ("sea_bass", "鱸魚", "🐠", "rare", 21, 40, 1000, 6000, 3, "會突然衝刺的掠食魚。"),
    ("rainbow_trout", "虹鱒", "🐠", "rare", 25, 45, 800, 4000, 3, "身上有彩虹般的光澤。"),
    ("catfish", "鯰魚", "🐠", "rare", 21, 38, 1500, 12000, 4, "夜行性，咬餌後會往深處鑽。"),
    ("salmon", "鮭魚", "🐡", "epic", 51, 80, 2000, 10000, 3, "逆流而上的勇者。"),
    ("mahi_mahi", "鬼頭刀", "🐡", "epic", 55, 90, 2000, 15000, 4, "速度飛快，會連續跳出水面。"),
    ("grouper", "石斑魚", "🐡", "epic", 60, 95, 3000, 25000, 4, "被釣到會拚命往礁石裡躲。"),
    ("formosan_salmon", "櫻花鉤吻鮭", "🌸", "legendary", 120, 190, 200, 1500, 3, "冰河時期留下的國寶魚。"),
    ("golden_koi", "黃金錦鯉", "🎏", "legendary", 140, 200, 3000, 12000, 4, "傳說釣到就會好運連連。"),
    ("oarfish", "皇帶魚", "🐉", "legendary", 101, 180, 20000, 150000, 5, "深海中的神秘巨魚。"),
)
# code, 類型, 名稱, emoji, 價格, 需要等級, 說明, 效果
GEAR = (
    ("bamboo_rod", "rod", "竹製魚竿", "🎋", 100, 1, "新手入門的魚竿。",
     {"reel_power": 10, "control_pct": 0, "reaction_bonus_ms": 0}),
    ("wooden_rod", "rod", "木製魚竿", "🎋", 600, 2, "比竹竿穩一點。",
     {"reel_power": 11, "control_pct": 5, "reaction_bonus_ms": 500}),
    ("iron_rod", "rod", "鐵製魚竿", "🎣", 2000, 4, "收線更有力，魚衝刺時比較穩。",
     {"reel_power": 13, "control_pct": 10, "reaction_bonus_ms": 1000}),
    ("pro_rod", "rod", "高級魚竿", "🎣", 6000, 7, "專業釣手愛用。",
     {"reel_power": 15, "control_pct": 18, "reaction_bonus_ms": 1500}),
    ("legend_rod", "rod", "傳說魚竿", "✨", 20000, 10, "傳說魚也拉得動的頂級魚竿。",
     {"reel_power": 18, "control_pct": 25, "reaction_bonus_ms": 2000}),
    ("basic_line", "line", "普通魚線", "🪢", 50, 1, "一般的尼龍線。", {"max_tension": 100}),
    ("strong_line", "line", "強化魚線", "🪢", 800, 3, "比較不容易斷。", {"max_tension": 115}),
    ("heavy_line", "line", "高強度魚線", "🪢", 3000, 6, "大魚的衝擊也撐得住。", {"max_tension": 130}),
    ("worm_bait", "bait", "普通蚯蚓", "🪱", 3, 1, "最普通的魚餌（每次拋竿用掉 1 個）。",
     {"rare_bp": 0, "epic_bp": 0, "legendary_bp": 0, "bite_speed_pct": 0}),
    ("premium_bait", "bait", "高級魚餌", "🦐", 6, 3, "稀有魚比較愛吃，上鉤也比較快。",
     {"rare_bp": 300, "epic_bp": 100, "legendary_bp": 30, "bite_speed_pct": 20}),
    ("special_bait", "bait", "特殊魚餌", "🌟", 12, 6, "特製香餌，吸引史詩與傳說魚。",
     {"rare_bp": 500, "epic_bp": 300, "legendary_bp": 150, "bite_speed_pct": 40}),
)


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    op.create_table(
        "fish_types",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("emoji", sa.String(16), server_default="🐟", nullable=False),
        sa.Column("rarity", sa.String(16), nullable=False),
        sa.Column("min_price", sa.Integer(), nullable=False),
        sa.Column("max_price", sa.Integer(), nullable=False),
        sa.Column("min_weight_g", sa.Integer(), nullable=False),
        sa.Column("max_weight_g", sa.Integer(), nullable=False),
        sa.Column("difficulty", sa.Integer(), server_default="3", nullable=False),
        sa.Column("description", sa.String(200), server_default="", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(f"rarity IN ({RARITY_SQL})", name=op.f("ck_fish_types_rarity_valid")),
        sa.CheckConstraint("min_price <= max_price", name=op.f("ck_fish_types_price_order")),
        sa.CheckConstraint(PRICE_BAND, name=op.f("ck_fish_types_price_in_rarity_band")),
        sa.CheckConstraint(
            "min_weight_g > 0 AND min_weight_g <= max_weight_g", name=op.f("ck_fish_types_weight_order")
        ),
        sa.CheckConstraint("difficulty BETWEEN 1 AND 5", name=op.f("ck_fish_types_difficulty_range")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fish_types")),
        sa.UniqueConstraint("code", name=op.f("uq_fish_types_code")),
        sa.UniqueConstraint("name", name=op.f("uq_fish_types_name")),
    )
    op.create_table(
        "fishing_gear",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("emoji", sa.String(16), server_default="🎣", nullable=False),
        sa.Column("description", sa.String(200), server_default="", nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("required_level", sa.Integer(), server_default="1", nullable=False),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("effects", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("kind IN ('rod', 'line', 'bait')", name=op.f("ck_fishing_gear_kind_valid")),
        sa.CheckConstraint("price > 0", name=op.f("ck_fishing_gear_price_positive")),
        sa.CheckConstraint("required_level >= 1", name=op.f("ck_fishing_gear_required_level_positive")),
        sa.CheckConstraint("stock IS NULL OR stock >= 0", name=op.f("ck_fishing_gear_stock_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fishing_gear")),
        sa.UniqueConstraint("code", name=op.f("uq_fishing_gear_code")),
        sa.UniqueConstraint("name", name=op.f("uq_fishing_gear_name")),
    )
    op.create_table(
        "farm_fishing_gear",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("gear_code", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("equipped", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('rod', 'line', 'bait')", name=op.f("ck_farm_fishing_gear_kind_valid")),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_farm_fishing_gear_quantity_non_negative")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_fishing_gear_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["gear_code"], ["fishing_gear.code"], name=op.f("fk_farm_fishing_gear_gear_code_fishing_gear"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_fishing_gear")),
        sa.UniqueConstraint("farm_id", "gear_code", name="uq_farm_fishing_gear_farm_gear"),
    )
    op.create_index(
        "uq_farm_fishing_gear_equipped", "farm_fishing_gear", ["farm_id", "kind"], unique=True,
        postgresql_where=sa.text("equipped"),
    )
    op.create_table(
        "fishing_settings",
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_fishing_settings_version_positive")),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_fishing_settings")),
    )
    op.create_table(
        "fishing_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("settings_version", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("gear", sa.JSON(), nullable=False),
        sa.Column("fish_code", sa.String(32), nullable=False),
        sa.Column("rarity", sa.String(16), nullable=False),
        sa.Column("weight_g", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("turns", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bite_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("react_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context_group_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_fishing_sessions_status_valid")),
        sa.CheckConstraint(f"rarity IN ({RARITY_SQL})", name=op.f("ck_fishing_sessions_rarity_valid")),
        sa.CheckConstraint(
            "react_deadline > bite_at AND bite_at > started_at", name=op.f("ck_fishing_sessions_time_order")
        ),
        sa.CheckConstraint(
            "(status IN ('casting', 'fighting')) = (ended_at IS NULL)",
            name=op.f("ck_fishing_sessions_ended_consistent"),
        ),
        sa.CheckConstraint("turns >= 0", name=op.f("ck_fishing_sessions_turns_non_negative")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_fishing_sessions_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["context_group_id"], ["groups.id"], name=op.f("fk_fishing_sessions_context_group_id_groups"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fishing_sessions")),
    )
    op.create_index(
        "uq_fishing_sessions_farm_active", "fishing_sessions", ["farm_id"], unique=True,
        postgresql_where=sa.text("status IN ('casting', 'fighting')"),
    )
    op.create_index("ix_fishing_sessions_farm_started", "fishing_sessions", ["farm_id", "started_at"])
    op.create_index("ix_fishing_sessions_started_at", "fishing_sessions", ["started_at"])
    op.create_table(
        "farm_fish",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("fish_code", sa.String(32), nullable=False),
        sa.Column("rarity", sa.String(16), nullable=False),
        sa.Column("weight_g", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("caught_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sold_price", sa.Integer(), nullable=True),
        sa.CheckConstraint(f"rarity IN ({RARITY_SQL})", name=op.f("ck_farm_fish_rarity_valid")),
        sa.CheckConstraint("price BETWEEN 1 AND 200", name=op.f("ck_farm_fish_price_range")),
        sa.CheckConstraint("weight_g > 0", name=op.f("ck_farm_fish_weight_positive")),
        sa.CheckConstraint("(sold_at IS NULL) = (sold_price IS NULL)", name=op.f("ck_farm_fish_sold_consistent")),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_farm_fish_farm_id_farms"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["fishing_sessions.id"], name=op.f("fk_farm_fish_session_id_fishing_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fish_code"], ["fish_types.code"], name=op.f("fk_farm_fish_fish_code_fish_types"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_fish")),
        sa.UniqueConstraint("session_id", name=op.f("uq_farm_fish_session_id")),
    )
    op.create_index(
        "ix_farm_fish_farm_unsold", "farm_fish", ["farm_id"], postgresql_where=sa.text("sold_at IS NULL")
    )
    op.create_index("ix_farm_fish_farm_caught", "farm_fish", ["farm_id", "caught_at"])
    op.create_table(
        "fish_shares",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["fish_id"], ["farm_fish.id"], name=op.f("fk_fish_shares_fish_id_farm_fish"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_fish_shares_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_fish_shares_group_id_groups"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fish_shares")),
    )
    op.create_index("ix_fish_shares_farm", "fish_shares", ["farm_id", "shared_at"])
    op.create_index("ix_fish_shares_fish", "fish_shares", ["fish_id"])

    insert_fish = sa.text(
        "INSERT INTO fish_types (code, name, emoji, rarity, min_price, max_price, min_weight_g, max_weight_g, "
        "difficulty, description, sort_order, active) "
        "SELECT :code, :name, :emoji, :rarity, :pmin, :pmax, :wmin, :wmax, :diff, :desc, :sort, true "
        "WHERE NOT EXISTS (SELECT 1 FROM fish_types WHERE code = :code OR name = :name)"
    )
    for i, (code, name, emoji, rarity, pmin, pmax, wmin, wmax, diff, desc) in enumerate(FISH):
        op.execute(insert_fish.bindparams(code=code, name=name, emoji=emoji, rarity=rarity, pmin=pmin, pmax=pmax,
                                          wmin=wmin, wmax=wmax, diff=diff, desc=desc, sort=(i + 1) * 10))
    insert_gear = sa.text(
        "INSERT INTO fishing_gear (code, kind, name, emoji, price, required_level, description, effects, "
        "sort_order, active) "
        "SELECT :code, :kind, :name, :emoji, :price, :level, :desc, CAST(:effects AS json), :sort, true "
        "WHERE NOT EXISTS (SELECT 1 FROM fishing_gear WHERE code = :code OR name = :name)"
    )
    for i, (code, kind, name, emoji, price, level, desc, effects) in enumerate(GEAR):
        op.execute(insert_gear.bindparams(code=code, kind=kind, name=name, emoji=emoji, price=price, level=level,
                                          desc=desc, effects=json.dumps(effects), sort=(i + 1) * 10))

    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0015 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM game_transactions WHERE event_type = ANY(:types)").bindparams(types=list(NEW_EVENTS))
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0015)
    )
    op.drop_index("ix_fish_shares_fish", table_name="fish_shares")
    op.drop_index("ix_fish_shares_farm", table_name="fish_shares")
    op.drop_table("fish_shares")
    op.drop_index("ix_farm_fish_farm_caught", table_name="farm_fish")
    op.drop_index("ix_farm_fish_farm_unsold", table_name="farm_fish")
    op.drop_table("farm_fish")
    op.drop_index("ix_fishing_sessions_started_at", table_name="fishing_sessions")
    op.drop_index("ix_fishing_sessions_farm_started", table_name="fishing_sessions")
    op.drop_index("uq_fishing_sessions_farm_active", table_name="fishing_sessions")
    op.drop_table("fishing_sessions")
    op.drop_table("fishing_settings")
    op.drop_index("uq_farm_fishing_gear_equipped", table_name="farm_fishing_gear")
    op.drop_table("farm_fishing_gear")
    op.drop_table("fishing_gear")
    op.drop_table("fish_types")
