"""groups, stealing, watering/withering, tools and effects

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

OLD_EVENT_TYPES = ("create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade")
NEW_EVENT_TYPES = OLD_EVENT_TYPES + ("buy_item", "use_item", "water", "clear", "steal", "stolen")

OLD_CROP_CONSISTENCY = (
    "(crop_type_id IS NULL AND planted_at IS NULL AND ready_at IS NULL) OR "
    "(crop_type_id IS NOT NULL AND planted_at IS NOT NULL AND ready_at IS NOT NULL "
    "AND ready_at > planted_at)"
)
NEW_CROP_CONSISTENCY = (
    "(crop_type_id IS NULL AND planted_at IS NULL AND watered_at IS NULL AND ready_at IS NULL "
    "AND remaining_quantity IS NULL AND NOT fertilized) OR "
    "(crop_type_id IS NOT NULL AND planted_at IS NOT NULL AND remaining_quantity IS NOT NULL "
    "AND (watered_at IS NULL) = (ready_at IS NULL) "
    "AND (watered_at IS NULL OR (watered_at >= planted_at AND ready_at > watered_at)))"
)


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    # ---- farm_plots：澆水、剩餘數量、化肥、種植序號 ----
    op.add_column("farm_plots", sa.Column("watered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("farm_plots", sa.Column("remaining_quantity", sa.Integer(), nullable=True))
    op.add_column("farm_plots", sa.Column("fertilized", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("farm_plots", sa.Column("planting_no", sa.BigInteger(), server_default="0", nullable=False))
    op.drop_constraint(op.f("ck_farm_plots_crop_consistency"), "farm_plots", type_="check")
    # 既有種植中的作物：視為種下時已澆水，剩餘數量為完整收成量（不改變原本的成熟時間）
    op.execute(
        "UPDATE farm_plots p SET watered_at = p.planted_at, remaining_quantity = c.harvest_quantity, "
        "planting_no = 1 FROM crop_types c WHERE p.crop_type_id = c.id"
    )
    op.create_check_constraint(op.f("ck_farm_plots_crop_consistency"), "farm_plots", NEW_CROP_CONSISTENCY)
    op.create_check_constraint(op.f("ck_farm_plots_remaining_quantity_positive"), "farm_plots", "remaining_quantity >= 1")
    op.create_check_constraint(op.f("ck_farm_plots_planting_no_non_negative"), "farm_plots", "planting_no >= 0")

    # ---- game_transactions：新事件類型與販售統計索引 ----
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(NEW_EVENT_TYPES)
    )
    op.create_index(
        "ix_game_transactions_sell_farm",
        "game_transactions",
        ["farm_id"],
        postgresql_where=sa.text("event_type = 'sell'"),
    )

    # ---- 群組 ----
    op.create_table(
        "groups",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("discord_guild_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name=op.f("ck_groups_name_len")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
        sa.UniqueConstraint("discord_guild_id", name=op.f("uq_groups_discord_guild_id")),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_group_members_group_id_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"], name=op.f("fk_group_members_player_id_players"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("group_id", "player_id", name=op.f("pk_group_members")),
    )
    op.create_index("ix_group_members_player_id", "group_members", ["player_id"])

    # ---- 永久工具與限時效果 ----
    op.create_table(
        "farm_tools",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_code", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_tools_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_tools")),
        sa.UniqueConstraint("farm_id", "tool_code", name="uq_farm_tools_farm_tool"),
    )
    op.create_table(
        "farm_effects",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("effect_code", sa.String(64), nullable=False),
        sa.Column("multiplier", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expires_at > started_at", name=op.f("ck_farm_effects_expires_after_start")),
        sa.CheckConstraint("multiplier > 0", name=op.f("ck_farm_effects_multiplier_positive")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_effects_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_effects")),
        sa.UniqueConstraint("farm_id", "effect_code", name="uq_farm_effects_farm_effect"),
    )

    # ---- 偷菜紀錄 ----
    op.create_table(
        "steal_records",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("thief_player_id", sa.BigInteger(), nullable=False),
        sa.Column("victim_player_id", sa.BigInteger(), nullable=False),
        sa.Column("plot_id", sa.BigInteger(), nullable=False),
        sa.Column("plot_number", sa.Integer(), nullable=False),
        sa.Column("planting_no", sa.BigInteger(), nullable=False),
        sa.Column("crop_type_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity BETWEEN 1 AND 2", name=op.f("ck_steal_records_quantity_range")),
        sa.CheckConstraint("thief_player_id <> victim_player_id", name=op.f("ck_steal_records_not_self")),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_steal_records_group_id_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["thief_player_id"],
            ["players.id"],
            name=op.f("fk_steal_records_thief_player_id_players"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["victim_player_id"],
            ["players.id"],
            name=op.f("fk_steal_records_victim_player_id_players"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plot_id"], ["farm_plots.id"], name=op.f("fk_steal_records_plot_id_farm_plots"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["crop_type_id"],
            ["crop_types.id"],
            name=op.f("fk_steal_records_crop_type_id_crop_types"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_steal_records")),
        sa.UniqueConstraint("plot_id", "planting_no", "thief_player_id", name="uq_steal_records_once_per_planting"),
    )
    op.create_index("ix_steal_records_created_at", "steal_records", ["created_at"])
    op.create_index("ix_steal_records_group_created", "steal_records", ["group_id", "created_at"])
    op.create_index("ix_steal_records_thief", "steal_records", ["thief_player_id"])
    op.create_index("ix_steal_records_victim", "steal_records", ["victim_player_id"])


def downgrade() -> None:
    # 注意：降版會刪除群組、偷菜紀錄、工具與效果，並清空「等待澆水」的土地
    op.drop_table("steal_records")
    op.drop_table("farm_effects")
    op.drop_table("farm_tools")
    op.drop_index("ix_group_members_player_id", table_name="group_members")
    op.drop_table("group_members")
    op.drop_table("groups")

    op.drop_index("ix_game_transactions_sell_farm", table_name="game_transactions")
    op.execute("DELETE FROM game_transactions WHERE " + "NOT " + _event_check(OLD_EVENT_TYPES))
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(OLD_EVENT_TYPES)
    )

    op.drop_constraint(op.f("ck_farm_plots_planting_no_non_negative"), "farm_plots", type_="check")
    op.drop_constraint(op.f("ck_farm_plots_remaining_quantity_positive"), "farm_plots", type_="check")
    op.drop_constraint(op.f("ck_farm_plots_crop_consistency"), "farm_plots", type_="check")
    op.execute(
        "UPDATE farm_plots SET crop_type_id = NULL, planted_at = NULL, ready_at = NULL WHERE watered_at IS NULL"
    )
    op.execute("UPDATE farm_plots SET planted_at = LEAST(planted_at, watered_at) WHERE watered_at IS NOT NULL")
    op.drop_column("farm_plots", "planting_no")
    op.drop_column("farm_plots", "fertilized")
    op.drop_column("farm_plots", "remaining_quantity")
    op.drop_column("farm_plots", "watered_at")
    op.create_check_constraint(op.f("ck_farm_plots_crop_consistency"), "farm_plots", OLD_CROP_CONSISTENCY)
