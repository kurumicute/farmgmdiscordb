"""pytest 共用設定：使用專用測試資料庫（名稱必須以 _test 結尾），每個測試前清空遊戲資料。"""

import json
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import make_url, text

from app.clock import ManualClock
from app.db.models import Base
from app.db.session import make_engine, make_session_factory
from app.game import balance, exploration, fishing
from app.services.monitor_service import MonitorService
from app.services.ranking_service import RankingService

ROOT = Path(__file__).resolve().parents[1]
GAME_TABLES = (
    "world_boss_reward_claims, world_boss_contributions, world_boss_supplies, world_boss_events, "
    "rumor_digs, ruins_guardian_hits, ruins_guardians, ruins_contributions, group_ruins, "
    "world_boss_schedules, daily_checkins, farm_codex, farm_achievements, farm_titles, weekly_shop_purchases, "
    "ranking_awards, ranking_settlements, ranking_reward_rules, game_events, "
    "competition_awards, competition_results, competition_contributions, competitions, "
    "quest_claims, quest_contributions, quests, admin_actions, "
    "exploration_logs, farm_explorations, "
    "fish_shares, farm_fish, fishing_sessions, farm_fishing_gear, fishing_settings, fishing_guards, "
    "craft_orders, farm_animals, farm_buildings, farm_defenses, steal_records, group_members, groups, "
    "gamble_prizes, farm_daily_stats, farm_stats, farm_effects, farm_tools, game_transactions, inventory_items, "
    "farm_feedback, farm_plots, farms, "
    "players, feature_settings"
)
_RESET_SQL: dict = {}  # engine → 清空用的 SQL（session 開始時產生一次）
RESET_TABLES = [t.strip() for t in GAME_TABLES.split(",")] + ["crop_genetics_settings"]


def reset_sql(conn) -> str:
    """每個測試前清空遊戲資料的 SQL：依外鍵順序 DELETE，再把序號歸 1（等同 TRUNCATE … RESTART IDENTITY）。

    原本每次 TRUNCATE 50 多張表約 0.4 秒（每張表都要建立新的資料檔），占整個測試時間的三分之二；
    每個測試只寫入少量資料，DELETE 只要幾毫秒。新增資料表時加入 GAME_TABLES 即可，順序由 Model 的外鍵決定。"""
    order = [t.name for t in reversed(Base.metadata.sorted_tables) if t.name in RESET_TABLES]
    missing = set(RESET_TABLES) - set(order)
    assert not missing, f"GAME_TABLES 中有不在 Model 裡的資料表：{missing}"
    seqs = [r[0] for r in conn.execute(text(
        "SELECT pg_get_serial_sequence(quote_ident(table_name), column_name) FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY(:t) "
        "AND (is_identity = 'YES' OR column_default LIKE 'nextval%')"), {"t": RESET_TABLES})]
    statements = [f"DELETE FROM {t}" for t in order]
    if seqs:
        statements.append("SELECT " + ", ".join(f"setval('{s}', 1, false)" for s in seqs))
    return "; ".join(statements)


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
    with eng.connect() as conn:
        _RESET_SQL[eng] = reset_sql(conn)
    yield eng  # 種類資料在 clean_db 清空遊戲資料後重設（避免殘留資料的外鍵阻擋刪除）
    eng.dispose()


