import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.check import StartupCheckError, check_database
from app.db.models import CropType, Farm, FarmPlot, InventoryItem, Player
from app.db.session import make_engine, make_session_factory
from app.game.errors import AlreadyExistsError


def test_insert_and_query(session_factory, service):
    player = service.create_player("資料庫測試")
    with session_factory() as s:
        row = s.scalar(select(Player).where(Player.id == player.id))
        assert row.display_name == "資料庫測試"
        assert row.created_at.tzinfo is not None
        crops = {c.code: c for c in s.scalars(select(CropType))}
        assert set(crops) == {"wheat", "carrot", "tomato", "strawberry"}
        assert (crops["strawberry"].seed_price, crops["strawberry"].grow_seconds) == (60, 600)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE farms SET gold = -1",
        "UPDATE inventory_items SET quantity = -1",
        "INSERT INTO farm_plots (farm_id, plot_number) SELECT id, 1 FROM farms",  # 重複土地編號
        "INSERT INTO inventory_items (farm_id, item_code, quantity) SELECT farm_id, item_code, 1 FROM inventory_items",
        "UPDATE farm_plots SET crop_type_id = 1 WHERE plot_number = 1",  # 有作物卻沒有時間
        "INSERT INTO farms (player_id, name, level, gold, warehouse_capacity) VALUES (999999, 'x', 1, 0, 1)",
        "INSERT INTO game_transactions (farm_id, event_type, description, created_at) "
        "SELECT id, 'hack', 'x', now() FROM farms",
        "INSERT INTO players (display_name, external_platform) VALUES ('x', 'discord')",
    ],
)
def test_constraints_enforced(session_factory, service, player_id, sql):
    service.buy_seeds(player_id, "wheat", 1)
    with session_factory() as s:
        with pytest.raises(IntegrityError):
            s.execute(text(sql))
            s.flush()


def test_one_farm_per_player_and_unique_external_identity(session_factory, service):
    p = service.create_player("discord 玩家", "discord", "1234567890")
    with pytest.raises(AlreadyExistsError):
        service.create_player("重複綁定", "discord", "1234567890")
    found = service.get_player_by_external("discord", "1234567890")
    assert found.id == p.id
    service.create_farm(p.id, home_group_id=service.create_group("群").id)
    with session_factory() as s:
        s.add(Farm(player_id=p.id, name="第二座", level=1, gold=0, warehouse_capacity=1))
        with pytest.raises(IntegrityError):
            s.flush()


def test_data_persists_across_engines(service, player_id, database_url):
    service.buy_seeds(player_id, "wheat", 3)
    engine = make_engine(database_url)
    try:
        with make_session_factory(engine)() as s:
            farm = s.scalar(select(Farm).where(Farm.player_id == player_id))
            assert farm.gold == 470
            assert s.scalar(select(InventoryItem.quantity).where(InventoryItem.farm_id == farm.id)) == 3
            assert len(list(s.scalars(select(FarmPlot).where(FarmPlot.farm_id == farm.id)))) == 4
    finally:
        engine.dispose()


def test_startup_check_passes_on_migrated_db(engine):
    check_database(engine)


def test_startup_check_reports_connection_failure_without_secrets():
    engine = make_engine("postgresql+psycopg://user:supersecret@127.0.0.1:1/nope")
    try:
        with pytest.raises(StartupCheckError) as info:
            check_database(engine)
        assert "supersecret" not in str(info.value)
        assert "無法連線" in str(info.value)
    finally:
        engine.dispose()
