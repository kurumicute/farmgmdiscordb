"""山洞冒險：區域解鎖與探險時間、掉落表、機器製作與功能、假幣兌換、種子製造機、稀有作物培育器。"""

import random
import threading
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import ExplorationLog, Farm, FarmTool, GameTransaction
from app.game import balance, exploration
from app.game.errors import (
    AlreadyExistsError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import PlotState
from app.repositories import inventory as inventory_repo
from app.services.ranking_service import RankingService

HOUR = 3600


class Seq:
    """依序回傳固定亂數；用完後重複最後一個值。"""

    def __init__(self, *values):
        self.values, self.i = list(values), 0

    def __call__(self):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


@pytest.fixture
def explorer(session_factory, clock):
    """亂數固定為 0：每次都抽到普通素材的第一項，數量取最小值。"""
    return RankingService(session_factory, clock, explore_rng=lambda: 0.0)


def set_farm(session_factory, player_id, **values):
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == player_id).values(**values))


def give(session_factory, player_id, item, qty, quality="normal", mutation="none"):
    with session_factory.begin() as s:
        farm = s.scalar(select(Farm).where(Farm.player_id == player_id))
        inventory_repo.add(s, farm.id, item, qty, quality, mutation)


def qty(service, player_id, code):
    """背包中普通品質的數量；種子用 seed_<作物> 代碼查詢（背包的種子行以作物代碼表示）。"""
    bp = service.get_backpack(player_id)
    if code.startswith("seed_"):
        return next((x.quantity for x in bp.seeds if f"seed_{x.code}" == code), 0)
    for line in [*bp.materials, *bp.produce, *bp.goods]:
        if line.code == code and line.quality == "normal" and line.mutation == "none":
            return line.quantity
    return 0


def give_machine_materials(session_factory, player_id, code):
    for item, n in exploration.MACHINES[code].materials.items():
        give(session_factory, player_id, item, n)


# ---------- 規則：機率與掉落表 ----------


def test_reward_probabilities_sum_to_100_percent():
    for bp in exploration.REWARD_TYPE_BP.values():
        assert set(bp) == set(exploration.REWARD_TYPES) and sum(bp.values()) == exploration.TOTAL_BP
    assert exploration.REWARD_TYPE_BP["normal"] == {"common": 6500, "rare": 2000, "seed": 800, "fake_coin": 500,
                                                    "event": 200}
    assert exploration.REWARD_TYPE_BP["advanced"] == {"common": 4500, "rare": 3000, "seed": 1200, "fake_coin": 800,
                                                      "event": 500}


def test_area_settings_match_requirements():
    a = exploration.AREAS
    assert [(a[c].unlock_level, a[c].duration_seconds) for c in exploration.AREA_ORDER] == [
        (1, 8 * HOUR), (5, 16 * HOUR), (10, 24 * HOUR)
    ]
    for area in a.values():  # 每個掉落項目都是已定義的素材或稀有種子
        for pool in area.pools.values():
            for d in pool:
                assert d.item in exploration.MATERIALS or d.item in (exploration.AMBER_SEED, exploration.MOON_SEED)
                assert 1 <= d.qty_min <= d.qty_max and d.weight > 0
    names = {c: {exploration.material_name(i) for i in exploration.possible_items(a[c])} for c in a}
    assert names["grassland"] >= {"木材", "石塊", "藥草", "假幣"} and "樹脂" not in names["grassland"]
    assert names["forest"] >= {"木材", "樹脂", "藥草", "齒輪", "古木核心", "假幣"} and "水晶" not in names["forest"]
    assert names["mine"] >= {"石塊", "銅礦", "鐵礦", "齒輪", "水晶", "機械核心", "假幣"} and "木材" not in names["mine"]
    assert exploration.MOON_SEED in exploration.possible_items(a["forest"])
    assert exploration.AMBER_SEED in exploration.possible_items(a["grassland"])
    assert len({frozenset(exploration.possible_items(x)) for x in a.values()}) == 3  # 三個區域掉落池都不同


