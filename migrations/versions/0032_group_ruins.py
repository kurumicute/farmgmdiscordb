"""group ruins, ruins guardian and farm rumors

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-09

- group_ruins：每個群組一列，古代遺跡的修復階段（0–3，3 = 修復完成）與目前階段已捐的素材 {代碼: 數量}。
- ruins_contributions：每座農場對群組遺跡累計捐了幾個素材（修復貢獻排行、完成時發稱號）。
- 山洞區域新增 ruins（古代遺跡）：farm_explorations／exploration_logs 的 area_code CHECK 加入 ruins。
- game_transactions：新增事件類型 ruins_donate（捐素材修復遺跡，金幣不變）、ruins_guardian（捐素材攻擊遺跡守護者，
  金幣不變）、rumor_treasure（在傳聞的農場挖到寶藏，金幣增加）。
- group_ruins.guardian_settled：已結算到哪一週（台灣週一）的守護者；collapsed_at：最近一次因守護者沒被打倒而崩壞的時間。
- ruins_guardians：每個群組每週一隻守護者（HP、累計傷害、被打倒的時間）；ruins_guardian_hits：每座農場每週造成的傷害。
- rumor_digs：每座農場每天只能在傳聞的田裡挖一次；(group_id, day) WHERE found 唯一，寶藏只會被一個人挖到。
（農場日報只讀取既有的交易紀錄，在面板總覽即時產生，不需要新資料表。）

降版會刪除上述表；ruins 區域的探險紀錄改成 mine（保留資料列以維持 CHECK 約束）、次數統計刪除，
ruins_donate／ruins_guardian 交易紀錄改成 use_item（金幣不變）、rumor_treasure 改成 quest_reward（同樣是獎勵金幣），
不影響「gold_delta 加總 = 全服金幣」的不變式。
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

EVENTS_0031 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal", "boss_donate", "boss_reward",
    "rename_farm", "checkin_reward", "achievement_reward",
)
NEW_EVENTS = ("ruins_donate", "ruins_guardian", "rumor_treasure")
AREAS_0031 = ("grassland", "forest", "mine")
NEW_AREAS = ("ruins",)
AREA_TABLES = ("farm_explorations", "exploration_logs")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _set_checks(events: tuple[str, ...], areas: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _in("event_type", events)
    )
    for table in AREA_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_area_code_valid"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_area_code_valid"), table, _in("area_code", areas))


def upgrade() -> None:
    _set_checks(EVENTS_0031 + NEW_EVENTS, AREAS_0031 + NEW_AREAS)

    op.create_table(
        "group_ruins",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guardian_settled", sa.Date(), nullable=True),
        sa.Column("collapsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("stage BETWEEN 0 AND 3", name=op.f("ck_group_ruins_stage_valid")),
        sa.CheckConstraint("(stage = 3) = (completed_at IS NOT NULL)",
                           name=op.f("ck_group_ruins_completed_consistency")),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name=op.f("fk_group_ruins_group_id_groups"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", name=op.f("pk_group_ruins")),
    )
    op.create_table(
        "ruins_contributions",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_ruins_contributions_quantity_non_negative")),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name=op.f("fk_ruins_contributions_group_id_groups"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_ruins_contributions_farm_id_farms"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "farm_id", name=op.f("pk_ruins_contributions")),
    )
    op.create_table(
        "ruins_guardians",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("hp_max", sa.Integer(), nullable=False),
        sa.Column("damage", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("defeated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("hp_max > 0", name=op.f("ck_ruins_guardians_hp_positive")),
        sa.CheckConstraint("damage >= 0", name=op.f("ck_ruins_guardians_damage_non_negative")),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name=op.f("fk_ruins_guardians_group_id_groups"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "week_start", name=op.f("pk_ruins_guardians")),
    )
    op.create_table(
        "ruins_guardian_hits",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("damage", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("damage >= 0", name=op.f("ck_ruins_guardian_hits_damage_non_negative")),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name=op.f("fk_ruins_guardian_hits_group_id_groups"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_ruins_guardian_hits_farm_id_farms"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "week_start", "farm_id", name=op.f("pk_ruins_guardian_hits")),
    )
    op.create_table(
        "rumor_digs",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("target_farm_id", sa.BigInteger(), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column("gold", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("gold >= 0 AND (found OR gold = 0)", name=op.f("ck_rumor_digs_gold_valid")),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_rumor_digs_farm_id_farms"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_farm_id"], ["farms.id"], name=op.f("fk_rumor_digs_target_farm_id_farms"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name=op.f("fk_rumor_digs_group_id_groups"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("farm_id", "day", name=op.f("pk_rumor_digs")),
    )
    op.create_index("uq_rumor_digs_group_day_found", "rumor_digs", ["group_id", "day"], unique=True,
                    postgresql_where=sa.text("found"))


def downgrade() -> None:
    op.drop_index("uq_rumor_digs_group_day_found", table_name="rumor_digs", postgresql_where=sa.text("found"))
    op.drop_table("rumor_digs")
    op.drop_table("ruins_guardian_hits")
    op.drop_table("ruins_guardians")
    op.drop_table("ruins_contributions")
    op.drop_table("group_ruins")
    op.execute("UPDATE game_transactions SET event_type = 'use_item' "
               "WHERE event_type IN ('ruins_donate', 'ruins_guardian')")
    op.execute("UPDATE game_transactions SET event_type = 'quest_reward' WHERE event_type = 'rumor_treasure'")
    # farm_explorations 只是各區域的次數統計，且 (farm_id, area_code) 唯一，直接刪除 ruins 列
    op.execute("DELETE FROM farm_explorations WHERE area_code = 'ruins'")
    op.execute("UPDATE exploration_logs SET area_code = 'mine' WHERE area_code = 'ruins'")
    _set_checks(EVENTS_0031, AREAS_0031)
