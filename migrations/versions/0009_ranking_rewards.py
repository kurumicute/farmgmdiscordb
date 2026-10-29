"""ranking reward rules, settlements and awards

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27

- ranking_reward_rules：排名獎勵規則（群組／個人、金幣／農產品販售／商品總販售、每日／每週／每月、各名次獎勵）。
- ranking_settlements：每條規則每個期間的結算快照，(rule_id, period_start) 唯一，不會重複發獎。
- ranking_awards：獎勵發放紀錄，(settlement_id, player_id) 唯一。
- 新事件類型 ranking_reward。只新增資料表與允許的事件類型，不修改既有資料。

降版注意：會刪除上述三張表與 ranking_reward 交易紀錄（已發出的金幣與道具不會收回）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

EVENTS_0008 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust",
)
EVENTS_0009 = EVENTS_0008 + ("ranking_reward",)


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    op.create_table(
        "ranking_reward_rules",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("metric", sa.String(16), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("rewards", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("active_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('group', 'individual')", name=op.f("ck_ranking_reward_rules_scope_valid")),
        sa.CheckConstraint(
            "metric IN ('money', 'sales', 'goods')", name=op.f("ck_ranking_reward_rules_metric_valid")
        ),
        sa.CheckConstraint(
            "period IN ('daily', 'weekly', 'monthly')", name=op.f("ck_ranking_reward_rules_period_valid")
        ),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 60", name=op.f("ck_ranking_reward_rules_name_len")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_reward_rules")),
    )
    op.create_table(
        "ranking_settlements",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=True),
        sa.Column("rule_snapshot", sa.JSON(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ranking", sa.JSON(), nullable=False),
        sa.Column("settled_by", sa.String(64), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("period_end > period_start", name=op.f("ck_ranking_settlements_period_valid")),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["ranking_reward_rules.id"],
            name=op.f("fk_ranking_settlements_rule_id_ranking_reward_rules"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_settlements")),
        sa.UniqueConstraint("rule_id", "period_start", name="uq_ranking_settlements_rule_period"),
    )
    op.create_index("ix_ranking_settlements_settled_at", "ranking_settlements", ["settled_at"])
    op.create_table(
        "ranking_awards",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=True),
        sa.Column("player_name", sa.String(32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("reward_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_ranking_awards_amount_positive")),
        sa.CheckConstraint("rank >= 1", name=op.f("ck_ranking_awards_rank_positive")),
        sa.ForeignKeyConstraint(
            ["settlement_id"],
            ["ranking_settlements.id"],
            name=op.f("fk_ranking_awards_settlement_id_ranking_settlements"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"], name=op.f("fk_ranking_awards_player_id_players"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_ranking_awards_group_id_groups"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ranking_awards")),
        sa.UniqueConstraint("settlement_id", "player_id", name="uq_ranking_awards_once"),
    )
    op.create_index("ix_ranking_awards_player", "ranking_awards", ["player_id"])
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0009)
    )


def downgrade() -> None:
    op.execute("DELETE FROM game_transactions WHERE event_type = 'ranking_reward'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0008)
    )
    op.drop_index("ix_ranking_awards_player", table_name="ranking_awards")
    op.drop_table("ranking_awards")
    op.drop_index("ix_ranking_settlements_settled_at", table_name="ranking_settlements")
    op.drop_table("ranking_settlements")
    op.drop_table("ranking_reward_rules")