@pytest.mark.parametrize("area", exploration.AREA_ORDER)
@pytest.mark.parametrize("level", [1, 4, 7, 10, 13, 16])
def test_drops_only_from_allowed_pool(area, level):
    a = exploration.AREAS[area]
    if level < a.unlock_level:
        return
    allowed = exploration.possible_items(a, level)
    rng = random.Random(f"{area}-{level}").random
    seen = Counter()
    for _ in range(3000):
        result = exploration.explore(area, level, rng)
        assert set(result.items) <= allowed, set(result.items) - allowed
        assert result.total >= 1
        seen.update(result.types)
    # 機率大致符合設定（高級探索時普通素材較少）
    common_share = seen["common"] / sum(seen.values())
    assert 0.3 < common_share < 0.95


def test_level_gates_inside_pool():
    grass = exploration.AREAS["grassland"]
    assert "copper_ore" not in exploration.possible_items(grass, 1)  # Lv.2 草原進階素材
    assert "copper_ore" in exploration.possible_items(grass, 2)
    assert "iron_ore" not in exploration.possible_items(grass, 3) and "iron_ore" in exploration.possible_items(grass, 4)
    forest = exploration.AREAS["forest"]
    assert "ancient_core" not in exploration.possible_items(forest, 6)
    assert "ancient_core" in exploration.possible_items(forest, 7)
    assert not exploration.is_advanced(forest, 8) and exploration.is_advanced(forest, 9)
    assert all(exploration.is_advanced(a, 16) for a in exploration.AREAS.values())
    # Lv.15 才會出現古代遺跡事件
    assert not [e for e in grass.events if e.code == "ancient_ruins" and e.min_level <= 14]


