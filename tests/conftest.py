"""pytest 共用設定：使用專用測試資料庫（名稱必須以 _test 結尾），每個測試前清空遊戲資料。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url, text

from app.clock import ManualClock
from app.db.session import make_engine, make_session_factory
from app.game import balance, exploration
from app.services.monitor_service import MonitorService
from app.services.ranking_service import RankingService

ROOT = Path(__file__).resolve().parents[1]
GAME_TABLES = (
    "ranking_awards, ranking_settlements, ranking_reward_rules, game_events, "
    "competition_awards, competition_results, competition_contributions, competitions, "
    "quest_claims, quest_contributions, quests, admin_actions, "
    "exploration_logs, farm_explorations, "
    "craft_orders, farm_animals, farm_buildings, farm_defenses, steal_records, group_members, groups, "
    "gamble_prizes, farm_daily_stats, farm_stats, farm_effects, farm_tools, game_transactions, inventory_items, "
    "farm_plots, farms, "
    "players"
)


class _TestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")
    test_database_url: str | None = None


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _TestSettings().test_database_url
    if not url:
        pytest.exit("請在 .env 或環境變數設定 TEST_DATABASE_URL", returncode=2)
    if not (make_url(url).database or "").endswith("_test"):
        pytest.exit("TEST_DATABASE_URL 的資料庫名稱必須以 _test 結尾，避免清除正式或開發資料", returncode=2)
    return url


@pytest.fixture(scope="session")
def engine(database_url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    eng = make_engine(database_url)
    yield eng  # 種類資料在 clean_db 清空遊戲資料後重設（避免殘留資料的外鍵阻擋刪除）
    eng.dispose()


# 既有測試以短生長時間撰寫（正式資料由 Migration 0007 設為 2 小時；test_genetics 另外驗證）
TEST_GROW_SECONDS = {"wheat": 60, "carrot": 180, "tomato": 300, "strawberry": 600}
# 基準設定：100% 普通、不突變、無等級加成，讓與品質無關的測試結果固定；品質測試會自行調整
BASELINE_GENETICS = {
    **balance.DEFAULT_GENETICS,
    "quality_bp": {"normal": 10000, "good": 0, "rare": 0, "legendary": 0},
    "mutation_bp": {"giant": 0, "golden": 0, "fast": 0, "variant": 0},
    "level_bonus_bp": 0,
    "level_bonus_max_bp": 0,
}


def set_test_crop_times(eng) -> None:
    """crop_types／animal_types 不會被清空，因此每個測試前都刪除測試新增的種類，並把初始種類重設為已知值。"""
    crop_codes = [c[0] for c in balance.INITIAL_CROPS] + [c[0] for c in exploration.RARE_CROPS]
    animal_codes = [a[0] for a in balance.INITIAL_ANIMALS]
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM crop_types WHERE NOT (code = ANY(:c))"), {"c": crop_codes})
        conn.execute(text("DELETE FROM animal_types WHERE NOT (code = ANY(:c))"), {"c": animal_codes})
        for code, name, seed, _grow, qty, sell in balance.INITIAL_CROPS:
            variant_code, variant_name = balance.INITIAL_VARIANTS[code]
            conn.execute(
                text(
                    "UPDATE crop_types SET name = :name, grow_seconds = :g, seed_price = :seed, harvest_quantity = :q, "
                    "sell_price = :sell, variant_code = :vc, variant_name = :vn, active = true, shop_buyable = true "
                    "WHERE code = :c"
                ),
                {"name": name, "g": TEST_GROW_SECONDS[code], "seed": seed, "q": qty, "sell": sell, "c": code,
                 "vc": variant_code, "vn": variant_name},
            )
        for code, name, seed, grow, qty, sell, variant_code, variant_name in exploration.RARE_CROPS:
            conn.execute(
                text(
                    "UPDATE crop_types SET name = :name, grow_seconds = :g, seed_price = :seed, harvest_quantity = :q, "
                    "sell_price = :sell, variant_code = :vc, variant_name = :vn, active = true, shop_buyable = false "
                    "WHERE code = :c"
                ),
                {"name": name, "g": grow, "seed": seed, "q": qty, "sell": sell, "c": code,
                 "vc": variant_code, "vn": variant_name},
            )
        for code, name, price, feed, cycle, _product, pname, pqty, pprice in balance.INITIAL_ANIMALS:
            conn.execute(
                text(
                    "UPDATE animal_types SET name = :name, price = :price, feed_cost = :feed, cycle_seconds = :cycle, "
                    "product_name = :pname, product_quantity = :pqty, product_price = :pprice, active = true "
                    "WHERE code = :c"
                ),
                {"name": name, "price": price, "feed": feed, "cycle": cycle, "pname": pname, "pqty": pqty,
                 "pprice": pprice, "c": code},
            )


@pytest.fixture(scope="session")
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture(autouse=True)
def clean_db(request):
    if "engine" not in request.fixturenames:
        yield
        return
    engine = request.getfixturevalue("engine")
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {GAME_TABLES}, crop_genetics_settings RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "INSERT INTO crop_genetics_settings (version, settings, created_by, created_at) "
                "VALUES (1, :s, 'test', now())"
            ),
            {"s": json.dumps(BASELINE_GENETICS)},
        )
    set_test_crop_times(engine)
    yield


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def service(session_factory, clock) -> RankingService:
    return RankingService(session_factory, clock)


@pytest.fixture
def monitor(session_factory, clock) -> MonitorService:
    return MonitorService(session_factory, clock)


@pytest.fixture
def home_group(service) -> int:
    return service.create_group("主群組").id


@pytest.fixture
def player_id(service, home_group) -> int:
    player = service.create_player("阿明")
    service.create_farm(player.id, home_group_id=home_group)
    return player.id


def make_farmer(service, name: str, group_id: int) -> int:
    """建立玩家與農場，農場主要所屬群組為 group_id（並自動成為該群組成員）。"""
    player = service.create_player(name)
    service.create_farm(player.id, home_group_id=group_id)
    return player.id


def grow(service, player_id: int, plot: int, crop: str) -> None:
    """買種子、種植並澆水（呼叫端再自行推進時鐘讓作物成熟）。"""
    service.buy_seeds(player_id, crop, 1)
    service.plant_crop(player_id, plot, crop)
    service.water_crop(player_id, plot)
