"""seasonal crops and fish, lower bait levels

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-09

- crop_types／fish_types：新增 seasons（JSON 季節清單，NULL = 不限）、hour_start／hour_end（台灣時間整點區間，
  可跨午夜，NULL = 不限）。作物在種下時檢查，魚在拋竿時檢查（規則在 game/seasons.py）。
- 作物（小麥四季都能種，其他每種兩個季節）：胡蘿蔔秋冬、番茄冬春、草莓冬春、玉米春夏、西瓜春夏、南瓜夏秋、
  葡萄夏秋、琥珀瓜秋冬、月光花冬春。
- 魚（只限 Migration 0033 新增的部分）：公魚、烏魚冬，鯖魚、旗魚秋，黃鰭鮪夏，龍王鯉春；藍鰓太陽魚 06–18，
  鱧魚、鰻魚 18–06，月光魟 20–04。
- fishing_gear：所有魚餌的需要等級調低 2 級（最低 Lv.1；使用者要求）。

降版會刪除上述欄位，並把 5 種內建魚餌的需要等級改回原本的值（其他魚餌維持調低後的等級）。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

CROP_SEASONS = {
    "carrot": ["autumn", "winter"], "tomato": ["winter", "spring"], "strawberry": ["winter", "spring"],
    "corn": ["spring", "summer"], "watermelon": ["spring", "summer"], "pumpkin": ["summer", "autumn"],
    "grape": ["summer", "autumn"], "amber_melon": ["autumn", "winter"], "moonflower": ["winter", "spring"],
}
FISH_LIMITS = {
    "pond_smelt": (["winter"], None, None),
    "mullet": (["winter"], None, None),
    "mackerel": (["autumn"], None, None),
    "yellowfin_tuna": (["summer"], None, None),
    "swordfish": (["autumn"], None, None),
    "dragon_king_carp": (["spring"], None, None),
    "bluegill": (None, 6, 18),
    "snakehead": (None, 18, 6),
    "eel": (None, 18, 6),
    "moon_ray": (None, 20, 4),
}
BAIT_LEVELS_0033 = {"worm_bait": 1, "premium_bait": 3, "special_bait": 6, "deep_sea_bait": 8, "dragon_bait": 10}
HOURS_CHECK = ("(hour_start IS NULL) = (hour_end IS NULL) AND (hour_start IS NULL OR "
               "(hour_start BETWEEN 0 AND 23 AND hour_end BETWEEN 0 AND 23 AND hour_start <> hour_end))")


def _add_columns(table: str) -> None:
    op.add_column(table, sa.Column("seasons", sa.JSON(), nullable=True))
    op.add_column(table, sa.Column("hour_start", sa.SmallInteger(), nullable=True))
    op.add_column(table, sa.Column("hour_end", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(op.f(f"ck_{table}_hours_valid"), table, HOURS_CHECK)


def upgrade() -> None:
    _add_columns("crop_types")
    _add_columns("fish_types")
    crop = sa.text("UPDATE crop_types SET seasons = CAST(:s AS json) WHERE code = :code")
    for code, seasons in CROP_SEASONS.items():
        op.execute(crop.bindparams(code=code, s=json.dumps(seasons)))
    fish = sa.text("UPDATE fish_types SET seasons = CAST(:s AS json), hour_start = :hs, hour_end = :he "
                   "WHERE code = :code")
    for code, (seasons, hs, he) in FISH_LIMITS.items():
        op.execute(fish.bindparams(code=code, s=json.dumps(seasons) if seasons else None, hs=hs, he=he))
    op.execute("UPDATE fishing_gear SET required_level = GREATEST(1, required_level - 2) WHERE kind = 'bait'")


def downgrade() -> None:
    restore = sa.text("UPDATE fishing_gear SET required_level = :lv WHERE code = :code")
    for code, level in BAIT_LEVELS_0033.items():
        op.execute(restore.bindparams(code=code, lv=level))
    for table in ("fish_types", "crop_types"):
        op.drop_constraint(op.f(f"ck_{table}_hours_valid"), table, type_="check")
        for column in ("hour_end", "hour_start", "seasons"):
            op.drop_column(table, column)