def test_event_roll_gives_fake_coins_and_bonus():
    # 第一次抽類型落在 event 區間（最後 2%），選第一個事件（寶箱）
    rng = Seq(0.99, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    result = exploration.explore("grassland", 1, rng)
    assert result.events == ["🎁 特殊寶箱"]
    assert result.items[exploration.FAKE_COIN] == 2  # 寶箱 2–4 枚，亂數 0 取最小值
    # Lv.1 草原沒有稀有素材 → 寶箱的額外稀有素材改給普通素材
    assert result.items["wood"] >= 2


# ---------- 探險：出發、探險時間、領取、一次一個區域 ----------


def trip(explorer, player_id, clock, area="grassland"):
    """出發 → 等到探險結束 → 領取，回傳領取結果。"""
    explorer.explore(player_id, area)
    clock.advance(exploration.AREAS[area].duration_seconds)
    return explorer.claim_exploration(player_id)


def test_level1_can_only_explore_grassland(explorer, player_id, clock):
    r = trip(explorer, player_id, clock)
    assert r["area"] == "grassland" and r["items"] and r["level"] == 1
    assert qty(explorer, player_id, "wood") == 6  # 3 份獎勵都是木材 ×2
    for area in ("forest", "古老森林", "mine", "神秘礦坑"):
        with pytest.raises(ValidationError, match="Lv"):
            explorer.explore(player_id, area)
    with pytest.raises(NotFoundError):
        explorer.explore(player_id, "火山")


def test_level_unlocks(explorer, player_id, session_factory, clock):
    set_farm(session_factory, player_id, level=5)
    assert explorer.explore(player_id, "forest")["area"] == "forest"
    clock.advance(16 * HOUR)
    explorer.claim_exploration(player_id)
    with pytest.raises(ValidationError, match="Lv.10"):
        explorer.explore(player_id, "mine")
    set_farm(session_factory, player_id, level=10)
    assert explorer.explore(player_id, "mine")["area"] == "mine"


@pytest.mark.parametrize("area,hours", [("grassland", 8), ("forest", 16), ("mine", 24)])
def test_trip_lasts_area_duration_and_rewards_on_claim(explorer, player_id, clock, session_factory, area, hours):
    set_farm(session_factory, player_id, level=10)
    start = clock.now()
    r = explorer.explore(player_id, area)
    assert r["ends_at"] - start == timedelta(hours=hours) and r["duration_seconds"] == hours * HOUR
    assert qty(explorer, player_id, "wood") + qty(explorer, player_id, "stone") == 0  # 出發時不發獎勵
    clock.advance(hours * HOUR - 1)
    with pytest.raises(ValidationError, match="探險中"):
        explorer.claim_exploration(player_id)
    trip_view = explorer.exploration_overview(player_id)["trip"]
    assert trip_view["area"] == area and trip_view["remaining_seconds"] == 1 and not trip_view["finished"]
    clock.advance(1)
    assert explorer.exploration_overview(player_id)["trip"]["finished"]
    got = explorer.claim_exploration(player_id)
    assert got["area"] == area and sum(i["quantity"] for i in got["items"]) > 0
    assert explorer.exploration_overview(player_id)["trip"] is None
    with pytest.raises(ValidationError, match="沒有可以領取"):
        explorer.claim_exploration(player_id)  # 不能重複領取


def test_only_one_area_at_a_time(explorer, player_id, clock, session_factory):
    set_farm(session_factory, player_id, level=10, warehouse_capacity=300)
    explorer.explore(player_id, "grassland")

    def ready():
        return {a["code"] for a in explorer.exploration_overview(player_id)["areas"] if a["ready"]}

    assert ready() == set()
    for area in exploration.AREA_ORDER:  # 探險中：哪個區域都不能出發，包含同一個區域
        with pytest.raises(ValidationError, match="一次只能去一個區域"):
            explorer.explore(player_id, area)
    clock.advance(8 * HOUR)
    for area in exploration.AREA_ORDER:  # 回來了但還沒領取：也不能出發
        with pytest.raises(ValidationError, match="先領取"):
            explorer.explore(player_id, area)
    explorer.claim_exploration(player_id)
    assert ready() == set(exploration.AREA_ORDER)
    explorer.explore(player_id, "mine")  # 領取後可以立刻去另一個區域
    clock.advance(24 * HOUR)
    explorer.claim_exploration(player_id)
    explorer.explore(player_id, "grassland")  # 也可以立刻再去同一個區域
    assert [a["explore_count"] for a in explorer.exploration_overview(player_id)["areas"]] == [2, 0, 1]


def test_explore_records_log_transaction_and_inventory(explorer, player_id, session_factory, clock):
    explorer.explore(player_id, "grassland")
    with session_factory() as s:
        log = s.scalars(select(ExplorationLog)).one()
        assert log.rewards == {"wood": 6} and log.claimed_at is None  # 獎勵出發時決定，還沒放進背包
        assert s.scalar(select(func.count()).select_from(GameTransaction).where(
            GameTransaction.event_type == "explore")) == 0
    assert explorer.exploration_logs(player_id) == []  # 還沒領取的獎勵不顯示在紀錄裡
    clock.advance(8 * HOUR)
    r = explorer.claim_exploration(player_id)
    with session_factory() as s:
        log = s.scalars(select(ExplorationLog)).one()
        assert log.area_code == "grassland" and log.farm_level == 1 and log.claimed_at == clock.now()
        tx = s.scalars(select(GameTransaction).where(GameTransaction.event_type == "explore")).one()
        assert tx.quantity == 6 and tx.gold_delta == 0 and "木材 ×6" in tx.description
    logs = explorer.exploration_logs(player_id)
    assert logs[0]["rewards"] == {"木材": 6} and logs[0]["area_name"] == "青青草原"
    assert r["items"] == [{"code": "wood", "name": "木材", "quantity": 6, "rarity": "普通"}]


def test_materials_go_to_material_bag_not_warehouse(explorer, player_id, session_factory, clock):
    set_farm(session_factory, player_id, warehouse_capacity=3)
    give(session_factory, player_id, "wheat", 3)  # 倉庫全滿
    trip(explorer, player_id, clock)  # 素材放素材背包，倉庫滿了也能領
    bp = explorer.get_backpack(player_id)
    assert bp.used == 3 and bp.material_used == 6 and bp.material_capacity == exploration.MATERIAL_BAG_CAPACITY
    assert explorer.get_farm_status(player_id).warehouse_used == 3
    with pytest.raises(WarehouseFullError, match="倉庫空間不足"):
        explorer.buy_seeds(player_id, "wheat", 1)  # 倉庫本身仍然是滿的


def test_full_material_bag_keeps_rewards_until_claimed(explorer, player_id, session_factory, clock, monkeypatch):
    monkeypatch.setattr(exploration, "MATERIAL_BAG_CAPACITY", 3)
    explorer.explore(player_id, "grassland")
    clock.advance(8 * HOUR)
    with pytest.raises(WarehouseFullError, match="素材背包空間不足.*清出空間後再領取"):
        explorer.claim_exploration(player_id)
    with pytest.raises(ValidationError, match="先領取"):
        explorer.explore(player_id, "grassland")
    assert qty(explorer, player_id, "wood") == 0
    monkeypatch.setattr(exploration, "MATERIAL_BAG_CAPACITY", 500)
    assert explorer.claim_exploration(player_id)["items"][0]["quantity"] == 6
    assert qty(explorer, player_id, "wood") == 6


def _race(fn, n=6):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def go():
        barrier.wait()
        try:
            results.append(fn())
        except ValidationError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_explore_and_claim_reward_once(explorer, player_id, session_factory, clock):
    set_farm(session_factory, player_id, level=10)
    areas = iter(["grassland", "forest", "mine", "grassland", "forest", "mine"])
    lock = threading.Lock()

    def start():
        with lock:
            area = next(areas)
        return explorer.explore(player_id, area)

    results, errors = _race(start)  # 同時出發到不同區域：只有一趟成功
    assert len(results) == 1 and len(errors) == 5
    clock.advance(24 * HOUR)
    results, errors = _race(lambda: explorer.claim_exploration(player_id))
    assert len(results) == 1 and len(errors) == 5
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ExplorationLog)) == 1
        assert s.scalar(select(func.count()).select_from(GameTransaction).where(
            GameTransaction.event_type == "explore")) == 1


