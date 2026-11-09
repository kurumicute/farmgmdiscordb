"""Migration 0033：新作物（玉米、南瓜、葡萄、西瓜）、20 種新魚與高級魚餌；部分史詩／傳說魚只有指定魚餌釣得到。"""

from collections import Counter

import pytest
from sqlalchemy import select, update

from app.db.models import CropType, Farm, FishingGear, FishingSession, FishType
from app.game import balance, fishing
from app.game.errors import ValidationError
from app.services.admin_service import AdminService
from tests.conftest import grow

NEW_CROPS = ("corn", "pumpkin", "grape", "watermelon")
NEW_FISH = fishing.INITIAL_FISH[15:35]  # Migration 0033 的 20 種（0035 的特殊魚另外測）


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


def test_migration_inserted_catalog(session_factory):
    with session_factory() as s:
        crops = {c.code: c for c in s.scalars(select(CropType).where(CropType.code.in_(NEW_CROPS)))}
        fish = {f.code: f for f in s.scalars(select(FishType))}
        baits = {g.code: g for g in s.scalars(select(FishingGear).where(FishingGear.kind == "bait"))}
    assert set(crops) == set(NEW_CROPS) and all(c.active and c.shop_buyable for c in crops.values())
    for code, name, seed, _grow, qty, sell in balance.INITIAL_CROPS[4:]:
        c = crops[code]
        assert (c.name, c.seed_price, c.harvest_quantity, c.sell_price) == (name, seed, qty, sell)
        assert (c.variant_code, c.variant_name) == balance.INITIAL_VARIANTS[code]
    assert len(NEW_FISH) == 20 and len(fish) == len(fishing.INITIAL_FISH) == 45
    assert Counter(f[3] for f in NEW_FISH) == {"common": 6, "rare": 6, "epic": 5, "legendary": 3}
    for code, required in fishing.FISH_REQUIRED_BAITS.items():
        assert fish[code].rarity in ("epic", "legendary") and fish[code].required_baits == list(required)
        assert set(required) <= set(baits)
    assert all(f.required_baits is None for c, f in fish.items() if c not in fishing.FISH_REQUIRED_BAITS)
    assert baits["dragon_bait"].price > baits["deep_sea_bait"].price > baits["special_bait"].price
    first = {f[0] for f in fishing.INITIAL_FISH[:35]}
    locked = [f for c, f in fish.items() if f.required_baits and c in first]
    for f in locked:  # 0033：要特殊魚餌的魚比同稀有度的一般魚貴
        free = [x.max_price for c, x in fish.items() if x.rarity == f.rarity and not x.required_baits and c in first]
        assert f.min_price > max(free) or f.rarity == "legendary"


def test_new_crop_can_be_bought_planted_and_harvested(admin, player_id, clock):
    admin.set_farm_values("test", player_id, gold=10_000)
    for code in NEW_CROPS:
        admin.buy_seeds(player_id, code, 1)
    grow_plot = 1
    admin.plant_crop(player_id, grow_plot, "玉米")
    admin.water_crop(player_id, grow_plot)
    clock.advance(120)
    r = admin.harvest_crop(player_id, grow_plot)
    assert r.quantity == 3 and "玉米" in r.message
    grow(admin, player_id, 2, "watermelon")


def setup_fisher(admin, session_factory, player_id, bait_code):
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == player_id).values(level=10, gold=100_000))
    admin.buy_fishing_gear(player_id, "bamboo_rod")
    admin.buy_fishing_gear(player_id, "basic_line")
    admin.buy_fishing_gear(player_id, bait_code, 5)


@pytest.mark.parametrize("bait,allowed", [
    ("worm_bait", set()),
    ("special_bait", set()),
    ("deep_sea_bait", {c for c, b in fishing.FISH_REQUIRED_BAITS.items() if "deep_sea_bait" in b}),
    ("dragon_bait", set(fishing.FISH_REQUIRED_BAITS)),
])
def test_bait_decides_which_locked_fish_can_appear(admin, session_factory, player_id, monkeypatch, bait, allowed):
    setup_fisher(admin, session_factory, player_id, bait)
    seen = {}

    def capture(pools, rarity, rng):
        seen.update({f.code: f for pool in pools.values() for f in pool})
        return pools["common"][0]

    monkeypatch.setattr(fishing, "pick_fish", capture)
    admin.start_fishing(player_id)
    locked = set(fishing.FISH_REQUIRED_BAITS)
    assert locked & set(seen) == allowed
    assert {"formosan_salmon", "golden_koi", "oarfish", "salmon"} <= set(seen)  # 原本的魚任何魚餌都釣得到


def test_locked_legendary_is_really_caught_with_dragon_bait(session_factory, clock, player_id):
    svc = AdminService(session_factory, clock, fishing_rng=lambda: 0.9999)  # 一定抽到傳說、挑池子最後一條
    setup_fisher(svc, session_factory, player_id, "dragon_bait")
    svc.start_fishing(player_id)
    with session_factory() as s:
        code = s.scalar(select(FishingSession.fish_code).order_by(FishingSession.id.desc()))
    assert code in {f[0] for f in fishing.INITIAL_FISH if f[3] == "legendary"}
    assert fishing.FISH_REQUIRED_BAITS.get(code) is None or "dragon_bait" in fishing.FISH_REQUIRED_BAITS[code]


def test_admin_can_set_and_keep_required_baits(admin):
    base = {"name": "測試魚", "emoji": "🐟", "rarity": "epic", "min_price": 1, "max_price": 2, "min_weight_g": 10,
            "max_weight_g": 20, "difficulty": 3, "description": "", "sort_order": 0}
    with pytest.raises(ValidationError, match="不是魚餌"):
        admin.create_fish_type("root", {**base, "code": "test_fish", "required_baits": ["bamboo_rod"]})
    made = admin.create_fish_type("root", {**base, "code": "test_fish", "required_baits": ["dragon_bait"]})
    assert made["required_baits"] == ["dragon_bait"]
    kept = admin.update_fish_type("root", "test_fish", {**base, "max_price": 5, "required_baits": None})
    assert kept["required_baits"] == ["dragon_bait"] and kept["max_price"] == 5  # 網站表單沒送：不變更
    cleared = admin.update_fish_type("root", "test_fish", {**base, "required_baits": []})
    assert cleared["required_baits"] == []
