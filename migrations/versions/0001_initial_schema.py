"""initial schema and crop types

Revision ID: 0001
Revises:
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EVENT_TYPES = ("create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade")

# Migration 內固定寫入初始值，不引用會變動的應用程式常數
INITIAL_CROPS = [
    {"code": "wheat", "name": "小麥", "seed_price": 10, "grow_seconds": 60, "harvest_quantity": 2, "sell_price": 15},
    {"code": "carrot", "name": "胡蘿蔔", "seed_price": 20, "grow_seconds": 180, "harvest_quantity": 3, "sell_price": 25},
    {"code": "tomato", "name": "番茄", "seed_price": 35, "grow_seconds": 300, "harvest_quantity": 4, "sell_price": 20},
    {"code": "strawberry", "name": "草莓", "seed_price": 60, "grow_seconds": 600, "harvest_quantity": 5, "sell_price": 25},
]


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, **kw)


def upgrade() -> None:
    op.create_table(
        "players",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("display_name", sa.String(32), nullable=False),
        sa.Column("external_platform", sa.String(32), nullable=True),
        sa.Column("external_user_id", sa.String(64), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("length(btrim(display_name)) BETWEEN 1 AND 32", name=op.f("ck_players_display_name_len")),
        sa.CheckConstraint("(external_platform IS NULL) = (external_user_id IS NULL)", name=op.f("ck_players_external_pair")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_players")),
    )
    op.create_index(
        "uq_players_external_identity",
        "players",
        ["external_platform", "external_user_id"],
        unique=True,
        postgresql_where=sa.text("external_platform IS NOT NULL"),
    )

    crop_types = op.create_table(
        "crop_types",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("seed_price", sa.Integer(), nullable=False),
        sa.Column("grow_seconds", sa.Integer(), nullable=False),
        sa.Column("harvest_quantity", sa.Integer(), nullable=False),
        sa.Column("sell_price", sa.Integer(), nullable=False),
        sa.CheckConstraint("seed_price > 0", name=op.f("ck_crop_types_seed_price_positive")),
        sa.CheckConstraint("grow_seconds > 0", name=op.f("ck_crop_types_grow_seconds_positive")),
        sa.CheckConstraint("harvest_quantity > 0", name=op.f("ck_crop_types_harvest_quantity_positive")),
        sa.CheckConstraint("sell_price >= 0", name=op.f("ck_crop_types_sell_price_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crop_types")),
        sa.UniqueConstraint("code", name=op.f("uq_crop_types_code")),
    )

    op.create_table(
        "farms",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("gold", sa.BigInteger(), nullable=False),
        sa.Column("warehouse_capacity", sa.Integer(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("gold >= 0", name=op.f("ck_farms_gold_non_negative")),
        sa.CheckConstraint("level >= 1", name=op.f("ck_farms_level_positive")),
        sa.CheckConstraint("warehouse_capacity > 0", name=op.f("ck_farms_warehouse_capacity_positive")),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name=op.f("ck_farms_name_len")),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], name=op.f("fk_farms_player_id_players"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farms")),
        sa.UniqueConstraint("player_id", name=op.f("uq_farms_player_id")),
    )

    op.create_table(
        "farm_plots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("plot_number", sa.Integer(), nullable=False),
        sa.Column("crop_type_id", sa.Integer(), nullable=True),
        sa.Column("planted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.CheckConstraint("plot_number >= 1", name=op.f("ck_farm_plots_plot_number_positive")),
        sa.CheckConstraint(
            "(crop_type_id IS NULL AND planted_at IS NULL AND ready_at IS NULL) OR "
            "(crop_type_id IS NOT NULL AND planted_at IS NOT NULL AND ready_at IS NOT NULL "
            "AND ready_at > planted_at)",
            name=op.f("ck_farm_plots_crop_consistency"),
        ),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_farm_plots_farm_id_farms"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["crop_type_id"], ["crop_types.id"], name=op.f("fk_farm_plots_crop_type_id_crop_types"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_plots")),
        sa.UniqueConstraint("farm_id", "plot_number", name="uq_farm_plots_farm_plot_number"),
    )
    op.create_index(op.f("ix_farm_plots_crop_type_id"), "farm_plots", ["crop_type_id"])
    op.create_index(
        "ix_farm_plots_ready_at", "farm_plots", ["ready_at"], postgresql_where=sa.text("crop_type_id IS NOT NULL")
    )

    op.create_table(
        "inventory_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        _ts("updated_at"),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_inventory_items_quantity_non_negative")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_inventory_items_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_items")),
        sa.UniqueConstraint("farm_id", "item_code", name="uq_inventory_items_farm_item"),
    )

    op.create_table(
        "game_transactions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=True),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gold_delta", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN (" + ", ".join(f"'{e}'" for e in EVENT_TYPES) + ")",
            name=op.f("ck_game_transactions_event_type_valid"),
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_game_transactions_quantity_non_negative")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_game_transactions_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_transactions")),
    )
    op.create_index("ix_game_transactions_farm_created", "game_transactions", ["farm_id", "created_at"])
    op.create_index("ix_game_transactions_created_at", "game_transactions", ["created_at"])

    op.bulk_insert(crop_types, INITIAL_CROPS)


def downgrade() -> None:
    op.drop_table("game_transactions")
    op.drop_table("inventory_items")
    op.drop_table("farm_plots")
    op.drop_table("farms")
    op.drop_table("crop_types")
    op.drop_table("players")