def test_players_have_separate_trips(explorer, player_id, home_group):
    other = explorer.create_player("阿華").id
    explorer.create_farm(other, home_group_id=home_group)
    explorer.explore(player_id, "grassland")
    explorer.explore(other, "grassland")
    with pytest.raises(ValidationError):
        explorer.explore(player_id, "grassland")


# ---------- 假幣 ----------


def test_fake_coin_exchange(explorer, player_id, session_factory):
    with pytest.raises(InsufficientItemsError):
        explorer.exchange_fake_coins(player_id, 1)
    give(session_factory, player_id, "fake_coin", 3)
    r = explorer.exchange_fake_coins(player_id, 2)
    assert r.gold == 500 + 200 and r.gold_delta == 200
    with pytest.raises(InsufficientItemsError, match="只有 1"):
        explorer.exchange_fake_coins(player_id, 2)
    assert explorer.get_farm_status(player_id).gold == 700 and qty(explorer, player_id, "fake_coin") == 1
    with pytest.raises(ValidationError):
        explorer.exchange_fake_coins(player_id, 0)
    with session_factory() as s:  # 全服金幣 = 交易紀錄加總
        assert s.scalar(select(func.sum(GameTransaction.gold_delta))) == s.scalar(select(func.sum(Farm.gold)))


def test_concurrent_fake_coin_exchange(explorer, player_id, session_factory):
    give(session_factory, player_id, "fake_coin", 1)
    barrier = threading.Barrier(5)
    ok, errors = [], []

    def go():
        barrier.wait()
        try:
            ok.append(explorer.exchange_fake_coins(player_id, 1))
        except InsufficientItemsError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 1 and len(errors) == 4
    assert explorer.get_farm_status(player_id).gold == 600


