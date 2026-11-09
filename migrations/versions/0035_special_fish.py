"""special seasonal fish

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-09

- fish_types：新增 10 種特殊魚，都有季節或時段限定（台灣時間），部分還要指定魚餌：
  黎明鱒 04–07、雪精靈冰魚 冬 05–09、螢火魷 春 20–04（深海誘餌以上）、燈籠魚 夏 20–04、楓紅鮭 秋 16–19、
  虹彩隆頭魚 夏 10–16、午夜鮟鱇 00–03（深海誘餌以上）、春神錦鯉 春 06–10（龍涎香餌）、
  幽靈鯊 秋 21–03（深海誘餌以上）、霜龍魚 冬 22–02（龍涎香餌）。
已有相同代碼或名稱時略過。降版刪除還沒被釣過的這些魚種。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

DEEP = ["deep_sea_bait", "dragon_bait"]
DRAGON = ["dragon_bait"]
# code, 名稱, emoji, 稀有度, 最低售價, 最高售價, 最小重量 g, 最大重量 g, 難度, 說明, 排序, 指定魚餌, 季節, 開始時, 結束時
FISH = (
    ("dawn_trout", "黎明鱒", "🌅", "rare", 35, 60, 300, 2500, 3, "只在天剛亮（04–07 點）時出現的鱒魚。", 360,
     None, None, 4, 7),
    ("snow_icefish", "雪精靈冰魚", "❄️", "rare", 40, 65, 50, 300, 2, "冬天清晨才會游到淺水的透明小魚。", 370,
     None, ["winter"], 5, 9),
    ("firefly_squid", "螢火魷", "✨", "epic", 100, 160, 50, 200, 3, "春天夜裡發出藍光的魷魚，要用深海誘餌以上的魚餌。",
     380, DEEP, ["spring"], 20, 4),
    ("lanternfish", "燈籠魚", "🏮", "epic", 90, 150, 100, 600, 3, "夏夜提著小燈籠浮上海面的深海魚。", 390,
     None, ["summer"], 20, 4),
    ("maple_salmon", "楓紅鮭", "🍁", "epic", 110, 170, 2000, 12000, 4, "秋天黃昏逆流而上，身上像楓葉一樣紅。", 400,
     None, ["autumn"], 16, 19),
    ("rainbow_wrasse", "虹彩隆頭魚", "🌈", "epic", 95, 155, 200, 2000, 3, "夏天正午的珊瑚礁上閃著七彩光芒。", 410,
     None, ["summer"], 10, 16),
    ("midnight_angler", "午夜鮟鱇", "🌑", "legendary", 320, 520, 5000, 40000, 5,
     "只在午夜 0–3 點提著燈出沒，要用深海誘餌以上的魚餌。", 420, DEEP, None, 0, 3),
    ("spring_koi", "春神錦鯉", "🌸", "legendary", 350, 600, 3000, 15000, 5, "春天早晨才會現身的神之錦鯉，只吃龍涎香餌。",
     430, DRAGON, ["spring"], 6, 10),
    ("ghost_shark", "幽靈鯊", "👻", "legendary", 400, 650, 20000, 150000, 5,
     "秋天深夜的海面下有一道半透明的影子，要用深海誘餌以上的魚餌。", 440, DEEP, ["autumn"], 21, 3),
    ("frost_dragon_fish", "霜龍魚", "🧊", "legendary", 450, 700, 10000, 60000, 5, "冬天深夜冰海中的龍形巨魚，只吃龍涎香餌。",
     450, DRAGON, ["winter"], 22, 2),
)


def upgrade() -> None:
    stmt = sa.text(
        "INSERT INTO fish_types (code, name, emoji, rarity, min_price, max_price, min_weight_g, max_weight_g, "
        "difficulty, description, sort_order, active, required_baits, seasons, hour_start, hour_end) "
        "SELECT :code, :name, :emoji, :rarity, :pmin, :pmax, :wmin, :wmax, :diff, :desc, :sort, true, "
        "CAST(:baits AS json), CAST(:seasons AS json), :hs, :he "
        "WHERE NOT EXISTS (SELECT 1 FROM fish_types WHERE code = :code OR name = :name)"
    )
    for code, name, emoji, rarity, pmin, pmax, wmin, wmax, diff, desc, sort, baits, seasons, hs, he in FISH:
        op.execute(stmt.bindparams(
            code=code, name=name, emoji=emoji, rarity=rarity, pmin=pmin, pmax=pmax, wmin=wmin, wmax=wmax, diff=diff,
            desc=desc, sort=sort, baits=json.dumps(baits) if baits else None,
            seasons=json.dumps(seasons) if seasons else None, hs=hs, he=he,
        ))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM fish_types f WHERE f.code = ANY(:codes) "
        "AND NOT EXISTS (SELECT 1 FROM farm_fish x WHERE x.fish_code = f.code) "
        "AND NOT EXISTS (SELECT 1 FROM fishing_sessions s WHERE s.fish_code = f.code)"
    ).bindparams(codes=[f[0] for f in FISH]))
