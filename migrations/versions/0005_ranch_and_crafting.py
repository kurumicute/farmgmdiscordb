"""animal ranching, buildings and crafting orders

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-27

- farm_buildings：畜舍與加工設施。
- farm_animals：動物與生產週期時間戳記。
- craft_orders：加工訂單（原料、配方、開始／完成時間、狀態）。
- 新事件類型：build、buy_animal、feed_animal、collect_animal、craft_start、craft_collect。
- 只新增資料表與允許的事件類型，不修改既有資料。
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

EVENTS_0004 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
)
EVENTS_0005 = EVENTS_0004 + (
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
)


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def _farm_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f(f"fk_{table}_farm_id_farms"), ondelete="CASCADE")


def upgrade() -> None:
    op.create_table(
        "farm_buildings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("building_code", sa.String(32), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot >= 1", name=op.f("ck_farm_buildings_slot_positive")),
        _farm_fk("farm_buildings"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_buildings")),
        sa.UniqueConstraint("farm_id", "building_code", "slot", name="uq_farm_buildings_farm_building_slot"),
    )
    op.create_table(
        "farm_animals",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("animal_no", sa.Integer(), nullable=False),
        sa.Column("animal_code", sa.String(32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cycles_completed", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("animal_no >= 1", name=op.f("ck_farm_animals_animal_no_positive")),
        sa.CheckConstraint(
            "(fed_at IS NULL AND ready_at IS NULL) OR (fed_at IS NOT NULL AND ready_at > fed_at)",
            name=op.f("ck_farm_animals_cycle_consistency"),
        ),
        sa.CheckConstraint("cycles_completed >= 0", name=op.f("ck_farm_animals_cycles_non_negative")),
        _farm_fk("farm_animals"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_animals")),
        sa.UniqueConstraint("farm_id", "animal_no", name="uq_farm_animals_farm_animal_no"),
    )
    op.create_table(
        "craft_orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("recipe_code", sa.String(32), nullable=False),
        sa.Column("building_code", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("output_code", sa.String(64), nullable=False),
        sa.Column("output_quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="in_progress", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('in_progress', 'collected')", name=op.f("ck_craft_orders_status_valid")),
        sa.CheckConstraint("quantity >= 1", name=op.f("ck_craft_orders_quantity_positive")),
        sa.CheckConstraint("output_quantity >= 1", name=op.f("ck_craft_orders_output_quantity_positive")),
        sa.CheckConstraint("ready_at > started_at", name=op.f("ck_craft_orders_ready_after_start")),
        sa.CheckConstraint(
            "(status = 'collected') = (collected_at IS NOT NULL)", name=op.f("ck_craft_orders_collected_consistency")
        ),
        _farm_fk("craft_orders"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_craft_orders")),
    )
    op.create_index("ix_craft_orders_farm_status", "craft_orders", ["farm_id", "status"])
    op.create_index("ix_craft_orders_started_at", "craft_orders", ["started_at"])

    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0005)
    )


def downgrade() -> None:
    # 注意：降版會刪除建築、動物、加工訂單與相關事件紀錄；背包中的動物產品與加工品保留在 inventory_items
    op.execute(
        "DELETE FROM game_transactions WHERE event_type IN "
        "('build', 'buy_animal', 'feed_animal', 'collect_animal', 'craft_start', 'craft_collect')"
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0004)
    )
    op.drop_index("ix_craft_orders_started_at", table_name="craft_orders")
    op.drop_index("ix_craft_orders_farm_status", table_name="craft_orders")
    op.drop_table("craft_orders")
    op.drop_table("farm_animals")
    op.drop_table("farm_buildings")