# ---------- 機器製作 ----------


@pytest.mark.parametrize("code", exploration.MACHINE_ORDER)
def test_build_machine_checks_level_and_materials(explorer, player_id, session_factory, code):
    machine = exploration.MACHINES[code]
    give_machine_materials(session_factory, player_id, code)
    if machine.unlock_level > 1:
        with pytest.raises(ValidationError, match=f"Lv.{machine.unlock_level}"):
            explorer.build_machine(player_id, code)
    set_farm(session_factory, player_id, level=machine.unlock_level)
    first, need = next(iter(machine.materials.items()))
    with session_factory.begin() as s:
        farm = s.scalar(select(Farm).where(Farm.player_id == player_id))
        inventory_repo.remove(s, farm.id, first, 2)
    with pytest.raises(InsufficientItemsError, match=f"{exploration.material_name(first)} ×2"):
        explorer.build_machine(player_id, machine.name)
    give(session_factory, player_id, first, 2)
    assert machine.name in explorer.build_machine(player_id, machine.name).message
    for item in machine.materials:
        assert qty(explorer, player_id, item) == 0
    with pytest.raises(AlreadyExistsError):
        explorer.build_machine(player_id, code)
    status = {m["code"]: m for m in explorer.machines_status(player_id)["machines"]}
    assert status[code]["owned"] and status[code]["enabled"]


def test_machine_recipes_match_requirements():
    m = exploration.MACHINES
    assert (m["auto_harvester"].unlock_level, m["auto_harvester"].materials) == (
        5, {"wood": 20, "iron_ore": 10, "gear": 5})
    assert (m["auto_processor"].unlock_level, m["auto_processor"].materials) == (
        8, {"stone": 15, "copper_ore": 15, "iron_ore": 10, "machine_core": 1})
    assert (m["rare_incubator"].unlock_level, m["rare_incubator"].materials) == (
        12, {"wood": 20, "herb": 15, "crystal": 5, "ancient_core": 1})
    assert (m["seed_maker"].unlock_level, m["seed_maker"].materials) == (
        3, {"wood": 10, "stone": 10, "copper_ore": 5, "gear": 3})


def test_concurrent_build_machine_charges_once(explorer, player_id, session_factory):
    set_farm(session_factory, player_id, level=3)
    for _ in range(2):
        give_machine_materials(session_factory, player_id, "seed_maker")
    barrier = threading.Barrier(4)
    errors = []

    def go():
        barrier.wait()
        try:
            explorer.build_machine(player_id, "seed_maker")
        except AlreadyExistsError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 3 and qty(explorer, player_id, "wood") == 10
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(FarmTool)) == 1


def owned(explorer, session_factory, player_id, code):
    set_farm(session_factory, player_id, level=16)
    give_machine_materials(session_factory, player_id, code)
    explorer.build_machine(player_id, code)


# ---------- 種子製造機 ----------


def test_seed_maker_two_seeds_per_crop(explorer, player_id, session_factory):
    with pytest.raises(ValidationError, match="種子製造機"):
        explorer.make_seeds(player_id, "wheat", 1)
    owned(explorer, session_factory, player_id, "seed_maker")
    give(session_factory, player_id, "wheat", 3)
    r = explorer.make_seeds(player_id, "小麥", 3)
    assert r.quantity == 6 and qty(explorer, player_id, "wheat") == 0
    assert next(x.quantity for x in explorer.get_backpack(player_id).seeds if x.code == "wheat") == 6
    with pytest.raises(InsufficientItemsError):
        explorer.make_seeds(player_id, "wheat", 1)
    give(session_factory, player_id, "wheat", 2, quality="rare")
    with pytest.raises(ValidationError, match="普通品質"):
        explorer.make_seeds(player_id, "wheat", 1, quality="rare")
    give(session_factory, player_id, "amber_melon", 1)
    with pytest.raises(ValidationError, match="稀有作物"):
        explorer.make_seeds(player_id, "琥珀瓜", 1)
    explorer.set_machine_enabled(player_id, "seed_maker", False)
    with pytest.raises(ValidationError, match="停用"):
        explorer.make_seeds(player_id, "wheat", 1)


