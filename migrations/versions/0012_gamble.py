"""gamble: prize stacks and gamble event type

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27

- gamble_prizes：賭場贏得的作物（價值倍率 2 或 5），佔倉庫空間，可賣出或保留展示。
- 新事件類型 gamble（下注結果，含擲出的數字；本身不改變金幣）。

降版會刪除 gamble_prizes 與 gamble 交易紀錄（尚未賣出的獎品會消失；已賣出的金幣保留在 sell 紀錄中）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

EVENTS_0010 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
)
EVENTS_0012 = EVENTS_0010 + ("gamble",)
QUALITIES = "'normal', 'good', 'rare', 'legendary'"
MUTATIONS = "'none', 'giant', 'golden', 'fast', 'variant'"


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    op.create_table(
        "gamble_prizes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("mutation", sa.String(16), nullable=False),
        sa.Column("multiplier", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"quality IN ({QUALITIES})", name=op.f("ck_gamble_prizes_quality_valid")),
        sa.CheckConstraint(f"mutation IN ({MUTATIONS})", name=op.f("ck_gamble_prizes_mutation_valid")),
        sa.CheckConstraint("multiplier IN (2, 5)", name=op.f("ck_gamble_prizes_multiplier_valid")),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_gamble_prizes_quantity_non_negative")),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_gamble_prizes_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gamble_prizes")),
        sa.UniqueConstraint(
            "farm_id", "item_code", "quality", "mutation", "multiplier", name="uq_gamble_prizes_farm_stack"
        ),
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0012)
    )


def downgrade() -> None:
    op.execute("DELETE FROM game_transactions WHERE event_type = 'gamble'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0010)
    )
    op.drop_table("gamble_prizes")
