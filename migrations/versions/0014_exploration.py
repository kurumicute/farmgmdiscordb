"""cave exploration: area cooldowns, exploration logs, machines settings, rare crops

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30

- farm_explorations：每座農場每個探索區域（grassland／forest／mine）的冷卻，(farm_id, area_code) 唯一。
- exploration_logs：每次探索的獎勵明細（追蹤素材、稀有種子與假幣的來源）。
- farm_tools.settings：機器設定（自動加工機的加工項目）。機器本身沿用 farm_tools（與自動澆水器等工具相同）。
- crop_types.shop_buyable：商店是否販售種子；新增稀有作物琥珀瓜、月光花（只能探索或培育取得種子）。
  同代碼已存在時不重複建立。
- 新事件類型：explore（探索）、build_machine（製作機器）、exchange（假幣兌換）、make_seed（種子製造機）。

降版會刪除兩個新資料表、新事件類型的交易紀錄、稀有作物（若尚未被種植、偷取或活動使用）與機器設定欄位；
已取得的素材、假幣與機器（farm_tools 列）保留在資料庫中。
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

EVENTS_0012 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble",
)
NEW_EVENTS = ("explore", "build_machine", "exchange", "make_seed")
AREAS = "'grassland', 'forest', 'mine'"
# code, 名稱, 種子參考價, 生長秒數, 收成數量, 單位售價, 變種代碼, 變種名稱
RARE_CROPS = (
    ("amber_melon", "琥珀瓜", 200, 43200, 3, 120, "golden_amber_melon", "黃金琥珀瓜"),
    ("moonflower", "月光花", 500, 86400, 2, 450, "starflower", "星辰花"),
)


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    op.create_table(
        "farm_explorations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("area_code", sa.String(16), nullable=False),
        sa.Column("last_explored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("explore_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(f"area_code IN ({AREAS})", name=op.f("ck_farm_explorations_area_code_valid")),
        sa.CheckConstraint("next_available_at > last_explored_at", name=op.f("ck_farm_explorations_next_after_last")),
        sa.CheckConstraint("explore_count >= 1", name=op.f("ck_farm_explorations_explore_count_positive")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_explorations_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_explorations")),
        sa.UniqueConstraint("farm_id", "area_code", name="uq_farm_explorations_farm_area"),
    )
    op.create_table(
        "exploration_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("area_code", sa.String(16), nullable=False),
        sa.Column("farm_level", sa.Integer(), nullable=False),
        sa.Column("advanced", sa.Boolean(), nullable=False),
        sa.Column("rewards", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("explored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"area_code IN ({AREAS})", name=op.f("ck_exploration_logs_area_code_valid")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_exploration_logs_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exploration_logs")),
    )
    op.create_index("ix_exploration_logs_farm_explored", "exploration_logs", ["farm_id", "explored_at"])
    op.add_column("farm_tools", sa.Column("settings", sa.JSON(), nullable=True))
    op.add_column(
        "crop_types", sa.Column("shop_buyable", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    insert = sa.text(
        "INSERT INTO crop_types (code, name, seed_price, grow_seconds, harvest_quantity, sell_price, variant_code, "
        "variant_name, active, shop_buyable) SELECT :code, :name, :seed, :grow, :qty, :sell, :vc, :vn, true, false "
        "WHERE NOT EXISTS (SELECT 1 FROM crop_types WHERE code = :code OR variant_code = :vc)"
    )
    for code, name, seed, grow, qty, sell, vc, vn in RARE_CROPS:
        op.execute(insert.bindparams(code=code, name=name, seed=seed, grow=grow, qty=qty, sell=sell, vc=vc, vn=vn))
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0012 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM game_transactions WHERE event_type = ANY(:types)").bindparams(types=list(NEW_EVENTS))
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0012)
    )
    op.execute(
        sa.text(
            "DELETE FROM crop_types c WHERE c.code = ANY(:codes) "
            "AND NOT EXISTS (SELECT 1 FROM farm_plots p WHERE p.crop_type_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM steal_records s WHERE s.crop_type_id = c.id) "
            "AND NOT EXISTS (SELECT 1 FROM game_events e WHERE e.crop_type_id = c.id)"
        ).bindparams(codes=[c[0] for c in RARE_CROPS])
    )
    op.drop_column("crop_types", "shop_buyable")
    op.drop_column("farm_tools", "settings")
    op.drop_index("ix_exploration_logs_farm_explored", table_name="exploration_logs")
    op.drop_table("exploration_logs")
    op.drop_table("farm_explorations")