def test_seed_maker_rare_crop_rule_is_configurable(explorer, player_id, session_factory, monkeypatch):
    owned(explorer, session_factory, player_id, "seed_maker")
    give(session_factory, player_id, "amber_melon", 1)
    monkeypatch.setattr(exploration, "SEED_MAKER_RARE_CROPS", True)
    assert explorer.make_seeds(player_id, "amber_melon", 1).quantity == 2


def test_seed_maker_rolls_back_when_warehouse_full(explorer, player_id, session_factory):
    owned(explorer, session_factory, player_id, "seed_maker")
    give(session_factory, player_id, "wheat", 30)
    set_farm(session_factory, player_id, warehouse_capacity=40)
    with pytest.raises(WarehouseFullError):
        explorer.make_seeds(player_id, "wheat", 30)
    assert qty(explorer, player_id, "wheat") == 30  # 扣作物與加種子同一筆交易，失敗時一起回滾


# ---------- 稀有種子作物與培育器 ----------


def test_rare_seeds_not_sold_but_plantable(explorer, player_id, session_factory, clock):
    with pytest.raises(ValidationError, match="商店不販售"):
        explorer.buy_seeds(player_id, "琥珀瓜", 1)
    assert "amber_melon" not in {c.code for c in explorer.get_market_prices() if c.shop_buyable}
    with pytest.raises(InsufficientItemsError, match="商店不販售"):
        explorer.plant_all(player_id, "amber_melon")
    give(session_factory, player_id, exploration.AMBER_SEED, 2)
    r = explorer.plant_all(player_id, "amber_melon")
    assert "在 2 格種下琥珀瓜" in r.message and "種子不夠" in r.message
    assert explorer.get_farm_status(player_id).gold == 500  # 沒有自動購買


def test_incubator_creates_rare_seed_order(explorer, player_id, session_factory, clock):
    with pytest.raises(ValidationError, match="稀有作物培育器"):
        explorer.start_incubation(player_id, "incubate_amber", 1)
    owned(explorer, session_factory, player_id, "rare_incubator")
    give(session_factory, player_id, "wheat", 10)
    give(session_factory, player_id, "herb", 3)
    r = explorer.start_incubation(player_id, "琥珀瓜", 1)
    assert "稀有作物培育器" in r.message
    assert qty(explorer, player_id, "wheat") == 0 and qty(explorer, player_id, "herb") == 0
    order = explorer.craft_orders(player_id)[0]
    assert order["building_name"] == "稀有作物培育器" and order["recipe_name"] == "琥珀瓜種子培育"
    clock.advance(4 * HOUR)
    set_farm(session_factory, player_id, warehouse_capacity=1)
    give(session_factory, player_id, "wheat", 1)  # 倉庫全滿：稀有種子放素材背包，照樣可以領
    explorer.collect_craft(player_id, order["order_id"])
    bp = explorer.get_backpack(player_id)
    assert next(x.quantity for x in bp.seeds if x.code == "amber_melon") == 1
    assert bp.used == 1 and bp.material_used == 1


def test_rare_seeds_use_material_bag(explorer, player_id, session_factory, clock, monkeypatch):
    explorer._explore_rng = lambda: 0.0
    monkeypatch.setattr(exploration, "MATERIAL_BAG_CAPACITY", 2)
    give(session_factory, player_id, "seed_moonflower", 2)  # 素材背包已滿
    bp = explorer.get_backpack(player_id)
    assert (bp.used, bp.material_used) == (0, 2)
    with pytest.raises(WarehouseFullError, match="素材背包空間不足"):
        with explorer._transaction() as session:
            farm = explorer._locked_farm(session, player_id)
            explorer._ensure_item_space(session, farm, "seed_amber_melon", 1)
    with explorer._transaction() as session:  # 一般種子仍放倉庫
        farm = explorer._locked_farm(session, player_id)
        explorer._ensure_item_space(session, farm, "seed_wheat", 1)


