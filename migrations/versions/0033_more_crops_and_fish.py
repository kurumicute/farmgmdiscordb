"""more crops, fish and premium baits

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-09

- crop_types：新增玉米、南瓜、葡萄、西瓜（各有珍稀變種）。
- fish_types：新增 20 種魚（普通 6、稀有 6、史詩 5、傳說 3）與欄位 required_baits（JSON 魚餌代碼清單，
  NULL = 任何魚餌都能釣到）；黃鰭鮪、旗魚、月光魟要深海誘餌或龍涎香餌，腔棘魚、龍王鯉只吃龍涎香餌。
- fishing_gear：新增魚餌「深海誘餌」「龍涎香餌」。
已有相同代碼或名稱（例如管理員先在網站建立過）時略過，不覆蓋。

降版會刪除還沒被使用過的新作物、魚種與魚餌（被種過、釣到過或買過的保留，避免破壞玩家資料），並刪除 required_baits 欄位。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

# code, 名稱, 種子價格, 生長秒數, 收成數量, 單位售價, 變種代碼, 變種名稱
CROPS = (
    ("corn", "玉米", 18, 10800, 3, 20, "golden_corn", "金粒玉米"),
    ("pumpkin", "南瓜", 80, 43200, 4, 55, "moon_pumpkin", "月影南瓜"),
    ("grape", "葡萄", 120, 57600, 6, 45, "crystal_grape", "水晶葡萄"),
    ("watermelon", "西瓜", 150, 86400, 3, 130, "starry_watermelon", "星紋西瓜"),
)
DEEP = ["deep_sea_bait", "dragon_bait"]
DRAGON = ["dragon_bait"]
# code, 名稱, emoji, 稀有度, 最低售價, 最高售價, 最小重量 g, 最大重量 g, 難度, 說明, 排序, 指定魚餌
FISH = (
    ("goby", "鰕虎", "🐟", "common", 2, 9, 10, 80, 1, "趴在溪底石頭上的小魚，一點也不怕人。", 160, None),
    ("pond_smelt", "公魚", "🐟", "common", 1, 6, 10, 60, 1, "冬天湖面上最受歡迎的小魚。", 170, None),
    ("bluegill", "藍鰓太陽魚", "🐟", "common", 2, 10, 50, 400, 1, "鰓蓋上有一抹藍色，貪吃又好釣。", 180, None),
    ("milkfish", "虱目魚", "🐟", "common", 4, 14, 300, 2000, 2, "魚塭裡的國民魚，刺很多但很好吃。", 190, None),
    ("mullet", "烏魚", "🐟", "common", 5, 16, 500, 3000, 2, "冬至前後成群洄游，烏魚子就是牠的。", 200, None),
    ("snakehead", "鱧魚", "🐟", "common", 8, 20, 1000, 6000, 3, "俗稱雷魚，咬餌兇猛，力氣比外表大。", 210, None),
    ("mackerel", "鯖魚", "🐠", "rare", 21, 35, 200, 1200, 2, "背上有波浪花紋，成群在海面附近游動。", 220, None),
    ("flounder", "比目魚", "🐠", "rare", 21, 38, 300, 3000, 2, "兩隻眼睛長在同一邊，平貼著海底。", 230, None),
    ("yellowfin_seabream", "黃鰭鯛", "🐠", "rare", 22, 40, 300, 2000, 2, "河口常見的鯛魚，鰭帶著亮黃色。", 240, None),
    ("black_seabream", "黑鯛", "🐠", "rare", 24, 42, 400, 3000, 3, "警戒心很強，磯釣客的夢幻對手。", 250, None),
    ("amberjack", "紅魽", "🐠", "rare", 25, 45, 1500, 9000, 3, "會繞著礁石狂奔的大型掠食魚。", 260, None),
    ("eel", "鰻魚", "🐍", "rare", 30, 50, 500, 3000, 4, "滑溜又有力，咬到餌就往石縫裡鑽。", 270, None),
    ("lionfish", "獅子魚", "🦁", "epic", 55, 85, 300, 1200, 3, "華麗的鰭條藏著毒刺，小心別被扎到。", 280, None),
    ("napoleon_fish", "曲紋唇魚", "🐡", "epic", 60, 95, 5000, 40000, 4, "俗稱蘇眉，額頭隆起的珊瑚礁巨魚。", 290, None),
    ("arowana", "金龍魚", "🐉", "epic", 65, 95, 1000, 6000, 4, "鱗片閃著金光，被視為招財的象徵。", 300, None),
    ("yellowfin_tuna", "黃鰭鮪", "🐟", "epic", 120, 180, 10000, 80000, 5,
     "深海的速度之王，只吃深海誘餌以上的魚餌。", 310, DEEP),
    ("swordfish", "旗魚", "🗡️", "epic", 130, 200, 30000, 200000, 5,
     "用長劍般的上顎突擊獵物，只吃深海誘餌以上的魚餌。", 320, DEEP),
    ("moon_ray", "月光魟", "🌙", "legendary", 220, 350, 10000, 80000, 5,
     "月圓時才浮上海面的發光魟魚，要用深海誘餌以上的魚餌。", 330, DEEP),
    ("coelacanth", "腔棘魚", "🦴", "legendary", 250, 400, 30000, 90000, 5, "活化石，只會被龍涎香餌吸引。", 340, DRAGON),
    ("dragon_king_carp", "龍王鯉", "🐲", "legendary", 300, 500, 5000, 30000, 5, "躍過龍門的鯉魚王，只吃龍涎香餌。", 350,
     DRAGON),
)
# code, 名稱, emoji, 價格, 需要等級, 說明, 效果, 排序
BAITS = (
    ("deep_sea_bait", "深海誘餌", "🦑", 40, 8, "深海魚才會咬的誘餌：黃鰭鮪、旗魚、月光魟要用它（或龍涎香餌）。",
     {"rare_bp": 400, "epic_bp": 500, "legendary_bp": 200, "bite_speed_pct": 30}, 140),
    ("dragon_bait", "龍涎香餌", "🐉", 120, 10, "傳說中的頂級香餌：腔棘魚、龍王鯉只吃它，深海魚也愛吃。",
     {"rare_bp": 300, "epic_bp": 600, "legendary_bp": 400, "bite_speed_pct": 20}, 150),
)


def upgrade() -> None:
    op.add_column("fish_types", sa.Column("required_baits", sa.JSON(), nullable=True))
    crop = sa.text(
        "INSERT INTO crop_types (code, name, seed_price, grow_seconds, harvest_quantity, sell_price, variant_code, "
        "variant_name, active, shop_buyable) SELECT :code, :name, :seed, :grow, :qty, :sell, :vc, :vn, true, true "
        "WHERE NOT EXISTS (SELECT 1 FROM crop_types WHERE code IN (:code, :vc) OR variant_code IN (:code, :vc) "
        "OR name IN (:name, :vn) OR variant_name IN (:name, :vn))"
    )
    for code, name, seed, grow, qty, sell, vc, vn in CROPS:
        op.execute(crop.bindparams(code=code, name=name, seed=seed, grow=grow, qty=qty, sell=sell, vc=vc, vn=vn))
    gear = sa.text(
        "INSERT INTO fishing_gear (code, kind, name, emoji, price, required_level, description, effects, "
        "sort_order, active) SELECT :code, 'bait', :name, :emoji, :price, :level, :desc, CAST(:effects AS json), "
        ":sort, true WHERE NOT EXISTS (SELECT 1 FROM fishing_gear WHERE code = :code OR name = :name)"
    )
    for code, name, emoji, price, level, desc, effects, sort in BAITS:
        op.execute(gear.bindparams(code=code, name=name, emoji=emoji, price=price, level=level, desc=desc,
                                   effects=json.dumps(effects), sort=sort))
    fish = sa.text(
        "INSERT INTO fish_types (code, name, emoji, rarity, min_price, max_price, min_weight_g, max_weight_g, "
        "difficulty, description, sort_order, active, required_baits) SELECT :code, :name, :emoji, :rarity, :pmin, "
        ":pmax, :wmin, :wmax, :diff, :desc, :sort, true, CAST(:baits AS json) "
        "WHERE NOT EXISTS (SELECT 1 FROM fish_types WHERE code = :code OR name = :name)"
    )
    for code, name, emoji, rarity, pmin, pmax, wmin, wmax, diff, desc, sort, baits in FISH:
        op.execute(fish.bindparams(code=code, name=name, emoji=emoji, rarity=rarity, pmin=pmin, pmax=pmax,
                                   wmin=wmin, wmax=wmax, diff=diff, desc=desc, sort=sort,
                                   baits=json.dumps(baits) if baits else None))


def downgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM fish_types f WHERE f.code = ANY(:codes) "
        "AND NOT EXISTS (SELECT 1 FROM farm_fish x WHERE x.fish_code = f.code) "
        "AND NOT EXISTS (SELECT 1 FROM fishing_sessions s WHERE s.fish_code = f.code)"
    ).bindparams(codes=[f[0] for f in FISH]))
    op.execute(sa.text(
        "DELETE FROM fishing_gear g WHERE g.code = ANY(:codes) "
        "AND NOT EXISTS (SELECT 1 FROM farm_fishing_gear x WHERE x.gear_code = g.code)"
    ).bindparams(codes=[b[0] for b in BAITS]))
    op.execute(sa.text(
        "DELETE FROM crop_types c WHERE c.code = ANY(:codes) "
        "AND NOT EXISTS (SELECT 1 FROM farm_plots p WHERE p.crop_type_id = c.id) "
        "AND NOT EXISTS (SELECT 1 FROM steal_records s WHERE s.crop_type_id = c.id) "
        "AND NOT EXISTS (SELECT 1 FROM game_events e WHERE e.crop_type_id = c.id)"
    ).bindparams(codes=[c[0] for c in CROPS]))
    op.drop_column("fish_types", "required_baits")
