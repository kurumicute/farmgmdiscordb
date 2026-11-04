"""world boss logistics event

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-04

- world_boss_events：世界 Boss 活動（群組、名稱、等級、HP、期間、狀態、戰鬥推進時間、設定與獎勵快照）。
  同一群組最多一場未結束的活動（部分唯一索引 uq_world_boss_events_group_open）。
- world_boss_supplies：四種補給（food／medical／combat／equipment）的目標與進度。
- world_boss_contributions：捐獻紀錄；(event_id, player_id, request_id) 唯一，防止重複提交造成重複扣除。
- world_boss_reward_claims：獎勵領取紀錄；(event_id, player_id) 唯一，防止重複領獎。
- farm_fish：新增 donated_at（捐獻的魚離開魚簍，與販售互斥）。
- game_transactions：新增事件類型 boss_donate、boss_reward。

降版會刪除世界 Boss 的所有資料與 boss_donate／boss_reward 交易紀錄，並刪除已捐獻的魚（不會退回物品）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

EVENTS_0018 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal",
)
NEW_EVENTS = ("boss_donate", "boss_reward")
SUPPLY_CHECK = "supply_code IN ('food', 'medical', 'combat', 'equipment')"
TS = sa.DateTime(timezone=True)


def _event_check(events: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.create_table(
        "world_boss_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("max_hp", sa.BigInteger(), nullable=False),
        sa.Column("hp", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("starts_at", TS, nullable=False),
        sa.Column("ends_at", TS, nullable=False),
        sa.Column("last_tick_at", TS, nullable=False),
        sa.Column("paused_at", TS),
        sa.Column("defeated_at", TS),
        sa.Column("ended_at", TS),
        sa.Column("end_reason", sa.String(16)),
        sa.Column("settled_at", TS),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("rewards", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="RESTRICT",
                                name=op.f("fk_world_boss_events_group_id_groups")),
        sa.CheckConstraint("status IN ('active', 'paused', 'ended')", name=op.f("ck_world_boss_events_status_valid")),
        sa.CheckConstraint("max_hp > 0 AND hp >= 0 AND hp <= max_hp", name=op.f("ck_world_boss_events_hp_range")),
        sa.CheckConstraint("level >= 1", name=op.f("ck_world_boss_events_level_positive")),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_world_boss_events_ends_after_start")),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name=op.f("ck_world_boss_events_name_len")),
        sa.CheckConstraint("(status = 'ended') = (ended_at IS NOT NULL AND end_reason IS NOT NULL)",
                           name=op.f("ck_world_boss_events_ended_consistent")),
        sa.CheckConstraint("end_reason IS NULL OR end_reason IN ('defeated', 'timeout', 'admin')",
                           name=op.f("ck_world_boss_events_end_reason_valid")),
        sa.CheckConstraint("(defeated_at IS NOT NULL) = (end_reason = 'defeated')",
                           name=op.f("ck_world_boss_events_defeated_consistent")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_world_boss_events")),
    )
    op.create_index("uq_world_boss_events_group_open", "world_boss_events", ["group_id"], unique=True,
                    postgresql_where=sa.text("status IN ('active', 'paused')"))
    op.create_index("ix_world_boss_events_group_created", "world_boss_events", ["group_id", "created_at"])
    op.create_index("ix_world_boss_events_status_ends", "world_boss_events", ["status", "ends_at"])

    op.create_table(
        "world_boss_supplies",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("supply_code", sa.String(16), nullable=False),
        sa.Column("target", sa.BigInteger(), nullable=False),
        sa.Column("progress", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["event_id"], ["world_boss_events.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_supplies_event_id_world_boss_events")),
        sa.CheckConstraint(SUPPLY_CHECK, name=op.f("ck_world_boss_supplies_supply_valid")),
        sa.CheckConstraint("target > 0", name=op.f("ck_world_boss_supplies_target_positive")),
        sa.CheckConstraint("progress >= 0", name=op.f("ck_world_boss_supplies_progress_non_negative")),
        sa.PrimaryKeyConstraint("event_id", "supply_code", name=op.f("pk_world_boss_supplies")),
    )

    op.create_table(
        "world_boss_contributions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("supply_code", sa.String(16), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column("quality", sa.String(16)),
        sa.Column("mutation", sa.String(16)),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("points", sa.BigInteger(), nullable=False),
        sa.Column("damage", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["world_boss_events.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_contributions_event_id_world_boss_events")),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_contributions_player_id_players")),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_contributions_farm_id_farms")),
        sa.UniqueConstraint("event_id", "player_id", "request_id", name="uq_world_boss_contributions_request"),
        sa.CheckConstraint("category IN ('crop', 'fish', 'material', 'goods', 'special')",
                           name=op.f("ck_world_boss_contributions_category_valid")),
        sa.CheckConstraint(SUPPLY_CHECK, name=op.f("ck_world_boss_contributions_supply_valid")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_world_boss_contributions_quantity_positive")),
        sa.CheckConstraint("points > 0", name=op.f("ck_world_boss_contributions_points_positive")),
        sa.CheckConstraint("damage >= 0", name=op.f("ck_world_boss_contributions_damage_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_world_boss_contributions")),
    )
    op.create_index("ix_world_boss_contributions_event_player", "world_boss_contributions", ["event_id", "player_id"])
    op.create_index("ix_world_boss_contributions_event_created", "world_boss_contributions",
                    ["event_id", "created_at"])

    op.create_table(
        "world_boss_reward_claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("player_name", sa.String(32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("points", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("gold", sa.BigInteger(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(20)),
        sa.Column("claimed_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["world_boss_events.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_reward_claims_event_id_world_boss_events")),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_reward_claims_player_id_players")),
        sa.UniqueConstraint("event_id", "player_id", name="uq_world_boss_reward_claims_once"),
        sa.CheckConstraint("outcome IN ('victory', 'participation')", name=op.f("ck_world_boss_reward_claims_outcome_valid")),
        sa.CheckConstraint("gold >= 0", name=op.f("ck_world_boss_reward_claims_gold_non_negative")),
        sa.CheckConstraint("rank >= 1", name=op.f("ck_world_boss_reward_claims_rank_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_world_boss_reward_claims")),
    )
    op.create_index("ix_world_boss_reward_claims_player", "world_boss_reward_claims", ["player_id"])

    op.add_column("farm_fish", sa.Column("donated_at", TS))
    op.create_check_constraint(op.f("ck_farm_fish_sold_or_donated"), "farm_fish",
                               "sold_at IS NULL OR donated_at IS NULL")

    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0018 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM game_transactions WHERE event_type = ANY(:types)").bindparams(types=list(NEW_EVENTS))
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0018)
    )
    op.execute("DELETE FROM farm_fish WHERE donated_at IS NOT NULL")
    op.drop_constraint(op.f("ck_farm_fish_sold_or_donated"), "farm_fish", type_="check")
    op.drop_column("farm_fish", "donated_at")
    op.drop_table("world_boss_reward_claims")
    op.drop_table("world_boss_contributions")
    op.drop_table("world_boss_supplies")
    op.drop_table("world_boss_events")