# 既有測試以短生長時間撰寫（正式資料由 Migration 0007 設為 2 小時；test_genetics 另外驗證）
TEST_GROW_SECONDS = {"wheat": 60, "carrot": 180, "tomato": 300, "strawberry": 600, "corn": 120, "pumpkin": 240,
                     "grape": 360, "watermelon": 480}
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
                    "sell_price = :sell, variant_code = :vc, variant_name = :vn, active = true, shop_buyable = true, "
                    "seasons = NULL, hour_start = NULL, hour_end = NULL WHERE code = :c"
                ),
                {"name": name, "g": TEST_GROW_SECONDS[code], "seed": seed, "q": qty, "sell": sell, "c": code,
                 "vc": variant_code, "vn": variant_name},
            )
        for code, name, seed, grow, qty, sell, variant_code, variant_name in exploration.RARE_CROPS:
            conn.execute(
                text(
                    "UPDATE crop_types SET name = :name, grow_seconds = :g, seed_price = :seed, harvest_quantity = :q, "
                    "sell_price = :sell, variant_code = :vc, variant_name = :vn, active = true, shop_buyable = false, "
                    "seasons = NULL, hour_start = NULL, hour_end = NULL WHERE code = :c"
                ),
                {"name": name, "g": grow, "seed": seed, "q": qty, "sell": sell, "c": code,
                 "vc": variant_code, "vn": variant_name},
            )
        # 釣魚目錄：刪除測試新增的魚種／釣具，初始資料重設為 Migration 0016 的值（farm_fish 等已先清空）
        conn.execute(text("DELETE FROM fish_types WHERE NOT (code = ANY(:c))"),
                     {"c": [f[0] for f in fishing.INITIAL_FISH]})
        conn.execute(text("DELETE FROM fishing_gear WHERE NOT (code = ANY(:c))"),
                     {"c": [g[0] for g in fishing.INITIAL_GEAR]})
        for i, (code, name, emoji, rarity, pmin, pmax, wmin, wmax, diff, desc) in enumerate(fishing.INITIAL_FISH):
            conn.execute(
                text(
                    "UPDATE fish_types SET name = :name, emoji = :emoji, rarity = :rarity, min_price = :pmin, "
                    "max_price = :pmax, min_weight_g = :wmin, max_weight_g = :wmax, difficulty = :diff, "
                    "description = :desc, sort_order = :sort, active = true, "
                    "required_baits = CAST(:baits AS json), seasons = NULL, hour_start = NULL, hour_end = NULL "
                    "WHERE code = :code"
                ),
                {"name": name, "emoji": emoji, "rarity": rarity, "pmin": pmin, "pmax": pmax, "wmin": wmin,
                 "wmax": wmax, "diff": diff, "desc": desc, "sort": (i + 1) * 10, "code": code,
                 "baits": json.dumps(list(fishing.FISH_REQUIRED_BAITS[code]))
                 if code in fishing.FISH_REQUIRED_BAITS else None},
            )
        for i, (code, kind, name, emoji, price, level, desc, effects) in enumerate(fishing.INITIAL_GEAR):
            conn.execute(
                text(
                    "UPDATE fishing_gear SET kind = :kind, name = :name, emoji = :emoji, price = :price, "
                    "required_level = :level, description = :desc, effects = CAST(:effects AS json), stock = NULL, "
                    "sort_order = :sort, active = true WHERE code = :code"
                ),
                {"kind": kind, "name": name, "emoji": emoji, "price": price, "level": level, "desc": desc,
                 "effects": json.dumps(effects), "sort": (i + 1) * 10, "code": code},
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
        conn.exec_driver_sql(_RESET_SQL[engine])
        conn.execute(
            text(
                "INSERT INTO crop_genetics_settings (version, settings, created_by, created_at) "
                "VALUES (1, :s, 'test', now())"
            ),
            {"s": json.dumps(BASELINE_GENETICS)},
        )
    set_test_crop_times(engine)
    # 新功能規則回到預設值（feature_settings 已清空；程序內快取也要重設）
    from app.game import feature_config
    from app.repositories import feature_settings as feature_settings_repo

    feature_config.reset()
    feature_settings_repo.invalidate()
    from app.services import news_service

    news_service.clear_cache()  # 日報快取以 (群組, 日期) 為鍵，資料庫重設後要清掉
    yield


@pytest.fixture(autouse=True)
def catalog_cache_off(monkeypatch):
    """目錄快取預設關閉：許多測試用原生 SQL 改目錄（服務層不會這樣寫）。
    快取測試自行開啟；設環境變數 FARMGM_TEST_CATALOG_TTL=10 可讓整個測試套件在快取開啟下跑一次。"""
    from app.db import catalog_cache

    monkeypatch.setattr(catalog_cache, "TTL_SECONDS", float(os.environ.get("FARMGM_TEST_CATALOG_TTL", "0")))
    catalog_cache.clear()


@pytest.fixture(autouse=True)
def fishing_guard_off(monkeypatch):
    """釣魚防腳本預設關閉（ManualClock 下所有操作間隔都是 0）；防腳本測試自行開啟。"""
    monkeypatch.setattr(fishing, "GUARD_ENABLED", False)


@pytest.fixture(autouse=True)
def no_auto_checkin(monkeypatch):
    """面板預設不自動每日簽到（會改變金幣、庫存與交易紀錄）；簽到測試自行開啟。"""
    from app.discord_bot.panels import FarmPanel

    monkeypatch.setattr(FarmPanel, "auto_checkin", False)


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
