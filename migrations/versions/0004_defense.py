"""farm defenses and steal attempt outcomes

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27

- farm_defenses：稻草人／警戒犬／防盜圍欄等永久設施與守護範圍。
- steal_records 擴充為「偷菜嘗試」：outcome（success/blocked）、blocked_by、block_chance、roll。
  既有紀錄全部標記為 success，不刪除或修改原有欄位資料。
- 新事件類型：steal_blocked、defended、defense_place。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

EVENTS_0003 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
)
EVENTS_0004 = EVENTS_0003 + ("steal_blocked", "defended", "defense_place")


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    op.create_table(
        "farm_defenses",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("device_code", sa.String(32), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("start_plot", sa.Integer(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot >= 1", name=op.f("ck_farm_defenses_slot_positive")),
        sa.CheckConstraint("start_plot IS NULL OR start_plot >= 1", name=op.f("ck_farm_defenses_start_plot_positive")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_defenses_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_defenses")),
        sa.UniqueConstraint("farm_id", "device_code", "slot", name="uq_farm_defenses_farm_device_slot"),
    )

    op.add_column(
        "steal_records", sa.Column("outcome", sa.String(16), server_default="success", nullable=False)
    )
    op.add_column("steal_records", sa.Column("blocked_by", sa.String(32), nullable=True))
    op.add_column("steal_records", sa.Column("block_chance", sa.Float(), nullable=True))
    op.add_column("steal_records", sa.Column("roll", sa.Float(), nullable=True))
    op.create_check_constraint(
        op.f("ck_steal_records_outcome_consistency"),
        "steal_records",
        "(outcome = 'success' AND blocked_by IS NULL) OR (outcome = 'blocked' AND blocked_by IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_steal_records_block_chance_range"),
        "steal_records",
        "block_chance IS NULL OR (block_chance >= 0 AND block_chance <= 1)",
    )
    op.create_index("ix_steal_records_victim_created", "steal_records", ["victim_player_id", "created_at"])

    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0004)
    )


def downgrade() -> None:
    # 注意：降版會刪除防守設施、被阻止的偷菜嘗試與其事件紀錄；成功的偷菜紀錄與其他遊戲資料保留
    op.execute("DELETE FROM game_transactions WHERE event_type IN ('steal_blocked', 'defended', 'defense_place')")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0003)
    )
    op.drop_index("ix_steal_records_victim_created", table_name="steal_records")
    op.execute("DELETE FROM steal_records WHERE outcome <> 'success'")
    op.drop_constraint(op.f("ck_steal_records_block_chance_range"), "steal_records", type_="check")
    op.drop_constraint(op.f("ck_steal_records_outcome_consistency"), "steal_records", type_="check")
    op.drop_column("steal_records", "roll")
    op.drop_column("steal_records", "block_chance")
    op.drop_column("steal_records", "blocked_by")
    op.drop_column("steal_records", "outcome")
    op.drop_table("farm_defenses")
