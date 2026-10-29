"""crop grow times 1/2/4/8 hours, free-water and harvest-loss event effects, weekday events

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27

- 初始作物生長時間：小麥 1 小時、胡蘿蔔 2 小時、番茄 4 小時、草莓 8 小時。
  只更新仍是 Migration 0007 預設值（7200 秒）的作物，管理員在網站調整過的不覆蓋。已澆水作物的成熟時間不變。
- game_events 新增兩種效果：free_water（1 = 種下立即澆水）、harvest_loss（收成時每格少 N 個，至少保留 1 個）。
- 新增 7 個每週活動（週一到週日各一個，全天），created_by = 'system'；同名活動已存在時不重複建立。
  之後可在管理網站修改或停用。

降版：刪除這 7 個系統活動與兩個欄位（使用新效果的其他活動會被一併刪除），作物時間恢復為 7200 秒
（只還原仍等於本 Migration 設定值的作物）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

OLD_SECONDS = 7200
NEW_SECONDS = {"wheat": 3600, "carrot": 7200, "tomato": 14400, "strawberry": 28800}

# (星期位元：週一 = 1 … 週日 = 64, 名稱, 說明, 效果)
WEEKDAY_EVENTS = (
    (1, "☀️ 晴朗好天氣", "陽光充足、土壤濕潤，今天種下的作物不用澆水就會開始生長。", {"free_water": 1}),
    (2, "🌬️ 大風來襲", "強風吹過農場，收成時每格作物會被吹走 1 個（至少保留 1 個）。", {"harvest_loss": 1}),
    (4, "🌧️ 及時雨", "一場好雨，今天澆水的作物生長時間減少 20%。", {"grow_reduction_pct": 20}),
    (8, "📈 市集熱絡", "城裡的商人都來了，今天販售農作物售價 ×1.2。", {"sell_price_pct": 120}),
    (16, "🐝 蜜蜂授粉", "蜜蜂忙著授粉，今天種下的作物收成 +20%。", {"harvest_bonus_pct": 20}),
    (32, "🌈 彩虹日", "彩虹帶來好運，今天種下的作物品質加成 +5%、突變機率 +2%。",
     {"quality_bonus_bp": 500, "mutation_bonus_bp": 200}),
    (64, "🎉 豐收慶典", "週末慶典！今天種下的作物不用澆水，販售售價 ×1.1。", {"free_water": 1, "sell_price_pct": 110}),
)
EFFECT_DEFAULTS = {
    "sell_price_pct": 100, "grow_reduction_pct": 0, "quality_bonus_bp": 0, "mutation_bonus_bp": 0,
    "harvest_bonus_pct": 0, "free_water": 0, "harvest_loss": 0,
}


def upgrade() -> None:
    for code, seconds in NEW_SECONDS.items():
        op.execute(
            sa.text("UPDATE crop_types SET grow_seconds = :new WHERE code = :code AND grow_seconds = :old").bindparams(
                new=seconds, code=code, old=OLD_SECONDS
            )
        )

    op.add_column("game_events", sa.Column("free_water", sa.Integer(), server_default="0", nullable=False))
    op.add_column("game_events", sa.Column("harvest_loss", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint(op.f("ck_game_events_free_water_range"), "game_events", "free_water BETWEEN 0 AND 1")
    op.create_check_constraint(
        op.f("ck_game_events_harvest_loss_range"), "game_events", "harvest_loss BETWEEN 0 AND 10"
    )

    insert = sa.text(
        "INSERT INTO game_events (name, description, crop_type_id, sell_price_pct, grow_reduction_pct, "
        "quality_bonus_bp, mutation_bonus_bp, harvest_bonus_pct, free_water, harvest_loss, schedule_type, "
        "start_minute, end_minute, weekdays, enabled, created_by, created_at, updated_at) "
        "SELECT :name, :description, NULL, :sell_price_pct, :grow_reduction_pct, :quality_bonus_bp, "
        ":mutation_bonus_bp, :harvest_bonus_pct, :free_water, :harvest_loss, 'weekly', 0, 1440, :weekdays, true, "
        "'system', now(), now() WHERE NOT EXISTS (SELECT 1 FROM game_events WHERE name = :name)"
    )
    for weekdays, name, description, effects in WEEKDAY_EVENTS:
        op.execute(insert.bindparams(name=name, description=description, weekdays=weekdays,
                                     **{**EFFECT_DEFAULTS, **effects}))


def downgrade() -> None:
    names = [name for _w, name, _d, _e in WEEKDAY_EVENTS]
    op.execute(
        sa.text("DELETE FROM game_events WHERE created_by = 'system' AND name = ANY(:names)").bindparams(names=names)
    )
    op.execute("DELETE FROM game_events WHERE free_water <> 0 OR harvest_loss <> 0")
    op.drop_constraint(op.f("ck_game_events_harvest_loss_range"), "game_events", type_="check")
    op.drop_constraint(op.f("ck_game_events_free_water_range"), "game_events", type_="check")
    op.drop_column("game_events", "harvest_loss")
    op.drop_column("game_events", "free_water")
    for code, seconds in NEW_SECONDS.items():
        op.execute(
            sa.text("UPDATE crop_types SET grow_seconds = :old WHERE code = :code AND grow_seconds = :new").bindparams(
                new=seconds, code=code, old=OLD_SECONDS
            )
        )
