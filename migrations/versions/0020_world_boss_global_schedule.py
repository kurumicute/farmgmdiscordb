"""world boss: global events and fixed schedules (深淵魔龍)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-04

- world_boss_events.group_id 改為可空：NULL 表示全服活動（所有伺服器的玩家都能參加）。
  「同一群組最多一場未結束的活動」改為「手動建立的活動同一範圍（全服或同一群組）最多一場」（COALESCE(group_id, 0)），
  排程產生的活動改為「每個排程最多一場未結束」。
- world_boss_schedules：固定週期出現的世界 Boss（間隔天數、台灣時間時分、起始日期、持續分鐘、HP、設定與獎勵範本）。
- world_boss_events 新增 schedule_id（排程刪除時設為 NULL，活動紀錄保留）與 slot_at（排定時間），(schedule_id, slot_at) 唯一，
  同一場不會被建立兩次。
- 寫入預設排程「深淵魔龍」：全服、每 2 天 21:00（台灣時間）出現、持續 3 小時、HP 15,000（數值與 app/game/world_boss.ABYSS_DRAGON 相同）。

降版會刪除全服活動、排程產生的活動與排程（含其捐獻與領獎紀錄），再還原欄位與索引。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)

ABYSS_SETTINGS = {
    "base_damage_per_hour": 1500,
    "instant_damage_per_point": 2,
    "gold_per_point": 10,
    "crop_supply": "food",
    "fish_supply": "combat",
    "supply_targets": {"food": 1000, "medical": 600, "combat": 700, "equipment": 800},
    "effects": {"ratio_cap_pct": 150, "base_uptime_pct": 30, "medical_recovery_pct": 50, "combat_attack_pct": 100,
                "equipment_attack_pct": 50},
    "limits": {"per_item_max": 200, "per_action_max": 99},
    "items": {
        "herb": {"supply": "medical", "points": 6},
        "egg": {"supply": "medical", "points": 3},
        "milk": {"supply": "medical", "points": 6},
        "strawberry_jam": {"supply": "medical", "points": 30},
        "flour": {"supply": "combat", "points": 5},
        "bread": {"supply": "combat", "points": 20},
        "wool": {"supply": "equipment", "points": 12},
        "wood": {"supply": "equipment", "points": 2},
        "stone": {"supply": "equipment", "points": 2},
        "copper_ore": {"supply": "equipment", "points": 3},
        "resin": {"supply": "equipment", "points": 2},
        "iron_ore": {"supply": "equipment", "points": 6},
        "gear": {"supply": "equipment", "points": 12},
        "crystal": {"supply": "equipment", "points": 15},
        "ancient_core": {"supply": "equipment", "points": 40},
        "machine_core": {"supply": "equipment", "points": 40},
    },
}
ABYSS_REWARDS = {
    "victory": {"gold": 1200, "items": {"crystal": 2}},
    "participation": {"gold": 300, "items": {}},
    "bonus_points": 200,
    "bonus": {"gold": 500, "items": {"seed_amber_melon": 2}},
    "rank": [
        {"gold": 3000, "items": {"seed_moonflower": 2, "ancient_core": 1}},
        {"gold": 2000, "items": {"seed_moonflower": 1, "machine_core": 1}},
        {"gold": 1500, "items": {"ancient_core": 1}},
        {"gold": 800, "items": {"gear": 2}},
        {"gold": 800, "items": {"gear": 2}},
    ] + [{"gold": 400, "items": {"crystal": 1}}] * 5,
    "title": "降龍勇士",
    "title_top": 3,
}


def upgrade() -> None:
    op.create_table(
        "world_boss_schedules",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("group_id", sa.BigInteger()),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("spawn_hour", sa.Integer(), nullable=False),
        sa.Column("spawn_minute", sa.Integer(), nullable=False),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("max_hp", sa.BigInteger(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("rewards", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE",
                                name=op.f("fk_world_boss_schedules_group_id_groups")),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 40", name=op.f("ck_world_boss_schedules_name_len")),
        sa.CheckConstraint("interval_days BETWEEN 1 AND 30", name=op.f("ck_world_boss_schedules_interval_range")),
        sa.CheckConstraint("spawn_hour BETWEEN 0 AND 23 AND spawn_minute BETWEEN 0 AND 59",
                           name=op.f("ck_world_boss_schedules_time_range")),
        sa.CheckConstraint("duration_minutes BETWEEN 10 AND 10080",
                           name=op.f("ck_world_boss_schedules_duration_range")),
        sa.CheckConstraint("max_hp > 0", name=op.f("ck_world_boss_schedules_max_hp_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_world_boss_schedules")),
    )
    op.alter_column("world_boss_events", "group_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("world_boss_events", sa.Column("schedule_id", sa.BigInteger()))
    op.add_column("world_boss_events", sa.Column("slot_at", TS))
    op.create_foreign_key(op.f("fk_world_boss_events_schedule_id_world_boss_schedules"), "world_boss_events",
                          "world_boss_schedules", ["schedule_id"], ["id"], ondelete="SET NULL")
    op.create_unique_constraint("uq_world_boss_events_schedule_slot", "world_boss_events", ["schedule_id", "slot_at"])
    op.drop_index("uq_world_boss_events_group_open", table_name="world_boss_events")
    op.create_index("uq_world_boss_events_scope_open", "world_boss_events", [sa.text("COALESCE(group_id, 0)")],
                    unique=True, postgresql_where=sa.text("status IN ('active', 'paused') AND schedule_id IS NULL"))
    op.create_index("uq_world_boss_events_schedule_open", "world_boss_events", ["schedule_id"], unique=True,
                    postgresql_where=sa.text("status IN ('active', 'paused') AND schedule_id IS NOT NULL"))
    op.execute(sa.text(
        "INSERT INTO world_boss_schedules (name, group_id, interval_days, spawn_hour, spawn_minute, anchor_date, "
        "duration_minutes, max_hp, settings, rewards, enabled, created_by, created_at, updated_at) VALUES "
        "('深淵魔龍', NULL, 2, 21, 0, DATE '2026-11-04', 180, 15000, CAST(:settings AS json), CAST(:rewards AS json), "
        "true, 'migration', now(), now())"
    ).bindparams(settings=json.dumps(ABYSS_SETTINGS, ensure_ascii=False),
                 rewards=json.dumps(ABYSS_REWARDS, ensure_ascii=False)))


def downgrade() -> None:
    op.execute("DELETE FROM world_boss_events WHERE group_id IS NULL")
    op.drop_index("uq_world_boss_events_schedule_open", table_name="world_boss_events")
    op.drop_index("uq_world_boss_events_scope_open", table_name="world_boss_events")
    op.execute("DELETE FROM world_boss_events WHERE schedule_id IS NOT NULL")
    op.create_index("uq_world_boss_events_group_open", "world_boss_events", ["group_id"], unique=True,
                    postgresql_where=sa.text("status IN ('active', 'paused')"))
    op.drop_constraint("uq_world_boss_events_schedule_slot", "world_boss_events", type_="unique")
    op.drop_constraint(op.f("fk_world_boss_events_schedule_id_world_boss_schedules"), "world_boss_events",
                       type_="foreignkey")
    op.drop_column("world_boss_events", "slot_at")
    op.drop_column("world_boss_events", "schedule_id")
    op.alter_column("world_boss_events", "group_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_table("world_boss_schedules")