# ---------- 自動收割機與自動加工機 ----------


def test_auto_harvester_collects_ready_and_late_crops(explorer, player_id, session_factory, clock):
    assert explorer.run_machines(player_id) == ""  # 沒有機器：不做任何事
    explorer.plant_all(player_id, "wheat")
    explorer.water_all(player_id)
    owned(explorer, session_factory, player_id, "auto_harvester")
    clock.advance(60)
    note = explorer.run_machines(player_id)
    assert "自動收割機收成 4 格" in note and qty(explorer, player_id, "wheat") == 8
    assert all(p.state == PlotState.EMPTY for p in explorer.get_farm_status(player_id).plots)
    # 擁有收割機期間成熟、之後才打開面板：已過乾枯時間也照樣收成
    explorer.plant_all(player_id, "wheat")
    explorer.water_all(player_id)
    clock.advance(60 + balance.WITHER_SECONDS + 10)
    assert explorer.get_farm_status(player_id).plots[0].state == PlotState.WITHERED
    assert "收成 4 格" in explorer.run_machines(player_id)
    # 停用後不再自動收成，手動收成仍正常
    explorer.plant_all(player_id, "wheat")
    explorer.water_all(player_id)
    explorer.set_machine_enabled(player_id, "auto_harvester", False)
    clock.advance(60)
    assert explorer.run_machines(player_id) == ""
    assert explorer.harvest_all(player_id).quantity == 8


def test_auto_harvester_stops_when_warehouse_full(explorer, player_id, session_factory, clock):
    owned(explorer, session_factory, player_id, "auto_harvester")
    explorer.plant_all(player_id, "wheat")
    explorer.water_all(player_id)
    set_farm(session_factory, player_id, warehouse_capacity=5)
    clock.advance(60)
    note = explorer.run_machines(player_id)
    assert "收成 2 格" in note and "倉庫已滿" in note
    assert sum(p.state == PlotState.READY for p in explorer.get_farm_status(player_id).plots) == 2


def test_auto_processor_collects_and_starts_orders(explorer, player_id, session_factory, clock):
    owned(explorer, session_factory, player_id, "auto_processor")
    explorer.set_processor_recipe(player_id, "麵粉")
    note = explorer.run_machines(player_id)
    assert "需要先建造磨坊" in note
    set_farm(session_factory, player_id, gold=10_000)
    explorer.build(player_id, "磨坊")
    give(session_factory, player_id, "wheat", 7)
    assert "開始加工 2 份麵粉" in explorer.run_machines(player_id)
    assert qty(explorer, player_id, "wheat") == 1
    assert explorer.run_machines(player_id) == ""  # 加工中：不重複開單
    clock.advance(2 * 30 * 60)
    note = explorer.run_machines(player_id)
    assert "領取 1 張訂單" in note and qty(explorer, player_id, "flour") == 2
    # 原料不夠 1 份時不開單；手動加工仍可使用
    give(session_factory, player_id, "wheat", 2)
    assert explorer.start_craft(player_id, "麵粉", 1).quantity == 1
    with pytest.raises(NotFoundError):
        explorer.set_processor_recipe(player_id, "火箭")


def test_backpack_lists_materials(explorer, player_id, session_factory):
    give(session_factory, player_id, "gear", 2)
    give(session_factory, player_id, "fake_coin", 1)
    bp = explorer.get_backpack(player_id)
    assert [(x.name, x.quantity, x.price) for x in bp.materials] == [("齒輪", 2, 0), ("假幣", 1, 100)]
    assert bp.used == 0 and bp.material_used == 3 and not bp.is_empty  # 素材不佔倉庫
