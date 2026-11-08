"""操作回饋：收成摘要、個人紀錄、歡迎回來摘要、偷菜訊息、升級前後對比、圖鑑與成就提示。"""

from app.discord_bot.panels import progress_diff, quote
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import admin, cmds, make_player  # noqa: F401, F811  pytest fixture


def test_harvest_all_summary_and_record(session_factory, clock, player_id):
    svc = AdminService(session_factory, clock)
    svc.plant_all(player_id, "wheat")
    svc.water_all(player_id)
    clock.advance(61)
    first = svc.harvest_all(player_id).message
    assert "🧺 收成 4 格，共 8 個作物。" in first and "小麥 ×8" in first and "約值" in first
    assert "🏆" not in first  # 第一次只記錄，不顯示新紀錄
    svc.set_farm_values("root", player_id, gold=1_000_000)
    svc.upgrade_farm(player_id)  # 5 格
    svc.plant_all(player_id, "wheat")
    svc.water_all(player_id)
    clock.advance(61)
    second = svc.harvest_all(player_id).message
    assert "🏆 新紀錄！一次收成 10 個作物（舊紀錄 8）" in second


def test_sale_record_and_gold_change(session_factory, clock, player_id):
    svc = AdminService(session_factory, clock)
    grow(svc, player_id, 1, "wheat")
    clock.advance(61)
    svc.harvest_crop(player_id, 1)
    gold = svc.get_farm_status(player_id).gold
    result = svc.sell(player_id, "wheat", 1)
    assert f"💰 金幣 {gold:,} → {gold + result.gold_delta:,}" in result.message
    assert svc.note_sale(player_id, result.gold_delta) == ""  # 第一次
    assert "🏆 新紀錄！一次販售" in svc.note_sale(player_id, result.gold_delta * 2)
    assert svc.note_sale(player_id, 1) == ""  # 沒有破紀錄


def test_upgrade_messages_show_before_and_after(session_factory, clock, player_id):
    svc = AdminService(session_factory, clock)
    svc.set_farm_values("root", player_id, gold=1_000_000)
    assert "🌱 農田 4 → 5 格｜⭐ 等級 1 → 2" in svc.upgrade_farm(player_id).message
    assert "⏱️ 生長時間 -0% → -" in svc.upgrade_greenhouse(player_id).message
    svc.buy_pet(player_id, "cat")
    assert "📈 Lv0 → Lv1：5% → 10%" in svc.upgrade_pet(player_id, "cat").message


def test_welcome_back_summary(session_factory, clock, home_group):
    svc = AdminService(session_factory, clock)
    owner = make_farmer(svc, "主人", home_group)
    thief = make_farmer(svc, "小偷", home_group)
    first = svc.welcome_back(owner)
    assert first["lines"] == [] and first["marks"]["codex"] == {}
    grow(svc, owner, 1, "wheat")
    clock.advance(3600)
    result = svc.steal_crop(home_group, thief, owner, 1, 1)
    assert "成功從 主人 的第 1 格" in result.message and "約值" in result.message
    assert "主人 下次打開農場時會知道是你偷的" in result.message
    lines = svc.welcome_back(owner)["lines"]
    assert lines[0].startswith("🌙 歡迎回來！你離開了 1 小時")
    assert any("被偷了 1 次，共 1 個作物（小偷）" in x for x in lines)
    assert any("1 格作物成熟了" in x for x in lines)
    clock.advance(60)
    assert svc.welcome_back(owner)["lines"] == []  # 剛看過，不再顯示


def test_progress_diff_shows_new_codex_and_achievements(session_factory, clock, player_id):
    svc = AdminService(session_factory, clock)
    before = svc.progress_marks(player_id)
    grow(svc, player_id, 1, "wheat")
    clock.advance(61)
    svc.harvest_crop(player_id, 1)
    text = progress_diff(before, svc.progress_marks(player_id))
    assert "📖 新圖鑑：小麥" in text
    assert progress_diff(svc.progress_marks(player_id), svc.progress_marks(player_id)) == ""
    old = {"codex": {}, "codex_total": 1, "ready": {}}
    new = {"codex": {}, "codex_total": 1, "ready": {"first": "🌱 第一次"}}
    assert "🏅 成就達成：🌱 第一次" in progress_diff(old, new)


def test_quote_marks_every_line():
    assert quote("a\n\nb") == "> a\n>\n> b"


def test_panel_shows_codex_hint_in_gold(cmds, clock):  # noqa: F811
    from app.discord_bot.panels import GOLD

    p = make_player(cmds, ALICE)
    p.go("plots")
    p.choose("選擇作物", "wheat")
    p.click("全部澆水")
    clock.advance(61)
    p.go("main")
    interaction = p.click("全部收成")
    assert "> ✅ 🧺 收成 4 格" in p.text and "📖 新圖鑑：小麥" in p.text
    embed = [kw for kind, _c, kw in interaction.log if kind == "edit" and "embed" in kw][-1]["embed"]
    assert embed.color.value == GOLD
